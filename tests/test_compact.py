# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from typing import TYPE_CHECKING

import httpx
from mcp.types import CallToolResult

from hermes_bridge.core import Object, object_json
from hermes_bridge.runs import Runs
from hermes_bridge.server import create_server, http_app
from tests.test_runs import FakeEngine
from tests.test_settings import fixture

if TYPE_CHECKING:
    from mcp.server import MCPServer


async def call(server: MCPServer, tool: str, action: str, params: Object | None = None) -> CallToolResult:
    result = await server.call_tool(tool, {"action": action, "params": params or {}})
    assert isinstance(result, CallToolResult)
    return result


class CompactTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_envelope_types_never_echo_input_before_the_guard(self) -> None:
        with fixture() as (settings, home):
            config = replace(settings.native.config, auth_enabled=False)
            server, surface = create_server(config)
            self.addCleanup(surface.runs.close)
            before = home.joinpath("config.yaml").read_bytes()
            app = http_app(server, config)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18788"
                ) as client,
            ):
                cases = (
                    {"action": "get", "params": "private-test-input"},
                    {"action": {"private-test-input": "secret"}, "params": {}},
                    {"action": "get", "params": ["private-test-input"]},
                    {
                        "action": "update",
                        "profile": "private-test-input",
                        "params": {
                            "changes": {"model.default": "wrong-profile"},
                            "expected_revision": surface.settings.get("default")["revision"],
                        },
                    },
                )
                for arguments in cases:
                    with self.subTest(arguments=arguments):
                        response = await client.post(
                            "/mcp",
                            headers={"Accept": "application/json, text/event-stream"},
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {"name": "hermes_settings", "arguments": arguments},
                            },
                        )
                        self.assertEqual(response.status_code, 200)
                        result = object_json(object_json(response.json())["result"])
                        self.assertTrue(result["isError"])
                        self.assertEqual(
                            object_json(object_json(result["structuredContent"])["error"])["code"],
                            "invalid_input",
                        )
                        self.assertNotIn("private-test-input", response.text)
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), before)

    async def test_grouped_tasks_preserve_replay_continuation_and_real_cancellation(self) -> None:
        with fixture() as (settings, _home):
            server, surface = create_server(settings.native.config)
            surface.runs.close()
            engine = FakeEngine()
            engine.release.set()
            surface.runs = Runs(surface.native, engine)
            self.addCleanup(surface.runs.close)
            self.addCleanup(engine.release.set)
            params: Object = {"agent": "default", "task": "first", "request_id": "first-request"}
            accepted = await call(server, "hermes_tasks", "run", params)
            self.assertFalse(accepted.is_error, str(accepted))
            first = object_json(accepted.structured_content)
            await asyncio.to_thread(surface.runs.wait_idle)
            replay = await call(server, "hermes_tasks", "run", params)
            self.assertEqual(object_json(replay.structured_content)["task_id"], first["task_id"])
            result = await call(server, "hermes_tasks", "result", {"task_id": first["task_id"]})
            self.assertEqual(object_json(result.structured_content)["output"], "answer")
            continued = await call(
                server,
                "hermes_tasks",
                "continue",
                {"task_id": first["task_id"], "message": "second", "request_id": "second-request"},
            )
            self.assertFalse(continued.is_error, str(continued))
            second = object_json(continued.structured_content)
            self.assertEqual(second["session_id"], first["session_id"])
            self.assertNotEqual(second["task_id"], first["task_id"])
            await asyncio.to_thread(surface.runs.wait_idle)
            engine.release.clear()
            engine.started.clear()
            third = await call(
                server,
                "hermes_tasks",
                "run",
                {"agent": "default", "task": "third", "request_id": "third-request"},
            )
            self.assertTrue(await asyncio.to_thread(engine.started.wait, 1))
            task_id = object_json(third.structured_content)["task_id"]
            stopped = await call(server, "hermes_tasks", "cancel", {"task_id": task_id})
            self.assertEqual(object_json(stopped.structured_content)["status"], "stopping")
            engine.release.set()
            await asyncio.to_thread(surface.runs.wait_idle)
            terminal = await call(server, "hermes_tasks", "status", {"task_id": task_id})
            self.assertEqual(object_json(terminal.structured_content)["status"], "cancelled")

    async def test_disabling_all_actions_removes_group_and_capabilities_match_discovery(self) -> None:
        with fixture() as (settings, _home):
            config = replace(
                settings.native.config,
                disabled_tools=(
                    "hermes_settings_schema",
                    "hermes_settings_get",
                    "hermes_settings_update",
                    "hermes_settings_apply",
                    "hermes_settings_versions",
                    "hermes_settings_restore",
                ),
            )
            server, surface = create_server(config)
            self.addCleanup(surface.runs.close)
            tools = {tool.name: tool for tool in await server.list_tools()}
            self.assertNotIn("hermes_settings", tools)
            result = await call(server, "hermes_system", "capabilities")
            actions = object_json(object_json(result.structured_content)["mcp_actions"])
            self.assertEqual(set(actions), set(tools))
            for name, tool in tools.items():
                schema = object_json(tool.input_schema)
                self.assertFalse(schema["additionalProperties"])
                action_schema = object_json(object_json(schema["properties"])["action"])
                enabled = actions[name]
                assert isinstance(enabled, list)
                self.assertEqual(action_schema["enum"], [*enabled, "help"])

    async def test_discovery_is_small_and_help_exposes_precise_operation_parameters(self) -> None:
        with fixture() as (settings, _home):
            server, surface = create_server(settings.native.config)
            self.addCleanup(surface.runs.close)
            tools = {tool.name: tool for tool in await server.list_tools()}
            self.assertEqual(
                set(tools),
                {
                    "hermes_system",
                    "hermes_profiles",
                    "hermes_tasks",
                    "hermes_kanban",
                    "hermes_workers",
                    "hermes_sessions",
                    "hermes_cron",
                    "hermes_workspaces",
                    "hermes_settings",
                    "hermes_events",
                },
            )
            # A large catalog defeats discovery; operation schemas are obtained on demand.
            self.assertLess(
                len(json.dumps([tool.model_dump(by_alias=True) for tool in tools.values()])), 10000
            )
            help_result = await call(server, "hermes_kanban", "help", {"action": "create"})
            self.assertFalse(help_result.is_error)
            data = object_json(help_result.structured_content)
            schema = object_json(data["params_schema"])
            self.assertEqual(schema["required"], ["title", "request_id"])
            self.assertEqual(object_json(object_json(schema["properties"])["triage"])["default"], True)
            self.assertTrue(data["write"])
            for name in (
                "hermes_kanban",
                "hermes_settings",
                "hermes_tasks",
                "hermes_profiles",
                "hermes_cron",
            ):
                annotations = tools[name].annotations
                assert annotations is not None
                self.assertFalse(annotations.read_only_hint)
            annotations = tools["hermes_system"].annotations
            assert annotations is not None
            self.assertTrue(annotations.read_only_hint)

    async def test_grouped_kanban_create_replay_update_and_read_use_native_store(self) -> None:
        with fixture() as (settings, _home):
            server, surface = create_server(settings.native.config)
            self.addCleanup(surface.runs.close)
            params: Object = {"title": "Compact test", "body": "first", "request_id": "compact-test"}
            created = await call(server, "hermes_kanban", "create", params)
            self.assertFalse(created.is_error, str(created))
            card = object_json(object_json(created.structured_content)["task"])
            self.assertEqual(card["status"], "triage")
            replayed = await call(server, "hermes_kanban", "create", params)
            self.assertEqual(object_json(replayed.structured_content)["task"], card)
            changed = await call(
                server, "hermes_kanban", "update", {"task_id": card["id"], "changes": {"body": "second"}}
            )
            self.assertFalse(changed.is_error, str(changed))
            reread = await call(server, "hermes_kanban", "task", {"task_id": card["id"]})
            self.assertEqual(object_json(object_json(reread.structured_content)["task"])["body"], "second")

    async def test_disabled_action_cannot_be_called_through_group_or_help(self) -> None:
        with fixture() as (settings, home):
            config = replace(settings.native.config, disabled_tools=("hermes_settings_update",))
            server, surface = create_server(config)
            self.addCleanup(surface.runs.close)
            before = home.joinpath("config.yaml").read_bytes()
            denied = await call(
                server, "hermes_settings", "update", {"changes": {}, "expected_revision": "x"}
            )
            self.assertTrue(denied.is_error)
            self.assertEqual(
                object_json(object_json(denied.structured_content)["error"])["code"], "access_denied"
            )
            help_result = await call(server, "hermes_settings", "help")
            self.assertNotIn("update", object_json(object_json(help_result.structured_content)["actions"]))
            denied_help = await call(server, "hermes_settings", "help", {"action": "update"})
            self.assertTrue(denied_help.is_error)
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), before)

    async def test_readonly_configuration_reports_only_read_actions_and_rejects_writes(self) -> None:
        with fixture() as (settings, _home):
            server, surface = create_server(replace(settings.native.config, writes=False))
            self.addCleanup(surface.runs.close)
            tools = {tool.name: tool for tool in await server.list_tools()}
            for tool in tools.values():
                assert tool.annotations is not None
                self.assertTrue(tool.annotations.read_only_hint)
            help_result = await call(server, "hermes_tasks", "help")
            self.assertEqual(
                set(object_json(object_json(help_result.structured_content)["actions"])), {"status", "result"}
            )
            denied = await call(
                server, "hermes_tasks", "run", {"agent": "default", "task": "unused", "request_id": "x"}
            )
            self.assertTrue(denied.is_error)
            self.assertEqual(
                object_json(object_json(denied.structured_content)["error"])["code"], "access_denied"
            )

    async def test_invalid_action_missing_extra_and_wrong_typed_parameters_are_safe_errors(self) -> None:
        with fixture() as (settings, home):
            server, surface = create_server(settings.native.config)
            self.addCleanup(surface.runs.close)
            before = home.joinpath("config.yaml").read_bytes()
            cases: tuple[tuple[str, Object, str], ...] = (
                ("unknown", {}, "invalid_action"),
                ("create", {"title": "secret-input"}, "invalid_input"),
                ("get", {"limit": "secret-input"}, "invalid_input"),
                ("get", {"limit": True}, "invalid_input"),
                ("get", {"unexpected": "secret-input"}, "invalid_input"),
                ("help", {"action": 1}, "invalid_input"),
                ("help", {"unexpected": "secret-input"}, "invalid_input"),
            )
            for action, params, code in cases:
                with self.subTest(action=action, params=params):
                    result = await call(server, "hermes_kanban", action, params)
                    self.assertTrue(result.is_error)
                    self.assertEqual(
                        object_json(object_json(result.structured_content)["error"])["code"], code
                    )
                    self.assertNotIn("secret-input", str(result))
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), before)

    async def test_bound_profile_is_used_in_nested_parameters_and_help_schema(self) -> None:
        with fixture() as (settings, home):
            other = home / "profiles" / "b"
            other.mkdir(parents=True)
            other.joinpath("config.yaml").write_text("model: model-b\n")
            server, surface = create_server(settings.native.config, default_profile="b")
            self.addCleanup(surface.runs.close)
            result = await call(server, "hermes_settings", "get")
            self.assertEqual(object_json(result.structured_content)["profile"], "b")
            help_result = await call(server, "hermes_settings", "help", {"action": "get"})
            schema = object_json(object_json(help_result.structured_content)["params_schema"])
            self.assertEqual(object_json(object_json(schema["properties"])["profile"])["default"], "b")
            explicit = await call(server, "hermes_settings", "get", {"profile": "default"})
            self.assertEqual(object_json(explicit.structured_content)["profile"], "default")
