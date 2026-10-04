# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.actions import Actions
from hermes_bridge.core import BridgeError, Config, object_json
from hermes_bridge.native import Native
from tests.support import REPO


class ActionsTests(unittest.TestCase):
    def test_native_kanban_replay_changed_input_conflict_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root).resolve()
            home = root_path / ".hermes"
            home.mkdir()
            home.joinpath("config.yaml").write_text("model: test\n")
            config = Config(REPO, home / "key", ("default",), ("default",), writes=True)
            with (
                patch("pathlib.Path.home", return_value=root_path),
                patch.dict(os.environ, {"HERMES_HOME": str(home)}),
            ):
                native = Native(config)
                actions = Actions(native)
                first = actions.kanban_create(
                    "default", "key1", {"title": "test", "body": "hello", "triage": True}
                )
                task = object_json(first["task"])
                self.assertEqual(task["status"], "triage")
                self.assertEqual(
                    actions.kanban_create(
                        "default", "key1", {"title": "test", "body": "hello", "triage": True}
                    )["task"],
                    task,
                )
                with self.assertRaises(BridgeError) as conflict:
                    actions.kanban_create("default", "key1", {"title": "different"})
                self.assertEqual(conflict.exception.code, "idempotency_conflict")
                changed = actions.kanban_update("default", str(task["id"]), {"body": "changed"})
                self.assertEqual(object_json(changed["task"])["body"], "changed")
                self.assertEqual(
                    object_json(native.kanban_task(str(task["id"]), "default")["task"])["body"], "changed"
                )

    def test_disabled_writes_fail_before_native_mutation(self) -> None:
        config = Config(REPO, Path("/key"), ("default",), ("default",), writes=False)
        with self.assertRaises(BridgeError) as denied:
            Actions(Native(config)).kanban_create("default", "key1", {"title": "x"})
        self.assertEqual(denied.exception.code, "access_denied")
