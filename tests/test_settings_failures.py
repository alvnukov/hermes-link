# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Fault injection against real private files and native persistence."""

from __future__ import annotations

import importlib
import json
import unittest
from typing import TYPE_CHECKING
from unittest.mock import patch

from hermes_bridge.core import BridgeError
from hermes_bridge.settings_history import SettingsHistory
from tests.test_settings import fixture

if TYPE_CHECKING:
    from pathlib import Path


class SettingsFailureTests(unittest.TestCase):
    def test_native_staging_write_failure_preserves_live_files_and_recovery_version(self) -> None:
        with fixture(env_text="KEY=original\n") as (settings, home):
            before = settings.get("default")
            original = home.joinpath("config.yaml").read_bytes()
            with (
                patch.object(settings.module, "save_config", side_effect=OSError("private write detail")),
                self.assertRaises(BridgeError) as error,
            ):
                settings.apply("default", {"model": "changed"}, {"KEY": "changed"}, str(before["revision"]))
            self.assertEqual(error.exception.code, "history_unavailable")
            self.assertNotIn("private write detail", str(error.exception.payload()))
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)
            self.assertEqual(home.joinpath(".env").read_bytes(), b"KEY=original\n")
            self.assertEqual(settings.versions("default")["count"], 1)

    def test_link_inserted_between_live_file_writes_requires_recovery(self) -> None:
        # Refusing an unsafe second path must still recover the already-published first file.
        with fixture(env_text="KEY=original\n") as (settings, home):
            before = settings.get("default")
            original = home.joinpath("config.yaml").read_bytes()
            external = home.parent / "external-env"
            external.write_bytes(b"KEY=external\n")
            utils = importlib.import_module("utils")
            write_bytes = utils.atomic_write_bytes

            def replace_env_after_config(path: Path, data: bytes, **kwargs: object) -> None:
                write_bytes(path, data, **kwargs)
                if path == home / "config.yaml" and data != original:
                    home.joinpath(".env").unlink()
                    home.joinpath(".env").symlink_to(external)

            with (
                patch("utils.atomic_write_bytes", side_effect=replace_env_after_config),
                self.assertRaises(BridgeError) as error,
            ):
                settings.apply("default", {"model": "changed"}, {"KEY": "changed"}, str(before["revision"]))
            self.assertEqual(error.exception.code, "settings_recovery_required")
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)
            self.assertEqual(external.read_bytes(), b"KEY=external\n")
            version = str(error.exception.details["saved_current_version_id"])
            state, _metadata = SettingsHistory("default", home).load(version)
            self.assertEqual(state.env, b"KEY=original\n")

    def test_silent_rollback_failure_requires_recovery(self) -> None:
        # A writer that returns without restoring bytes must never report restoration.
        with fixture(env_text="KEY=original\n") as (settings, home):
            before = settings.get("default")
            original = home.joinpath("config.yaml").read_bytes()
            utils = importlib.import_module("utils")
            write_bytes = utils.atomic_write_bytes

            def incomplete_write(path: Path, data: bytes, **kwargs: object) -> None:
                if path == home / ".env":
                    msg = "injected write failure"
                    raise OSError(msg)
                if path == home / "config.yaml" and data == original:
                    return
                write_bytes(path, data, **kwargs)

            with (
                patch("utils.atomic_write_bytes", side_effect=incomplete_write),
                self.assertRaises(BridgeError) as error,
            ):
                settings.apply("default", {"model": "changed"}, {"KEY": "changed"}, str(before["revision"]))
            self.assertEqual(error.exception.code, "settings_recovery_required")
            version = str(error.exception.details["saved_current_version_id"])
            state, _metadata = SettingsHistory("default", home).load(version)
            self.assertEqual(state.config, original)
            self.assertEqual(state.env, b"KEY=original\n")

    def test_non_object_history_metadata_is_reported_as_corrupt_history(self) -> None:
        # Valid JSON with an invalid shape must stay in the history error contract.
        with fixture() as (settings, home):
            before = settings.get("default")
            changed = settings.apply("default", {"model": "changed"}, None, str(before["revision"]))
            version = str(changed["before_version_id"])
            metadata = home / "backups" / "config" / "bridge-versions" / f"{version}.json"
            metadata.write_text(json.dumps(["private history payload"]), encoding="utf-8")
            with self.assertRaises(BridgeError) as error:
                settings.restore("default", version, str(changed["revision"]))
            self.assertEqual(error.exception.code, "history_unavailable")
            self.assertNotIn("private history payload", str(error.exception.payload()))
            self.assertIn(b"changed", home.joinpath("config.yaml").read_bytes())

    def test_non_finite_metadata_value_is_reported_as_corrupt_history(self) -> None:
        with fixture() as (settings, home):
            before = settings.get("default")
            changed = settings.apply("default", {"model": "changed"}, None, str(before["revision"]))
            version = str(changed["before_version_id"])
            metadata = home / "backups" / "config" / "bridge-versions" / f"{version}.json"
            metadata.write_text('{"revision": NaN}', encoding="utf-8")
            with self.assertRaises(BridgeError) as error:
                settings.restore("default", version, str(changed["revision"]))
            self.assertEqual(error.exception.code, "history_unavailable")
