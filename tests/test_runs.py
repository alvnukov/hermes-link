# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Native run execution and durable idempotency integration checks."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from unittest.mock import patch

from hermes_bridge.core import BridgeError, Config, Object
from hermes_bridge.native import Native
from hermes_bridge.runs import NativeEngine, Runs
from tests.support import REPO

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from hermes_bridge.runs import AgentControl


class FakeAgent:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.hard_cancel = False

    def interrupt(self, *, hard_cancel: bool = False) -> bool:
        self.stop.set()
        self.hard_cancel = hard_cancel
        return True


class FakeEngine:
    def __init__(self) -> None:
        self.agent = FakeAgent()
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.result: Object | None = None
        self.requests: list[tuple[str, str, str]] = []
        self.cancellation: threading.Event | None = None

    def execute(
        self,
        profile: str,
        session_id: str,
        message: str,
        publish: Callable[[AgentControl], None],
        cancelled: threading.Event,
    ) -> Object:
        self.calls += 1
        self.requests.append((profile, session_id, message))
        self.cancellation = cancelled
        publish(self.agent)
        self.started.set()
        self.release.wait(2)
        return self.result or {
            "final_response": "answer",
            "completed": True,
            "interrupted": self.agent.stop.is_set(),
        }


@contextmanager
def isolated_native(*, concurrency: int = 2) -> Iterator[tuple[Native, Path]]:
    with tempfile.TemporaryDirectory() as root:
        home = Path(root) / ".hermes"
        home.mkdir()
        home.joinpath("config.yaml").write_text("model: test\n", encoding="utf-8")
        config = Config(
            REPO,
            home / "key",
            ("default",),
            ("default",),
            writes=True,
            max_concurrent_runs=concurrency,
        )
        with (
            patch("pathlib.Path.home", return_value=Path(root)),
            patch.dict(os.environ, {"HERMES_HOME": str(home)}),
        ):
            yield Native(config), home


class NativeAgentDouble(FakeAgent):
    def __init__(self, **kwargs: object) -> None:
        super().__init__()
        self.kwargs = kwargs
        self.session_id = str(kwargs["session_id"])
        self.closed = False
        self.called = False
        self.history: object = None
        self.message = ""
        self.task_id = ""
        self.result: Object | None = None
        self.failure: Exception | None = None

    def close(self) -> None:
        self.closed = True

    def run_conversation(self, message: str, *, conversation_history: object, task_id: str) -> Object:
        self.called = True
        self.history = conversation_history
        self.message = message
        self.task_id = task_id
        if self.failure is not None:
            raise self.failure
        if self.result is not None:
            return self.result
        return {
            "completed": True,
            "final_response": "answer",
            "input_tokens": 3,
            "output_tokens": 7,
            "total_tokens": 10,
            "api_calls": 1,
            "estimated_cost_usd": 0.05,
        }


@contextmanager
def fake_agent_boundary(
    created: list[NativeAgentDouble],
    resolved: list[dict[str, object]] | None = None,
    *,
    result: Object | None = None,
    fallback: Object | None = None,
    failure: Exception | None = None,
) -> Iterator[None]:
    agent_module = ModuleType("run_agent")

    def build(**kwargs: object) -> NativeAgentDouble:
        agent = NativeAgentDouble(**kwargs)
        agent.result = result
        agent.failure = failure
        created.append(agent)
        return agent

    agent_module.__dict__["AIAgent"] = build
    scope = ModuleType("hermes_cli.web_server_profiles")

    @contextmanager
    def credential_scope(_profile: str) -> Iterator[None]:
        constants = importlib.import_module("hermes_constants")
        secrets = importlib.import_module("agent.secret_scope")
        home: Path = constants.get_hermes_home()
        # NativeEngine already selected the temporary profile home. Bind only
        # that profile's fixture secrets; never use launch env or external sources.
        multiplex = secrets.set_multiplex_context(True)
        try:
            token = secrets.set_secret_scope(
                secrets.load_env_file(home / ".env"),
                profile_home=str(home),
            )
            try:
                yield
            finally:
                secrets.reset_secret_scope(token)
        finally:
            secrets.reset_multiplex_context(multiplex)

    scope.__dict__["_config_profile_scope"] = credential_scope
    provider = ModuleType("hermes_cli.runtime_provider")

    def resolve(**kwargs: object) -> dict[str, object]:
        if resolved is not None:
            resolved.append(kwargs)
        return {
            "provider": "custom",
            "requested_provider": "named-custom",
            "api_key": "test-key",
            "base_url": "http://127.0.0.1:9999/v1",
            "api_mode": "chat_completions",
            "request_overrides": {"extra_body": {"service_tier": "priority"}},
        }

    def resolve_fallback(_config: object, **kwargs: object) -> tuple[dict[str, object], Object | None]:
        return resolve(**kwargs), fallback

    provider.__dict__["resolve_runtime_provider"] = resolve
    provider.__dict__["resolve_runtime_with_fallback"] = resolve_fallback
    provider.__dict__["is_foreign_provider_endpoint"] = lambda _provider, _url: False
    with patch.dict(
        sys.modules,
        {
            "run_agent": agent_module,
            "hermes_cli.web_server_profiles": scope,
            "hermes_cli.runtime_provider": provider,
        },
    ):
        yield


