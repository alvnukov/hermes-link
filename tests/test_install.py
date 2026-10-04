# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import contextlib
import importlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from hermes_bridge.core import Config, read_token
from hermes_bridge.native import Native
from scripts.install import install
from tests.support import REPO


class InstallTests(unittest.TestCase):
    def test_install_seeds_native_form_with_actual_policy_and_preserves_saved_choices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, runtime = root / ".hermes", root / "runtime"
            home.mkdir()
            runtime.mkdir()
            repo = REPO
            home.joinpath("config.yaml").write_text(
                "unrelated: retained\nplugins:\n  entries:\n    http-mcp:\n      settings:\n"
                "        disabled_tools: [hermes_run]\n        tool_hermes_result: false\n"
            )
            runtime.joinpath("config.json").write_text(
                json.dumps(
                    {
                        "hermes_repo": str(repo),
                        "token_file": str(runtime / "mcp.key"),
                        "profiles": ["*"],
                        "boards": ["default"],
                        "writes": False,
                        "auth_enabled": True,
                        "disabled_tools": ["hermes_info"],
                    }
                )
            )
            with patch("pathlib.Path.home", return_value=root), contextlib.redirect_stdout(io.StringIO()):
                install(repo, home, runtime)
                native = Native(Config.load(runtime / "config.json"))
                with native.home_scope("default"):
                    config_module = importlib.import_module("hermes_cli.config")
                    plugin_settings = importlib.import_module("hermes_cli.plugins_settings")

                    fields = {
                        item["key"]: item
                        for item in plugin_settings.plugin_settings_fields(
                            "http-mcp", home / "plugins/http-mcp"
                        )
                    }
                    self.assertFalse(fields["writes"]["value"])
                    self.assertTrue(fields["auth_enabled"]["value"])
                    self.assertFalse(fields["tool_hermes_run"]["value"])
                    self.assertFalse(fields["tool_hermes_result"]["value"])
                    self.assertTrue(fields["tool_hermes_info"]["value"])
                    self.assertTrue(fields["tool_hermes_agents"]["value"])
                    self.assertEqual(config_module.read_user_config_raw()["unrelated"], "retained")
                    plugin_settings.save_plugin_settings(
                        "http-mcp", home / "plugins/http-mcp", {"tool_hermes_run": True}
                    )
                    config_module.save_env_value("HERMES_HTTP_MCP_TOKEN", "u" * 48)
                    legacy_key = read_token(runtime / "mcp.key")
                    runtime.joinpath("mcp-authorization.header").write_text("Bearer " + "u" * 48 + "\n")
                install(repo, home, runtime)
                self.assertEqual(
                    runtime.joinpath("mcp-authorization.header").read_text().strip(), "Bearer " + "u" * 48
                )
                self.assertEqual(read_token(runtime / "mcp.key"), legacy_key)
                with native.home_scope("default"):
                    fields = {
                        item["key"]: item
                        for item in plugin_settings.plugin_settings_fields(
                            "http-mcp", home / "plugins/http-mcp"
                        )
                    }
                    self.assertTrue(fields["tool_hermes_run"]["value"])
                    self.assertFalse(fields["tool_hermes_result"]["value"])

    def test_new_install_enables_full_functionality_and_prepares_private_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repo = REPO
            home, runtime = root / "hermes-home", root / "runtime"
            with patch("pathlib.Path.home", return_value=root), contextlib.redirect_stdout(io.StringIO()):
                install(repo, home, runtime)
            config_file = runtime / "config.json"
            config = Config.load(config_file)
            self.assertTrue(config.writes)
            key = read_token(config.token_file)
            header_file = runtime / "mcp-authorization.header"
            self.assertEqual(header_file.read_text().strip(), "Bearer " + key)
            self.assertEqual(stat.S_IMODE(header_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.token_file.stat().st_mode), 0o600)
            previous = json.loads(config_file.read_text())
            previous["writes"] = False
            config_file.write_text(json.dumps(previous))
            with patch("pathlib.Path.home", return_value=root), contextlib.redirect_stdout(io.StringIO()):
                install(repo, home, runtime)
            self.assertFalse(Config.load(config_file).writes)
            self.assertEqual(read_token(config.token_file), key)

    def test_install_localizes_native_form_from_the_settings_owner_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, runtime = root / "home", root / "runtime"
            owner = home / "profiles/reviewer"
            owner.mkdir(parents=True)
            home.joinpath("config.yaml").write_text("display:\n  language: ru\n")
            owner.joinpath("config.yaml").write_text("display:\n  language: de-DE\n")
            runtime.mkdir()
            runtime.joinpath("mcp.key").write_text("a" * 48 + "\n")
            runtime.joinpath("mcp.key").chmod(0o600)
            runtime.joinpath("config.json").write_text(
                json.dumps(
                    {
                        "hermes_repo": str(REPO),
                        "token_file": str(runtime / "mcp.key"),
                        "profiles": ["*"],
                        "boards": ["default"],
                        "board_profile": "reviewer",
                    }
                )
            )
            with patch("pathlib.Path.home", return_value=root), contextlib.redirect_stdout(io.StringIO()):
                install(REPO, home, runtime)
            manifest = yaml.safe_load(home.joinpath("plugins/http-mcp/plugin.yaml").read_text())
            self.assertEqual(manifest["config_schema"]["auth_token"]["label"], "Zugriffstoken")
            for locale in ("en", "de", "ar", "zh-hant", "ga", "ko"):
                self.assertTrue(
                    home.joinpath("plugins/http-mcp/hermes_bridge/locales", locale + ".json").is_file()
                )
            self.assertEqual(Config.load(runtime / "config.json").board_profile, "reviewer")
            self.assertEqual(read_token(runtime / "mcp.key"), "a" * 48)
