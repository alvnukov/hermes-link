# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.auth import MAX_ENV_BYTES, read_auth_token
from hermes_bridge.core import BridgeError, Config, bounded, json_value, read_token, rows, validate_token
from tests.test_settings import fixture


class InputTests(unittest.TestCase):
    def test_json_rejects_nonfinite_values_and_nonstring_keys(self) -> None:
        for value in (float("nan"), float("inf"), {1: "value"}, (1, 2), b"bytes"):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                json_value(value)
        self.assertEqual(json_value([1.25, {"text": "ok"}]), [1.25, {"text": "ok"}])
        invalid_row_values: tuple[object, ...] = ({}, ["string"], [1])
        for invalid_rows in invalid_row_values:
            with self.subTest(value=invalid_rows), self.assertRaises(BridgeError):
                rows(json_value(invalid_rows))

    def test_bounded_rejects_wrong_types_and_outside_values(self) -> None:
        for value in (None, True, False, "3", 1.5, 0, 9):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                bounded(value, 1, 8)
        self.assertEqual(bounded(1, 1, 8), 1)
        self.assertEqual(bounded(8, 1, 8), 8)

    def test_configuration_rejects_missing_malformed_and_invalid_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for text in ("{", ""):
                path.write_text(text)
                with self.assertRaises(BridgeError) as error:
                    Config.load(path)
                self.assertEqual(error.exception.code, "invalid_config")
            valid: dict[str, object] = {
                "hermes_repo": "/repo",
                "token_file": "/key",
                "profiles": ["*"],
                "boards": ["default"],
            }
            for key, value in (
                ("hermes_repo", "relative"),
                ("token_file", 1),
                ("profiles", []),
                ("boards", ["*"]),
                ("port", True),
                ("port", 1023),
                ("port", "18788"),
                ("max_concurrent_runs", 9),
                ("writes", "true"),
                ("board_profile", "../profile"),
                ("disabled_tools", ["x"] * 65),
            ):
                path.write_text(json.dumps({**valid, key: value}))
                with self.subTest(key=key, value=value), self.assertRaises(BridgeError):
                    Config.load(path)
            path.unlink()
            with self.assertRaises(BridgeError):
                Config.load(path)

    def test_token_format_permissions_symlinks_and_encoding_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            for value in ("short", "a" * 257, "a" * 31 + "+"):
                with self.subTest(length=len(value)), self.assertRaises(BridgeError):
                    validate_token(value)
            key.write_bytes(b"\xff")
            key.chmod(0o600)
            with self.assertRaises(BridgeError):
                read_token(key)
            key.write_text("a" * 48)
            link = key.with_name("link")
            link.symlink_to(key)
            with self.assertRaises(BridgeError):
                read_token(link)
            with (
                patch("hermes_bridge.core.os.getuid", return_value=os.getuid() + 1),
                self.assertRaises(BridgeError),
            ):
                read_token(key)
            self.assertEqual(read_token(key), "a" * 48)


class AuthReadTests(unittest.TestCase):
    def test_absent_native_env_or_variable_falls_back_to_private_key(self) -> None:
        with fixture() as (settings, home):
            key = settings.native.config.token_file
            key.write_text("f" * 48)
            key.chmod(0o600)
            env = home / ".env"
            env.chmod(0o600)
            config = replace(settings.native.config, token_env_file=env)
            self.assertEqual(read_auth_token(config), "f" * 48)
            env.unlink()
            self.assertEqual(read_auth_token(config), "f" * 48)

    def test_oversized_native_secret_file_is_refused(self) -> None:
        with fixture() as (settings, home):
            env = home / ".env"
            env.write_bytes(b"x" * (MAX_ENV_BYTES + 1))
            env.chmod(0o600)
            with self.assertRaises(BridgeError) as error:
                read_auth_token(replace(settings.native.config, token_env_file=env))
            self.assertEqual(error.exception.code, "invalid_token")

    def test_native_parser_failure_is_sanitized(self) -> None:
        with fixture() as (settings, home):
            env = home / ".env"
            env.chmod(0o600)
            with (
                patch("agent.secret_scope._parse_env_text", side_effect=RuntimeError("SECRET_VALUE")),
                self.assertRaises(BridgeError) as error,
            ):
                read_auth_token(replace(settings.native.config, token_env_file=env))
            self.assertEqual(error.exception.code, "invalid_token")
            self.assertNotIn("SECRET_VALUE", json.dumps(error.exception.payload()))

    def test_native_parser_nonstring_token_is_refused(self) -> None:
        with fixture() as (settings, home):
            env = home / ".env"
            env.chmod(0o600)
            with (
                patch("agent.secret_scope._parse_env_text", return_value={"HERMES_HTTP_MCP_TOKEN": 123}),
                self.assertRaises(BridgeError) as error,
            ):
                read_auth_token(replace(settings.native.config, token_env_file=env))
            self.assertEqual(error.exception.code, "invalid_token")
