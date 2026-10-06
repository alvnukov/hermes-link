# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import importlib
import unittest

from hermes_bridge.core import object_json
from hermes_bridge.server import create_server
from tests.test_compact import call
from tests.test_settings import fixture


class SettingsDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalized_authored_defaults_survive_snapshot_apply(self) -> None:
        with fixture("model: original\nmax_turns: null\n") as (settings, _home):
            before = settings.get("default")
            after = settings.apply("default", object_json(before["config"]), None, str(before["revision"]))
            raw = object_json(after["config_overrides"])
            self.assertIn("max_turns", object_json(raw.get("agent", {})))
            self.assertIsNone(object_json(raw["agent"])["max_turns"])

    async def test_default_opt_in_preserves_normalized_new_alias(self) -> None:
        with fixture() as (settings, _home):
            before = settings.get("default")
            after = settings.apply(
                "default", {"max_turns": None}, None, str(before["revision"]), persist_defaults=True
            )
            raw = object_json(after["config_overrides"])
            self.assertIn("max_turns", object_json(raw.get("agent", {})))

    async def test_apply_preserves_new_extension_nulls_and_empty_objects(self) -> None:
        with fixture("model:\n  default: original\n") as (settings, _home):
            before = settings.get("default")
            config = object_json(before["config"])
            config["custom_extension"] = {"unset": None, "empty": {}}
            config["model"] = {**object_json(config["model"]), "custom_null": None}
            after = settings.apply("default", config, None, str(before["revision"]))
            self.assertEqual(
                object_json(after["config_overrides"])["custom_extension"], {"unset": None, "empty": {}}
            )
            self.assertIn("custom_null", object_json(object_json(after["config_overrides"])["model"]))

    async def test_effective_snapshot_round_trip_keeps_sparse_config_and_dispatch_enabled(self) -> None:
        with fixture("model: original\n") as (settings, home):
            before = settings.get("default")
            config = object_json(before["config"])
            config["model"] = {**object_json(config["model"]), "default": "changed"}
            settings.apply("default", config, None, str(before["revision"]))

            persisted = settings.module.require_readable_config_before_write(home / "config.yaml")
            self.assertTrue("kanban" not in persisted, "Snapshot persisted an inherited Kanban policy")
            self.assertEqual(set(persisted) - {"_config_version"}, {"model"})
            self.assertEqual(persisted["model"], {"default": "changed"})
            dispatcher = importlib.import_module("hermes_cli.kanban_db_dispatch")
            self.assertTrue(dispatcher._profile_exists_fn()("default"))

    async def test_existing_explicit_defaults_and_dispatch_denials_survive_full_snapshot(self) -> None:
        for value in ("null", "[]", "[allowed]"):
            with (
                self.subTest(allowlist=value),
                fixture(f"model: original\nkanban:\n  dispatch_profiles: {value}\n") as (settings, _home),
            ):
                before = settings.get("default")
                expected = object_json(before["config_overrides"])
                after = settings.apply(
                    "default", object_json(before["config"]), None, str(before["revision"])
                )
                actual = object_json(after["config_overrides"])
                self.assertTrue(
                    set(actual) - {"_config_version"} == set(expected), "Inherited roots were saved"
                )
                self.assertEqual(actual["kanban"], expected["kanban"])
                dispatcher = importlib.import_module("hermes_cli.kanban_db_dispatch")
                self.assertFalse(dispatcher._profile_exists_fn()("default"))

    async def test_convenience_update_can_explicitly_set_an_absent_default(self) -> None:
        with fixture() as (settings, _home):
            before = settings.get("default")
            default = object_json(settings.module.DEFAULT_CONFIG["memory"])["user_char_limit"]
            after = settings.update("default", {"memory.user_char_limit": default}, str(before["revision"]))
            self.assertEqual(
                object_json(object_json(after["config_overrides"])["memory"])["user_char_limit"], default
            )

    async def test_mcp_apply_can_opt_in_to_new_explicit_defaults(self) -> None:
        with fixture() as (settings, _home):
            before = settings.get("default")
            server, _surface = create_server(settings.native.config)
            result = await call(
                server,
                "hermes_settings",
                "apply",
                {
                    "config": {"kanban": {"dispatch_profiles": None}},
                    "expected_revision": before["revision"],
                    "persist_defaults": True,
                },
            )
            self.assertFalse(result.is_error, str(result))
            after = object_json(result.structured_content)
            self.assertIn("dispatch_profiles", object_json(object_json(after["config_overrides"])["kanban"]))
            dispatcher = importlib.import_module("hermes_cli.kanban_db_dispatch")
            self.assertFalse(dispatcher._profile_exists_fn()("default"))
