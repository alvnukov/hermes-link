# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Native engine execution with the remote agent boundary replaced."""

from __future__ import annotations

import importlib
import threading
import unittest
from contextlib import ExitStack
from typing import TYPE_CHECKING

from hermes_bridge.runs import NativeEngine
from tests.test_runs import NativeAgentDouble, fake_agent_boundary, isolated_native

if TYPE_CHECKING:
    from hermes_bridge.core import Object


class NativeEngineTests(unittest.TestCase):
    def test_numeric_usage_excludes_boolean_and_string_counters(self) -> None:
        created: list[NativeAgentDouble] = []
        result: Object = {
            "completed": True,
            "final_response": "answer",
            "input_tokens": True,
            "output_tokens": "7",
            "total_tokens": 12,
            "api_calls": 2,
            "estimated_cost_usd": 0.25,
        }
        with isolated_native() as (native, _home), fake_agent_boundary(created, result=result):
            response = NativeEngine(native).execute(
                "default",
                "session1",
                "hello",
                lambda _agent: None,
                threading.Event(),
            )
        self.assertEqual(
            response["usage"],
            {
                "total_tokens": 12,
                "api_calls": 2,
                "estimated_cost_usd": 0.25,
            },
        )
        self.assertEqual(response["session_id"], "session1")
        self.assertEqual(created[0].message, "hello")
        self.assertEqual(created[0].task_id, "session1")
        self.assertTrue(created[0].closed)

    def test_absent_usage_remains_null(self) -> None:
        created: list[NativeAgentDouble] = []
        result: Object = {"completed": True, "final_response": "answer", "input_tokens": False}
        with isolated_native() as (native, _home), fake_agent_boundary(created, result=result):
            response = NativeEngine(native).execute(
                "default",
                "session1",
                "hello",
                lambda _agent: None,
                threading.Event(),
            )
        self.assertIsNone(response["usage"])

    def test_agent_is_closed_when_native_conversation_raises(self) -> None:
        created: list[NativeAgentDouble] = []
        error = "provider execution failed"
        with (
            isolated_native() as (native, _home),
            fake_agent_boundary(created, failure=RuntimeError(error)),
            self.assertRaisesRegex(RuntimeError, error),
        ):
            NativeEngine(native).execute(
                "default",
                "session1",
                "hello",
                lambda _agent: None,
                threading.Event(),
            )
        self.assertTrue(created[0].closed)

    def test_agent_is_closed_when_publication_raises(self) -> None:
        created: list[NativeAgentDouble] = []
        error = "server rejected agent publication"

        def reject_publication(_agent: object) -> None:
            raise RuntimeError(error)

        with (
            isolated_native() as (native, _home),
            fake_agent_boundary(created),
            self.assertRaisesRegex(RuntimeError, error),
        ):
            NativeEngine(native).execute(
                "default",
                "session1",
                "hello",
                reject_publication,
                threading.Event(),
            )
        self.assertFalse(created[0].called)
        self.assertTrue(created[0].closed)

    def test_resume_retains_current_provider_when_only_stored_model_changes(self) -> None:
        created: list[NativeAgentDouble] = []
        resolved: list[dict[str, object]] = []
        with (
            isolated_native() as (native, home),
            fake_agent_boundary(created, resolved),
            ExitStack() as stack,
        ):
            state = importlib.import_module("hermes_state")
            db = state.SessionDB(db_path=home / "state.db")
            stack.callback(db.close)
            db.create_session(
                "saved",
                "cli",
                model="stored-model",
                model_config={
                    "gateway_runtime": {"provider": "auto", "api_mode": "responses"},
                },
            )
            NativeEngine(native).execute(
                "default",
                "saved",
                "next",
                lambda _agent: None,
                threading.Event(),
            )
        self.assertEqual(resolved[0]["requested"], "auto")
        self.assertIsNone(resolved[0]["explicit_base_url"])
        self.assertEqual(created[0].kwargs["model"], "stored-model")
        self.assertEqual(created[0].kwargs["api_mode"], "responses")

    def test_fallback_model_uses_resolved_wire_instead_of_stored_wire(self) -> None:
        created: list[NativeAgentDouble] = []
        with (
            isolated_native() as (native, home),
            fake_agent_boundary(created, fallback={"model": "fallback-model"}),
            ExitStack() as stack,
        ):
            state = importlib.import_module("hermes_state")
            db = state.SessionDB(db_path=home / "state.db")
            stack.callback(db.close)
            db.create_session(
                "saved",
                "cli",
                model="stored-model",
                model_config={
                    "gateway_runtime": {"provider": "auto", "api_mode": "responses"},
                },
            )
            NativeEngine(native).execute(
                "default",
                "saved",
                "next",
                lambda _agent: None,
                threading.Event(),
            )
        self.assertEqual(created[0].kwargs["model"], "fallback-model")
        self.assertEqual(created[0].kwargs["api_mode"], "chat_completions")

    def test_agent_boundary_preserves_native_credential_isolation_and_restores_caller_scope(self) -> None:
        created: list[NativeAgentDouble] = []
        observed: list[tuple[object, object, object]] = []
        with isolated_native() as (native, home), fake_agent_boundary(created):
            home.joinpath(".env").write_text("XAI_API_KEY=profile-test-key\n", encoding="utf-8")
            secrets = importlib.import_module("agent.secret_scope")
            multiplex = secrets.set_multiplex_context(True)
            caller_scope = secrets.set_secret_scope(
                {"XAI_API_KEY": "caller-test-key"},
                profile_home=str(home / "caller"),
            )

            def observe_scope(_agent: object) -> None:
                observed.append(
                    (
                        secrets.get_secret("XAI_API_KEY"),
                        secrets.get_secret("ANTHROPIC_API_KEY"),
                        secrets.current_secret_scope_home(),
                    )
                )

            try:
                result = NativeEngine(native).execute(
                    "default",
                    "session1",
                    "hello",
                    observe_scope,
                    threading.Event(),
                )
                self.assertEqual(result["final_response"], "answer")
                self.assertEqual(observed, [("profile-test-key", None, str(home))])
                self.assertEqual(secrets.get_secret("XAI_API_KEY"), "caller-test-key")
                self.assertEqual(secrets.current_secret_scope_home(), str(home / "caller"))
            finally:
                secrets.reset_secret_scope(caller_scope)
                secrets.reset_multiplex_context(multiplex)
