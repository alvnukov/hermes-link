# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import argparse
import unittest
from dataclasses import replace

from mcp.types import CallToolResult

from hermes_bridge.core import BridgeError, Object, object_json
from hermes_bridge.main import setup_cli
from hermes_bridge.server import create_server
from tests.test_settings import fixture


class ProfileDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_omitted_profile_uses_consumer_for_schemas_reads_and_writes(self) -> None:
        with fixture("model: root-model\n") as (settings, home):
            other = home / "profiles" / "work"
            other.mkdir(parents=True)
            other.joinpath("config.yaml").write_text("model: work-model\n")
            other.joinpath(".env").write_text("")
            server, surface = create_server(settings.native.config, default_profile="work")

            async def call(action: str, arguments: Object) -> Object:
                result = await server.call_tool("hermes_settings", {"action": action, "params": arguments})
                self.assertIsInstance(result, CallToolResult)
                assert isinstance(result, CallToolResult)
                self.assertFalse(result.is_error, str(result))
                return object_json(result.structured_content)

            try:
                checked = 0
                for tool in await server.list_tools():
                    actions_result = await server.call_tool(tool.name, {"action": "help"})
                    assert isinstance(actions_result, CallToolResult)
                    actions = object_json(object_json(actions_result.structured_content)["actions"])
                    for action in actions:
                        schema_result = await server.call_tool(
                            tool.name, {"action": "help", "params": {"action": action}}
                        )
                        assert isinstance(schema_result, CallToolResult)
                        schema = object_json(object_json(schema_result.structured_content)["params_schema"])
                        properties = object_json(schema.get("properties", {}))
                        if "profile" in properties:
                            self.assertEqual(object_json(properties["profile"])["default"], "work")
                            checked += 1
                self.assertGreater(checked, 10)
                before = await call("get", {})
                self.assertEqual(before["profile"], "work")
                self.assertEqual(object_json(before["settings"])["model.default"], "work-model")
                after = await call(
                    "update",
                    {"changes": {"model.default": "work-updated"}, "expected_revision": before["revision"]},
                )
                self.assertEqual(after["profile"], "work")
                self.assertEqual(object_json(after["settings"])["model.default"], "work-updated")
                root = await call("get", {"profile": "default"})
                self.assertEqual(object_json(root["settings"])["model.default"], "root-model")
            finally:
                surface.runs.close()

    async def test_http_default_and_profile_allowlist_are_preserved(self) -> None:
        with fixture() as (settings, _home):
            server, surface = create_server(settings.native.config)
            try:
                result = await server.call_tool("hermes_settings", {"action": "get"})
                assert isinstance(result, CallToolResult)
                self.assertEqual(object_json(result.structured_content)["profile"], "default")
            finally:
                surface.runs.close()
            with self.assertRaises(BridgeError) as denied:
                create_server(replace(settings.native.config, profiles=("default",)), default_profile="work")
            self.assertEqual(denied.exception.code, "access_denied")
            with self.assertRaises(BridgeError) as missing:
                create_server(settings.native.config, default_profile="missing")
            self.assertEqual(missing.exception.code, "profile_not_found")

    async def test_cli_accepts_explicit_consumer_profile(self) -> None:
        parser = argparse.ArgumentParser()
        setup_cli(parser)
        args = parser.parse_args(
            ["--config", "/local/config.json", "--transport", "stdio", "--profile", "work"]
        )
        self.assertEqual(args.profile, "work")
