# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import argparse
import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from hermes_bridge import main
from hermes_bridge.core import BridgeError, Config
from hermes_bridge.native import Native
from hermes_bridge.server import Surface
from tests.support import REPO

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class CLI:
    def __init__(self) -> None:
        self.names: list[str] = []

    def register_cli_command(
        self,
        *,
        name: str,
        help: str,  # noqa: A002 - native registration keyword contract
        setup_fn: Callable[[argparse.ArgumentParser], None],
        handler_fn: Callable[[argparse.Namespace], None],
        description: str = "",
    ) -> object:
        self.help_text = help
        self.handler = handler_fn
        self.description = description
        parser = argparse.ArgumentParser()
        setup_fn(parser)
        args = parser.parse_args(["--config", "/tmp/config.json"])
        self.assert_namespace(args)
        self.names.append(name)
        return None

    @staticmethod
    def assert_namespace(args: argparse.Namespace) -> None:
        if args.config != Path("/tmp/config.json") or args.transport != "http":
            msg = "Invalid native CLI registration"
            raise AssertionError(msg)


class PluginTests(unittest.TestCase):
    def test_native_directory_plugin_registers_cli_without_starting_server(self) -> None:
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "test_http_mcp_plugin", root / "__init__.py", submodule_search_locations=[str(root)]
        )
        assert spec is not None
        assert spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        cli = CLI()
        plugin.register(cli)
        self.assertEqual(cli.names, ["http-mcp"])


