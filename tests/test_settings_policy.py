# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Native managed policy, optimistic concurrency, and validation boundaries."""

from __future__ import annotations

import importlib
import io
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.core import BridgeError, Object, object_json
from hermes_bridge.settings import Settings
from tests.test_settings import fixture


class SettingsPolicyTests(unittest.TestCase):
    def test_managed_model_remains_visible_after_native_sibling_save(self) -> None:
        with fixture("model:\n  default: current\n  provider: auto\n") as (settings, home):
            admin = home.parent / "admin"
            admin.mkdir()
            policy = admin / "config.yaml"
            policy.write_text("model:\n  default: current\n", encoding="utf-8")
            original_policy = policy.read_bytes()
            with patch.dict(os.environ, {"HERMES_MANAGED_DIR": str(admin)}):
                settings.module.managed_scope.invalidate_managed_cache()
                before = settings.get("default")
                with redirect_stderr(io.StringIO()):
                    after = settings.update("default", {"model.provider": "openai"}, str(before["revision"]))
                self.assertEqual(object_json(after["settings"])["model.default"], "current")
                self.assertEqual(object_json(after["settings"])["model.provider"], "openai")
                self.assertNotIn("default", object_json(object_json(after["config_overrides"])["model"]))
                reread = settings.get("default")
                self.assertEqual(reread["revision"], after["revision"])
                self.assertEqual(object_json(reread["settings"])["model.default"], "current")
                self.assertEqual(policy.read_bytes(), original_policy)
            settings.module.managed_scope.invalidate_managed_cache()

    def test_managed_overlay_is_unexpanded_redacted_and_leaves_raw_files_unchanged(self) -> None:
        with fixture("model:\n  default: user-model\n  provider: auto\n") as (settings, home):
            admin = home.parent / "admin"
            admin.mkdir()
            admin.joinpath("config.yaml").write_text(
                "model: ${RUNTIME_MODEL}\napi_key: ADMIN_SECRET\n", encoding="utf-8"
            )
            original = home.joinpath("config.yaml").read_bytes()
            with patch.dict(
                os.environ, {"HERMES_MANAGED_DIR": str(admin), "RUNTIME_MODEL": "RUNTIME_SECRET"}
            ):
                settings.module.managed_scope.invalidate_managed_cache()
                environment = dict(os.environ)
                result = settings.get("default")
                self.assertEqual(object_json(result["settings"])["model.default"], "${RUNTIME_MODEL}")
                self.assertEqual(object_json(result["settings"])["model.provider"], "auto")
                self.assertEqual(
                    object_json(object_json(result["config_overrides"])["model"])["default"], "user-model"
                )
                self.assertEqual(
                    object_json(object_json(result["config"])["api_key"]), {"$hermes_secret": "preserve"}
                )
                self.assertNotIn("ADMIN_SECRET", str(result))
                self.assertNotIn("RUNTIME_SECRET", str(result))
                self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)
                self.assertEqual(dict(os.environ), environment)
            settings.module.managed_scope.invalidate_managed_cache()

    def test_convenience_validation_rejects_boundary_values(self) -> None:
        # Invalid values reaching native persistence would bypass the convenience contract.
        cases: tuple[Object, ...] = (
            {},
            {"model.default": ""},
            {"model.default": "x" * 257},
            {"model.default": "line\nbreak"},
            {"agent.max_turns": 0},
            {"agent.max_turns": 1_000_001},
            {"memory.memory_char_limit": None},
            {"toolsets": ["x"] * 65},
            {"toolsets": [""]},
            {"toolsets": ["x" * 129]},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(BridgeError):
                Settings.validate(changes)

    def test_convenience_update_persists_nullable_numbers_and_root_arrays(self) -> None:
        with fixture() as (settings, _home):
            before = settings.get("default")
            after = settings.update(
                "default", {"agent.max_tokens": None, "toolsets": ["terminal"]}, str(before["revision"])
            )
            values = object_json(after["settings"])
            self.assertIsNone(values["agent.max_tokens"])
            self.assertEqual(values["toolsets"], ["terminal"])

    def test_schema_redacts_default_secrets(self) -> None:
        with fixture() as (settings, _home):
            defaults = {"model": {"default": "test", "api_key": "default-secret"}}
            with patch.object(settings.module, "DEFAULT_CONFIG", defaults):
                schema = settings.schema()
            self.assertNotIn("default-secret", str(schema))
            marker = object_json(object_json(object_json(schema["defaults"])["model"])["api_key"])
            self.assertEqual(marker, {"$hermes_secret": "preserve"})

    def test_invalid_revision_cannot_write(self) -> None:
        with fixture() as (settings, home):
            original = home.joinpath("config.yaml").read_bytes()
            for revision in ("", "f" * 63, "G" * 64):
                with self.subTest(revision=revision), self.assertRaises(BridgeError) as error:
                    settings.apply("default", {"model": "changed"}, None, revision)
                self.assertEqual(error.exception.code, "invalid_input")
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)

    def test_concurrent_read_refuses_mixed_file_snapshot(self) -> None:
        with fixture() as (settings, home):
            load_env = settings.module.load_env

            def change_during_read() -> object:
                home.joinpath("config.yaml").write_text("model: changed\n", encoding="utf-8")
                return load_env()

            with (
                patch.object(settings.module, "load_env", side_effect=change_during_read),
                self.assertRaises(BridgeError) as error,
            ):
                settings.get("default")
            self.assertEqual(error.exception.code, "revision_conflict")

    def test_corrupt_native_yaml_does_not_echo_supplied_secret(self) -> None:
        with fixture("model: [HIDDEN_SECRET\n") as (settings, home):
            original = home.joinpath("config.yaml").read_bytes()
            with (
                redirect_stderr(io.StringIO()),
                self.assertLogs("hermes_cli.config_read_errors", level="WARNING"),
                self.assertRaises(BridgeError) as error,
            ):
                settings.get("default")
            self.assertEqual(error.exception.code, "invalid_settings")
            self.assertNotIn("HIDDEN_SECRET", str(error.exception.payload()))
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)

    def test_native_validator_failure_has_sanitized_message(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            original = home.joinpath("config.yaml").read_bytes()
            for result, error in (("invalid validator result", None), (None, ValueError("HIDDEN_SECRET"))):
                with (
                    self.subTest(result=result),
                    patch.object(
                        settings.module, "validate_config_structure", return_value=result, side_effect=error
                    ),
                    self.assertRaises(BridgeError) as refused,
                ):
                    settings.apply("default", {"model": "changed"}, None, str(before["revision"]))
                self.assertNotIn("HIDDEN_SECRET", str(refused.exception.payload()))
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)

    def test_installation_managed_mode_denies_write(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            original = home.joinpath("config.yaml").read_bytes()
            with patch.dict(os.environ, {"HERMES_MANAGED": "1"}), self.assertRaises(BridgeError) as error:
                settings.apply("default", {"model": "changed"}, None, str(before["revision"]))
            self.assertEqual(error.exception.code, "access_denied")
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)

    def test_native_managed_keys_allow_siblings_and_deny_managed_config_or_env(self) -> None:
        # Real administrator files protect both changed and deleted managed values.
        with fixture("model:\n  default: current\n  provider: auto\n", "POLICY_KEY=kept\n") as (
            settings,
            home,
        ):
            admin = home.parent / "admin"
            admin.mkdir()
            admin.joinpath("config.yaml").write_text("model:\n  default: current\n", encoding="utf-8")
            admin.joinpath(".env").write_text("POLICY_KEY=kept\n", encoding="utf-8")
            managed = importlib.import_module("hermes_cli.managed_scope")
            with patch.dict(os.environ, {"HERMES_MANAGED_DIR": str(admin)}):
                managed.invalidate_managed_cache()
                before = settings.get("default")
                with redirect_stderr(io.StringIO()):
                    after = settings.update("default", {"model.provider": "openai"}, str(before["revision"]))
                self.assertEqual(object_json(after["settings"])["model.provider"], "openai")
                for desired, env in (
                    ({"model": {"default": "changed"}}, None),
                    ({"model": {"default": "current"}}, {"POLICY_KEY": "changed"}),
                    ({"model": {"default": "current"}}, {}),
                ):
                    with self.subTest(config=desired, env=env), self.assertRaises(BridgeError) as error:
                        settings.apply(
                            "default",
                            object_json(desired),
                            object_json(env) if env is not None else None,
                            str(after["revision"]),
                        )
                    self.assertEqual(error.exception.code, "access_denied")
                self.assertEqual(home.joinpath(".env").read_bytes(), b"POLICY_KEY=kept\n")
            managed.invalidate_managed_cache()

    def test_read_io_failure_is_sanitized(self) -> None:
        with fixture() as (settings, home):
            read_bytes = Path.read_bytes

            def unreadable(path: Path) -> bytes:
                if path == home / ".env":
                    msg = "private filesystem detail"
                    raise OSError(msg)
                return read_bytes(path)

            with patch("pathlib.Path.read_bytes", unreadable), self.assertRaises(BridgeError) as error:
                settings.get("default")
            self.assertEqual(error.exception.code, "invalid_settings")
            self.assertNotIn("private filesystem detail", str(error.exception.payload()))
