# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Mutation safety against the actual ephemeral native Hermes stores."""

from __future__ import annotations

import importlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING
from unittest.mock import patch

from hermes_bridge.actions import Actions
from hermes_bridge.core import BridgeError, Object, object_json
from tests.test_settings import fixture

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from concurrent.futures import Future


class ActionSafetyTests(unittest.TestCase):
    def test_cron_delete_cannot_remove_a_sibling_named_after_a_concurrently_removed_id(self) -> None:
        with fixture() as (settings, home), ThreadPoolExecutor(max_workers=1) as executor:
            actions = Actions(settings.native)
            selected = actions.cron_create("default", "fixture", "every 1h", "selected", paused=True)
            job_id = str(selected["id"])
            sibling = actions.cron_create("default", "fixture", "every 1h", job_id, paused=True)
            jobs = importlib.import_module("cron.jobs")
            remove = jobs.remove_job
            writers: list[Future[None]] = []

            def competing_delete() -> None:
                # Exercise the actual native writer lock and persistence, with
                # a canonical ID so the competing writer cannot use an alias.
                with jobs.use_cron_store(home), jobs._jobs_lock():
                    remaining = [job for job in jobs.load_jobs() if job["id"] != job_id]
                    jobs.save_jobs(remaining, removed_ids={job_id})

            def race_before_remove(identifier: str) -> bool:
                writer = executor.submit(competing_delete)
                writers.append(writer)
                # Correctly serialized writer waits for the bridge mutation.
                with suppress(TimeoutError):
                    writer.result(timeout=1)
                return bool(remove(identifier))

            with patch.object(jobs, "remove_job", side_effect=race_before_remove):
                self.assertTrue(actions.cron_delete("default", job_id)["deleted"])
            for writer in writers:
                writer.result(timeout=2)
            self.assertEqual(settings.native.cron("default")["count"], 1)
            self.assertEqual(settings.native.cron_job(str(sibling["id"]), "default")["name"], job_id)

    def test_bad_late_field_cannot_leave_earlier_assignment_committed(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "unsafe-patch", {"title": "before", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            before = settings.native.kanban_task(task_id, "default")
            for changes in (
                {"assignee": "default", "title": " "},
                {"assignee": "default", "status": "running"},
                {"assignee": "default", "body": "valid but independent"},
                {"priority": True},
                {"provider_override": "orphan-provider"},
                {"summary": "silently ignored before"},
                {"body": None},
                {"model_override": "model", "clear_model_override": True},
                {"clear_reasoning_effort": False},
                {"reasoning_effort": " "},
                {"clear_model_override": True, "provider_override": "openai"},
                {"model_override": "model", "provider_override": " "},
            ):
                with self.subTest(changes=changes), self.assertRaises(BridgeError) as error:
                    actions.kanban_update("default", task_id, object_json(changes))
                self.assertEqual(settings.native.kanban_task(task_id, "default"), before)
                self.assertEqual(error.exception.details.get("applied_fields"), [])

    def test_native_text_and_priority_edit_commits_one_complete_result(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "atomic-edit", {"title": "before", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            result = actions.kanban_update(
                "default", task_id, {"title": "after", "body": "body", "priority": 5}
            )
            task = object_json(settings.native.kanban_task(task_id, "default")["task"])
            self.assertEqual((task["title"], task["body"], task["priority"]), ("after", "body", 5))
            mutation = object_json(result["mutation"])
            self.assertEqual(mutation["applied_fields"], ["body", "priority", "title"])
            self.assertEqual(mutation["compare_and_swap"], False)

    def test_valid_single_native_override_and_status_actions_reach_native_store(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "valid-actions", {"title": "test", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            for changes in (
                {"model_override": "test-model", "provider_override": "openai"},
                {"clear_model_override": True},
                {"reasoning_effort": "low"},
                {"clear_reasoning_effort": True},
                {"status": "ready"},
                {"status": "blocked", "block_reason": "waiting"},
                {"status": "ready"},
                {"status": "scheduled", "block_reason": "later"},
                {"status": "ready"},
                {"status": "review", "summary": "review this"},
                {"status": "done", "result": "complete", "summary": "finished"},
            ):
                with self.subTest(changes=changes):
                    result = actions.kanban_update("default", task_id, object_json(changes))
                    self.assertEqual(result["task"], settings.native.kanban_task(task_id, "default")["task"])
                    self.assertEqual(object_json(result["mutation"])["applied_fields"], sorted(changes))
            final = object_json(settings.native.kanban_task(task_id, "default")["task"])
            self.assertEqual(final["status"], "done")
            self.assertEqual(final["result"], "complete")

    def test_post_commit_notification_failure_reports_observed_changed_state(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "observer-failure", {"title": "test", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            # The native observer is an external integration after the real DB commit.
            with (
                patch("hermes_cli.kanban_db.notify_task_updated", side_effect=RuntimeError("observer")),
                self.assertRaises(BridgeError) as error,
            ):
                actions.kanban_update("default", task_id, {"body": "persisted"})
            self.assertEqual(error.exception.code, "native_update_failed")
            self.assertTrue(error.exception.details["state_changed"] is True)
            self.assertIsNone(error.exception.details["applied_fields"])
            self.assertEqual(
                object_json(settings.native.kanban_task(task_id, "default")["task"])["body"], "persisted"
            )

    def test_refused_completion_reports_no_change_without_claiming_partial_write(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            parent = actions.kanban_create("default", "parent", {"title": "parent", "triage": True})
            child = actions.kanban_create(
                "default", "child", {"title": "child", "parents": [object_json(parent["task"])["id"]]}
            )
            task_id = str(object_json(child["task"])["id"])
            before = settings.native.kanban_task(task_id, "default")
            with self.assertRaises(BridgeError) as error:
                actions.kanban_update("default", task_id, {"status": "done"})
            self.assertTrue(error.exception.details["state_changed"] is False)
            self.assertEqual(error.exception.details["applied_fields"], [])
            self.assertEqual(settings.native.kanban_task(task_id, "default"), before)

    def test_archive_is_idempotent_and_preserves_an_active_native_run(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            self.assertTrue(callable(getattr(actions, "kanban_archive", None)))
            created = actions.kanban_create("default", "archive-draft", {"title": "draft", "triage": True})
            task_id = str(object_json(created["task"])["id"])
            archived = actions.kanban_archive("default", task_id)
            self.assertEqual(object_json(archived["task"])["status"], "archived")
            self.assertTrue(actions.kanban_archive("default", task_id)["already_archived"])
            active = actions.kanban_create("default", "active-card", {"title": "active"})
            active_id = str(object_json(active["task"])["id"])
            kanban = importlib.import_module("hermes_cli.kanban_db")
            with settings.native.board_connection("default", write=True) as conn:
                self.assertIsNotNone(kanban.claim_task(conn, active_id))
            before = settings.native.kanban_task(active_id, "default")
            with self.assertRaises(BridgeError) as error:
                actions.kanban_archive("default", active_id)
            self.assertEqual(error.exception.code, "resource_active")
            self.assertEqual(settings.native.kanban_task(active_id, "default"), before)

    def test_archive_refuses_task_claimed_after_initial_read(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            self.assertTrue(callable(getattr(actions, "kanban_archive", None)))
            created = actions.kanban_create("default", "archive-race", {"title": "claim race"})
            task_id = str(object_json(created["task"])["id"])
            kanban = importlib.import_module("hermes_cli.kanban_db")
            native_archive = kanban.archive_task
            claimed: Object = {}

            def claim_then_archive(conn: sqlite3.Connection, selected: str) -> bool:
                with settings.native.board_connection("default", write=True) as competing:
                    self.assertIsNotNone(kanban.claim_task(competing, selected))
                claimed.update(settings.native.kanban_task(selected, "default"))
                return bool(native_archive(conn, selected))

            with (
                patch("hermes_cli.kanban_db.archive_task", side_effect=claim_then_archive),
                self.assertRaises(BridgeError) as error,
            ):
                actions.kanban_archive("default", task_id)
            self.assertEqual(error.exception.code, "resource_active")
            self.assertEqual(settings.native.kanban_task(task_id, "default"), claimed)

    def test_delete_profile_cleans_only_explicit_inactive_profile(self) -> None:
        # Process enumeration is outside the sandbox; the native files and cleanup
        # remain real and rooted in this isolated home, with no service installed.
        with fixture() as (settings, home), patch("psutil.process_iter", return_value=iter(())):
            actions = Actions(settings.native)
            self.assertTrue(callable(getattr(actions, "profile_delete", None)))
            actions.profile_create("smoke", "temporary", {})
            actions.profile_create("keep", "unrelated", {})
            for name in ("default", "keep"):
                home.joinpath("active_profile").write_text("keep\n", encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(BridgeError) as error:
                    actions.profile_delete(name)
                self.assertEqual(error.exception.code, "resource_active")
            result = actions.profile_delete("smoke")
            self.assertTrue(result["deleted"])
            self.assertFalse(home.joinpath("profiles", "smoke").exists())
            self.assertTrue(home.joinpath("profiles", "keep", "config.yaml").is_file())
            self.assertTrue(home.joinpath("config.yaml").is_file())

    def test_delete_profile_refuses_open_session_and_native_worker(self) -> None:
        with fixture() as (settings, home), patch("psutil.process_iter", return_value=iter(())):
            actions = Actions(settings.native)
            actions.profile_create("session_owner", "has session", {})
            actions.profile_create("worker_owner", "has worker", {})
            state = importlib.import_module("hermes_state")
            db = state.SessionDB(db_path=home / "profiles" / "session_owner" / "state.db")
            try:
                db.create_session("fixture-session", "cli", profile_name="session_owner")
            finally:
                db.close()
            task = actions.kanban_create(
                "default", "worker-owner", {"title": "claimed", "assignee": "worker_owner"}
            )
            kanban = importlib.import_module("hermes_cli.kanban_db")
            with settings.native.board_connection("default", write=True) as conn:
                self.assertIsNotNone(kanban.claim_task(conn, str(object_json(task["task"])["id"])))
            for name in ("session_owner", "worker_owner"):
                with self.subTest(name=name), self.assertRaises(BridgeError) as error:
                    actions.profile_delete(name)
                self.assertEqual(error.exception.code, "resource_active")
                self.assertTrue(home.joinpath("profiles", name, "config.yaml").is_file())

    def test_unavailable_activity_probe_refuses_profile_deletion(self) -> None:
        with fixture() as (settings, home):
            actions = Actions(settings.native)
            actions.profile_create("probe_failure", "must retain", {})
            with (
                patch("psutil.process_iter", side_effect=PermissionError("probe unavailable")),
                self.assertRaises(BridgeError) as error,
            ):
                actions.profile_delete("probe_failure")
            self.assertEqual(error.exception.code, "activity_unavailable")
            self.assertTrue(home.joinpath("profiles", "probe_failure", "config.yaml").is_file())

    def test_delete_cron_requires_exact_paused_idle_job_and_preserves_sibling(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            self.assertTrue(callable(getattr(actions, "cron_delete", None)))
            temporary = actions.cron_create("default", "fixture", "every 1h", "temporary", paused=True)
            keep = actions.cron_create("default", "fixture", "every 1h", "keep", paused=False)
            with self.assertRaises(BridgeError) as error:
                actions.cron_delete("default", str(keep["id"]))
            self.assertEqual(error.exception.code, "resource_active")
            with self.assertRaises(BridgeError) as error:
                actions.cron_delete("default", "temporary")
            self.assertEqual(error.exception.code, "job_not_found")
            self.assertTrue(actions.cron_delete("default", str(temporary["id"]))["deleted"])
            self.assertEqual(settings.native.cron("default")["count"], 1)
            self.assertEqual(settings.native.cron_job(str(keep["id"]), "default")["name"], "keep")

    def test_paused_cron_with_live_claim_cannot_be_deleted(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            created = actions.cron_create("default", "fixture", "every 1h", "claimed", paused=False)
            job_id = str(created["id"])
            jobs = importlib.import_module("cron.jobs")
            with settings.native.home_scope("default") as home, jobs.use_cron_store(home):
                self.assertTrue(jobs.claim_job_for_fire(job_id, manual=True))
            actions.cron_set_paused("default", job_id, paused=True)
            with self.assertRaises(BridgeError) as error:
                actions.cron_delete("default", job_id)
            self.assertEqual(error.exception.code, "resource_active")
            self.assertEqual(settings.native.cron("default")["count"], 1)

    def test_cron_changes_during_cleanup_are_not_repaired_or_misreported(self) -> None:
        with fixture() as (settings, home):
            actions = Actions(settings.native)
            created = actions.cron_create("default", "fixture", "every 1h", "racing", paused=True)
            job_id = str(created["id"])
            store = home / "cron" / "jobs.json"
            original = store.read_bytes()
            for acquired, content, expected in (
                (False, original, "resource_busy"),
                (True, b"{bad-json", "storage_corrupt"),
                (True, json.dumps({"jobs": []}).encode(), "job_not_found"),
            ):
                store.write_bytes(original)

                @contextmanager
                def fence(
                    _job_id: str, *, snapshot: bytes = content, locked: bool = acquired
                ) -> Iterator[bool]:
                    store.write_bytes(snapshot)
                    yield locked

                with (
                    self.subTest(expected=expected),
                    patch("cron.jobs._fire_job_lock", side_effect=fence),
                    self.assertRaises(BridgeError) as error,
                ):
                    actions.cron_delete("default", job_id)
                self.assertEqual(error.exception.code, expected)
                self.assertEqual(store.read_bytes(), content)
