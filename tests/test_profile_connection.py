# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from hermes_bridge.core import BridgeError, Object, object_json
from tests.support import REPO
from tests.test_settings import fixture

if TYPE_CHECKING:
    from types import ModuleType

    from hermes_bridge.settings import Settings

PLUGIN = Path(__file__).resolve().parents[1]


def runtime_config(home: Path, **overrides: object) -> Path:
    path = home.parent / "bridge.json"
    path.write_text(
        json.dumps(
            {
                "hermes_repo": str(REPO),
                "token_file": str(home / "key"),
                "profiles": ["*"],
                "boards": ["default"],
                **overrides,
            }
        )
    )
    return path


def profile_home(home: Path, text: str = "model: model-b\n") -> Path:
    other = home / "profiles" / "b"
    other.mkdir(parents=True)
    other.joinpath("config.yaml").write_text(text)
    other.joinpath(".env").write_text("OPENAI_API_KEY=PRIVATE_B\n")
    return other


class ProfileConnectionTests(unittest.TestCase):
    def module(self) -> ModuleType:
        self.assertIsNotNone(
            importlib.util.find_spec("hermes_bridge.profile_connection"),
            "The profile connection command is missing",
        )
        return importlib.import_module("hermes_bridge.profile_connection")

    def connect(self, path: Path, **options: object) -> Object:
        return object_json(self.module().configure_connection(path, "b", **options))

    def test_connect_is_profile_scoped_and_restorable_without_changing_secrets(self) -> None:
        with fixture(env_text="OPENAI_API_KEY=PRIVATE_DEFAULT\n") as (settings, home):
            target = profile_home(
                home,
                "# Keep this comment\nmodel:\n  default: model-b\n  api_key: PRIVATE_CONFIG\n"
                "terminal:\n  cwd: /project\ncustom_extension:\n  retained: true\n",
            )
            default_bytes = home.joinpath("config.yaml").read_bytes()
            target_bytes = target.joinpath("config.yaml").read_bytes()
            env_bytes = target.joinpath(".env").read_bytes()
            path = runtime_config(home)
            result = self.connect(path)
            self.assertTrue(result["connected"])
            self.assertTrue(result["cli_available"])
            self.assertEqual(result["profile"], "b")
            cfg = object_json(settings.get("b")["config_overrides"])
            entry = object_json(object_json(cfg["mcp_servers"])["hermes-link"])
            self.assertEqual(entry["command"], str(REPO / "venv/bin/python"))
            self.assertEqual(entry["cwd"], str(PLUGIN))
            self.assertEqual(
                entry["env"],
                {
                    "HERMES_HOME": str(home),
                    "HERMES_KANBAN_DB": "",
                    "HERMES_KANBAN_BOARD": "",
                    "HERMES_KANBAN_HOME": "",
                    "HERMES_KANBAN_WORKSPACES_ROOT": "",
                    "HERMES_KANBAN_ATTACHMENTS_ROOT": "",
                },
            )
            self.assertEqual(
                entry["args"],
                ["-m", "hermes_bridge.main", "--config", str(path), "--transport", "stdio", "--profile", "b"],
            )
            self.assertEqual(entry["trust"], "full")
            self.assertEqual(cfg["custom_extension"], {"retained": True})
            self.assertEqual(cfg["terminal"], {"cwd": "/project"})
            self.assertIn("# Keep this comment", target.joinpath("config.yaml").read_text())
            self.assertIn("PRIVATE_CONFIG", target.joinpath("config.yaml").read_text())
            self.assertEqual(target.joinpath(".env").read_bytes(), env_bytes)
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), default_bytes)
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertIn("before_version_id", result)
            settings.restore("b", str(result["before_version_id"]), str(result["revision"]))
            self.assertEqual(target.joinpath("config.yaml").read_bytes(), target_bytes)

    def test_same_connection_is_idempotent_and_disconnect_preserves_other_servers(self) -> None:
        with fixture() as (settings, home):
            target = profile_home(home, "model: model-b\nmcp_servers:\n  other:\n    command: other-tool\n")
            path = runtime_config(home)
            first = self.connect(path)
            original = target.joinpath("config.yaml").read_bytes()
            second = self.connect(path)
            self.assertFalse(second["changed"])
            self.assertEqual(second["revision"], first["revision"])
            self.assertEqual(target.joinpath("config.yaml").read_bytes(), original)
            untrusted = self.connect(path, trust="untrusted")
            self.assertEqual(untrusted["trust"], "untrusted")
            self.assertTrue(untrusted["changed"])
            disconnected = self.connect(path, disconnect=True)
            self.assertFalse(disconnected["connected"])
            self.assertFalse(disconnected["cli_available"])
            self.assertEqual(
                object_json(settings.get("b")["config_overrides"])["mcp_servers"],
                {"other": {"command": "other-tool"}},
            )
            self.assertFalse(self.connect(path, disconnect=True)["changed"])

    def test_explicit_mcp_allowlist_retains_existing_tools_when_connection_is_added(self) -> None:
        with fixture() as (settings, home):
            profile_home(
                home,
                "model: model-b\nmcp_servers:\n  other:\n    command: other-tool\n"
                "platform_toolsets:\n  cli: [terminal, other]\n  telegram: [terminal]\n",
            )
            path = runtime_config(home)
            result = self.connect(path)
            self.assertTrue(result["cli_available"])
            cfg = object_json(settings.get("b")["config_overrides"])
            self.assertEqual(
                cfg["platform_toolsets"],
                {"cli": ["terminal", "other", "hermes-link"], "telegram": ["terminal"]},
            )
            self.connect(path, disconnect=True)
            self.assertEqual(
                object_json(settings.get("b")["config_overrides"])["platform_toolsets"],
                {"cli": ["terminal", "other"], "telegram": ["terminal"]},
            )

    def test_explicit_cli_gates_fail_before_any_write(self) -> None:
        gates = (
            "platform_toolsets:\n  cli: [terminal, no_mcp]\n",
            "agent:\n  disabled_toolsets: [hermes-link]\n",
            "agent:\n  disabled_toolsets: '[\"hermes-link\"]'\n",
        )
        for gate in gates:
            with self.subTest(gate=gate), fixture() as (_settings, home):
                target = profile_home(home, "model: model-b\n" + gate)
                original = target.joinpath("config.yaml").read_bytes()
                with self.assertRaises(BridgeError) as denied:
                    self.connect(runtime_config(home))
                self.assertEqual(denied.exception.code, "connection_disabled")
                self.assertEqual(target.joinpath("config.yaml").read_bytes(), original)
                self.assertFalse(target.joinpath("backups").exists())

    def test_collision_refuses_connect_and_disconnect(self) -> None:
        with fixture() as (_settings, home):
            target = profile_home(
                home, "model: model-b\nmcp_servers:\n  hermes-link:\n    url: https://example.test/mcp\n"
            )
            original = target.joinpath("config.yaml").read_bytes()
            path = runtime_config(home)
            for disconnect in (False, True):
                with self.subTest(disconnect=disconnect):
                    with self.assertRaises(BridgeError) as collision:
                        self.connect(path, disconnect=disconnect)
                    self.assertEqual(collision.exception.code, "connection_conflict")
                    self.assertEqual(target.joinpath("config.yaml").read_bytes(), original)

    def test_concurrent_edit_is_preserved_by_revision_guard(self) -> None:
        with fixture() as (settings, home):
            target = profile_home(home)
            apply = type(settings).apply

            def edit_then_apply(
                instance: Settings,
                profile: str,
                config: Object,
                env: Object | None,
                expected_revision: str,
            ) -> Object:
                target.joinpath("config.yaml").write_text("model: changed-concurrently\n")
                return apply(instance, profile, config, env, expected_revision)

            with (
                patch("hermes_bridge.settings.Settings.apply", new=edit_then_apply),
                self.assertRaises(BridgeError) as stale,
            ):
                self.connect(runtime_config(home))
            self.assertEqual(stale.exception.code, "revision_conflict")
            self.assertEqual(target.joinpath("config.yaml").read_text(), "model: changed-concurrently\n")

    def test_cli_reports_connection_without_exposing_private_settings(self) -> None:
        with fixture() as (_settings, home):
            profile_home(home)
            path = runtime_config(home)
            output = io.StringIO()
            with redirect_stdout(output):
                code = self.module().main(["--config", str(path), "--profile", "b", "--trust", "untrusted"])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["trust"], "untrusted")
            self.assertTrue(result["cli_available"])
            self.assertIn("next session", result["notice"].lower())
            self.assertNotIn("PRIVATE", output.getvalue())

    def test_read_only_policy_does_not_write_a_connection(self) -> None:
        with fixture() as (_settings, home):
            target = profile_home(home)
            original = target.joinpath("config.yaml").read_bytes()
            with self.assertRaises(BridgeError) as denied:
                self.connect(runtime_config(home, writes=False))
            self.assertEqual(denied.exception.code, "access_denied")
            self.assertEqual(target.joinpath("config.yaml").read_bytes(), original)

    def test_missing_runtime_or_incomplete_plugin_fails_before_persistence(self) -> None:
        with fixture() as (_settings, home):
            target = profile_home(home)
            original = target.joinpath("config.yaml").read_bytes()
            absent_repo = home.parent / "incomplete-hermes"
            absent_repo.mkdir()
            absent_repo.joinpath("mcp_serve.py").touch()
            incomplete_plugin = home.parent / "incomplete-plugin"
            incomplete_plugin.mkdir()
            first_config = home.parent / "missing-runtime.json"
            first_config.write_bytes(runtime_config(home, hermes_repo=str(absent_repo)).read_bytes())
            cases: tuple[tuple[Path, dict[str, object]], ...] = (
                (first_config, {}),
                (runtime_config(home), {"plugin_dir": incomplete_plugin}),
            )
            for config_path, options in cases:
                with self.subTest(options=options):
                    with self.assertRaises(BridgeError) as absent:
                        self.connect(config_path, **options)
                    self.assertEqual(absent.exception.code, "hermes_unavailable")
                    self.assertEqual(target.joinpath("config.yaml").read_bytes(), original)
                    self.assertFalse(target.joinpath("backups").exists())

    def test_pinned_connection_cannot_be_disconnected_or_reported_removed(self) -> None:
        with fixture() as (_settings, home):
            target = profile_home(home)
            original = target.joinpath("config.yaml").read_bytes()
            path = runtime_config(home)
            managed = home.parent / "managed"
            managed.mkdir()
            managed.joinpath("config.yaml").write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "hermes-link": {
                                "command": str(REPO / "venv/bin/python"),
                                "args": [
                                    "-m",
                                    "hermes_bridge.main",
                                    "--config",
                                    str(path),
                                    "--transport",
                                    "stdio",
                                    "--profile",
                                    "b",
                                ],
                                "cwd": str(PLUGIN),
                                "env": {"HERMES_HOME": str(home)},
                                "enabled": True,
                                "trust": "full",
                            }
                        }
                    }
                )
            )
            with patch.dict(os.environ, {"HERMES_MANAGED_DIR": str(managed)}):
                with self.assertRaises(BridgeError) as pinned:
                    self.connect(path, disconnect=True)
                self.assertEqual(pinned.exception.code, "access_denied")
            self.assertEqual(target.joinpath("config.yaml").read_bytes(), original)
            self.assertFalse(target.joinpath("backups").exists())

    def test_unrelated_disabled_toolset_does_not_bootstrap_or_block_the_connection(self) -> None:
        with fixture() as (settings, home):
            target = profile_home(home, "model: model-b\nagent:\n  disabled_toolsets: [terminal]\n")
            original = target.joinpath("config.yaml").read_bytes()
            result = self.connect(runtime_config(home))
            self.assertTrue(result["cli_available"])
            cfg = object_json(settings.get("b")["config_overrides"])
            self.assertEqual(cfg["agent"], {"disabled_toolsets": ["terminal"]})
            settings.restore("b", str(result["before_version_id"]), str(result["revision"]))
            self.assertEqual(target.joinpath("config.yaml").read_bytes(), original)

    def test_disconnect_does_not_enable_previously_excluded_mcp_servers(self) -> None:
        with fixture() as (settings, home):
            profile_home(
                home,
                "model: model-b\nmcp_servers:\n  other:\n    command: other-tool\n"
                "platform_toolsets:\n  cli: [terminal, hermes-link]\n",
            )
            path = runtime_config(home)
            self.connect(path)
            self.connect(path, disconnect=True)
            cfg = object_json(settings.get("b")["config"])
            selection = object_json(cfg["platform_toolsets"])["cli"]
            self.assertEqual(selection, ["terminal", "no_mcp"])
            tools_config = importlib.import_module("hermes_cli.tools_config")
            discovery = importlib.import_module("hermes_cli.plugins_discovery")
            with settings.native.home_scope("b"), discovery.suppress_plugin_discovery():
                available = tools_config._get_platform_tools(cfg, "cli")
            self.assertIn("terminal", available)
            self.assertNotIn("other", available)
            self.assertNotIn("hermes-link", available)

    def test_custom_managed_scope_is_carried_to_the_stdio_child_without_secrets(self) -> None:
        with fixture() as (settings, home):
            profile_home(home)
            managed = home.parent / "custom-managed"
            managed.mkdir()
            managed.joinpath("config.yaml").write_text("agent:\n  max_turns: 17\n")
            managed.joinpath(".env").write_text("CUSTOM_ADMIN_SECRET=PRIVATE_ADMIN\n")
            with patch.dict(os.environ, {"HERMES_MANAGED_DIR": str(managed)}):
                result = self.connect(runtime_config(home))
                cfg = object_json(settings.get("b")["config_overrides"])
                entry = object_json(object_json(cfg["mcp_servers"])["hermes-link"])
            self.assertTrue(result["connected"])
            self.assertEqual(
                entry["env"],
                {
                    "HERMES_HOME": str(home),
                    "HERMES_MANAGED_DIR": str(managed.resolve()),
                    "HERMES_KANBAN_DB": "",
                    "HERMES_KANBAN_BOARD": "",
                    "HERMES_KANBAN_HOME": "",
                    "HERMES_KANBAN_WORKSPACES_ROOT": "",
                    "HERMES_KANBAN_ATTACHMENTS_ROOT": "",
                },
            )
            self.assertNotIn("PRIVATE_ADMIN", json.dumps(entry))

    def test_cli_configuration_errors_are_sanitized(self) -> None:
        with fixture() as (_settings, home):
            output = io.StringIO()
            with redirect_stderr(output):
                code = self.module().main(["--config", str(home / "absent.json"), "--profile", "b"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_config")
            self.assertNotIn("Traceback", output.getvalue())

    def test_cli_dependency_and_filesystem_failures_do_not_expose_tracebacks(self) -> None:
        with fixture() as (_settings, home):
            path = runtime_config(home)
            module = self.module()
            for failure in (ImportError("PRIVATE_DEPENDENCY_DETAIL"), OSError("PRIVATE_PATH_DETAIL")):
                with self.subTest(failure=type(failure).__name__):
                    output = io.StringIO()
                    with (
                        patch("hermes_bridge.profile_connection.Native", side_effect=failure),
                        redirect_stderr(output),
                    ):
                        try:
                            code = module.main(["--config", str(path), "--profile", "b"])
                        except (ImportError, OSError):
                            self.fail("The CLI must sanitize startup failures")
                    self.assertEqual(code, 1)
                    self.assertEqual(json.loads(output.getvalue())["error"]["code"], "hermes_unavailable")
                    self.assertNotIn("PRIVATE", output.getvalue())
                    self.assertNotIn("Traceback", output.getvalue())

    def test_worker_board_pins_are_cleared_by_the_native_stdio_environment(self) -> None:
        with fixture() as (settings, home):
            profile_home(home)
            self.connect(runtime_config(home))
            cfg = object_json(settings.get("b")["config_overrides"])
            entry = object_json(object_json(cfg["mcp_servers"])["hermes-link"])
            env = object_json(entry["env"])
            worker_pins = {
                "HERMES_KANBAN_DB": "/worker/other.db",
                "HERMES_KANBAN_BOARD": "other",
                "HERMES_KANBAN_HOME": "/worker/kanban",
                "HERMES_KANBAN_WORKSPACES_ROOT": "/worker/workspaces",
                "HERMES_KANBAN_ATTACHMENTS_ROOT": "/worker/attachments",
            }
            native_config = importlib.import_module("tools.mcp_tool_config")
            with patch.dict(os.environ, {**worker_pins, "HERMES_KANBAN_TASK": "task-from-another-board"}):
                child = native_config._build_safe_env(env)
            for name in worker_pins:
                with self.subTest(variable=name):
                    self.assertEqual(child.get(name), "")
            self.assertEqual(child["HERMES_HOME"], str(home))
            self.assertNotIn("OPENAI_API_KEY", child)

    def test_explicit_future_managed_directory_is_retained_for_child_policy(self) -> None:
        with fixture() as (settings, home):
            profile_home(home)
            future = home.parent / "future-managed"
            with patch.dict(os.environ, {"HERMES_MANAGED_DIR": str(future)}):
                self.connect(runtime_config(home))
                cfg = object_json(settings.get("b")["config_overrides"])
                entry = object_json(object_json(cfg["mcp_servers"])["hermes-link"])
            self.assertEqual(object_json(entry["env"]).get("HERMES_MANAGED_DIR"), str(future.resolve()))
            self.assertFalse(future.exists())

    def test_removing_a_disabled_connection_retains_already_active_mcp_servers(self) -> None:
        with fixture() as (settings, home):
            profile_home(
                home,
                "model: model-b\nmcp_servers:\n  other:\n    command: other-tool\n"
                "platform_toolsets:\n  cli: [terminal, hermes-link]\n",
            )
            path = runtime_config(home)
            self.connect(path)
            current = settings.get("b")
            cfg = object_json(current["config_overrides"])
            servers = object_json(cfg["mcp_servers"])
            entry = object_json(servers["hermes-link"])
            entry["enabled"] = False
            servers["hermes-link"] = entry
            cfg["mcp_servers"] = servers
            settings.apply("b", cfg, None, str(current["revision"]))
            self.connect(path, disconnect=True)
            after = object_json(settings.get("b")["config"])
            self.assertEqual(object_json(after["platform_toolsets"])["cli"], ["terminal"])
            tools_config = importlib.import_module("hermes_cli.tools_config")
            discovery = importlib.import_module("hermes_cli.plugins_discovery")
            with settings.native.home_scope("b"), discovery.suppress_plugin_discovery():
                available = tools_config._get_platform_tools(after, "cli")
            self.assertIn("other", available)
