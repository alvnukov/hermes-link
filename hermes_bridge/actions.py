# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Validated mutation actions that persist through the native Hermes APIs."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from dataclasses import asdict
from typing import TYPE_CHECKING

from . import action_cleanup, kanban_revision
from .action_schema import KanbanChanges, Phase, validated_patch
from .core import BridgeError, Object, bounded, object_json, safe_id
from .settings import Settings

if TYPE_CHECKING:
    from .native import Native

CREATE_FIELDS = frozenset(
    {
        "title",
        "body",
        "assignee",
        "tenant",
        "priority",
        "parents",
        "triage",
        "max_runtime_seconds",
        "skills",
        "goal_mode",
        "goal_max_turns",
        "model_override",
        "provider_override",
        "reasoning_effort",
    }
)


class Actions:
    """Expose native writes with access checks, idempotency, and scoped stores."""

    def __init__(self, native: Native) -> None:
        """Retain the native integration and its configured write permissions."""
        self.native = native

    def kanban_create(self, board: str, request_id: str, fields: Object) -> Object:
        """Create a task or replay identical input using the native idempotency key."""
        self.native.config.require_writes()
        self.native.config.board(board)
        safe_id(request_id)
        if not fields or set(fields) - CREATE_FIELDS:
            msg = "invalid_input"
            raise BridgeError(msg, "Unsupported Kanban create fields")
        if fields.get("assignee"):
            self.native.config.profile(str(fields["assignee"]))
        # Native schema validation, native idempotency key and native created_by
        # provenance hold the receipt. No additional database or receipt file.
        api = importlib.import_module("plugins.kanban.dashboard.plugin_api")
        kanban = importlib.import_module("hermes_cli.kanban_db")
        payload = api.CreateTaskBody(**fields)
        fingerprint = "mcp:" + hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
        key = "mcp:" + request_id
        with self.native.board_connection(board, write=True) as conn:
            row = conn.execute(
                "SELECT id, created_by FROM tasks WHERE idempotency_key=? ORDER BY created_at LIMIT 1", (key,)
            ).fetchone()
            if row is not None:
                if row["created_by"] != fingerprint:
                    msg = "idempotency_conflict"
                    raise BridgeError(msg, "request_id was used with different Kanban input")
                task_id = str(row["id"])
            else:
                task_id = str(
                    kanban.create_task(
                        conn,
                        created_by=fingerprint,
                        board=board,
                        **payload.model_dump(exclude={"idempotency_key"}),
                        idempotency_key=key,
                    )
                )
                task = kanban.get_task(conn, task_id)
                if task is None or task.created_by != fingerprint:
                    msg = "idempotency_conflict"
                    raise BridgeError(msg, "Concurrent request_id conflict")
        return self.native.kanban_task(task_id, board)

    def kanban_update(
        self,
        board: str,
        task_id: str,
        changes: KanbanChanges | Object,
        *,
        expected_revision: str | None = None,
    ) -> Object:
        """Validate the full patch and execute one native mutation operation."""
        self.native.config.require_writes()
        self.native.config.board(board)
        safe_id(task_id)
        kanban_revision.validate_expected(expected_revision)
        patch, values, phase = validated_patch(changes)
        if patch.assignee:
            self.native.home(patch.assignee)
        before = self._kanban_state(board, task_id)
        kanban = importlib.import_module("hermes_cli.kanban_db")
        try:
            with (
                self.native.board_connection(board, write=True) as conn,
                kanban.scoped_current_board(board),
                kanban_revision.mutation_guard(conn, task_id, board, expected_revision) as guard,
            ):
                changed = self._kanban_operation(conn, board, task_id, patch, values, phase)
                revision_after = kanban_revision.revision(conn, task_id, board)
        except BridgeError as exc:
            if exc.code in {"revision_conflict", "invalid_input"}:
                raise
            raise self._kanban_failure(board, task_id, values, before, expected_revision) from exc
        except Exception as exc:
            raise self._kanban_failure(board, task_id, values, before, expected_revision) from exc
        if not changed:
            raise self._kanban_failure(board, task_id, values, before, expected_revision)
        result = self.native.kanban_task(task_id, board)
        result["mutation"] = object_json(
            {
                "operation": phase,
                "applied_fields": sorted(values),
                "failed_fields": [],
                "atomicity": "single_native_operation",
                "compare_and_swap": expected_revision is not None,
                "revision_before": guard.revision_before,
                "revision_after": revision_after,
                "revision_after_observed": True,
            }
        )
        return result

    def _kanban_operation(
        self,
        conn: sqlite3.Connection,
        board: str,
        task_id: str,
        patch: KanbanChanges,
        values: Object,
        phase: Phase,
    ) -> bool:
        """Run one native transaction phase on the connection carrying its fence."""
        kanban = importlib.import_module("hermes_cli.kanban_db")
        if phase == "edit":
            return bool(
                kanban.edit_task(
                    conn,
                    task_id,
                    title=patch.title.strip() if patch.title is not None else None,
                    body=patch.body,
                    priority=patch.priority,
                    board=board,
                )
            )
        if phase == "assignment":
            return bool(kanban.assign_task(conn, task_id, patch.assignee or None))
        if phase == "model":
            return bool(
                kanban.set_model_override(
                    conn,
                    task_id,
                    None if patch.clear_model_override else patch.model_override,
                    provider=patch.provider_override,
                )
            )
        if phase == "reasoning":
            return bool(
                kanban.set_reasoning_effort(
                    conn,
                    task_id,
                    None if patch.clear_reasoning_effort else patch.reasoning_effort,
                )
            )
        api = importlib.import_module("plugins.kanban.dashboard.plugin_api")
        # Native dashboard routing preserves review reopen, parent invalidation
        # and post-commit worker termination; the HTTP wrapper opens another conn.
        return bool(
            api._apply_status(  # noqa: SLF001 — native routing has no equivalent public connection API
                conn,
                task_id,
                patch.status,
                api.UpdateTaskBody(**values),
                "Unsupported Kanban status",
            )
        )

    def _kanban_state(self, board: str, task_id: str) -> str:
        """Hash raw native state internally; redacted views and audit ETags cannot prove equality."""
        kanban = importlib.import_module("hermes_cli.kanban_db")
        with self.native.board_connection(board) as conn:
            conn.execute("BEGIN")
            task = kanban.get_task(conn, task_id)
            if task is None:
                msg = "task_not_found"
                raise BridgeError(msg, "Native Kanban task does not exist")
            state = object_json(
                {"task": asdict(task), "runs": [asdict(run) for run in kanban.list_runs(conn, task_id)]}
            )
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()

    def _kanban_failure(
        self, board: str, task_id: str, values: Object, before: str, expected_revision: str | None = None
    ) -> BridgeError:
        """Report observed state after a native refusal or post-commit failure."""
        try:
            changed: bool | None = self._kanban_state(board, task_id) != before
        except (BridgeError, OSError, sqlite3.Error):
            changed = None
        return BridgeError(
            "native_update_failed",
            "Native mutation failed; observed state is reported without assuming rollback",
            object_json(
                {
                    "applied_fields": [] if changed is False else None,
                    "failed_fields": sorted(values),
                    "state_changed": changed,
                    "compare_and_swap": expected_revision is not None,
                }
            ),
        )

    def kanban_archive(self, board: str, task_id: str) -> Object:
        """Archive one explicit idle card, with a transactional active-run guard."""
        return action_cleanup.archive(self.native, board, task_id)

    def profile_delete(self, name: str) -> Object:
        """Delete one explicitly named inactive profile using native cleanup."""
        return action_cleanup.delete_profile(self.native, name)

    def cron_delete(self, profile: str, job_id: str) -> Object:
        """Delete one exact paused, idle job using native cleanup."""
        return action_cleanup.delete_cron(self.native, profile, job_id)

    def profile_create(self, name: str, description: str, settings: Object) -> Object:
        """Create an isolated native profile and optionally apply convenience settings."""
        self.native.config.require_writes()
        self.native.config.profile(name)
        bounded(len(description), 0, 2000)
        if settings:
            Settings.validate(settings)
        if self.native.profiles_module.profile_exists(name):
            msg = "profile_exists"
            raise BridgeError(msg, "Native profile already exists")
        self.native.profiles_module.create_profile(
            name, description=description, no_alias=True, no_skills=True
        )
        if settings:
            manager = Settings(self.native)
            revision = str(manager.get(name)["revision"])
            manager.update(name, settings, revision)
        return {"profile": self.native.profile(name), "created": True}

    def cron_create(self, profile: str, prompt: str, schedule: str, name: str, *, paused: bool) -> Object:
        """Persist a native cron job and register its scheduler after a corruption check."""
        self.native.config.require_writes()
        bounded(len(prompt), 1, 100000)
        bounded(len(schedule), 1, 256)
        bounded(len(name), 1, 256)
        self.native.cron(profile)  # refuse a corrupt store before a native writer can repair it
        jobs = importlib.import_module("cron.jobs")
        scheduler = importlib.import_module("cron.scheduler")
        with self.native.home_scope(profile) as home, jobs.use_cron_store(home):
            source: object = scheduler.create_job_with_scheduler_registration(
                prompt=prompt, schedule=schedule, name=name, paused=paused
            )
        result = object_json(source)
        return self.native.cron_job(str(result["id"]), profile)

    def cron_set_paused(self, profile: str, job_id: str, *, paused: bool) -> Object:
        """Pause or resume an existing job within the selected native profile store."""
        self.native.config.require_writes()
        self.native.cron_job(job_id, profile)
        jobs = importlib.import_module("cron.jobs")
        with self.native.home_scope(profile) as home, jobs.use_cron_store(home):
            result: object = (
                jobs.pause_job(job_id, reason="User request via MCP") if paused else jobs.resume_job(job_id)
            )
        if result is None:
            msg = "job_not_found"
            raise BridgeError(msg, "Native cron job no longer exists")
        return self.native.cron_job(job_id, profile)
