# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Run ownership, cancellation, and worker failure recovery."""

from __future__ import annotations

import importlib
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from unittest.mock import patch

from hermes_bridge.core import BridgeError, Object
from hermes_bridge.runs import Runs
from tests.test_runs import FakeEngine, isolated_native

if TYPE_CHECKING:
    from collections.abc import Callable

    from hermes_bridge.runs import AgentControl


class LatePublishingEngine(FakeEngine):
    def execute(
        self,
        _profile: str,
        _session_id: str,
        _message: str,
        publish: Callable[[AgentControl], None],
        _cancelled: threading.Event,
    ) -> Object:
        self.calls += 1
        self.started.set()
        if not self.release.wait(2):
            message = "test worker was not released"
            raise TimeoutError(message)
        publish(self.agent)
        return {"final_response": "", "interrupted": self.agent.stop.is_set()}


class RaisingEngine(FakeEngine):
    def execute(
        self,
        _profile: str,
        _session_id: str,
        message: str,
        publish: Callable[[AgentControl], None],
        _cancelled: threading.Event,
    ) -> Object:
        self.calls += 1
        publish(self.agent)
        if message == "fail":
            error = "provider https://secret-key@provider.invalid failed"
            raise RuntimeError(error)
        return {"final_response": "recovered", "session_id": "compressed_tip"}