class NativePluginSettingsTests(unittest.TestCase):
    settings_module: ModuleType
    config_module: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(REPO))
        cls.config_module = importlib.import_module("hermes_cli.config")
        cls.settings_module = importlib.import_module("hermes_cli.plugins_settings")
        importlib.import_module("hermes_cli.profiles")

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = root / ".hermes"
        self.home.mkdir()
        self.home.joinpath("config.yaml").write_text("model: test\nunrelated: retained\n")
        self.enterContext(patch("pathlib.Path.home", return_value=root))
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": str(self.home)}))
        self.runtime_path = root / "bridge.json"
        self.runtime: dict[str, object] = {
            "hermes_repo": str(REPO),
            "token_file": str(root / "token"),
            "profiles": ["*"],
            "boards": ["default"],
            "writes": False,
            "auth_enabled": False,
            "disabled_tools": ["hermes_info"],
            "port": 18888,
            "max_concurrent_runs": 3,
        }
        self.save_runtime()

    def save_runtime(self) -> None:
        self.runtime_path.write_text(json.dumps(self.runtime))

    def load_effective(self) -> Config:
        return main.load_effective_config(self.runtime_path)

    def test_native_form_has_enabled_defaults_and_saves_values(self) -> None:
        fields = self.settings_module.plugin_settings_fields("http-mcp", PLUGIN_ROOT)
        by_key = {field["key"]: field for field in fields}
        self.assertEqual(
            {key: (by_key[key]["type"], by_key[key]["value"]) for key in ("writes", "auth_enabled")},
            {"writes": ("boolean", True), "auth_enabled": ("boolean", True)},
        )
        self.assertNotIn("disabled_tools", by_key)
        tool_fields = {
            key.removeprefix("tool_"): value for key, value in by_key.items() if key.startswith("tool_")
        }
        self.assertEqual(set(tool_fields), {name for name in dir(Surface) if name.startswith("hermes_")})
        for field in tool_fields.values():
            self.assertEqual((field["type"], field["value"]), ("boolean", True))
        self.settings_module.save_plugin_settings(
            "http-mcp", PLUGIN_ROOT, {"writes": False, "auth_enabled": False, "tool_hermes_run": False}
        )
        fields = self.settings_module.plugin_settings_fields("http-mcp", PLUGIN_ROOT)
        self.assertEqual(
            {
                field["key"]: field["value"]
                for field in fields
                if field["key"] in ("writes", "auth_enabled", "tool_hermes_run")
            },
            {"writes": False, "auth_enabled": False, "tool_hermes_run": False},
        )
        self.assertEqual(self.config_module.read_user_config_raw()["unrelated"], "retained")

    def test_native_form_secret_token_is_saved_separately_and_never_echoed(self) -> None:
        fields = self.settings_module.plugin_settings_fields("http-mcp", PLUGIN_ROOT)
        secret = next((field for field in fields if field["key"] == "auth_token"), None)
        self.assertIsNotNone(secret)
        assert secret is not None
        self.assertEqual(secret["type"], "secret")
        self.assertEqual(secret["env"], "HERMES_HTTP_MCP_TOKEN")
        self.assertNotIn("value", secret)
        token = "TEST_TOKEN_" + "x" * 32
        with self.assertRaises(ValueError):
            self.settings_module.save_plugin_settings("http-mcp", PLUGIN_ROOT, {"auth_token": token})
        self.config_module.save_env_value("HERMES_HTTP_MCP_TOKEN", token)
        self.assertIn(token, self.home.joinpath(".env").read_text())
        self.assertNotIn(token, self.home.joinpath("config.yaml").read_text())
        fields = self.settings_module.plugin_settings_fields("http-mcp", PLUGIN_ROOT)
        secret = next(field for field in fields if field["key"] == "auth_token")
        self.assertTrue(secret["has_value"])
        self.assertNotIn("value", secret)
        self.assertNotIn(token, str(fields))

    def save_legacy_disabled(self, tools: list[str]) -> None:
        state = importlib.import_module("hermes_cli.plugins_state")
        state.save_plugin_setting("http-mcp", ("disabled_tools",), tools)

    def test_unsaved_form_defaults_preserve_runtime_settings(self) -> None:
        config = self.load_effective()
        self.assertFalse(config.writes)
        self.assertFalse(config.auth_enabled)
        self.assertEqual(config.disabled_tools, ("hermes_info",))
        self.assertEqual(getattr(config, "token_env_file", None), self.home / ".env")

    def test_native_individual_switches_override_legacy_tool_list(self) -> None:
        self.settings_module.save_plugin_settings(
            "http-mcp", PLUGIN_ROOT, {"tool_hermes_info": True, "tool_hermes_run": False}
        )
        config = self.load_effective()
        self.assertEqual(set(config.disabled_tools), {"hermes_run"})

    def test_legacy_native_tool_list_remains_supported(self) -> None:
        self.save_legacy_disabled(["hermes_run", "hermes_settings_apply"])
        self.settings_module.save_plugin_settings("http-mcp", PLUGIN_ROOT, {"tool_hermes_run": True})
        self.assertEqual(set(self.load_effective().disabled_tools), {"hermes_settings_apply"})

    def test_malformed_or_unknown_native_switch_refuses_startup(self) -> None:
        state = importlib.import_module("hermes_cli.plugins_state")
        for key, value in (("tool_hermes_run", "false"), ("tool_hermes_unknown", False)):
            with self.subTest(setting=key):
                self.home.joinpath("config.yaml").write_text("model: test\n")
                state.save_plugin_setting("http-mcp", (key,), value)
                with self.assertRaises(BridgeError) as error:
                    self.load_effective()
                self.assertEqual(error.exception.code, "invalid_config")

    def test_saved_values_override_runtime_and_preserve_connection_settings(self) -> None:
        self.settings_module.save_plugin_settings(
            "http-mcp", PLUGIN_ROOT, {"writes": True, "auth_enabled": True, "tool_hermes_info": True}
        )
        config = self.load_effective()
        self.assertTrue(config.writes)
        self.assertTrue(config.auth_enabled)
        self.assertEqual(config.disabled_tools, ())
        self.assertEqual(config.port, 18888)
        self.assertEqual(config.max_concurrent_runs, 3)
        self.assertEqual(config.token_file, Path(str(self.runtime["token_file"])))
        self.assertEqual(config.profiles, ("*",))
        self.assertEqual(config.boards, ("default",))

    def test_partial_saved_settings_preserve_unsaved_runtime_values(self) -> None:
        self.settings_module.save_plugin_settings("http-mcp", PLUGIN_ROOT, {"writes": True})
        config = self.load_effective()
        self.assertTrue(config.writes)
        self.assertFalse(config.auth_enabled)
        self.assertEqual(config.disabled_tools, ("hermes_info",))

    def test_native_settings_follow_board_profile_and_restore_home_scope(self) -> None:
        self.settings_module.save_plugin_settings("http-mcp", PLUGIN_ROOT, {"writes": True})
        other = self.home / "profiles" / "b"
        other.mkdir(parents=True)
        other.joinpath("config.yaml").write_text("model: test-b\n")
        self.runtime["board_profile"] = "b"
        self.save_runtime()
        native = Native(Config.load(self.runtime_path))
        with native.home_scope("b"):
            self.settings_module.save_plugin_settings(
                "http-mcp",
                PLUGIN_ROOT,
                {"writes": False, "auth_enabled": True, "tool_hermes_info": True, "tool_hermes_run": False},
            )
        second = self.load_effective()
        self.assertFalse(second.writes)
        self.assertTrue(second.auth_enabled)
        self.assertEqual(second.disabled_tools, ("hermes_run",))
        self.assertEqual(getattr(second, "token_env_file", None), other / ".env")
        self.assertEqual(native.constants.get_hermes_home(), self.home)
        self.runtime["board_profile"] = "default"
        self.save_runtime()
        first = self.load_effective()
        self.assertTrue(first.writes)
        self.assertFalse(first.auth_enabled)
        self.assertEqual(first.disabled_tools, ("hermes_info",))
        self.assertEqual(getattr(first, "token_env_file", None), self.home / ".env")

    def test_malformed_native_setting_refuses_startup(self) -> None:
        self.home.joinpath("config.yaml").write_text(
            "plugins:\n  entries:\n    http-mcp:\n      settings:\n        auth_enabled: disabled\n"
        )
        with self.assertRaises(BridgeError) as error:
            self.load_effective()
        self.assertEqual(error.exception.code, "invalid_config")

    def test_malformed_native_yaml_refuses_last_known_good_fallback(self) -> None:
        self.settings_module.save_plugin_settings(
            "http-mcp", PLUGIN_ROOT, {"writes": True, "auth_enabled": True}
        )
        self.assertTrue(self.load_effective().auth_enabled)
        self.config_module.load_config_readonly()
        self.home.joinpath("config.yaml").write_text("display: [BROKEN_FIXTURE\n")
        with redirect_stderr(io.StringIO()), self.assertRaises(BridgeError) as error:
            self.load_effective()
        self.assertEqual(error.exception.code, "invalid_config")
        self.assertNotIn("BROKEN_FIXTURE", str(error.exception.payload()))

    def test_malformed_native_yaml_refuses_defaults_fallback(self) -> None:
        self.home.joinpath("config.yaml").write_text("plugins: [BROKEN_FIXTURE\n")
        with redirect_stderr(io.StringIO()), self.assertRaises(BridgeError) as error:
            self.load_effective()
        self.assertEqual(error.exception.code, "invalid_config")
        self.assertNotIn("BROKEN_FIXTURE", str(error.exception.payload()))

    def test_invalid_native_yaml_stops_command_before_server_creation(self) -> None:
        self.home.joinpath("config.yaml").write_text("plugins: [BROKEN_FIXTURE\n")
        stderr = io.StringIO()
        with (
            patch(
                "hermes_bridge.server.create_server", side_effect=AssertionError("Server creation reached")
            ),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as stopped,
        ):
            main.command(argparse.Namespace(config=self.runtime_path, transport="http"))
        self.assertEqual(stopped.exception.code, 1)
        payload = json.loads(stderr.getvalue().splitlines()[-1])
        self.assertEqual(
            payload,
            {"error": {"code": "invalid_config", "message": "Native configuration cannot be read safely"}},
        )

    def test_malformed_native_plugin_sections_refuse_runtime_fallback(self) -> None:
        malformed = (
            "plugins: []\n",
            "plugins:\n  entries: []\n",
            "plugins:\n  entries:\n    http-mcp: []\n",
            "plugins:\n  entries:\n    http-mcp:\n      settings: []\n",
            "plugins: null\n",
            "plugins:\n  entries: null\n",
            "plugins:\n  entries:\n    http-mcp: null\n",
            "plugins:\n  entries:\n    http-mcp:\n      settings: null\n",
        )
        for config_text in malformed:
            with self.subTest(config=config_text):
                self.home.joinpath("config.yaml").write_text(config_text)
                with redirect_stderr(io.StringIO()), self.assertRaises(BridgeError) as error:
                    self.load_effective()
                self.assertEqual(error.exception.code, "invalid_config")

    def test_standalone_entrypoint_applies_native_settings_before_server_creation(self) -> None:
        self.settings_module.save_plugin_settings(
            "http-mcp",
            PLUGIN_ROOT,
            {"writes": True, "auth_enabled": True, "tool_hermes_info": True, "tool_hermes_run": False},
        )
        captured: list[tuple[Config, str | None]] = []

        class ServerCreationReachedError(Exception):
            pass

        def capture(config: Config, *, default_profile: str | None = None) -> object:
            captured.append((config, default_profile))
            raise ServerCreationReachedError

        with (
            patch("hermes_bridge.server.create_server", side_effect=capture),
            patch.object(
                sys,
                "argv",
                ["hermes_bridge.main", "--config", str(self.runtime_path), "--transport", "stdio"],
            ),
            self.assertRaises(ServerCreationReachedError),
        ):
            main.main()
        self.assertEqual(len(captured), 1)
        config, profile = captured[0]
        self.assertIsNone(profile)
        self.assertTrue(config.writes)
        self.assertTrue(config.auth_enabled)
        self.assertEqual(config.disabled_tools, ("hermes_run",))
