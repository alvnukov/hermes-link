# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_bridge.core import BridgeError, Config, bounded, object_json, project, safe_id


class CoreTests(unittest.TestCase):
    def test_numeric_limits_reject_fractional_numbers(self) -> None:
        with self.assertRaises(BridgeError) as error:
            bounded(1.5, 1, 8)
        self.assertEqual(error.exception.code, "invalid_input")

    def test_individual_tool_controls_override_only_explicit_choices(self) -> None:
        config = Config(
            Path("/repo"),
            Path("/key"),
            ("default",),
            ("default",),
            disabled_tools=("hermes_info", "hermes_run"),
        )
        available = {"hermes_info", "hermes_run", "hermes_result"}
        changed = config.with_tool_settings(
            {"tool_hermes_info": True, "tool_hermes_result": False}, available
        )
        self.assertEqual(changed.disabled_tools, ("hermes_result", "hermes_run"))
        self.assertEqual(config.disabled_tools, ("hermes_info", "hermes_run"))
        for value in ({"tool_hermes_run": "false"}, {"tool_hermes_typo": True}, {"tool_hermes_typo": False}):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                config.with_tool_settings(object_json(value), available)

    def test_projection_is_a_positive_allowlist(self) -> None:
        source = {
            "name": "devops",
            "model": "gpt",
            "token": "SECRET",
            "env": {"API_KEY": "SECRET"},
            "system_prompt": "PRIVATE",
        }
        self.assertEqual(project(source, ("name", "model")), {"name": "devops", "model": "gpt"})

    def test_invalid_ids_cannot_be_paths(self) -> None:
        for value in ("../default", "x/y", "x?profile=secret", "", "x" * 161):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                safe_id(value)
        self.assertEqual(safe_id("devops"), "devops")

    def test_json_is_validated_at_the_untyped_boundary(self) -> None:
        self.assertEqual(object_json({"x": [1, True, None]}), {"x": [1, True, None]})
        with self.assertRaises(BridgeError):
            object_json({"invalid": object()})

    def test_config_denies_unlisted_profiles_boards_and_writes(self) -> None:
        c = Config(Path("/repo"), Path("/key"), ("default",), ("default",), writes=False)
        self.assertEqual(c.profile("default"), "default")
        for action in (lambda: c.profile("devops"), lambda: c.board("secret"), c.require_writes):
            with self.assertRaises(BridgeError):
                action()

    def test_full_functionality_is_default_and_explicit_read_only_is_preserved(self) -> None:
        direct = Config(Path("/repo"), Path("/key"), ("default",), ("default",))
        self.assertTrue(direct.writes)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            content = object_json(
                {"hermes_repo": "/repo", "token_file": "/key", "profiles": ["*"], "boards": ["default"]}
            )
            path.write_text(json.dumps(content))
            self.assertTrue(Config.load(path).writes)
            content["writes"] = False
            path.write_text(json.dumps(content))
            self.assertFalse(Config.load(path).writes)

    def test_config_rejects_unknown_fields_and_nonloopback_host(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            content = object_json(
                {
                    "hermes_repo": "/repo",
                    "token_file": "/key",
                    "host": "0.0.0.0",  # noqa: S104 - verifies non-loopback rejection
                    "profiles": ["default"],
                    "boards": ["default"],
                }
            )
            path.write_text(json.dumps(content))
            with self.assertRaises(BridgeError):
                Config.load(path)
            content["host"] = "127.0.0.1"
            content["typo"] = True
            path.write_text(json.dumps(content))
            with self.assertRaises(BridgeError):
                Config.load(path)

    def test_policy_settings_validate_authentication_and_disabled_tool_names(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            content = object_json(
                {"hermes_repo": "/repo", "token_file": "/key", "profiles": ["*"], "boards": ["default"]}
            )
            path.write_text(json.dumps(content))
            config = Config.load(path)
            self.assertTrue(config.auth_enabled)
            self.assertEqual(config.disabled_tools, ())
            policy = config.with_plugin_settings({"auth_enabled": False, "disabled_tools": ["hermes_run"]})
            self.assertFalse(policy.auth_enabled)
            self.assertEqual(policy.disabled_tools, ("hermes_run",))
            self.assertEqual(policy.token_file, config.token_file)
            self.assertTrue(policy.writes)
            for settings in (
                {"auth_enabled": "false"},
                {"disabled_tools": "hermes_run"},
                {"disabled_tools": ["../tool"]},
            ):
                with self.subTest(settings=settings), self.assertRaises(BridgeError):
                    config.with_plugin_settings(object_json(settings))
            content.update(auth_enabled=False, disabled_tools=["hermes_run"])
            path.write_text(json.dumps(content))
            self.assertEqual(Config.load(path), policy)


if __name__ == "__main__":
    unittest.main()
