# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Conservative cleanup through native APIs, retaining their stores and lifecycle."""

from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

from .core import BridgeError, Object, json_value, object_json, rows, safe_id

if TYPE_CHECKING:
    from pathlib import Path

    from .native import Native


def _active(message: str) -> BridgeError:
    return BridgeError("resource_active", message)


def archive(native: Native, board: str, task_id: str) -> Object:
    """Archive exactly one idle card, rejecting a racing native worker claim."""
    native.config.require_writes()
    safe_id(task_id)
    kanban = importlib.import_module("hermes_cli.kanban_db")
    current = native.kanban_task(task_id, board)
    if object_json(current["task"])["status"] == "archived":
        return {**current, "already_archived": True}
    with native.board_connection(board, write=True) as conn:
        # Connection-local and absent from the durable schema. The condition runs
        # inside archive_task's own transaction, after any concurrent claim commits.
        # Native archive cannot expose this guard and otherwise terminates workers.
        conn.create_function("mcp_archive_target", 0, lambda: task_id)
        conn.execute(
            "CREATE TEMP TRIGGER mcp_archive_idle BEFORE UPDATE ON main.tasks "
            "WHEN OLD.id=mcp_archive_target() AND (OLD.status='running' OR EXISTS "
            "(SELECT 1 FROM task_runs WHERE task_id=OLD.id AND ended_at IS NULL)) "
            "BEGIN SELECT RAISE(ABORT, 'mcp_resource_active'); END"
        )
        try:
            changed: object = kanban.archive_task(conn, task_id)
        except sqlite3.IntegrityError as error:
            if str(error) == "mcp_resource_active":
                msg = "Active Kanban runs must finish before archiving"
                raise _active(msg) from error
            raise
        if not changed:
            latest = native.kanban_task(task_id, board)
            if object_json(latest["task"])["status"] != "archived":
                msg = "native_update_failed"
                raise BridgeError(msg, "Native Kanban archive refused")
    return {**native.kanban_task(task_id, board), "already_archived": False}


def delete_cron(native: Native, profile: str, job_id: str) -> Object:
    """Delete an exact paused job under the native dispatch fence; never a name alias."""
    native.config.require_writes()
    safe_id(job_id)
    native.cron_job(job_id, profile)  # includes repair-free corruption/exact-ID checks
    jobs = importlib.import_module("cron.jobs")
    scheduler = importlib.import_module("cron.scheduler")
    with (
        native.home_scope(profile) as home,
        jobs.use_cron_store(home),
        jobs._fire_job_lock(job_id) as acquired,  # noqa: SLF001 — native fail-closed fence has no public idle-job equivalent
    ):
        if not acquired:
            msg = "resource_busy"
            raise BridgeError(msg, "Native cron dispatch fence is unavailable")
        # Native removal accepts ID-or-name and takes the jobs lock only after
        # resolving that reference. Hold its reentrant writer lock across the
        # exact-ID check and removal so another writer cannot trigger alias fallback.
        # Match the native dispatch order: fire fence first, then jobs lock.
        with jobs._jobs_lock():  # noqa: SLF001 — native global writer serialization has no public equivalent
            source: object = jobs._peek_jobs_unlocked()  # noqa: SLF001 — native repair-free store reader
            if source is None:
                msg = "storage_corrupt"
                raise BridgeError(msg, "Native cron store is unreadable")
            job = next((item for item in rows(json_value(source)) if item.get("id") == job_id), None)
            if job is None:
                msg = "job_not_found"
                raise BridgeError(msg, "Native cron job no longer exists")
            if (
                job.get("enabled") is not False
                or job.get("fire_claim")
                or job.get("run_claim")
                or scheduler.is_job_running(job_id, home=home)
            ):
                msg = "Pause the cron job and wait for active claims to finish before deleting"
                raise _active(msg)
            removed: object = jobs.remove_job(job_id)
            if not removed:
                msg = "job_not_found"
                raise BridgeError(msg, "Native cron job no longer exists")
    return {"profile": profile, "job_id": job_id, "deleted": True}


def _profile_has_open_sessions(home: Path) -> bool:
    path = home / "state.db"
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        return conn.execute("SELECT 1 FROM sessions WHERE ended_at IS NULL LIMIT 1").fetchone() is not None


def _profile_has_workers(native: Native, name: str) -> bool:
    for board in native.config.boards:
        try:
            with native.board_connection(board) as conn:
                found = conn.execute(
                    "SELECT 1 FROM task_runs WHERE profile=? AND ended_at IS NULL LIMIT 1", (name,)
                ).fetchone()
                if found is not None:
                    return True
        except BridgeError as error:
            if error.code != "kanban_unavailable":
                raise
    return False


def delete_profile(native: Native, name: str) -> Object:
    """Delete only an explicit inactive named profile after native activity checks."""
    native.config.require_writes()
    native.config.profile(name)
    profiles = native.profiles_module
    protected = {"default", native.config.board_profile, profiles.current_profile_name()}
    protected.add(profiles.get_active_profile())
    if name in protected:
        msg = "Default, current, board-owner and active profiles cannot be deleted"
        raise _active(msg)
    home = native.home(name)
    try:
        active = (
            profiles._check_gateway_running(home)  # noqa: SLF001 — native deletion uses this authoritative gateway probe
            or profiles._profile_bound_backend_pids(name, home)  # noqa: SLF001 — native backend ownership check, without terminating anything
            or _profile_has_open_sessions(home)
            or _profile_has_workers(native, name)
        )
    except (OSError, sqlite3.Error) as error:
        code = "activity_unavailable"
        raise BridgeError(code, "Cannot verify native activity; profile deletion refused") from error
    if active:
        msg = "Profile has active native sessions or workers; stop them before deleting"
        raise _active(msg)
    profiles.delete_profile(name, yes=True)
    return {"profile": name, "deleted": True, "activity_check": "preflight; native API has no atomic lease"}
