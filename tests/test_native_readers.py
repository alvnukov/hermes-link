# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Native readers exercise isolated native stores and opaque boundary projections."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.core import BridgeError, Config, object_json, rows
from hermes_bridge.native import Native, decode_cursor, encode_cursor
from tests.support import REPO


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        sys.path.insert(0, str(REPO))
        importlib.import_module("hermes_cli.config")
        importlib.import_module("hermes_cli.profiles")
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / ".hermes"
        self.home.mkdir()
        self.home.joinpath("config.yaml").write_text("model: test\n")
        self.enterContext(patch("pathlib.Path.home", return_value=self.root))
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": str(self.home)}))
        self.native = Native(Config(REPO, self.home / "key", ("default", "b"), ("default",)))

    def test_invalid_cursor_objects_are_reported_as_bad_input(self) -> None:
        for cursor in ("W10", "bnVsbA", "eA", "a" * 2049):
            with self.subTest(cursor=cursor[:20]), self.assertRaises(BridgeError) as error:
                decode_cursor(cursor, {})
            self.assertEqual(error.exception.code, "invalid_input")
        for value in (-1, 2**63, True):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                decode_cursor(encode_cursor({}, value), {})
        self.assertEqual(decode_cursor("", {}), 0)

    def test_profiles_use_native_registry_and_allowlist(self) -> None:
        other = self.home / "profiles" / "b"
        other.mkdir(parents=True)
        other.joinpath("config.yaml").write_text("model: second-model\n")
        self.assertEqual({row["name"] for row in self.native.profile_rows()}, {"default", "b"})
        self.assertEqual(self.native.profile("b")["model"], "second-model")
        restricted = Native(replace(self.native.config, profiles=("default",)))
        self.assertEqual([row["name"] for row in restricted.profile_rows()], ["default"])
        with self.assertRaises(BridgeError):
            restricted.profile("b")
        with self.assertRaises(BridgeError):
            self.native.profile("missing")
        with patch.object(self.native.profiles_module, "profile_exists", return_value=False):
            with self.assertRaises(BridgeError) as error:
                self.native.home("b")
            self.assertEqual(error.exception.code, "profile_not_found")
        with patch.object(self.native.profiles_module, "get_profile_dir", return_value="invalid"):
            with self.assertRaises(BridgeError) as error:
                self.native.home("default")
            self.assertEqual(error.exception.code, "invalid_response")
        with patch.object(self.native.profiles_module, "list_profiles", return_value=[]):
            with self.assertRaises(BridgeError) as error:
                self.native.profile("b")
            self.assertEqual(error.exception.code, "profile_not_found")

    def test_home_scope_restores_after_exception(self) -> None:
        previous = self.native.constants.get_hermes_home()
        with self.assertRaisesRegex(RuntimeError, "test"), self.native.home_scope("default"):
            msg = "test"
            raise RuntimeError(msg)
        self.assertEqual(self.native.constants.get_hermes_home(), previous)
        with self.assertRaises(BridgeError):
            Native(replace(self.native.config, hermes_repo=self.root))

    def test_runtime_projection_and_capabilities_do_not_export_opaque_fields(self) -> None:
        status = importlib.import_module("gateway.status")
        rendezvous = importlib.import_module("gateway.host_rendezvous")
        with (
            patch.object(status, "read_runtime_status", return_value={"pid": 123, "secret": "PRIVATE"}),
            patch.object(status, "runtime_status_pid_is_live", return_value=False),
            patch.object(rendezvous, "read_record", return_value={"token": "PRIVATE"}),
        ):
            result = self.native.info()
        self.assertFalse(result["gateway_live"])
        self.assertTrue(result["desktop_available"])
        self.assertEqual(result["runtime"], {"pid": 123})
        self.assertNotIn("PRIVATE", str(result))
        capabilities = self.native.capabilities()
        self.assertEqual(capabilities["boards"], ["default"])
        self.assertEqual(object_json(capabilities["events"])["runtime_journal"], "unsupported")

    def test_missing_stores_are_read_without_creation(self) -> None:
        before = set(self.home.iterdir())
        self.assertEqual(self.native.sessions("default", 10, 0, active_only=False)["storage"], "not_created")
        self.assertEqual(self.native.workspaces("default")["projects"], [])
        self.assertEqual(self.native.cron("default")["jobs"], [])
        for method, args in (
            (self.native.session, ("missing", "default")),
            (self.native.workspace, ("missing", "default")),
            (self.native.cron_job, ("missing", "default")),
        ):
            with self.assertRaises(BridgeError):
                method(*args)
        with self.assertRaises(BridgeError), self.native.board_connection("default"):
            self.fail("Missing native board must not open")
        self.assertEqual(set(self.home.iterdir()), before)

    def test_sessions_filter_activity_keep_page_offsets_and_hide_content(self) -> None:
        state = importlib.import_module("hermes_state")
        with closing(state.SessionDB(db_path=self.home / "state.db")) as db:
            db.create_session("active", source="cli", model="test", system_prompt="PRIVATE")
            db.create_session("ended", source="cli", model="test")
            db.end_session("ended", "test")
        active = self.native.sessions("default", 20, 0, active_only=True)
        self.assertEqual([row["id"] for row in rows(active["sessions"])], ["active"])
        self.assertNotIn("PRIVATE", str(active))
        self.assertEqual(self.native.sessions("default", 1, 0, active_only=False)["next_offset"], 1)
        with self.assertRaises(BridgeError) as error:
            self.native.session("missing", "default")
        self.assertEqual(error.exception.code, "session_not_found")

    def test_cron_projection_and_corrupt_store_refusal_preserve_bytes(self) -> None:
        cron_dir = self.home / "cron"
        cron_dir.mkdir()
        path = cron_dir / "jobs.json"
        path.write_text(json.dumps({"jobs": [{"id": "job1", "name": "test", "prompt": "PRIVATE"}]}))
        before = path.read_bytes()
        self.assertEqual(
            self.native.cron_job("job1", "default"), {"profile": "default", "id": "job1", "name": "test"}
        )
        self.assertEqual(path.read_bytes(), before)
        path.write_text("{broken")
        with self.assertRaises(BridgeError) as error:
            self.native.cron("default")
        self.assertEqual(error.exception.code, "storage_corrupt")
        self.assertEqual(path.read_text(), "{broken")

    def test_workspaces_read_native_project_ids_and_cached_repos(self) -> None:
        module = importlib.import_module("hermes_cli.projects_db")
        with self.native.home_scope("default"), closing(module.connect()) as conn:
            identity = module.create_project(
                conn, name="Example", slug="example", primary_path=str(self.root)
            )
        result = self.native.workspaces("default")
        self.assertFalse(result["scan_performed"])
        self.assertEqual(rows(result["projects"])[0]["id"], identity)
        self.assertEqual(
            object_json(self.native.workspace("example", "default")["workspace"])["id"], identity
        )
        with patch.object(module, "list_discovered_repos", return_value=[{"path": "/cached/repo"}]):
            self.assertEqual(
                self.native.workspace("/cached/repo", "default")["workspace"], {"path": "/cached/repo"}
            )

    def test_kanban_native_task_worker_and_event_projections(self) -> None:
        module = importlib.import_module("hermes_cli.kanban_db")
        with self.native.board_connection("default", write=True) as conn:
            task_id = module.create_task(conn, title="test", assignee="default", triage=True)
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
            run_id = conn.execute(
                "INSERT INTO task_runs(task_id, profile, status, worker_pid, started_at, metadata) "
                "VALUES(?, 'default', 'running', 123, ?, 'PRIVATE')",
                (task_id, int(time.time())),
            ).lastrowid
            conn.commit()
        self.assertEqual(rows(self.native.kanban_get("default")["tasks"])[0]["id"], task_id)
        self.assertEqual(object_json(self.native.kanban_task(task_id, "default")["task"])["id"], task_id)
        workers = self.native.workers("default")
        self.assertEqual(rows(workers["workers"])[0]["worker_pid"], 123)
        self.assertNotIn("PRIVATE", str(workers))
        self.assertIsInstance(run_id, int)
        assert isinstance(run_id, int)
        self.assertNotIn("PRIVATE", str(self.native.worker(run_id, "default")))
        with self.assertRaises(BridgeError):
            self.native.worker(9999, "default")
        with self.assertRaises(BridgeError):
            self.native.kanban_task("missing", "default")
        self.assertIn("default", [row["name"] for row in rows(self.native.assignees("default")["assignees"])])
        with patch.object(
            module, "list_boards", return_value=[{"slug": "default", "name": "yes"}, {"slug": "private"}]
        ):
            self.assertEqual(
                rows(self.native.kanban_boards()["boards"]), [{"slug": "default", "name": "yes"}]
            )
        events = self.native.events("kanban", "default", "default", task_id, "", "", 1)
        self.assertEqual(len(rows(events["events"])), 1)
        following = self.native.events("kanban", "default", "default", task_id, "", str(events["cursor"]), 1)
        self.assertEqual(following["events"], [])
        self.assertEqual(following["cursor"], events["cursor"])

    def test_events_reject_incompatible_scopes_and_missing_board(self) -> None:
        for subsystem, task, session in (
            ("runtime", "", ""),
            ("session", "", ""),
            ("session", "x", "x"),
            ("kanban", "", "x"),
            ("kanban", "", ""),
        ):
            with (
                self.subTest(subsystem=subsystem, task=task, session=session),
                self.assertRaises(BridgeError),
            ):
                self.native.events(subsystem, "default", "default", task, session, "", 10)

    def test_memory_provider_boundary_hides_contents_and_credentials(self) -> None:
        scope = importlib.import_module("hermes_cli.web_server_profiles")
        providers = importlib.import_module("hermes_cli.web_server_memory")
        config = importlib.import_module("hermes_cli.config")
        with (
            patch.object(scope, "_config_profile_scope", return_value=nullcontext()),
            patch.object(
                providers,
                "_discover_memory_provider_statuses",
                return_value=[{"name": "hindsight", "configured": True, "contents": "PRIVATE"}],
            ),
            patch.object(
                config, "load_config", return_value={"memory": {"provider": "hindsight", "token": "PRIVATE"}}
            ),
        ):
            result = self.native.memory_status("default")
        self.assertEqual(result["selected_provider"], "hindsight")
        self.assertNotIn("PRIVATE", str(result))
        with (
            patch.object(scope, "_config_profile_scope", return_value=nullcontext()),
            patch.object(providers, "_discover_memory_provider_statuses", return_value=[]),
            patch.object(config, "load_config", return_value={}),
        ):
            self.assertFalse(object_json(self.native.memory_status("default")["hindsight"])["available"])

    def test_session_event_pages_retain_cursor_and_reject_inconsistent_native_results(self) -> None:
        state = importlib.import_module("hermes_state")
        timeline = importlib.import_module("hermes_state_timeline")
        with closing(state.SessionDB(db_path=self.home / "state.db")) as db:
            db.create_session("events", source="cli", model="test")
        scope = {
            "subsystem": "session",
            "profile": "default",
            "board": "default",
            "task_id": "",
            "session_id": "events",
        }
        cursor = encode_cursor(scope, 7)
        pages = [
            {"entries": [], "pagination": {"has_more": False, "next_cursor": None}},
            {"entries": [{"row_id": 8, "timestamp": 1}], "pagination": {"has_more": True, "next_cursor": 8}},
        ]
        for page, expected in zip(pages, (7, 8), strict=True):
            with patch.object(timeline, "get_session_timeline", return_value=page):
                result = self.native.events("session", "default", "default", "", "events", cursor, 1)
            self.assertEqual(decode_cursor(str(result["cursor"]), scope), expected)
        invalid_pages = [
            ({"entries": [], "pagination": {"has_more": True, "next_cursor": None}}, "invalid_response"),
            (
                {"entries": [{"row_id": 999, "timestamp": 1}], "pagination": {"has_more": False}},
                "events_changed",
            ),
        ]
        for page, code in invalid_pages:
            with (
                patch.object(timeline, "get_session_timeline", return_value=page),
                self.assertRaises(BridgeError) as error,
            ):
                self.native.events("session", "default", "default", "", "events", cursor, 1)
            self.assertEqual(error.exception.code, code)
