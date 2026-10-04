# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Accepted tasks must name durable native sessions and a live execution owner."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.core import BridgeError, Object
from hermes_bridge.runs import Runs
from tests.test_runs import FakeEngine, isolated_native


class AcceptedTaskContractTests(unittest.TestCase):
    def test_thread_start_failure_remains_terminal_when_its_journal_write_fails(self) -> None:
        self._assert_failed_start_survives_journal_fault("thread")

    def test_session_creation_failure_remains_terminal_when_its_journal_write_fails(self) -> None:
        self._assert_failed_start_survives_journal_fault("session")

    def _assert_failed_start_survives_journal_fault(self, phase: str) -> None:
        with isolated_native(concurrency=1) as (native, home):
            engine = FakeEngine()
            engine.release.set()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            journal = runs.journal("default")
            expected_error = "native_start_failed" if phase == "thread" else "session_storage_unavailable"
            if phase == "session":
                home.joinpath("state.db").mkdir()
            with ExitStack() as failures:
                if phase == "thread":
                    failures.enter_context(
                        patch("threading.Thread.start", side_effect=RuntimeError("no worker"))
                    )
                failures.enter_context(
                    patch.object(journal, "update_status", side_effect=OSError("disk full"))
                )
                accepted = runs.submit("default", "first", "first", "session1")
                self.assertEqual(accepted["status"], "failed")
                self.assertEqual(accepted["session_retained"], phase == "thread")
                if phase == "thread":
                    self.assertEqual(native.session(str(accepted["session_id"]), "default")["id"], "session1")
                else:
                    self.assertIsNone(accepted["session_id"])
                with self.assertRaises(BridgeError) as unavailable:
                    runs.submit("default", "first", "first", "session1")
                self.assertEqual(unavailable.exception.code, "storage_unavailable")
                runs.wait_idle()
            if phase == "session":
                home.joinpath("state.db").rmdir()
            # A terminal result awaiting durable repair must not consume the
            # concurrency slot or block a new turn in the same native session.
            following = runs.submit("default", "next", "second", "session1")
            runs.wait_idle()
            self.assertEqual(runs.status(str(following["task_id"]))["status"], "completed")
            self.assertEqual(engine.calls, 1)
            replay = runs.submit("default", "first", "first", "session1")
            self.assertEqual(replay["task_id"], accepted["task_id"])
            self.assertEqual(replay["status"], "failed")
            self.assertEqual(replay["session_retained"], phase == "thread")
            error = replay["error"]
            assert isinstance(error, dict)
            self.assertEqual(error["code"], expected_error)
            self.assertEqual(runs.cancel(str(accepted["task_id"])), replay)
            runs.close()
            restarted = Runs(native, engine)
            self.addCleanup(restarted.close)
            self.assertEqual(restarted.submit("default", "first", "first", "session1"), replay)
            self.assertEqual(engine.calls, 1)

    def test_queued_session_exists_before_execution_and_survives_start_failure(self) -> None:
        with isolated_native() as (native, _home):
            runs = Runs(native, FakeEngine())
            self.addCleanup(runs.close)
            with patch("threading.Thread.start", side_effect=RuntimeError("worker unavailable")):
                accepted = runs.submit("default", "hello", "start-failure")
            session_id = str(accepted["session_id"])
            self.assertEqual(native.session(session_id, "default")["id"], session_id)
            self.assertEqual(accepted["status"], "failed")
            self.assertTrue(accepted["session_persisted"])
            self.assertTrue(accepted["session_retained"])
            events = native.events("session", "default", "default", "", session_id, "", 10)
            self.assertIsInstance(events["events"], list)

    def test_session_storage_failure_never_starts_a_model_or_claims_a_session(self) -> None:
        with isolated_native() as (native, home):
            home.joinpath("state.db").mkdir()
            engine = FakeEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            accepted = runs.submit("default", "hello", "bad-store")
            self.assertEqual(accepted["status"], "failed")
            self.assertIsNone(accepted["session_id"])
            self.assertFalse(accepted["session_persisted"])
            self.assertFalse(accepted["session_retained"])
            self.assertEqual(engine.calls, 0)
            with self.assertRaises(BridgeError) as absent:
                runs.continue_task(str(accepted["task_id"]), "next", "retry")
            self.assertEqual(absent.exception.code, "session_not_found")

    def test_legacy_orphan_without_native_session_does_not_claim_retention(self) -> None:
        with isolated_native() as (native, _home):
            runs = Runs(native, FakeEngine())
            self.addCleanup(runs.close)
            task_id = "hm3_00000000000000000000000000000099_default"
            runs.journal("default").reserve(
                "mcp-bridge:default",
                "old-task",
                "fingerprint",
                task_id,
                {"task_id": task_id, "session_id": "never-created", "agent": "default", "status": "running"},
                owner_pid=os.getpid(),
                owner_started=1,
            )
            result = runs.status(task_id)
            self.assertEqual(result["status"], "interrupted")
            self.assertFalse(result["session_persisted"])
            self.assertFalse(result["session_retained"])
            self.assertIsNone(result["session_id"])
            with self.assertRaises(BridgeError) as absent:
                runs.continue_task(task_id, "next", "retry")
            self.assertEqual(absent.exception.code, "session_not_found")

    def test_previously_interrupted_legacy_task_does_not_repeat_false_retention_claim(self) -> None:
        with isolated_native() as (native, _home):
            runs = Runs(native, FakeEngine())
            self.addCleanup(runs.close)
            task_id = "hm3_00000000000000000000000000000098_default"
            runs.journal("default").reserve(
                "mcp-bridge:default",
                "old-terminal-task",
                "fingerprint",
                task_id,
                {
                    "task_id": task_id,
                    "session_id": "never-created",
                    "agent": "default",
                    "status": "interrupted",
                    "error": {
                        "code": "owner_exited",
                        "message": "Execution owner exited; session is retained",
                    },
                },
                owner_pid=os.getpid(),
                owner_started=1,
            )
            status = runs.status(task_id)
            self.assertFalse(status["session_retained"])
            self.assertIsNone(status["session_id"])
            self.assertNotIn("session is retained", str(status["error"]))

    def test_native_process_probe_failure_does_not_interrupt_a_live_owned_thread(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            self.addCleanup(engine.release.set)
            accepted = runs.submit("default", "hello", "live-owner")
            self.assertTrue(engine.started.wait(1))
            process = importlib.import_module("gateway.status")
            with patch.object(process, "get_process_start_time", return_value=None):
                status = runs.status(str(accepted["task_id"]))
            self.assertEqual(status["status"], "running")
            self.assertTrue(status["owner_alive"])
            self.assertEqual(status["owner_pid"], os.getpid())
            self.assertEqual(status["runtime_backend"], "python_imports")

    def test_status_storage_error_after_reservation_cannot_leave_an_unowned_queued_task(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            journal = runs.journal("default")
            update = journal.update_status
            unavailable = True

            def fail_first_update(task_id: str, status: Object) -> None:
                nonlocal unavailable
                if unavailable:
                    unavailable = False
                    message = "temporary native journal failure"
                    raise OSError(message)
                update(task_id, status)

            with patch.object(journal, "update_status", side_effect=fail_first_update):
                accepted = runs.submit("default", "hello", "storage-race")
                runs.wait_idle()
            status = runs.status(str(accepted["task_id"]))
            self.assertEqual(status["status"], "failed")
            self.assertTrue(status["session_retained"])
            self.assertEqual(engine.calls, 0)

    def test_vendor_system_exit_finishes_the_task_without_losing_its_session(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            with patch.object(engine, "execute", side_effect=SystemExit(73)):
                accepted = runs.submit("default", "hello", "native-exit")
                runs.wait_idle()
            status = runs.status(str(accepted["task_id"]))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(
                status["error"],
                {
                    "code": "native_runtime_exit",
                    "message": "Native runtime stopped the execution thread",
                    "exit_code": 73,
                },
            )
            self.assertTrue(status["session_persisted"])
            self.assertTrue(status["owner_alive"])

    def test_real_native_subprocess_completes_and_continues_with_one_durable_session(self) -> None:
        with isolated_native() as (native, home):
            project = Path(__file__).resolve().parents[1]
            python = Path(
                os.environ.get("TEST_HERMES_PYTHON", str(native.config.hermes_repo / "venv/bin/python"))
            )
            if not python.is_file():
                self.skipTest("Native Hermes interpreter is unavailable")
            # Native test guards treat HOME/.hermes as production even when HOME
            # is temporary. Keep the profile store separate and arm the guard
            # explicitly so test success cannot depend on psutil ancestry access.
            os_home = home.parent / "os-home"
            os_home.mkdir()
            home.joinpath("config.yaml").write_text(
                "model:\n  default: gpt-4o-mini\n  provider: custom\n"
                "platform_toolsets:\n  cli: []\nmemory:\n  enabled: false\n"
                "agent:\n  max_turns: 2\n  background_review: false\n",
                encoding="utf-8",
            )
            result = subprocess.run(  # noqa: S603 — native interpreter with isolated HOME and offline fixture
                [str(python), "-m", "tests.native_run_probe", str(native.config.hermes_repo), str(home)],
                cwd=project,
                env={
                    "HOME": str(os_home),
                    "HERMES_HOME": str(home),
                    "HERMES_TEST_ISOLATION": "1",
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(project),
                },
                capture_output=True,
                text=True,
                timeout=65,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-1800:])
            reports = [
                line.removeprefix("NATIVE_RUN_REPORT=")
                for line in result.stdout.splitlines()
                if line.startswith("NATIVE_RUN_REPORT=")
            ]
            self.assertEqual(len(reports), 1, result.stdout[-1800:])
            report = json.loads(reports[0])
            for turn in (report["first"], report["second"]):
                self.assertEqual(turn["status"], "completed")
                self.assertEqual(turn["output"], "SMOKE_OK")
                self.assertEqual(turn["usage"]["total_tokens"], 5)
                self.assertTrue(turn["owner_alive"])
                self.assertTrue(turn["session_persisted"])
            sid = report["first"]["session_id"]
            self.assertEqual(report["second"]["session_id"], sid)
            self.assertEqual(report["immediate"]["id"], sid)
            self.assertEqual(report["session"]["id"], sid)
            self.assertEqual(report["session"]["message_count"], 4)
            self.assertEqual(len(report["events"]["events"]), 2)

    def test_native_import_cannot_relaunch_the_mcp_owner(self) -> None:
        with isolated_native() as (native, home):
            project = Path(__file__).resolve().parents[1]
            code = """
import importlib, json, sys
from pathlib import Path
from unittest.mock import patch
from hermes_bridge.core import Config
from hermes_bridge.native import Native
from hermes_bridge.runs import NativeEngine
repo, home = (Path(value) for value in sys.argv[1:])
native = Native(Config(repo, home / "key", ("default",), ("default",), writes=True))
# A bootstrap dependency-generation lookup is forbidden in an already-running
# MCP owner: native prepare_launch would eventually execv and discard live tasks.
with patch("pm.venv_is_current", side_effect=SystemExit(73)):
    NativeEngine(native)
    importlib.import_module("run_agent")
print(json.dumps({"owner_survived_native_import": True}))
"""
            result = subprocess.run(  # noqa: S603 — local interpreter and fixed isolated probe
                [sys.executable, "-c", code, str(native.config.hermes_repo), str(home)],
                cwd=project,
                env={
                    "HOME": str(home.parent),
                    "HERMES_HOME": str(home),
                    "HERMES_DISABLE_LAZY_INSTALLS": "0",
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(project),
                },
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-1200:])
            self.assertEqual(json.loads(result.stdout), {"owner_survived_native_import": True})
