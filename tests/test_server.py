# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from hermes_bridge.core import BridgeError, Config, Object, object_json, read_token, rows
from hermes_bridge.server import create_server, http_app
from tests.support import REPO
from tests.test_settings import fixture


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_full_access_still_requires_authorization_for_settings_write(self) -> None:
        with fixture("model: original\n") as (settings, home):
            key = settings.native.config.token_file
            key.write_text("a" * 48)
            key.chmod(0o600)
            config = settings.native.config
            server, surface = create_server(config)
            try:
                before = surface.settings.get("default")
                original = home.joinpath("config.yaml").read_bytes()
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "hermes_settings",
                        "arguments": {
                            "action": "update",
                            "params": {
                                "changes": {"model.default": "authorized"},
                                "expected_revision": before["revision"],
                            },
                        },
                    },
                }
                headers = {"Accept": "application/json, text/event-stream"}
                app = http_app(server, config)
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18788"
                    ) as client,
                ):
                    denied = await client.post("/mcp", json=payload, headers=headers)
                    self.assertEqual(denied.status_code, 401)
                    self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)
                    response = await client.post(
                        "/mcp", json=payload, headers={**headers, "Authorization": "Bearer " + "a" * 48}
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    result = object_json(object_json(response.json())["result"])
                    self.assertFalse(result.get("isError", False), response.text)
                    data = object_json(result["structuredContent"])
                    self.assertEqual(object_json(data["settings"])["model.default"], "authorized")
            finally:
                surface.runs.close()

    async def test_disabled_tools_are_removed_and_unknown_tool_names_fail(self) -> None:
        config = Config(REPO, Path("/key"), ("*",), ("default",))
        config = replace(config, disabled_tools=("hermes_run", "hermes_settings_restore"))
        server, surface = create_server(config)
        try:
            names = {tool.name for tool in await server.list_tools()}
            self.assertNotIn("hermes_run", names)
            self.assertNotIn("hermes_settings_restore", names)
            self.assertIn("hermes_settings", names)
            self.assertEqual(len(names), 10)
            result = await server.call_tool("hermes_settings", {"action": "help"})
            assert isinstance(result, CallToolResult)
            actions = object_json(object_json(result.structured_content)["actions"])
            self.assertNotIn("restore", actions)
            self.assertIn("apply", actions)
            with self.assertRaisesRegex(ToolError, "Unknown tool: hermes_run"):
                await server.call_tool(
                    "hermes_run", {"agent": "default", "task": "unused", "request_id": "unused"}
                )
        finally:
            surface.runs.close()
        with self.assertRaises(BridgeError) as error:
            create_server(replace(config, disabled_tools=("hermes_typo",)))
        self.assertEqual(error.exception.code, "invalid_config")

    async def test_authentication_can_be_explicitly_disabled_and_origin_checks_remain(self) -> None:
        config = Config(REPO, Path("/missing-token"), ("*",), ("default",))
        server, surface = create_server(replace(config, auth_enabled=False))
        try:
            app = http_app(server, replace(config, auth_enabled=False))
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18788"
                ) as client,
            ):
                payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                headers = {"Accept": "application/json, text/event-stream"}
                response = await client.post("/mcp", json=payload, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(len(rows(object_json(object_json(response.json())["result"])["tools"])), 10)
                denied = await client.post(
                    "/mcp", json=payload, headers={**headers, "Origin": "https://evil.example"}
                )
                self.assertEqual(denied.status_code, 403)
            with self.assertRaises(BridgeError) as error:
                http_app(server, config)
            self.assertEqual(error.exception.code, "invalid_token")
        finally:
            surface.runs.close()

    async def test_full_settings_round_trip_and_disk_restore_through_mcp(self) -> None:
        with fixture("model: original\n", "OPENAI_API_KEY=PRIVATE_KEY\nHERMES_LOG_LEVEL=info\n") as (
            settings,
            home,
        ):
            project_root = await asyncio.to_thread(lambda: Path(__file__).resolve().parents[1])
            original = (home.joinpath("config.yaml").read_bytes(), home.joinpath(".env").read_bytes())
            server, surface = create_server(settings.native.config)

            async def call(action: str, arguments: Object) -> Object:
                result = await server.call_tool("hermes_settings", {"action": action, "params": arguments})
                if not isinstance(result, CallToolResult) or result.is_error:
                    msg = f"MCP settings failed: {action}: {result}"
                    raise AssertionError(msg)
                self.assertNotIn("PRIVATE_KEY", str(result))
                return object_json(result.structured_content)

            try:
                before = await call("get", {})
                config = deepcopy(object_json(before["config"]))
                model = config["model"]
                assert isinstance(model, dict)
                model["default"] = "changed"
                env = object_json(before["env"])
                env["HERMES_LOG_LEVEL"] = "debug"
                after = await call(
                    "apply",
                    {
                        "config": config,
                        "env": env,
                        "expected_revision": before["revision"],
                    },
                )
                self.assertEqual(object_json(after["settings"])["model.default"], "changed")
                self.assertEqual(object_json(after["env"])["HERMES_LOG_LEVEL"], "debug")
                version = after["before_version_id"]
                restarted = await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        "-c",
                        (
                            "import json, sys; from pathlib import Path; "
                            "sys.path.insert(0, sys.argv[2]); "
                            "from hermes_bridge.settings_history import SettingsHistory; "
                            "print(json.dumps(SettingsHistory('default', Path(sys.argv[1])).versions(50)))"
                        ),
                        str(home),
                        str(settings.native.config.hermes_repo),
                    ],
                    cwd=project_root,
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(restarted.returncode, 0, restarted.stderr)
                durable = object_json(json.loads(restarted.stdout))
                self.assertIn(version, [entry["id"] for entry in rows(durable["versions"])])
            finally:
                surface.runs.close()

            # Recreating all bridge objects must still find and restore the disk version.
            server, surface = create_server(settings.native.config)
            try:
                history = await call("versions", {})
                self.assertIn(version, [entry["id"] for entry in rows(history["versions"])])
                current = await call("get", {})
                await call(
                    "restore",
                    {
                        "version_id": version,
                        "expected_revision": current["revision"],
                    },
                )
                self.assertEqual(
                    (home.joinpath("config.yaml").read_bytes(), home.joinpath(".env").read_bytes()), original
                )
            finally:
                surface.runs.close()

    async def test_native_composition_tool_discovery_annotations_and_denied_write(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            key = Path(root) / "key"
            key.write_text("a" * 48)
            key.chmod(0o600)
            config = Config(REPO, key, ("*",), ("default",), writes=False)
            server, surface = create_server(config)
            try:
                tools = {t.name: t for t in await server.list_tools()}
                for name in (
                    "hermes_system",
                    "hermes_tasks",
                    "hermes_profiles",
                    "hermes_workers",
                    "hermes_sessions",
                    "hermes_settings",
                ):
                    self.assertIn(name, tools)
                self.assertNotIn("permissions_respond", tools)
                self.assertNotIn("attachments_fetch", tools)
                annotations = tools["hermes_settings"].annotations
                self.assertIsNotNone(annotations)
                if annotations is not None:
                    self.assertTrue(annotations.read_only_hint)
                result = await server.call_tool(
                    "hermes_tasks",
                    {"action": "run", "params": {"agent": "default", "task": "hello", "request_id": "x"}},
                )
                assert isinstance(result, CallToolResult)
                self.assertTrue(result.is_error)
                self.assertIn("access_denied", str(result))
            finally:
                surface.runs.close()

    async def test_http_rejects_missing_credentials_wrong_origin_and_private_key_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            key = Path(root) / "key"
            key.write_text("a" * 48)
            key.chmod(0o600)
            config = Config(REPO, key, ("*",), ("default",))
            server, surface = create_server(config)
            try:
                app = http_app(server, config)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18788"
                ) as client:
                    self.assertEqual((await client.post("/mcp")).status_code, 401)
                    discovery = await client.get("/.well-known/oauth-protected-resource/mcp")
                    self.assertEqual(discovery.status_code, 404)
                    response = await client.post("/mcp", headers={b"authorization": b"Bearer \xff"})
                    self.assertEqual(response.status_code, 401)
                    bad_origin = await client.post(
                        "/mcp",
                        headers={"Authorization": "Bearer " + "a" * 48, "Origin": "https://evil.example"},
                    )
                    self.assertEqual(bad_origin.status_code, 403)
                key.chmod(0o644)
                with self.assertRaises(BridgeError):
                    read_token(key)
            finally:
                surface.runs.close()
