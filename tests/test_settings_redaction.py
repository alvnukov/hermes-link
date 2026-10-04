# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mcp.types import CallToolResult

from hermes_bridge.core import Object, object_json
from hermes_bridge.server import create_server
from hermes_bridge.settings import PRESERVE
from tests.test_settings import fixture

CANARY = "SUPER_SECRET_CANARY_928374"


class SettingsRedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookies_and_credential_arguments_are_hidden_and_round_trip(self) -> None:
        cases = (
            f"headers:\n      Cookie: 'sessionid={CANARY}'\n      Set-Cookie: '{CANARY}'",
            f"cookies:\n      session: '{CANARY}'",
            f"args: ['--token', '{CANARY}']",
            f"args: ['--api-key={CANARY}']",
            f"args: ['-H', 'Authorization: Bearer {CANARY}']",
            f"args: ['mcp-remote', 'https://example.test/mcp', '--header', 'X-API-Key: {CANARY}']",
            f"args: ['--header=X-API-Key: {CANARY}']",
            f"args: ['-H', 'X-Auth-Token: {CANARY}']",
        )
        for fields in cases:
            with (
                self.subTest(storage=fields.split(":", 1)[0]),
                fixture("model: original\nmcp_servers:\n  private:\n    " + fields + "\n") as (
                    settings,
                    home,
                ),
            ):
                before = settings.get("default")
                self.assertTrue(CANARY not in json.dumps(before), "Credential escaped settings export")
                after = settings.apply(
                    "default",
                    object_json(before["config_overrides"]),
                    None,
                    str(before["revision"]),
                )
                self.assertTrue(CANARY not in json.dumps(after), "Credential escaped apply response")
                self.assertIn(CANARY, home.joinpath("config.yaml").read_text())
                self.assertNotIn("$hermes_secret", home.joinpath("config.yaml").read_text())

    async def test_noncredential_headers_and_arguments_remain_editable(self) -> None:
        arguments = ["mcp-remote", "https://example.test/mcp", "--header=Content-Type: application/json"]
        with fixture("mcp_servers:\n  public:\n    args: " + json.dumps(arguments) + "\n") as (
            settings,
            _home,
        ):
            exported = object_json(object_json(settings.get("default")["config_overrides"])["mcp_servers"])
            self.assertEqual(object_json(exported["public"])["args"], arguments)

    async def test_compact_settings_view_keeps_revision_and_hides_full_tree(self) -> None:
        with fixture() as (settings, _home):
            server, surface = create_server(settings.native.config)
            try:
                full = settings.get("default")
                result = await server.call_tool(
                    "hermes_settings",
                    {
                        "action": "get",
                        "params": {"view": "editable"},
                    },
                )
                assert isinstance(result, CallToolResult)
                self.assertFalse(result.is_error, result)
                compact = object_json(result.structured_content)
                self.assertEqual(compact["revision"], full["revision"])
                self.assertEqual(compact["settings"], full["settings"])
                self.assertNotIn("config", compact)
                self.assertNotIn("config_overrides", compact)
                self.assertNotIn("env", compact)
            finally:
                surface.runs.close()

    async def test_credentials_never_leave_mcp_and_preserve_markers_round_trip(self) -> None:
        config = (
            "model: original\nagent:\n  max_tokens: 1234\n"
            "plugins:\n  demo:\n    auth_enabled: true\n"
            f"    peers:\n      - peer_auth_tokens: '{CANARY}'\n"
            f"    credential_bundle:\n      opaque: '{CANARY}'\n"
            f"    password_hash: '{CANARY}'\n"
            "mcp_servers:\n  demo:\n"
            f"    env:\n      PEER_TOKENS: '{CANARY}'\n"
            f"    headers:\n      X-Credential: '{CANARY}'\n"
        )
        with fixture(config, f"A2A_PEER_TOKENS={CANARY}\nHERMES_LOG_LEVEL=info\n") as (settings, home):
            server, surface = create_server(settings.native.config)

            async def call(tool: str, action: str, params: Object | None = None) -> Object:
                result = await server.call_tool(tool, {"action": action, "params": params or {}})
                self.assertIsInstance(result, CallToolResult)
                assert isinstance(result, CallToolResult)
                self.assertFalse(result.is_error, result)
                self.assertTrue(
                    CANARY not in result.model_dump_json(), "Credential escaped MCP serialization"
                )
                return object_json(result.structured_content)

            try:
                original = (home.joinpath("config.yaml").read_bytes(), home.joinpath(".env").read_bytes())
                before = await call("hermes_settings", "get")
                self.assertEqual(object_json(before["env"])["A2A_PEER_TOKENS"], PRESERVE)
                self.assertEqual(object_json(before["settings"])["agent.max_tokens"], 1234)
                self.assertEqual(object_json(before["env"])["HERMES_LOG_LEVEL"], "info")
                demo = object_json(object_json(object_json(before["config"])["plugins"])["demo"])
                self.assertIs(demo["auth_enabled"], True)  # noqa: FBT003 - Require an actual boolean.
                await call("hermes_settings", "schema")
                await call("hermes_system", "capabilities")
                applied = await call(
                    "hermes_settings",
                    "apply",
                    {
                        "config": before["config_overrides"],
                        "env": before["env"],
                        "expected_revision": before["revision"],
                    },
                )
                self.assertIn(CANARY, home.joinpath("config.yaml").read_text())
                self.assertIn(CANARY, home.joinpath(".env").read_text())
                self.assertNotIn("$hermes_secret", home.joinpath("config.yaml").read_text())
                changed = await call(
                    "hermes_settings",
                    "update",
                    {
                        "changes": {"model.default": "changed"},
                        "expected_revision": applied["revision"],
                    },
                )
                await call("hermes_settings", "versions")
                await call(
                    "hermes_settings",
                    "restore",
                    {
                        "version_id": applied["before_version_id"],
                        "expected_revision": changed["revision"],
                    },
                )
                self.assertEqual(
                    (home.joinpath("config.yaml").read_bytes(), home.joinpath(".env").read_bytes()),
                    original,
                )
            finally:
                surface.runs.close()

    async def test_schema_defaults_use_the_same_credential_redaction(self) -> None:
        with (
            fixture() as (settings, _home),
            patch.object(
                settings.module, "DEFAULT_CONFIG", {"nested": [{"credentials": {"opaque": CANARY}}]}
            ),
        ):
            self.assertNotIn(CANARY, json.dumps(settings.schema()))

    async def test_embedded_credentials_and_registered_opaque_secret_are_preserved(self) -> None:
        with (
            fixture(
                f"custom:\n  endpoint: 'https://user:{CANARY}@example.test/api'\n"
                f"  url: 'https://example.test/?access_token={CANARY}'\n"
                f"  headers:\n    X-Custom: 'Bearer {CANARY}'\n",
                f"OPAQUE_PLUGIN_VALUE={CANARY}\n",
            ) as (settings, home),
            patch.object(settings.module, "OPTIONAL_ENV_VARS", {"OPAQUE_PLUGIN_VALUE": {"password": True}}),
        ):
            before = settings.get("default")
            self.assertTrue(CANARY not in json.dumps(before), "Embedded or registered credential leaked")
            after = settings.apply(
                "default",
                object_json(before["config_overrides"]),
                object_json(before["env"]),
                str(before["revision"]),
            )
            self.assertTrue(CANARY not in json.dumps(after), "Credential leaked after apply")
            self.assertEqual(home.joinpath("config.yaml").read_text().count(CANARY), 3)
            self.assertIn(CANARY, home.joinpath(".env").read_text())
