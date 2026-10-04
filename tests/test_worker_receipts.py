# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Redacted public views must not obscure durable native mutation failures."""

from __future__ import annotations

import importlib
import unittest
from unittest.mock import patch

from hermes_bridge.actions import Actions
from hermes_bridge.core import BridgeError, object_json
from tests.test_settings import fixture


class WorkerReceiptTests(unittest.TestCase):
    def test_callback_failure_detects_edit_when_redacted_public_views_match(self) -> None:
        first = "opaque-receipt-secret-first-0192"
        second = "opaque-receipt-secret-second-3728"
        with fixture(env_text=f"FIRST_TOKEN={first}\nSECOND_TOKEN={second}\n") as (settings, _home):
            actions = Actions(settings.native)
            created = actions.kanban_create("default", "redacted-receipt", {"title": first, "triage": True})
            task_id = str(object_json(created["task"])["id"])
            revision = str(object_json(created["task"])["revision"])
            native = importlib.import_module("hermes_cli.kanban_db")
            with (
                patch.object(native, "notify_task_updated", side_effect=RuntimeError("fixture observer")),
                self.assertRaises(BridgeError) as failure,
            ):
                actions.kanban_update("default", task_id, {"title": second}, expected_revision=revision)
            self.assertTrue(failure.exception.details["state_changed"])
            self.assertIsNone(failure.exception.details["applied_fields"])
            with settings.native.board_connection("default") as conn:
                self.assertEqual(native.get_task(conn, task_id).title, second)
            self.assertNotIn(second, str(failure.exception.payload()))
