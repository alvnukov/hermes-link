# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import importlib
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from hermes_bridge.core import BridgeError, Config, object_json
from hermes_bridge.native import Native
from hermes_bridge.settings import Settings
from tests.support import REPO

if TYPE_CHECKING:
    from collections.abc import Iterator

sys.path.insert(0, str(REPO))
importlib.import_module("hermes_cli.config")  # Import before changing home.


@contextmanager
def fixture(config_text: str = "model: model-a\n", env_text: str = "") -> Iterator[tuple[Settings, Path]]:
    with tempfile.TemporaryDirectory() as root:
        root_path = Path(root).resolve()
        home = root_path / ".hermes"
        home.mkdir()
        home.joinpath("config.yaml").write_text(config_text)
        home.joinpath(".env").write_text(env_text)
        config = Config(REPO, home / "key", ("*",), ("default",), writes=True)
        with (
            patch("pathlib.Path.home", return_value=root_path),
            patch.dict(os.environ, {"HERMES_HOME": str(home)}),
        ):
            yield Settings(Native(config)), home


class SettingsTests(unittest.TestCase):
    def test_update_preserves_secrets_and_checks_revision(self) -> None:
        with fixture(
            "model:\n  default: test\n  api_key: SECRET\nagent:\n  max_turns: 17\nunrelated: retained\n"
        ) as (settings, home):
            before = settings.get("default")
            self.assertNotIn("SECRET", str(before))
            self.assertIn("api_key", object_json(object_json(before["config"])["model"]))
            after = settings.update("default", {"model.default": "new"}, str(before["revision"]))
            self.assertEqual(object_json(after["settings"])["model.default"], "new")
            self.assertIn("SECRET", home.joinpath("config.yaml").read_text())
            self.assertIn("retained", home.joinpath("config.yaml").read_text())
            self.assertEqual(object_json(after["settings"])["agent.max_turns"], 17)
            with self.assertRaises(BridgeError) as stale:
                settings.update("default", {"agent.max_turns": 9}, str(before["revision"]))
            self.assertEqual(stale.exception.code, "revision_conflict")

    def test_full_read_is_unexpanded_redacted_and_profile_isolated(self) -> None:
        with (
            fixture(
                "model:\n  default: ${RUNTIME_MODEL}\n  api_key: CONFIG_SECRET\n"
                "mcp_servers:\n  demo:\n    headers:\n      X-API-Key: HEADER_SECRET\n"
                "    env:\n      CUSTOM_TOKEN: MCP_SECRET\nterminal:\n  cwd: ${HOME}\n",
                "OPENAI_API_KEY=ENV_SECRET\nHERMES_LOG_LEVEL=debug\nOPAQUE_REF=${RUNTIME_MODEL}\n",
            ) as (settings, home),
            patch.dict(os.environ, {"RUNTIME_MODEL": "RUNTIME_SECRET"}),
        ):
            original_env = dict(os.environ)
            first = settings.get("default")
            for secret in ("CONFIG_SECRET", "HEADER_SECRET", "MCP_SECRET", "ENV_SECRET", "RUNTIME_SECRET"):
                self.assertNotIn(secret, str(first))
            self.assertEqual(object_json(first["settings"])["model.default"], "${RUNTIME_MODEL}")
            self.assertEqual(object_json(first["env"])["OPAQUE_REF"], "${RUNTIME_MODEL}")
            self.assertEqual(object_json(first["env"])["HERMES_LOG_LEVEL"], "debug")
            self.assertIn("agent", object_json(first["config"]))
            other = home / "profiles" / "b"
            other.mkdir(parents=True)
            other.joinpath("config.yaml").write_text("model: model-b\n")
            other.joinpath(".env").write_text("HERMES_LOG_LEVEL=info\n")
            self.assertEqual(object_json(settings.get("b")["settings"])["model.default"], "model-b")
            self.assertEqual(settings.get("default"), first)
            self.assertEqual(dict(os.environ), original_env)

    def test_scalar_model_provider_update_keeps_model(self) -> None:
        with fixture() as (settings, _home):
            before = settings.get("default")
            self.assertEqual(object_json(before["settings"])["model.default"], "model-a")
            after = settings.update("default", {"model.provider": "auto"}, str(before["revision"]))
            self.assertEqual(object_json(after["settings"])["model.default"], "model-a")

    def test_env_change_invalidates_revision(self) -> None:
        with fixture(env_text="OPENAI_API_KEY=first\n") as (settings, home):
            before = settings.get("default")
            home.joinpath(".env").write_text("OPENAI_API_KEY=second\n")
            with self.assertRaises(BridgeError) as stale:
                settings.update("default", {"model.default": "new"}, str(before["revision"]))
            self.assertEqual(stale.exception.code, "revision_conflict")
            self.assertEqual(home.joinpath("config.yaml").read_text(), "model: model-a\n")

    def test_apply_replaces_omissions_preserves_markers_and_quotes_env(self) -> None:
        with fixture(
            "model:\n  default: old\n  api_key: CONFIG_SECRET\nremove_me: true\n",
            "OPENAI_API_KEY=ENV_SECRET\nREMOVE_ME=old\n",
        ) as (settings, home):
            before = settings.get("default")
            model = object_json(object_json(before["config"])["model"])
            after = settings.apply(
                "default",
                {
                    "model": {"default": "new", "api_key": model["api_key"]},
                    "custom_extension": {"anything": [1, True, "value"]},
                },
                {
                    "OPENAI_API_KEY": object_json(before["env"])["OPENAI_API_KEY"],
                    "DYNAMIC_SETTING": 'spaces # quotes " and \\',
                },
                str(before["revision"]),
            )
            self.assertNotIn("SECRET", str(after))
            self.assertIn("CONFIG_SECRET", home.joinpath("config.yaml").read_text())
            self.assertIn("ENV_SECRET", home.joinpath(".env").read_text())
            self.assertNotIn("remove_me", home.joinpath("config.yaml").read_text())
            self.assertNotIn("REMOVE_ME", home.joinpath(".env").read_text())
            self.assertEqual(object_json(after["env"])["DYNAMIC_SETTING"], 'spaces # quotes " and \\')
            self.assertIn("custom_extension", object_json(after["config"]))
            self.assertIsInstance(after["version_id"], str)

    def test_full_get_tree_round_trips_without_secret_marker_literals(self) -> None:
        with fixture(
            "model:\n  default: test\n  api_key: CONFIG_SECRET\n", "OPENAI_API_KEY=ENV_SECRET\n"
        ) as (settings, home):
            before = settings.get("default")
            after = settings.apply(
                "default", object_json(before["config"]), object_json(before["env"]), str(before["revision"])
            )
            self.assertEqual(object_json(after["settings"])["model.default"], "test")
            self.assertNotIn("$hermes_secret", home.joinpath("config.yaml").read_text())
            self.assertIn("CONFIG_SECRET", home.joinpath("config.yaml").read_text())
            self.assertNotIn("CONFIG_SECRET", str(after))
            self.assertNotIn("ENV_SECRET", str(after))

    def test_missing_secret_marker_is_rejected(self) -> None:
        with fixture("model:\n  default: test\n  api_key: secret\n") as (settings, home):
            before = settings.get("default")
            marker = object_json(object_json(before["config"])["model"])["api_key"]
            with self.assertRaises(BridgeError) as error:
                settings.apply("default", {"other": {"api_key": marker}}, None, str(before["revision"]))
            self.assertEqual(error.exception.code, "invalid_setting")
            self.assertEqual(
                home.joinpath("config.yaml").read_text(), "model:\n  default: test\n  api_key: secret\n"
            )

    def test_after_version_storage_failure_happens_before_live_changes(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            original = home.joinpath("config.yaml").read_bytes()
            utils = importlib.import_module("utils")
            write_json = utils.atomic_json_write
            calls = 0

            def failing_second(path: Path, data: object, **kwargs: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    msg = "after metadata cannot be saved"
                    raise OSError(msg)
                write_json(path, data, **kwargs)

            with (
                patch("utils.atomic_json_write", side_effect=failing_second),
                self.assertRaises(BridgeError) as error,
            ):
                settings.apply("default", {"model": "updated"}, None, str(before["revision"]))
            self.assertEqual(error.exception.code, "history_unavailable")
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)

    def test_history_tampering_and_history_symlink_refuse_restore(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            after = settings.apply("default", {"model": "updated"}, None, str(before["revision"]))
            version = str(after["before_version_id"])
            backup = next(home.joinpath("backups/config").glob(f"config.yaml.bridge-{version}.*"))
            backup.write_bytes(b"model: tampered\n")
            with self.assertRaises(BridgeError) as error:
                settings.restore("default", version, str(after["revision"]))
            self.assertEqual(error.exception.code, "history_unavailable")
            self.assertEqual(object_json(settings.get("default")["settings"])["model.default"], "updated")
            backup.unlink()
            backup.symlink_to(home / "config.yaml")
            with self.assertRaises(BridgeError) as unsafe:
                settings.restore("default", version, str(after["revision"]))
            self.assertEqual(unsafe.exception.code, "unsafe_settings_path")

    def test_failed_live_env_write_restores_both_files(self) -> None:
        with fixture(env_text="OPENAI_API_KEY=original_secret\n") as (settings, home):
            before = settings.get("default")
            config_bytes = home.joinpath("config.yaml").read_bytes()
            env_bytes = home.joinpath(".env").read_bytes()
            utils = importlib.import_module("utils")
            write_bytes = utils.atomic_write_bytes

            def failing_live_env(path: Path, data: bytes, **kwargs: object) -> None:
                if path == home / ".env":
                    msg = "environment write failure"
                    raise OSError(msg)
                write_bytes(path, data, **kwargs)

            with (
                patch("utils.atomic_write_bytes", side_effect=failing_live_env),
                self.assertRaises(BridgeError) as error,
            ):
                settings.apply(
                    "default", {"model": "new"}, {"OPENAI_API_KEY": "new_secret"}, str(before["revision"])
                )
            self.assertEqual(error.exception.code, "settings_write_failed")
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), config_bytes)
            self.assertEqual(home.joinpath(".env").read_bytes(), env_bytes)

    def test_copied_history_is_bound_to_original_profile(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            after = settings.apply("default", {"model": "new"}, None, str(before["revision"]))
            other = home / "profiles" / "b"
            other.mkdir(parents=True)
            other.joinpath("config.yaml").write_text("model: model-b\n")
            shutil.copytree(home / "backups", other / "backups")
            second = settings.get("b")
            with self.assertRaises(BridgeError) as error:
                settings.restore("b", str(after["version_id"]), str(second["revision"]))
            self.assertEqual(error.exception.code, "version_not_found")
            self.assertEqual(other.joinpath("config.yaml").read_text(), "model: model-b\n")

    def test_public_history_directory_refuses_restore(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            after = settings.apply("default", {"model": "new"}, None, str(before["revision"]))
            home.joinpath("backups/config/bridge-versions").chmod(0o755)
            with self.assertRaises(BridgeError) as error:
                settings.restore("default", str(after["before_version_id"]), str(after["revision"]))
            self.assertEqual(error.exception.code, "history_unavailable")
            self.assertEqual(object_json(settings.get("default")["settings"])["model.default"], "new")

    def test_invalid_section_type_raises_sanitized_validation_error(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            with self.assertRaises(BridgeError) as error:
                settings.apply("default", {"agent": "HIDDEN_SECRET"}, None, str(before["revision"]))
            self.assertEqual(error.exception.code, "invalid_setting")
            self.assertNotIn("HIDDEN_SECRET", str(error.exception.payload()))
            self.assertEqual(home.joinpath("config.yaml").read_text(), "model: model-a\n")

    def test_none_env_keeps_exact_bytes(self) -> None:
        with fixture(env_text="# comment\nexport OPENAI_API_KEY='secret' # retained\n") as (settings, home):
            before = settings.get("default")
            original = home.joinpath(".env").read_bytes()
            settings.apply("default", {"model": "updated"}, None, str(before["revision"]))
            self.assertEqual(home.joinpath(".env").read_bytes(), original)

    def test_versions_restore_survive_restart_and_are_private(self) -> None:
        with fixture(env_text="OPENAI_API_KEY=first_secret\n") as (settings, home):
            initial = settings.get("default")
            first = settings.apply(
                "default", {"model": "first"}, {"OPENAI_API_KEY": "second_secret"}, str(initial["revision"])
            )
            first_config = home.joinpath("config.yaml").read_bytes()
            first_env = home.joinpath(".env").read_bytes()
            second = settings.apply(
                "default", {"model": "second"}, {"OPENAI_API_KEY": "third_secret"}, str(first["revision"])
            )
            reloaded = Settings(settings.native)
            versions = reloaded.versions("default", limit=100)
            self.assertNotIn("secret", str(versions))
            self.assertGreaterEqual(int(str(versions["count"])), 4)
            restored = reloaded.restore("default", str(first["version_id"]), str(second["revision"]))
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), first_config)
            self.assertEqual(home.joinpath(".env").read_bytes(), first_env)
            self.assertEqual(object_json(restored["settings"])["model.default"], "first")
            self.assertIn("saved_current_version_id", restored)
            self.assertNotIn("second_secret", str(restored))
            for path in home.joinpath("backups").rglob("*"):
                self.assertFalse(path.is_symlink())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)

    def test_restore_initial_snapshot_with_missing_env(self) -> None:
        with fixture() as (settings, home):
            home.joinpath(".env").unlink()
            original = home.joinpath("config.yaml").read_bytes()
            before = settings.get("default")
            after = settings.apply(
                "default", {"model": "new"}, {"OPENAI_API_KEY": "new_secret"}, str(before["revision"])
            )
            settings.restore("default", str(after["before_version_id"]), str(after["revision"]))
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)
            self.assertFalse(home.joinpath(".env").exists())

    def test_snapshot_and_metadata_failures_refuse_write(self) -> None:
        for target, side_effect in (
            ("hermes_cli.config_backups.backup_config", None),
            ("utils.atomic_json_write", OSError("storage failure")),
        ):
            with self.subTest(target=target), fixture(env_text="OPENAI_API_KEY=secret\n") as (settings, home):
                before = settings.get("default")
                config_bytes = home.joinpath("config.yaml").read_bytes()
                env_bytes = home.joinpath(".env").read_bytes()
                with (
                    patch(target, side_effect=side_effect, return_value=None),
                    self.assertRaises(BridgeError) as failed,
                ):
                    settings.apply("default", {"model": "new"}, {}, str(before["revision"]))
                self.assertEqual(failed.exception.code, "history_unavailable")
                self.assertEqual(home.joinpath("config.yaml").read_bytes(), config_bytes)
                self.assertEqual(home.joinpath(".env").read_bytes(), env_bytes)

    def test_invalid_structure_and_env_cannot_write_or_echo_values(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            original = home.joinpath("config.yaml").read_bytes()
            for config, env in (
                ({"custom_providers": "HIDDEN_SECRET"}, None),
                ({"model": "new"}, {"BAD=KEY": "HIDDEN_SECRET"}),
                ({"model": "new"}, {"GOOD_KEY": "line\nHIDDEN_SECRET"}),
            ):
                with self.subTest(config=config, env=env), self.assertRaises(BridgeError) as invalid:
                    settings.apply(
                        "default",
                        object_json(config),
                        object_json(env) if env else None,
                        str(before["revision"]),
                    )
                self.assertNotIn("HIDDEN_SECRET", str(invalid.exception.payload()))
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)

    def test_symlinks_and_foreign_profile_versions_are_rejected(self) -> None:
        with fixture() as (settings, home):
            initial = settings.get("default")
            after = settings.apply("default", {"model": "new"}, None, str(initial["revision"]))
            other = home / "profiles" / "b"
            other.mkdir(parents=True)
            other.joinpath("config.yaml").write_text("model: model-b\n")
            before_b = settings.get("b")
            with self.assertRaises(BridgeError):
                settings.restore("b", str(after["version_id"]), str(before_b["revision"]))
            config_path = home / "config.yaml"
            config_path.unlink()
            config_path.symlink_to(other / "config.yaml")
            with self.assertRaises(BridgeError) as unsafe:
                settings.get("default")
            self.assertEqual(unsafe.exception.code, "unsafe_settings_path")
            self.assertEqual(other.joinpath("config.yaml").read_text(), "model: model-b\n")

    def test_validation_rejects_unknown_and_wrong_types(self) -> None:
        for change in (
            {"model.api_key": "x"},
            {"agent.max_turns": True},
            {"memory.enabled": "true"},
            {"toolsets": [3]},
        ):
            with self.subTest(change=change), self.assertRaises(BridgeError):
                Settings.validate(object_json(change))


if __name__ == "__main__":
    unittest.main()
