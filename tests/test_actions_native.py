# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Exercise profile and cron actions through native ephemeral stores."""

from __future__ import annotations

import importlib
import json
import unittest
from unittest.mock import patch

from hermes_bridge.actions import Actions
from hermes_bridge.core import BridgeError, object_json, rows
from hermes_bridge.settings import Settings
from tests.test_settings import fixture


class NativeActionTests(unittest.TestCase):
    def test_native_task_assignment_persists_allowed_profile(self) -> None:
        with fixture() as (settings, _home):
            actions = Actions(settings.native)
            actions.profile_create("research", "task owner", {})
            created = actions.kanban_create(
                "default", "assigned-request", {"title": "assigned", "assignee": "research"}
            )
            task = object_json(created["task"])
            self.assertEqual(task["assignee"], "research")
            changed = actions.kanban_update("default", str(task["id"]), {"assignee": "default"})
            self.assertEqual(object_json(changed["task"])["assignee"], "default")
            self.assertEqual(
                object_json(settings.native.kanban_task(str(task["id"]), "default")["task"])["assignee"],
                "default",
            )

    def test_profile_creation_applies_settings_without_copying_default_secrets(self) -> None:
        # Creating in the wrong scope would overwrite the default or inherit its credentials.
        with fixture(env_text="OPENAI_API_KEY=default-secret\n") as (settings, home):
            original = home.joinpath("config.yaml").read_bytes()
            created = Actions(settings.native).profile_create(
                "research", "Isolated research", {"model.default": "research-model", "agent.max_turns": 7}
            )
            self.assertTrue(created["created"])
            self.assertEqual(object_json(created["profile"])["description"], "Isolated research")
            profile_home = home / "profiles" / "research"
            config = object_json(Settings(settings.native).get("research")["settings"])
            self.assertEqual(config["model.default"], "research-model")
            self.assertEqual(config["agent.max_turns"], 7)
            self.assertNotIn("default-secret", profile_home.joinpath(".env").read_text(encoding="utf-8"))
            self.assertEqual(home.joinpath("config.yaml").read_bytes(), original)

    def test_duplicate_profile_refuses_to_overwrite_native_files(self) -> None:
        with fixture() as (settings, home):
            actions = Actions(settings.native)
            actions.profile_create("research", "first", {})
            path = home / "profiles" / "research" / "config.yaml"
            original = path.read_bytes()
            with self.assertRaises(BridgeError) as error:
                actions.profile_create("research", "replacement", {"model.default": "changed"})
            self.assertEqual(error.exception.code, "profile_exists")
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_profile_settings_refuse_creation(self) -> None:
        with fixture() as (settings, home):
            actions = Actions(settings.native)
            with self.assertRaises(BridgeError) as error:
                actions.profile_create("research", "invalid", {"memory.enabled": "yes"})
            self.assertEqual(error.exception.code, "invalid_setting")
            self.assertFalse(home.joinpath("profiles", "research").exists())

    def test_native_cron_create_pause_resume_persists_and_scopes_profile(self) -> None:
        # Incorrect scoping or pause handling changes the native JSON store or sibling profile.
        with fixture() as (settings, home):
            actions = Actions(settings.native)
            actions.profile_create("research", "cron scope", {})
            created = actions.cron_create("research", "Summarize", "every 30m", "summary", paused=False)
            job_id = str(created["id"])
            self.assertTrue(created["enabled"])
            self.assertEqual(created["name"], "summary")
            self.assertEqual(settings.native.cron("default")["count"], 0)
            self.assertFalse(home.joinpath("cron", "jobs.json").exists())
            paused = actions.cron_set_paused("research", job_id, paused=True)
            self.assertFalse(paused["enabled"])
            self.assertEqual(paused["state"], "paused")
            self.assertEqual(paused["paused_reason"], "User request via MCP")
            resumed = actions.cron_set_paused("research", job_id, paused=False)
            self.assertTrue(resumed["enabled"])
            store = home / "profiles" / "research" / "cron" / "jobs.json"
            self.assertIn(job_id, store.read_text(encoding="utf-8"))
            self.assertEqual(rows(settings.native.cron("research")["jobs"])[0]["id"], job_id)

    def test_paused_cron_creation_never_creates_an_enabled_job(self) -> None:
        with fixture() as (settings, _home):
            created = Actions(settings.native).cron_create(
                "default", "Summarize", "every 1h", "paused", paused=True
            )
            self.assertFalse(created["enabled"])
            self.assertEqual(created["state"], "paused")

    def test_corrupt_cron_store_is_not_repaired_by_create(self) -> None:
        with fixture() as (settings, home):
            store = home / "cron" / "jobs.json"
            store.parent.mkdir()
            store.write_bytes(b"{corrupt native store")
            with self.assertRaises(BridgeError) as error:
                Actions(settings.native).cron_create("default", "Summarize", "every 1h", "x", paused=True)
            self.assertEqual(error.exception.code, "storage_corrupt")
            self.assertEqual(store.read_bytes(), b"{corrupt native store")

    def test_disappearing_cron_job_reports_not_found(self) -> None:
        with fixture() as (settings, home):
            actions = Actions(settings.native)
            created = actions.cron_create("default", "Summarize", "every 1h", "x", paused=True)
            jobs = importlib.import_module("cron.jobs")
            pause_job = jobs.pause_job

            def remove_before_pause(job_id: str, *, reason: str) -> object:
                home.joinpath("cron", "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
                return pause_job(job_id, reason=reason)

            with (
                patch("cron.jobs.pause_job", side_effect=remove_before_pause),
                self.assertRaises(BridgeError) as error,
            ):
                actions.cron_set_paused("default", str(created["id"]), paused=True)
            self.assertEqual(error.exception.code, "job_not_found")
            self.assertEqual(settings.native.cron("default")["count"], 0)

    def test_missing_board_update_does_not_initialize_native_store(self) -> None:
        with fixture() as (settings, home):
            with self.assertRaises(BridgeError) as error:
                Actions(settings.native).kanban_update("default", "nonexistent", {"body": "changed"})
            self.assertEqual(error.exception.code, "kanban_unavailable")
            self.assertFalse(home.joinpath("kanban.db").exists())

    def test_unknown_kanban_fields_refuse_native_mutation(self) -> None:
        with fixture() as (settings, home):
            actions = Actions(settings.native)
            for fields in ({}, {"title": "x", "not_native": True}):
                with self.subTest(fields=fields), self.assertRaises(BridgeError) as error:
                    actions.kanban_create("default", "request", object_json(fields))
                self.assertEqual(error.exception.code, "invalid_input")
            for changes in ({}, {"not_native": True}):
                with self.subTest(changes=changes), self.assertRaises(BridgeError) as error:
                    actions.kanban_update("default", "task", object_json(changes))
                self.assertEqual(error.exception.code, "invalid_input")
            self.assertFalse(home.joinpath("kanban.db").exists())
