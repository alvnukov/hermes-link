# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Conditional Kanban mutations against disposable native stores."""

from __future__ import annotations

import importlib
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from unittest.mock import patch

from hermes_bridge.actions import Actions
from hermes_bridge.core import BridgeError, object_json
from tests.test_settings import fixture


class KanbanRevisionTests(unittest.TestCase):
    def _revision(self, actions: Actions, task_id: str, board: str = "default") -> str:
        module = importlib.import_module("hermes_bridge.kanban_revision")
        with actions.native.board_connection(board) as conn:
            value: object = module.revision(conn, task_id, board)
        self.assertIsInstance(value, str)
        return str(value)

    def _audit(self, actions: Actions, task_id: str) -> tuple[object, object, object]:
        with actions.native.board_connection("default") as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            events = conn.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
            runs = conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return tuple(task), tuple(tuple(row) for row in events), tuple(tuple(row) for row in runs)

    def test_invalid_revision_is_rejected_before_native_mutation(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create(
                "default", "invalid-revision", {"title": "before", "triage": True}
            )
            task_id = str(object_json(created["task"])["id"])
            before = settings.native.kanban_task(task_id, "default")
            failure: object = None
            try:
                actions.kanban_update("default", task_id, {"body": "rejected"}, expected_revision="invalid")
            except (BridgeError, TypeError) as error:
                failure = error
            self.assertIsInstance(failure, BridgeError)
            self.assertEqual(getattr(failure, "code", None), "invalid_input")
            self.assertEqual(settings.native.kanban_task(task_id, "default"), before)

    def test_two_clients_from_one_revision_have_exactly_one_winner(self) -> None:
        with fixture() as (settings, _home), ThreadPoolExecutor(max_workers=2) as executor:
            actions = Actions(settings.native)
            created = actions.kanban_create(
                "default", "competing-clients", {"title": "before", "triage": True}
            )
            task_id = str(object_json(created["task"])["id"])
            expected = self._revision(actions, task_id)
            barrier = Barrier(2)

            def update(body: str) -> str:
                barrier.wait(timeout=5)
                try:
                    actions.kanban_update("default", task_id, {"body": body}, expected_revision=expected)
                except BridgeError as error:
                    return error.code
                return "updated"

            results = [executor.submit(update, body) for body in ("client A", "client B")]
            self.assertEqual(
                sorted(result.result(timeout=10) for result in results), ["revision_conflict", "updated"]
            )
            with settings.native.board_connection("default") as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='edited'", (task_id,)
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_every_phase_rejects_stale_revision_without_task_audit_or_run_changes(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create(
                "default", "stale-all-phases", {"title": "before", "triage": True}
            )
            task_id = str(object_json(created["task"])["id"])
            old = self._revision(actions, task_id)
            actions.kanban_update("default", task_id, {"body": "native current"})
            for changes in (
                {"title": "stale"},
                {"assignee": "default"},
                {"status": "ready"},
                {"model_override": "stale-model"},
                {"reasoning_effort": "low"},
            ):
                before = self._audit(actions, task_id)
                with self.subTest(changes=changes), self.assertRaises(BridgeError) as failure:
                    actions.kanban_update("default", task_id, object_json(changes), expected_revision=old)
                self.assertEqual(failure.exception.code, "revision_conflict")
                self.assertEqual(self._audit(actions, task_id), before)

    def test_all_phases_and_completion_work_with_fresh_revision(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create(
                "default", "fresh-all-phases", {"title": "before", "triage": True}
            )
            task_id = str(object_json(created["task"])["id"])
            for changes in (
                {"title": "fresh", "body": "same native transaction", "priority": 4},
                {"assignee": "default"},
                {"model_override": "fixture-model"},
                {"reasoning_effort": "low"},
                {"status": "ready"},
                {"status": "blocked", "block_reason": "wait"},
                {"status": "ready"},
                {"status": "scheduled", "block_reason": "later"},
                {"status": "ready"},
                {"status": "review", "summary": "review"},
                {"status": "done", "result": "completed", "summary": "handoff"},
            ):
                with self.subTest(changes=changes):
                    expected = self._revision(actions, task_id)
                    changed = actions.kanban_update(
                        "default", task_id, object_json(changes), expected_revision=expected
                    )
                    receipt = object_json(changed["mutation"])
                    self.assertTrue(receipt["compare_and_swap"])
                    self.assertEqual(receipt["revision_before"], expected)
                    self.assertEqual(receipt["revision_after"], self._revision(actions, task_id))
            task = object_json(settings.native.kanban_task(task_id, "default")["task"])
            self.assertEqual(task["status"], "done")
            self.assertEqual(task["result"], "completed")

    def test_native_writer_after_guard_install_cannot_be_overwritten(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "native-race", {"title": "before", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            expected = self._revision(actions, task_id)
            module = importlib.import_module("hermes_bridge.kanban_revision")
            kanban = importlib.import_module("hermes_cli.kanban_db")
            with settings.native.board_connection("default", write=True) as guarded:
                # The guard context translates SQLite's transactional sentinel
                # after the native helper has rolled its own transaction back.
                with (
                    self.assertRaises(BridgeError) as failure,
                    module.mutation_guard(guarded, task_id, "default", expected),
                ):
                    with settings.native.board_connection("default", write=True) as competing:
                        self.assertTrue(
                            kanban.edit_task(competing, task_id, body="Desktop wins", board="default")
                        )
                    before = self._audit(actions, task_id)
                    kanban.edit_task(guarded, task_id, title="stale", board="default")
                self.assertEqual(failure.exception.code, "revision_conflict")
                self.assertEqual(self._audit(actions, task_id), before)
                self.assertEqual(
                    guarded.execute(
                        "SELECT COUNT(*) FROM sqlite_temp_master WHERE type='trigger'"
                    ).fetchone()[0],
                    0,
                )
            with settings.native.board_connection("default") as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type='trigger' AND name LIKE 'mcp_revision%'"
                    ).fetchone()[0],
                    0,
                )

    def test_tokens_are_scoped_to_task_and_board(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            first = actions.kanban_create("default", "scope-first", {"title": "same", "triage": True})
            second = actions.kanban_create("default", "scope-second", {"title": "same", "triage": True})
            first_id = str(object_json(first["task"])["id"])
            second_id = str(object_json(second["task"])["id"])
            foreign = self._revision(actions, first_id)
            with self.assertRaises(BridgeError) as failure:
                actions.kanban_update(
                    "default", second_id, {"body": "wrong target"}, expected_revision=foreign
                )
            self.assertEqual(failure.exception.code, "revision_conflict")
            module = importlib.import_module("hermes_bridge.kanban_revision")
            with settings.native.board_connection("default") as conn:
                self.assertNotEqual(module.revision(conn, first_id, "other-board"), foreign)

    def test_identical_tasks_in_another_native_store_have_different_tokens(self) -> None:
        with fixture() as (settings, home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "scope-store", {"title": "same", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            original = self._revision(actions, task_id)
            module = importlib.import_module("hermes_bridge.kanban_revision")
            with (
                settings.native.board_connection("default") as conn,
                closing(sqlite3.connect(home / "another.db")) as other,
            ):
                conn.backup(other)
                other.row_factory = sqlite3.Row
                self.assertNotEqual(module.revision(other, task_id, "default"), original)

    def test_native_aba_and_audit_gc_do_not_restore_a_prior_revision(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "audit-aba", {"title": "A", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            prior = self._revision(actions, task_id)
            actions.kanban_update("default", task_id, {"title": "B"})
            actions.kanban_update("default", task_id, {"title": "A"})
            self.assertNotEqual(prior, self._revision(actions, task_id))
            with settings.native.board_connection("default", write=True) as conn:
                # Reproduce native retention's deletion without a wall-clock sleep.
                conn.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))
                conn.commit()
            empty_audit = self._revision(actions, task_id)
            actions.kanban_update("default", task_id, {"title": "B"})
            actions.kanban_update("default", task_id, {"title": "A"})
            with settings.native.board_connection("default", write=True) as conn:
                conn.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))
                conn.commit()
            self.assertNotEqual(empty_audit, self._revision(actions, task_id))

    def test_stale_revision_blocks_native_audit_before_any_task_update(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "audit-first", {"title": "before", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            expected = self._revision(actions, task_id)
            module = importlib.import_module("hermes_bridge.kanban_revision")
            kanban = importlib.import_module("hermes_cli.kanban_db")
            with settings.native.board_connection("default", write=True) as guarded:
                with (
                    self.assertRaises(BridgeError) as failure,
                    module.mutation_guard(guarded, task_id, "default", expected),
                ):
                    with settings.native.board_connection("default", write=True) as competing:
                        kanban.edit_task(competing, task_id, body="current", board="default")
                    before = self._audit(actions, task_id)
                    with kanban.write_txn(guarded):
                        kanban._append_event(guarded, task_id, "completion_blocked_empty_result", {})
                self.assertEqual(failure.exception.code, "revision_conflict")
                self.assertEqual(self._audit(actions, task_id), before)

    def test_conditional_post_commit_failure_reports_durable_state_without_rollback_claim(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create(
                "default", "conditional-observer", {"title": "before", "triage": True}
            )
            task_id = str(object_json(created["task"])["id"])
            expected = self._revision(actions, task_id)
            kanban = importlib.import_module("hermes_cli.kanban_db")

            def fail_observer(
                conn: sqlite3.Connection, _task_id: str, _fields: object, **_kwargs: object
            ) -> None:
                self.assertFalse(conn.in_transaction)
                msg = "fixture observer failed after commit"
                raise RuntimeError(msg)

            with (
                patch.object(kanban, "notify_task_updated", side_effect=fail_observer),
                self.assertRaises(BridgeError) as failure,
            ):
                actions.kanban_update("default", task_id, {"body": "persisted"}, expected_revision=expected)
            self.assertEqual(failure.exception.code, "native_update_failed")
            self.assertTrue(failure.exception.details["state_changed"])
            self.assertTrue(failure.exception.details["compare_and_swap"])
            self.assertIsNone(failure.exception.details["applied_fields"])
            task = object_json(settings.native.kanban_task(task_id, "default")["task"])
            self.assertEqual(task["body"], "persisted")

    def test_audit_only_etag_change_does_not_claim_task_state_changed_on_callback_failure(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create(
                "default", "no-op-observer", {"title": "before", "triage": True, "assignee": "default"}
            )
            task_id = str(object_json(created["task"])["id"])
            expected = self._revision(actions, task_id)
            kanban = importlib.import_module("hermes_cli.kanban_db")
            with (
                patch.object(kanban, "notify_task_updated", side_effect=RuntimeError("fixture observer")),
                self.assertRaises(BridgeError) as failure,
            ):
                actions.kanban_update("default", task_id, {"assignee": "default"}, expected_revision=expected)
            self.assertFalse(failure.exception.details["state_changed"])
            self.assertEqual(failure.exception.details["applied_fields"], [])
            self.assertNotEqual(self._revision(actions, task_id), expected)

    def test_unconditional_update_keeps_receipt_and_native_refusal_behavior(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "unconditional", {"title": "before", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            updated = actions.kanban_update("default", task_id, {"status": "triage"})
            self.assertFalse(object_json(updated["mutation"])["compare_and_swap"])
            for _ in range(2):
                updated = actions.kanban_update("default", task_id, {"assignee": "default"})
                self.assertFalse(object_json(updated["mutation"])["compare_and_swap"])