class RunsTests(unittest.TestCase):
    def test_replay_marks_stale_pid_incarnation_interrupted_without_reexecuting(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            task_id = "hm3_00000000000000000000000000000001_default"
            fingerprint = hashlib.sha256(json.dumps(["default", "hello", "session1"]).encode()).hexdigest()
            runs.journal("default").reserve(
                "mcp-bridge:default",
                "orphan",
                fingerprint,
                task_id,
                {"task_id": task_id, "session_id": "session1", "agent": "default", "status": "running"},
                owner_pid=os.getpid(),
                owner_started=1,
            )
            replay = runs.submit("default", "hello", "orphan", "session1")
            self.assertEqual(replay["status"], "interrupted")
            self.assertEqual(runs.status(task_id)["status"], "interrupted")
            self.assertEqual(engine.calls, 0)
            with self.assertRaises(BridgeError) as conflict:
                runs.submit("default", "different", "orphan", "session1")
            self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_native_failure_envelope_is_failed_and_does_not_expose_provider_error(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            engine.result = {
                "failed": True,
                "completed": False,
                "error": "provider https://secret-key@provider.invalid failed",
                "final_response": "provider https://secret-key@provider.invalid failed",
            }
            engine.release.set()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            accepted = runs.submit("default", "hello", "failed-turn")
            runs.wait_idle()
            result = runs.status(str(accepted["task_id"]))
            self.assertEqual(result["status"], "failed")
            self.assertIsNone(result["output"])
            self.assertNotIn("secret-key", str(result))

    def test_cancellation_does_not_expose_a_racing_provider_failure(self) -> None:
        with isolated_native() as (native, _home):
            engine = FakeEngine()
            engine.result = {
                "failed": True,
                "completed": False,
                "error": "provider rejected secret-key",
                "final_response": "provider rejected secret-key",
            }
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            accepted = runs.submit("default", "hello", "cancelled-error")
            self.assertTrue(engine.started.wait(1))
            runs.cancel(str(accepted["task_id"]))
            engine.release.set()
            runs.wait_idle()
            result = runs.status(str(accepted["task_id"]))
            self.assertEqual(result["status"], "cancelled")
            self.assertNotIn("secret-key", str(result))

    def test_thread_launch_failure_is_durable_and_releases_capacity(self) -> None:
        with isolated_native(concurrency=1) as (native, _home):
            engine = FakeEngine()
            engine.release.set()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            with patch("threading.Thread.start", side_effect=RuntimeError("no worker")):
                accepted = runs.submit("default", "hello", "launch-failed", "session1")
            self.assertEqual(accepted["status"], "failed")
            replay = runs.submit("default", "hello", "launch-failed", "session1")
            self.assertEqual(replay["task_id"], accepted["task_id"])
            following = runs.submit("default", "retry", "next", "session1")
            runs.wait_idle()
            self.assertEqual(runs.status(str(following["task_id"]))["status"], "completed")

    def test_same_session_is_serialized_and_capacity_is_released(self) -> None:
        with isolated_native(concurrency=2) as (native, _home):
            engine = FakeEngine()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            first = runs.submit("default", "hello", "first", "session1")
            self.assertTrue(engine.started.wait(1))
            with self.assertRaises(BridgeError) as duplicate:
                runs.submit("default", "other", "second", "session1")
            self.assertEqual(duplicate.exception.code, "session_busy")
            runs.submit("default", "other", "third", "session2")
            with self.assertRaises(BridgeError) as full:
                runs.submit("default", "other", "fourth", "session3")
            self.assertEqual(full.exception.code, "busy")
            engine.release.set()
            runs.wait_idle()
            follow = runs.continue_task(str(first["task_id"]), "next", "fifth")
            runs.wait_idle()
            self.assertEqual(runs.status(str(follow["task_id"]))["status"], "completed")

    def test_native_agent_closes_when_cancelled_at_publication(self) -> None:
        created: list[NativeAgentDouble] = []
        with isolated_native() as (native, _home), fake_agent_boundary(created):
            cancelled = threading.Event()

            def cancel_at_publish(agent: AgentControl) -> None:
                cancelled.set()
                agent.interrupt(hard_cancel=True)

            result = NativeEngine(native).execute(
                "default", "session1", "hello", cancel_at_publish, cancelled
            )
            self.assertTrue(result["interrupted"])
            self.assertFalse(created[0].called)
            self.assertTrue(created[0].closed)

    def test_native_top_level_usage_is_reported_as_usage(self) -> None:
        created: list[NativeAgentDouble] = []
        with isolated_native() as (native, _home), fake_agent_boundary(created):
            result = NativeEngine(native).execute(
                "default", "session1", "hello", lambda _agent: None, threading.Event()
            )
            self.assertEqual(
                result.get("usage"),
                {
                    "input_tokens": 3,
                    "output_tokens": 7,
                    "total_tokens": 10,
                    "api_calls": 1,
                    "estimated_cost_usd": 0.05,
                },
            )

    def test_native_resume_restores_saved_model_and_provider_before_resolving_credentials(self) -> None:
        created: list[NativeAgentDouble] = []
        resolved: list[dict[str, object]] = []
        with isolated_native() as (native, home), fake_agent_boundary(created, resolved):
            state = importlib.import_module("hermes_state")
            db = state.SessionDB(db_path=home / "state.db")
            try:
                db.create_session(
                    "saved",
                    "cli",
                    model="stored-model",
                    model_config={
                        "gateway_runtime": {"provider": "anthropic", "api_mode": "anthropic_messages"},
                    },
                )
                NativeEngine(native).execute(
                    "default", "saved", "next", lambda _agent: None, threading.Event()
                )
                self.assertEqual(created[0].kwargs["model"], "stored-model")
                self.assertEqual(resolved[0]["requested"], "anthropic")
                self.assertEqual(resolved[0]["target_model"], "stored-model")
                self.assertEqual(created[0].kwargs["api_mode"], "anthropic_messages")
            finally:
                db.close()

    def test_native_resume_reopens_compression_tip_and_forwards_runtime_options(self) -> None:
        created: list[NativeAgentDouble] = []
        with isolated_native() as (native, home), fake_agent_boundary(created):
            home.joinpath("config.yaml").write_text(
                "model:\n  default:\n    provider: named-custom\n    model: test-model\n"
                "  reasoning_effort: high\nplatform_toolsets:\n  cli: [terminal]\nagent:\n  max_turns: 7\n",
                encoding="utf-8",
            )
            state = importlib.import_module("hermes_state")
            with ExitStack() as stack:
                db = state.SessionDB(db_path=home / "state.db")
                stack.callback(db.close)
                db.create_session("parent", "cli", model="test-model")
                db.append_message("parent", "assistant", "original answer")
                db.end_session("parent", "compression")
                db.create_session("tip", "cli", model="test-model", parent_session_id="parent")
                db.append_message("tip", "assistant", "latest answer")
                db.append_message("tip", "assistant", "continued answer")
                db.end_session("tip", "agent_close")
                result = NativeEngine(native).execute(
                    "default", "parent", "next", lambda _agent: None, threading.Event()
                )
                agent = created[0]
                self.assertEqual(agent.session_id, "tip")
                self.assertEqual(result["session_id"], "tip")
                self.assertIsNone(db.get_session("tip")["ended_at"])
                self.assertNotIn("original", str(agent.history))
                self.assertIn("latest", str(agent.history))
                self.assertIn("continued answer", str(agent.history))
                self.assertIsInstance(agent.history, list)
                assert isinstance(agent.history, list)
                self.assertEqual(len(agent.history), 1)
                self.assertEqual(agent.kwargs["model"], "test-model")
                self.assertEqual(agent.kwargs["requested_provider"], "named-custom")
                toolsets = agent.kwargs["enabled_toolsets"]
                assert isinstance(toolsets, list)
                self.assertIn("terminal", toolsets)
                self.assertNotIn("web", toolsets)
                self.assertEqual(agent.kwargs["max_iterations"], 7)
                self.assertEqual(
                    agent.kwargs["request_overrides"], {"extra_body": {"service_tier": "priority"}}
                )
                self.assertTrue(agent.closed)

    def test_terminal_journal_failure_releases_capacity_and_reconciles_on_status_retry(self) -> None:
        with isolated_native(concurrency=1) as (native, _home):
            engine = FakeEngine()
            engine.release.set()
            runs = Runs(native, engine)
            self.addCleanup(runs.close)
            journal = runs.journal("default")
            write_status = journal.update_status
            unavailable = threading.Event()
            unavailable.set()

            def write_with_failure(task_id: str, status: Object) -> None:
                if unavailable.is_set() and status.get("status") == "completed":
                    error = "journal disk full"
                    raise OSError(error)
                write_status(task_id, status)

            with patch.object(journal, "update_status", side_effect=write_with_failure):
                first = runs.submit("default", "hello", "first", "session1")
                runs.wait_idle()
                following = runs.submit("default", "next", "second", "session1")
                runs.wait_idle()
                with self.assertRaises(BridgeError) as storage:
                    runs.status(str(first["task_id"]))
                self.assertEqual(storage.exception.code, "storage_unavailable")
                unavailable.clear()
                self.assertEqual(runs.status(str(first["task_id"]))["status"], "completed")
                self.assertEqual(runs.status(str(following["task_id"]))["status"], "completed")
                self.assertEqual(engine.calls, 2)
                self.assertEqual(
                    runs.submit("default", "hello", "first", "session1")["task_id"], first["task_id"]
                )

    def test_async_idempotency_conflict_cancel_and_restart_status(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".hermes"
            home.mkdir()
            home.joinpath("config.yaml").write_text("model: test\n", encoding="utf-8")
            config = Config(REPO, home / "key", ("default",), ("default",), writes=True)
            with (
                patch("pathlib.Path.home", return_value=Path(root)),
                patch.dict(os.environ, {"HERMES_HOME": str(home)}),
            ):
                engine = FakeEngine()
                runs = Runs(Native(config), engine)
                first = runs.submit("default", "hello", "key1")
                task = str(first["task_id"])
                self.assertTrue(engine.started.wait(1))
                self.assertEqual(runs.submit("default", "hello", "key1")["task_id"], task)
                self.assertEqual(engine.calls, 1)
                with self.assertRaises(BridgeError) as conflict:
                    runs.submit("default", "changed", "key1")
                self.assertEqual(conflict.exception.code, "idempotency_conflict")
                self.assertEqual(runs.cancel(task)["status"], "stopping")
                engine.release.set()
                runs.close()
                restarted = Runs(Native(config), FakeEngine())
                result = restarted.status(task)
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual(result["output"], "answer")
                self.assertTrue(home.joinpath("runs_idempotency.db").is_file())
                restarted.close()

    def test_continue_keeps_session_and_replay_survives_terminal_checks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".hermes"
            home.mkdir()
            home.joinpath("config.yaml").write_text("model: test\n", encoding="utf-8")
            config = Config(REPO, home / "key", ("default",), ("default",), writes=True)
            with (
                patch("pathlib.Path.home", return_value=Path(root)),
                patch.dict(os.environ, {"HERMES_HOME": str(home)}),
            ):
                engine = FakeEngine()
                engine.release.set()
                runs = Runs(Native(config), engine)
                first = runs.submit("default", "hello", "key1")
                runs.wait_idle()
                follow = runs.continue_task(str(first["task_id"]), "next", "key2")
                runs.wait_idle()
                self.assertEqual(first["session_id"], follow["session_id"])
                self.assertEqual(
                    runs.continue_task(str(first["task_id"]), "next", "key2")["task_id"], follow["task_id"]
                )
                self.assertEqual(engine.calls, 2)
                runs.close()
