# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from hermes_bridge.core import BridgeError, Config, rows
from hermes_bridge.native import Native, decode_cursor, encode_cursor
from tests.support import REPO


class CursorTests(unittest.TestCase):
    def test_cursor_is_durable_and_scope_bound(self) -> None:
        scope = {"subsystem": "kanban", "profile": "default", "board": "default", "task_id": ""}
        cursor = encode_cursor(scope, 42)
        self.assertEqual(decode_cursor(cursor, scope), 42)
        with self.assertRaises(BridgeError):
            decode_cursor(cursor, {**scope, "board": "other"})
        with self.assertRaises(BridgeError):
            decode_cursor("garbage", scope)


class NativeSessionTests(unittest.TestCase):
    state: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        repo = str(REPO)
        sys.path.insert(0, repo)
        cls.state = importlib.import_module("hermes_state")
        # Prepare native dependency imports before substituting the test profile home.
        importlib.import_module("hermes_cli.config")
        importlib.import_module("hermes_cli.profiles")

    def test_native_read_only_sessions_persist_and_exclude_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".hermes"
            home.mkdir()
            home.joinpath("config.yaml").write_text("model: gpt-test\n")
            with (
                patch("pathlib.Path.home", return_value=Path(root)),
                patch.dict(os.environ, {"HERMES_HOME": str(home)}),
            ):
                writer = self.state.SessionDB(db_path=home / "state.db")
                writer.create_session(
                    "session_test", source="cli", model="gpt-test", system_prompt="PRIVATE PROMPT"
                )
                writer.append_message("session_test", "assistant", "PRIVATE MESSAGE")
                writer.close()
                config = Config(REPO, home / "key", ("default",), ("default",))
                native = Native(config)
                before = home.joinpath("state.db").read_bytes()
                for _ in range(2):
                    result = native.sessions("default", 20, 0, active_only=False)
                    self.assertEqual(rows(result["sessions"])[0]["id"], "session_test")
                    detail = native.session("session_test", "default")
                    self.assertEqual(detail["profile"], "default")
                    self.assertNotIn("PRIVATE", str(result) + str(detail))
                self.assertEqual(home.joinpath("state.db").read_bytes(), before)

    def test_session_event_last_page_retains_logical_order_after_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root).resolve()
            home = root_path / ".hermes"
            home.mkdir()
            home.joinpath("config.yaml").write_text("model: test\n")
            writer = self.state.SessionDB(db_path=home / "state.db")
            writer.create_session("event_session", source="cli", model="test")
            writer.append_message("event_session", "assistant", "PRIVATE")
            writer.close()
            with closing(sqlite3.connect(home / "state.db")) as conn, conn:
                conn.execute("UPDATE messages SET id=100, display_order=1 WHERE session_id='event_session'")
            timeline = ModuleType("hermes_state_timeline")
            timeline.__dict__.update(
                {
                    "get_session_timeline": Mock(
                        return_value={
                            "entries": [{"row_id": 100, "timestamp": 1, "preview": "PRIVATE"}],
                            "pagination": {"has_more": False, "next_cursor": None},
                        }
                    ),
                    "_snapshot": lambda db: db._read_ctx(),
                    "_register_functions": Mock(),
                    "_display_rows_sql": Mock(
                        return_value=(
                            "WITH display_rows AS (SELECT id AS row_id, display_order AS sort_id "
                            "FROM messages WHERE session_id = :sid)"
                        )
                    ),
                }
            )
            config = Config(REPO, home / "key", ("default",), ("default",))
            with (
                patch("pathlib.Path.home", return_value=root_path),
                patch.dict(os.environ, {"HERMES_HOME": str(home)}),
                patch.dict(sys.modules, {"hermes_state_timeline": timeline}),
            ):
                native = Native(config)
                result = native.events("session", "default", "default", "", "event_session", "", 10)
                scope = {
                    "subsystem": "session",
                    "profile": "default",
                    "board": "default",
                    "task_id": "",
                    "session_id": "event_session",
                }
                self.assertEqual(decode_cursor(str(result["cursor"]), scope), 1)
                self.assertNotIn("PRIVATE", str(result))


if __name__ == "__main__":
    unittest.main()
