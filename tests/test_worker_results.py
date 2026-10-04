# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.core import Config, Object, object_json, rows
from hermes_bridge.native import Native
from tests.support import REPO


class WorkerResultTests(unittest.TestCase):
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
        self.native = Native(Config(REPO, self.home / "key", ("default",), ("default",)))
        self.kanban = importlib.import_module("hermes_cli.kanban_db")
        self.state = importlib.import_module("hermes_state")

    def session(self, session_id: str, *, source: str = "kanban", profile: str = "default") -> None:
        db = self.state.SessionDB(db_path=self.home / "state.db")
        try:
            db.create_session(session_id, source=source, model="test", profile_name=profile)
            db.append_message(session_id, "assistant", "Assistant output is separate from handoff")
            db.update_token_counts(session_id, input_tokens=999, output_tokens=111)
        finally:
            db.close()

    def make_run(
        self, *, summary: str | None = "HANDOFF_OK", metadata: Object | None = None
    ) -> tuple[str, int]:
        with self.native.board_connection("default", write=True) as conn:
            task_id: str = self.kanban.create_task(
                conn, title="isolated test", assignee="default", session_id="origin_chat"
            )
            with conn:
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
            claimed = self.kanban.claim_task(conn, task_id, claimer="test")
            self.assertIsNotNone(claimed)
            run_id = int(claimed.current_run_id)
            self.assertTrue(
                self.kanban.complete_task(
                    conn, task_id, summary=summary, metadata=metadata, expected_run_id=run_id
                )
            )
        return task_id, run_id

    def test_durable_handoff_linkage_preserves_origin_and_never_uses_latest_session(self) -> None:
        self.session("worker_exact")
        task_id, run_id = self.make_run(metadata={"worker_session_id": "worker_exact"})
        self.session("unrelated_newer")
        before = self.home.joinpath("state.db").read_bytes()
        result = self.native.worker(run_id, "default")
        run = object_json(result["run"])
        self.assertEqual(run["worker_session_id"], "worker_exact")
        self.assertEqual(run["session_link_status"], "persisted")
        normalized = object_json(result["result"])
        self.assertEqual(normalized["output"], "HANDOFF_OK")
        self.assertEqual(normalized["output_source"], "native_run_summary")
        self.assertIsNone(normalized["usage"])
        self.assertEqual(normalized["usage_scope"], "unavailable")
        detail = self.native.kanban_task(task_id, "default")
        self.assertEqual(object_json(detail["task"])["session_id"], "origin_chat")
        self.assertEqual(rows(detail["runs"])[0], run)
        self.assertNotIn("Assistant output", json.dumps(result))
        self.assertEqual(self.home.joinpath("state.db").read_bytes(), before)

    def test_crash_diagnostics_export_only_saved_sanitized_evidence(self) -> None:
        task_id, run_id = self.make_run()
        with self.native.board_connection("default", write=True) as conn, conn:
            conn.execute(
                "UPDATE task_runs SET status='crashed',outcome='crashed',summary=NULL,error=?,metadata=? "
                "WHERE id=?",
                (
                    "Worker exited with status 1",
                    json.dumps(
                        {"exit_code": 1, "exit_kind": "nonzero_exit", "worker_output": "Profile missing"}
                    ),
                    run_id,
                ),
            )
        result = self.native.worker(run_id, "default")
        run = object_json(result["run"])
        self.assertEqual(run["exit_code"], 1)
        self.assertEqual(run["exit_kind"], "nonzero_exit")
        self.assertEqual(run["last_output"], "Profile missing")
        self.assertEqual(run["session_link_status"], "unavailable")
        normalized = object_json(result["result"])
        self.assertIsNone(normalized["output"])
        self.assertEqual(object_json(normalized["error"])["message"], "Worker exited with status 1")
        self.assertEqual(normalized["task_id"], task_id)

    def test_credentials_are_scrubbed_from_every_text_and_arbitrary_metadata_is_omitted(self) -> None:
        canary = "opaque-canary-secret-without-vendor-prefix"
        self.home.joinpath(".env").write_text(f"A2A_PEER_TOKENS={canary}\n")
        task_id, run_id = self.make_run(
            summary=f"answer {canary}; https://user:{canary}@example.test/?api_key={canary}",
            metadata={"worker_output": f"Authorization: Bearer {canary}", "arbitrary": canary},
        )
        with self.native.board_connection("default", write=True) as conn, conn:
            conn.execute("UPDATE task_runs SET error=? WHERE id=?", (f"failed: {canary}", run_id))
        for response in (self.native.worker(run_id, "default"), self.native.kanban_task(task_id, "default")):
            self.assertNotIn(canary, json.dumps(response))
            self.assertNotIn("arbitrary", json.dumps(response))

    def test_unavailable_and_wrong_scope_links_never_read_other_sessions(self) -> None:
        self.session("foreign", source="cli")
        cases: tuple[tuple[Object, str], ...] = (
            ({}, "unavailable"),
            ({"worker_session_id": "missing"}, "not_found"),
            ({"worker_session_id": "../state.db"}, "invalid"),
            ({"worker_session_id": "foreign"}, "scope_mismatch"),
        )
        for metadata, expected in cases:
            with self.subTest(metadata=metadata):
                _, run_id = self.make_run(metadata=metadata)
                run = object_json(self.native.worker(run_id, "default")["run"])
                self.assertEqual(run["session_link_status"], expected)
                self.assertIsNone(run["worker_session_id"])
        _, run_id = self.make_run(summary="PRIVATE")
        with self.native.board_connection("default", write=True) as conn, conn:
            conn.execute("UPDATE task_runs SET profile='other' WHERE id=?", (run_id,))
        restricted = Native(replace(self.native.config, profiles=("default",)))
        response = restricted.worker(run_id, "default")
        self.assertNotIn("PRIVATE", json.dumps(response))
        self.assertEqual(object_json(response["run"])["session_link_status"], "profile_unavailable")

    def test_redactor_failure_never_returns_raw_output(self) -> None:
        _, run_id = self.make_run(summary="PRIVATE RAW OUTPUT")
        redactor = importlib.import_module("agent.redact")
        with patch.object(redactor, "redact_for_egress", side_effect=RuntimeError("PRIVATE FAILURE")):
            result = self.native.worker(run_id, "default")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(object_json(result["run"])["redaction_status"], "unavailable")

    def test_container_credentials_and_redaction_import_failure_fail_closed(self) -> None:
        canary = "opaque-secret-in-native-peer-map"
        self.home.joinpath(".env").write_text(f'A2A_PEER_TOKENS=\'{{"peer":"{canary}"}}\'\n')
        _, run_id = self.make_run(summary=f"answer {canary}")
        result = self.native.worker(run_id, "default")
        self.assertNotIn(canary, json.dumps(result))
        import_module = importlib.import_module

        def reject_redactor(name: str, package: str | None = None) -> object:
            if name == "agent.redact":
                msg = "PRIVATE IMPORT ERROR"
                raise ImportError(msg)
            return import_module(name, package)

        with patch("hermes_bridge.worker_results.importlib.import_module", side_effect=reject_redactor):
            unavailable = self.native.worker(run_id, "default")
        self.assertNotIn(canary, json.dumps(unavailable))
        self.assertNotIn("PRIVATE", json.dumps(unavailable))
        normalized = object_json(unavailable["result"])
        self.assertIsNone(normalized["output"])
        self.assertIsNone(normalized["output_source"])
        self.assertEqual(normalized["redaction_status"], "unavailable")

    def test_settings_classified_argv_headers_urls_and_numeric_secrets_are_scrubbed(self) -> None:
        canary = "opaque-canary-from-config-storage"
        cases: tuple[Object, ...] = (
            {"args": ["--api-key", canary]},
            {"args": [f"--api-key={canary}"]},
            {"args": ["--header", f"X-API-Key: {canary}"]},
            {"args": [f"--header=X-Auth-Token: {canary}"]},
            {"url": f"https://user:{canary}@example.test/?api_key={canary}"},
            {"api_key": 123456789012345},
        )
        for server in cases:
            with self.subTest(server=server):
                secret = "123456789012345" if "api_key" in server else canary
                self.home.joinpath("config.yaml").write_text(
                    json.dumps({"model": "test", "mcp_servers": {"demo": server}})
                )
                _, run_id = self.make_run(summary=f"result contains {secret}")
                response = self.native.worker(run_id, "default")
                self.assertNotIn(secret, json.dumps(response))
                self.assertEqual(object_json(response["run"])["redaction_status"], "applied")

    def test_whole_task_readers_scrub_saved_task_text_without_replacing_origin_session(self) -> None:
        canary = "opaque-secret-in-card-text"
        self.home.joinpath(".env").write_text(f"A2A_PEER_TOKENS={canary}\n")
        task_id, _ = self.make_run(summary=f"handoff {canary}")
        with self.native.board_connection("default", write=True) as conn, conn:
            conn.execute(
                "UPDATE tasks SET title=?,body=?,result=?,last_failure_error=? WHERE id=?",
                (canary, canary, canary, canary, task_id),
            )
        for response in (self.native.kanban_task(task_id, "default"), self.native.kanban_get("default")):
            self.assertNotIn(canary, json.dumps(response))
        task = object_json(self.native.kanban_task(task_id, "default")["task"])
        self.assertEqual(task["session_id"], "origin_chat")
        self.assertEqual(task["text_redaction_status"], "applied")

    def test_retries_and_later_session_continuation_do_not_replace_attempt_handoff(self) -> None:
        self.session("worker_first")
        task_id, first_run_id = self.make_run(
            summary="FIRST_HANDOFF", metadata={"worker_session_id": "worker_first"}
        )
        db = self.state.SessionDB(db_path=self.home / "state.db")
        try:
            db.append_message("worker_first", "assistant", "LATER_CONTINUATION")
            db.update_token_counts("worker_first", input_tokens=1000)
        finally:
            db.close()
        self.session("worker_second")
        with self.native.board_connection("default", write=True) as conn:
            with conn:
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
            claimed = self.kanban.claim_task(conn, task_id, claimer="test")
            second_run_id = int(claimed.current_run_id)
            self.assertTrue(
                self.kanban.complete_task(
                    conn,
                    task_id,
                    summary="SECOND_HANDOFF",
                    metadata={"worker_session_id": "worker_second"},
                    expected_run_id=second_run_id,
                )
            )
        first = object_json(self.native.worker(first_run_id, "default")["result"])
        second = object_json(self.native.worker(second_run_id, "default")["result"])
        self.assertEqual(first["output"], "FIRST_HANDOFF")
        self.assertEqual(first["session_id"], "worker_first")
        self.assertIsNone(first["usage"])
        self.assertEqual(second["output"], "SECOND_HANDOFF")
        self.assertEqual(second["session_id"], "worker_second")

    def test_denied_historical_profile_withholds_card_text_before_reading_its_secrets(self) -> None:
        task_id, run_id = self.make_run(summary="PRIVATE")
        with self.native.board_connection("default", write=True) as conn, conn:
            conn.execute("UPDATE task_runs SET profile='denied' WHERE id=?", (run_id,))
            conn.execute(
                "UPDATE tasks SET result=?,last_failure_error=? WHERE id=?", ("PRIVATE", "PRIVATE", task_id)
            )
        response = self.native.kanban_task(task_id, "default")
        self.assertNotIn("PRIVATE", json.dumps(response))
        task = object_json(response["task"])
        self.assertEqual(task["session_id"], "origin_chat")
        self.assertEqual(task["text_redaction_status"], "profile_unavailable")

    def test_manual_terminal_rows_use_board_scope_but_unknown_worker_rows_withhold_text(self) -> None:
        with self.native.board_connection("default", write=True) as conn:
            task_id = self.kanban.create_task(conn, title="manual card", triage=True)
            with conn:
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
            self.assertTrue(self.kanban.complete_task(conn, task_id, result="manual result", force=True))
        detail = self.native.kanban_task(task_id, "default")
        self.assertEqual(object_json(detail["task"])["result"], "manual result")
        for status, outcome in (("running", None), ("completed", "blocked"), ("unknown", "unknown")):
            with self.subTest(status=status, outcome=outcome):
                with self.native.board_connection("default", write=True) as conn, conn:
                    conn.execute(
                        "UPDATE task_runs SET status=?, outcome=? WHERE task_id=?",
                        (status, outcome, task_id),
                    )
                unknown = object_json(self.native.kanban_task(task_id, "default")["task"])
                self.assertEqual(unknown["text_redaction_status"], "profile_unavailable")
                self.assertNotIn("manual result", json.dumps(unknown))
        with self.native.board_connection("default", write=True) as conn, conn:
            conn.execute(
                "UPDATE task_runs SET status='completed', outcome='completed', ended_at=started_at+1 "
                "WHERE task_id=?",
                (task_id,),
            )
        unknown = object_json(self.native.kanban_task(task_id, "default")["task"])
        self.assertEqual(unknown["text_redaction_status"], "profile_unavailable")
        self.assertNotIn("manual result", json.dumps(unknown))

    def test_text_is_bounded_after_secret_redaction_and_unsupported_diagnostics_are_omitted(self) -> None:
        canary = "opaque-secret-at-truncation-boundary"
        self.home.joinpath(".env").write_text(f"API_KEY={canary}\n")
        _, run_id = self.make_run(
            summary="x" * 65_530 + canary + "y" * 100,
            metadata={"worker_output": "x" * 4090 + canary, "exit_code": True, "exit_kind": canary},
        )
        result = self.native.worker(run_id, "default")
        run = object_json(result["run"])
        self.assertNotIn(canary, json.dumps(result))
        self.assertEqual(len(str(run["summary"])), 65_536)
        self.assertEqual(len(str(run["last_output"])), 4096)
        self.assertTrue(run["text_truncated"])
        self.assertIsNone(run["exit_code"])
        self.assertIsNone(run["exit_kind"])


if __name__ == "__main__":
    unittest.main()
