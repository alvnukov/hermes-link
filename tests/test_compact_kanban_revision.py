# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Exercise the conditional Kanban contract through the real compact MCP router."""

from __future__ import annotations

import unittest

from hermes_bridge.core import Object, object_json
from hermes_bridge.server import create_server
from tests.test_compact import call
from tests.test_settings import fixture


class CompactRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_exposes_optional_revision_without_growing_catalog(self) -> None:
        with fixture() as (settings, _home):
            server, surface = create_server(settings.native.config)
            self.addCleanup(surface.runs.close)
            result = await call(server, "hermes_kanban", "help", {"action": "update"})
            self.assertFalse(result.is_error)
            data = object_json(result.structured_content)
            parameters = object_json(data["params_schema"])
            properties = object_json(parameters["properties"])
            self.assertIn("expected_revision", properties)
            required = parameters["required"]
            assert isinstance(required, list)
            self.assertNotIn("expected_revision", required)
            self.assertEqual(len(await server.list_tools()), 10)

    async def test_stale_compact_update_returns_domain_conflict_without_second_edit(self) -> None:
        with fixture() as (settings, _home):
            server, surface = create_server(settings.native.config)
            self.addCleanup(surface.runs.close)
            created = await call(
                server,
                "hermes_kanban",
                "create",
                {"title": "Original", "request_id": "compact-cas"},
            )
            self.assertFalse(created.is_error)
            task = object_json(object_json(created.structured_content)["task"])
            revision = task["revision"]
            params: Object = {
                "task_id": task["id"],
                "expected_revision": revision,
                "changes": {"title": "First"},
            }
            first = await call(server, "hermes_kanban", "update", params)
            self.assertFalse(first.is_error, str(first))
            params["changes"] = {"title": "Second"}
            stale = await call(server, "hermes_kanban", "update", params)
            self.assertTrue(stale.is_error)
            error = object_json(object_json(stale.structured_content)["error"])
            self.assertEqual(error["code"], "revision_conflict")
            observed = await call(server, "hermes_kanban", "task", {"task_id": task["id"]})
            self.assertEqual(object_json(object_json(observed.structured_content)["task"])["title"], "First")
