# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import unittest

from hermes_bridge.core import Object, object_json, rows
from hermes_bridge.server import create_server
from tests.test_compact import call
from tests.test_settings import fixture


class ManagementContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_actions_are_discoverable_and_profile_creation_can_be_undone(self) -> None:
        with fixture() as (settings, home):
            server, surface = create_server(settings.native.config)
            self.addCleanup(surface.runs.close)
            for tool, action in (
                ("hermes_profiles", "delete"),
                ("hermes_cron", "delete"),
                ("hermes_kanban", "archive"),
            ):
                with self.subTest(tool=tool):
                    help_result = await call(server, tool, "help", {"action": action})
                    self.assertFalse(help_result.is_error, help_result)
                    self.assertTrue(object_json(help_result.structured_content)["write"])
            created = await call(server, "hermes_profiles", "create", {"name": "disposable"})
            self.assertFalse(created.is_error, created)
            self.assertTrue((home / "profiles/disposable/config.yaml").exists())
            deleted = await call(server, "hermes_profiles", "delete", {"name": "disposable"})
            self.assertFalse(deleted.is_error, deleted)
            self.assertFalse((home / "profiles/disposable").exists())
            listed = await call(server, "hermes_profiles", "list")
            self.assertEqual(
                [row["name"] for row in rows(object_json(listed.structured_content)["profiles"])], ["default"]
            )

    async def test_kanban_updates_have_closed_typed_schema_and_reject_before_mutation(self) -> None:
        with fixture() as (settings, _home):
            server, surface = create_server(settings.native.config)
            self.addCleanup(surface.runs.close)
            help_result = await call(server, "hermes_kanban", "help", {"action": "update"})
            schema = object_json(object_json(help_result.structured_content)["params_schema"])
            definitions = object_json(schema.get("$defs", {}))
            self.assertTrue(definitions, "The changes model needs a concrete typed schema")
            self.assertTrue(
                any(
                    row.get("additionalProperties") is False
                    for row in definitions.values()
                    if isinstance(row, dict)
                )
            )
            created = await call(
                server, "hermes_kanban", "create", {"title": "Before", "request_id": "typed-update"}
            )
            task = object_json(object_json(created.structured_content)["task"])
            cases: tuple[Object, ...] = (
                {"title": "after", "unknown": "hidden"},
                {"priority": "high"},
                {"assignee": "default", "title": "   "},
            )
            for changes in cases:
                with self.subTest(changes=changes):
                    denied = await call(
                        server, "hermes_kanban", "update", {"task_id": task["id"], "changes": changes}
                    )
                    self.assertTrue(denied.is_error, denied)
                    current = await call(server, "hermes_kanban", "task", {"task_id": task["id"]})
                    self.assertEqual(object_json(current.structured_content)["task"], task)