class RunsLifecycleTests(unittest.TestCase):
    def test_rejects_memory_only_journal_and_can_retry_after_storage_repair(self) -> None:
        with isolated_native() as (native, home):
            journal_path = home / "runs_idempotency.db"
            journal_path.mkdir()
            engine = FakeEngine()
            engine.release.set()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            with (
                self.assertLogs(level="WARNING"),
                self.assertRaises(BridgeError) as unavailable,
            ):
                runs.submit("default", "hello", "key")
            self.assertEqual(unavailable.exception.code, "storage_unavailable")
            self.assertEqual(engine.calls, 0)
            journal_path.rmdir()
            accepted = runs.submit("default", "hello", "key")
            runs.wait_idle()
            self.assertEqual(runs.status(str(accepted["task_id"]))["status"], "completed")

    def test_invalid_and_missing_task_handles_report_distinct_errors(self) -> None:
        with isolated_native() as (native, _home):
            runs = Runs(native, FakeEngine())
            self.addCleanup(runs.close)
            for task_id in ("hermes_v2_job", "hm3_short_default"):
                with self.subTest(task_id=task_id), self.assertRaises(BridgeError) as invalid:
                    runs.status(task_id)
                self.assertEqual(invalid.exception.code, "invalid_task_id")
            with self.assertRaises(BridgeError) as missing:
                runs.status("hm3_00000000000000000000000000000001_default")
            self.assertEqual(missing.exception.code, "task_not_found")

    def test_shutdown_rejects_new_requests_but_preserves_replay_and_terminal_cancel(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            engine.release.set()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            accepted = runs.submit("default", "hello", "key", "session1")
            runs.wait_idle()
            task_id = str(accepted["task_id"])
            before = runs.status(task_id)
            runs.close()
            replay = runs.submit("default", "hello", "key", "session1")
            self.assertEqual(replay, before)
            self.assertEqual(runs.cancel(task_id), before)
            with self.assertRaises(BridgeError) as closed:
                runs.submit("default", "new", "another")
            self.assertEqual(closed.exception.code, "shutting_down")
            self.assertEqual(engine.calls, 1)

    def test_live_turn_cannot_be_continued(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            self.addCleanup(engine.release.set)
            accepted = runs.submit("default", "hello", "key", "session1")
            self.assertTrue(engine.started.wait(1))
            with self.assertRaises(BridgeError) as busy:
                runs.continue_task(str(accepted["task_id"]), "next", "second")
            self.assertEqual(busy.exception.code, "session_busy")
            self.assertEqual(engine.calls, 1)

    def test_active_foreign_owner_cannot_be_cancelled_from_this_server(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            process = importlib.import_module("gateway.status")
            started = process.get_process_start_time(os.getpid())
            task_id = "hm3_00000000000000000000000000000002_default"
            runs.journal("default").reserve(
                "mcp-bridge:default",
                "foreign",
                "fingerprint",
                task_id,
                {"task_id": task_id, "session_id": "session1", "agent": "default", "status": "running"},
                owner_pid=os.getpid(),
                owner_started=started,
            )
            with self.assertRaises(BridgeError) as foreign:
                runs.cancel(task_id)
            self.assertEqual(foreign.exception.code, "owner_unavailable")
            self.assertEqual(runs.status(task_id)["status"], "running")
            self.assertEqual(engine.calls, 0)

    def test_cancel_before_worker_start_skips_execution_and_releases_session(self) -> None:
        with isolated_native(concurrency=1) as (native, _home):
            engine = FakeEngine()
            engine.release.set()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            # The lock holds the real worker at its initial status transition.
            with runs._lock:
                accepted = runs.submit("default", "hello", "first", "session1")
                stopped = runs.cancel(str(accepted["task_id"]))
            self.assertEqual(stopped["status"], "stopping")
            runs.wait_idle()
            self.assertEqual(runs.status(str(accepted["task_id"]))["status"], "cancelled")
            self.assertEqual(engine.calls, 0)
            following = runs.submit("default", "next", "second", "session1")
            runs.wait_idle()
            self.assertEqual(runs.status(str(following["task_id"]))["status"], "completed")
            self.assertEqual(engine.calls, 1)

    def test_cancel_before_agent_publication_interrupts_when_agent_arrives(self) -> None:
        with isolated_native() as (native, _home):
            engine = LatePublishingEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            self.addCleanup(engine.release.set)
            accepted = runs.submit("default", "hello", "first")
            self.assertTrue(engine.started.wait(1))
            self.assertEqual(runs.cancel(str(accepted["task_id"]))["status"], "stopping")
            engine.release.set()
            runs.wait_idle()
            self.assertTrue(engine.agent.stop.is_set())
            self.assertTrue(engine.agent.hard_cancel)
            self.assertEqual(runs.status(str(accepted["task_id"]))["status"], "cancelled")

    def test_worker_exception_is_sanitized_and_next_turn_uses_effective_session(self) -> None:
        with isolated_native(concurrency=1) as (native, home):
            # Native compression creates the effective session before returning its ID.
            state = importlib.import_module("hermes_state")
            db = state.SessionDB(db_path=home / "state.db")
            db.create_session("compressed_tip", "mcp-bridge", profile_name="default")
            db.close()
            engine = RaisingEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            failed = runs.submit("default", "fail", "first", "session1")
            runs.wait_idle()
            status = runs.status(str(failed["task_id"]))
            self.assertEqual(status["status"], "failed")
            error = status["error"]
            assert isinstance(error, dict)
            self.assertEqual(error["code"], "native_execution_failed")
            self.assertNotIn("secret-key", str(status))
            self.assertIsNone(status["output"])
            recovered = runs.continue_task(str(failed["task_id"]), "recover", "second")
            runs.wait_idle()
            recovered_status = runs.status(str(recovered["task_id"]))
            self.assertEqual(recovered_status["status"], "completed")
            self.assertEqual(recovered_status["session_id"], "compressed_tip")
            self.assertEqual(recovered_status["output"], "recovered")

    def test_two_owners_racing_to_reserve_execute_only_the_winning_request(self) -> None:
        cases = (("hello", "hello", 2, 0), ("hello", "different", 1, 1))
        for first_message, second_message, success_count, conflict_count in cases:
            with self.subTest(message=second_message), isolated_native() as (native, _home):
                engine = FakeEngine()
                engine.release.set()
                first_owner = Runs(native, engine)
                second_owner = Runs(native, engine)
                self.addCleanup(first_owner.close)
                self.addCleanup(second_owner.close)
                first_journal = first_owner.journal("default")
                second_journal = second_owner.journal("default")
                barrier = threading.Barrier(2)

                def synchronized_lookup(
                    lookup: Callable[[str, str, str], tuple[str, object]],
                    gate: threading.Barrier,
                ) -> Callable[[str, str, str], tuple[str, object]]:
                    def lookup_after_barrier(scope: str, key: str, fingerprint: str) -> tuple[str, object]:
                        # Both real journals observe missing before either reserves.
                        result = lookup(scope, key, fingerprint)
                        gate.wait(timeout=2)
                        return result

                    return lookup_after_barrier

                def submit(owner: Runs, message: str) -> Object | BridgeError:
                    try:
                        return owner.submit("default", message, "racing", "session1")
                    except BridgeError as error:
                        return error

                with (
                    patch.object(
                        first_journal,
                        "lookup",
                        side_effect=synchronized_lookup(first_journal.lookup, barrier),
                    ),
                    patch.object(
                        second_journal,
                        "lookup",
                        side_effect=synchronized_lookup(second_journal.lookup, barrier),
                    ),
                    ThreadPoolExecutor(max_workers=2) as executor,
                ):
                    first = executor.submit(submit, first_owner, first_message)
                    second = executor.submit(submit, second_owner, second_message)
                    results = (first.result(timeout=3), second.result(timeout=3))
                first_owner.wait_idle()
                second_owner.wait_idle()
                successes = [result for result in results if isinstance(result, dict)]
                conflicts = [result for result in results if isinstance(result, BridgeError)]
                self.assertEqual(len(successes), success_count)
                self.assertEqual(len(conflicts), conflict_count)
                self.assertEqual(engine.calls, 1)
                for conflict in conflicts:
                    self.assertEqual(conflict.code, "idempotency_conflict")
                task_ids = {str(result["task_id"]) for result in successes}
                self.assertEqual(len(task_ids), 1)
                task_id = task_ids.pop()
                self.assertEqual(first_owner.status(task_id)["status"], "completed")
                self.assertEqual(second_owner.status(task_id)["output"], "answer")

    def test_ambiguous_terminal_write_does_not_need_another_write_to_recover_status(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            engine.release.set()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            journal = runs.journal("default")
            write_status = journal.update_status

            def write_then_fail(task_id: str, status: Object) -> None:
                write_status(task_id, status)
                if status["status"] == "completed":
                    error = "journal acknowledgement unavailable"
                    raise OSError(error)

            with patch.object(journal, "update_status", side_effect=write_then_fail):
                accepted = runs.submit("default", "hello", "first")
                runs.wait_idle()
                status = runs.status(str(accepted["task_id"]))
                self.assertEqual(status["status"], "completed")
                self.assertEqual(status["output"], "answer")
            runs.close()
            restarted = Runs(native, engine)
            self.addCleanup(restarted.close)
            self.assertEqual(restarted.submit("default", "hello", "first"), status)
            self.assertEqual(engine.calls, 1)

    def test_close_keeps_journal_usable_until_a_queued_worker_exits(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            # Real worker cannot finish before close's bounded join expires.
            with runs._lock:
                accepted = runs.submit("default", "hello", "first")
                runs.close()
                self.assertEqual(runs.status(str(accepted["task_id"]))["status"], "queued")
            runs.wait_idle()
            status = runs.status(str(accepted["task_id"]))
            self.assertEqual(status["status"], "cancelled")
            self.assertEqual(engine.calls, 0)
            runs.close()
            self.assertEqual(runs.submit("default", "hello", "first"), status)

    def test_non_text_native_output_is_not_exposed_as_a_structured_tool_response(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            engine.result = {"final_response": {"internal": "details"}, "usage": {"total_tokens": 9}}
            engine.release.set()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            accepted = runs.submit("default", "hello", "first")
            runs.wait_idle()
            status = runs.status(str(accepted["task_id"]))
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["output"], "")
            self.assertEqual(status["usage"], {"total_tokens": 9})
