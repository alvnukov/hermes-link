# Copyright (c) 2026 Hermes HTTP MCP contributors
"""History corruption and private backup invariants with real disk fixtures."""

from __future__ import annotations

import json
import os
import unittest
from typing import TYPE_CHECKING
from unittest.mock import patch

from hermes_bridge.core import BridgeError, object_json, rows
from hermes_bridge.settings_history import SettingsHistory, State, read_bytes, read_state, safe_path
from tests.test_settings import fixture

if TYPE_CHECKING:
    from pathlib import Path


class SettingsHistoryTests(unittest.TestCase):
    def test_restore_missing_initial_config_removes_new_live_file(self) -> None:
        with fixture() as (settings, home):
            home.joinpath("config.yaml").unlink()
            home.joinpath(".env").unlink()
            before = settings.get("default")
            changed = settings.apply("default", {"model": "changed"}, None, str(before["revision"]))
            settings.restore("default", str(changed["before_version_id"]), str(changed["revision"]))
            self.assertFalse(home.joinpath("config.yaml").exists())
            self.assertFalse(home.joinpath(".env").exists())

    def test_missing_history_returns_empty_and_limit_reports_remaining(self) -> None:
        with fixture() as (settings, home):
            self.assertEqual(settings.versions("default")["count"], 0)
            before = settings.get("default")
            settings.apply("default", {"model": "changed"}, None, str(before["revision"]))
            home.joinpath("backups", "config", "bridge-versions", "unrelated.txt").write_text(
                "ignore", encoding="utf-8"
            )
            result = settings.versions("default", limit=1)
            self.assertEqual(result["count"], 2)
            self.assertEqual(len(rows(result["versions"])), 1)
            self.assertTrue(result["has_more"])

    def test_missing_files_and_empty_files_have_distinct_restorable_versions(self) -> None:
        with fixture() as (_settings, home):
            home.joinpath("config.yaml").unlink()
            home.joinpath(".env").unlink()
            history = SettingsHistory("default", home)
            missing = history.capture(home, State(None, None), "missing")
            home.joinpath("config.yaml").write_bytes(b"")
            home.joinpath(".env").write_bytes(b"")
            empty = history.capture(home, State(b"", b""), "empty")
            missing_state, _missing_metadata = history.load(missing)
            empty_state, _empty_metadata = history.load(empty)
            self.assertIsNone(missing_state.config)
            self.assertIsNone(missing_state.env)
            self.assertEqual(empty_state.config, b"")
            self.assertEqual(empty_state.env, b"")
            self.assertNotEqual(missing_state.revision, empty_state.revision)

    def test_capture_refuses_a_changed_live_file(self) -> None:
        with fixture() as (_settings, home):
            before = read_state(home)
            home.joinpath("config.yaml").write_text("model: changed\n", encoding="utf-8")
            with self.assertRaises(BridgeError) as error:
                SettingsHistory("default", home).capture(home, before, "before_apply")
            self.assertEqual(error.exception.code, "revision_conflict")
            self.assertFalse(list(home.joinpath("backups", "config", "bridge-versions").glob("*.json")))

    def test_backup_corruption_during_capture_refuses_metadata_publication(self) -> None:
        with fixture() as (_settings, home):
            history = SettingsHistory("default", home)
            backup_config = history.module.backup_config

            def corrupt_copy(source: Path, reason: str, *, keep: int) -> Path:
                backup: Path = backup_config(source, reason, keep=keep)
                backup.write_bytes(b"corrupt backup")
                return backup

            with (
                patch.object(history.module, "backup_config", side_effect=corrupt_copy),
                self.assertRaises(BridgeError) as error,
            ):
                history.capture(home, read_state(home), "before_apply")
            self.assertEqual(error.exception.code, "history_unavailable")
            self.assertFalse(list(history.index.glob("*.json")))

    def test_corrupt_metadata_invalid_shape_or_json_refuses_restore(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            changed = settings.apply("default", {"model": "changed"}, None, str(before["revision"]))
            version = str(changed["before_version_id"])
            metadata = home / "backups" / "config" / "bridge-versions" / f"{version}.json"
            initial = object_json(json.loads(metadata.read_text(encoding="utf-8")))
            for key, value in (
                ("config_file", "../../config.yaml"),
                ("config_exists", "yes"),
                ("revision", "tampered"),
            ):
                modified = dict(initial)
                modified[key] = value
                metadata.write_text(json.dumps(modified), encoding="utf-8")
                with self.subTest(key=key), self.assertRaises(BridgeError) as error:
                    settings.restore("default", version, str(changed["revision"]))
                self.assertEqual(error.exception.code, "history_unavailable")
            metadata.write_text("{private malformed history", encoding="utf-8")
            with self.assertRaises(BridgeError) as error:
                settings.versions("default")
            self.assertEqual(error.exception.code, "history_unavailable")
            self.assertNotIn("private malformed history", str(error.exception.payload()))
            self.assertIn(b"changed", home.joinpath("config.yaml").read_bytes())

    def test_public_metadata_or_backup_permissions_refuse_restore(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            changed = settings.apply("default", {"model": "changed"}, None, str(before["revision"]))
            version = str(changed["before_version_id"])
            metadata = home / "backups" / "config" / "bridge-versions" / f"{version}.json"
            backup = next(home.joinpath("backups", "config").glob(f"config.yaml.bridge-{version}.*"))
            for path in (metadata, backup):
                path.chmod(0o644)
                with self.subTest(path=path), self.assertRaises(BridgeError) as error:
                    settings.restore("default", version, str(changed["revision"]))
                self.assertEqual(error.exception.code, "history_unavailable")
                path.chmod(0o600)

    def test_missing_indexed_backup_refuses_restore(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            changed = settings.apply("default", {"model": "changed"}, None, str(before["revision"]))
            version = str(changed["before_version_id"])
            backup = next(home.joinpath("backups", "config").glob(f"config.yaml.bridge-{version}.*"))
            backup.unlink()
            with self.assertRaises(BridgeError) as error:
                settings.restore("default", version, str(changed["revision"]))
            self.assertEqual(error.exception.code, "history_unavailable")

    def test_invalid_or_missing_version_ids_are_distinguished(self) -> None:
        with fixture() as (_settings, home):
            history = SettingsHistory("default", home)
            for version, code in (("bad-id", "invalid_input"), ("f" * 32, "version_not_found")):
                with self.subTest(version=version), self.assertRaises(BridgeError) as error:
                    history.load(version)
                self.assertEqual(error.exception.code, code)

    def test_directory_and_fifo_cannot_be_read_as_settings(self) -> None:
        with fixture() as (_settings, home):
            with self.assertRaises(BridgeError) as directory:
                read_bytes(home)
            self.assertEqual(directory.exception.code, "unsafe_settings_path")
            fifo = home / "fifo"
            os.mkfifo(fifo, mode=0o600)
            with self.assertRaises(BridgeError) as special:
                safe_path(fifo)
            self.assertEqual(special.exception.code, "unsafe_settings_path")
