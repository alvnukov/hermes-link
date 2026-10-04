# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Read native Hermes registries and stores through allowlisted metadata projections."""

from __future__ import annotations

import base64
import importlib
import json
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, kanban_revision
from .core import (
    BridgeError,
    Config,
    Json,
    Object,
    bounded,
    json_value,
    object_json,
    project,
    rows,
    safe_id,
)
from .worker_results import WorkerResults

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_CURSOR_LENGTH = 2048
ACTIVE_SESSION_SECONDS = 300

SESSION_FIELDS = (
    "id",
    "source",
    "model",
    "started_at",
    "ended_at",
    "last_active",
    "message_count",
    "parent_session_id",
    "cwd",
    "archived",
    "pinned",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "project_id",
    "task_id",
    "run_id",
    "session_key",
    "compression_parent_id",
)
CRON_FIELDS = (
    "id",
    "name",
    "enabled",
    "state",
    "schedule_display",
    "next_run_at",
    "last_run_at",
    "created_at",
    "updated_at",
    "paused_at",
    "model",
    "provider",
    "run_count",
    "paused_reason",
)
WORKER_FIELDS = (
    "run_id",
    "task_id",
    "task_status",
    "task_assignee",
    "profile",
    "worker_pid",
    "started_at",
    "claim_expires",
    "last_heartbeat_at",
    "max_runtime_seconds",
)


def encode_cursor(scope: dict[str, str], position: int) -> str:
    """Encode a durable cursor bound to its requested native scope."""
    return (
        base64.urlsafe_b64encode(json.dumps({"scope": scope, "position": position}, sort_keys=True).encode())
        .decode()
        .rstrip("=")
    )


def decode_cursor(cursor: str, scope: dict[str, str]) -> int:
    """Validate a cursor and recover its logical event position."""
    if not cursor:
        return 0
    if len(cursor) > MAX_CURSOR_LENGTH:
        msg = "invalid_input"
        raise BridgeError(msg, "Event cursor too large")
    try:
        value = object_json(
            json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        )
    except (ValueError, UnicodeError, BridgeError) as exc:
        code = "invalid_input"
        raise BridgeError(code, "Event cursor is invalid or belongs to another scope") from exc
    position = value.get("position")
    if (
        value.get("scope") != scope
        or not isinstance(position, int)
        or isinstance(position, bool)
        or not 0 <= position < 2**63
    ):
        code = "invalid_input"
        raise BridgeError(code, "Event cursor is invalid or belongs to another scope")
    return position


class Native:
    """Imports Hermes's own readers; never maintains a shadow registry or store."""

    def __init__(self, config: Config) -> None:
        """Bind the configured native checkout without constructing shadow stores."""
        self.config = config
        root = str(config.hermes_repo)
        if root not in sys.path:
            sys.path.insert(0, root)
        if not (config.hermes_repo / "mcp_serve.py").is_file():
            msg = "hermes_unavailable"
            raise BridgeError(msg, "Hermes repository with native MCP server was not found")
        self.profiles_module = importlib.import_module("hermes_cli.profiles")
        self.constants = importlib.import_module("hermes_constants")

    def profile_rows(self) -> list[Object]:
        """Project allowlisted profiles from the native registry."""
        result: list[Object] = []
        for profile in self.profiles_module.list_profiles(lazy_skill_count=True):
            name = str(profile.name)
            if "*" not in self.config.profiles and name not in self.config.profiles:
                continue
            result.append(
                {
                    key: json_value(getattr(profile, key, None))
                    for key in (
                        "name",
                        "model",
                        "provider",
                        "role",
                        "is_default",
                        "gateway_running",
                        "description",
                        "display_name",
                        "skill_count",
                    )
                }
            )
        return result

    def profile(self, name: str) -> Object:
        """Read one allowlisted profile or reject a missing registry entry."""
        self.config.profile(name)
        for item in self.profile_rows():
            if item.get("name") == name:
                return item
        msg = "profile_not_found"
        raise BridgeError(msg, "Native profile does not exist")

    def home(self, profile: str) -> Path:
        """Resolve an existing allowlisted native profile directory."""
        self.config.profile(profile)
        if not self.profiles_module.profile_exists(profile):
            msg = "profile_not_found"
            raise BridgeError(msg, "Native profile does not exist")
        value: object = self.profiles_module.get_profile_dir(profile)
        if not isinstance(value, Path):
            msg = "invalid_response"
            raise BridgeError(msg, "Native profile path is invalid")
        return value

    @contextmanager
    def home_scope(self, profile: str) -> Iterator[Path]:
        """Bind the native home for this context and restore it on exit."""
        home = self.home(profile)
        token = self.constants.set_hermes_home_override(home)
        try:
            yield home
        finally:
            self.constants.reset_hermes_home_override(token)

    @contextmanager
    def board_connection(self, board: str, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open an allowlisted native board with explicit read or write intent."""
        self.config.board(board)
        kanban = importlib.import_module("hermes_cli.kanban_db")
        with self.home_scope(self.config.board_profile):
            path = Path(kanban.kanban_db_path(board=board))
            if write:
                self.config.require_writes()
                connect = importlib.import_module("hermes_cli.kanban_db_connect")
                conn = connect.connect(board=board)
            else:
                if not path.is_file():
                    msg = "kanban_unavailable"
                    raise BridgeError(msg, "Native board store does not exist")
                conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
                conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    def info(self, profile: str = "default") -> Object:
        """Project native runtime health without exporting rendezvous credentials."""
        status = importlib.import_module("gateway.status")
        rendezvous = importlib.import_module("gateway.host_rendezvous")
        version = importlib.import_module("hermes_cli")
        with self.home_scope(profile):
            raw: object = status.read_runtime_status()
            live: object = status.runtime_status_pid_is_live(raw)
            record: object = rendezvous.read_record("desktop-serve")
        return object_json(
            {
                "source": "native_hermes",
                "extension_version": __version__,
                "version": str(getattr(version, "__version__", "unknown")),
                "profile": profile,
                "desktop_available": record is not None,
                "gateway_live": bool(live),
                "runtime": project(
                    raw or {},
                    (
                        "gateway_state",
                        "gateway_mode",
                        "updated_at",
                        "pid",
                        "state",
                        "active_agents",
                        "active_sessions",
                    ),
                ),
            }
        )

    def capabilities(self) -> Object:
        """Describe the configured tool policy and native subsystem boundaries."""
        return object_json(
            {
                "source": "native_hermes",
                "management_transport": "python_imports",
                "profiles": list(self.config.profiles),
                "boards": list(self.config.boards),
                "writes_enabled": self.config.writes,
                "auth_enabled": self.config.auth_enabled,
                "disabled_tools": list(self.config.disabled_tools),
                "subsystems": [
                    "profiles",
                    "sessions",
                    "cron",
                    "workspaces",
                    "kanban",
                    "settings",
                    "memory_metadata",
                ],
                "events": {
                    "supported": ["kanban", "session"],
                    "runtime_journal": "unsupported",
                    "durable": True,
                    "scope_bound_cursors": True,
                },
                "security": {
                    "secrets": False,
                    "environment": False,
                    "persisted_environment": "redacted_settings_only",
                    "arbitrary_files": False,
                    "memory_contents": False,
                    "approval_bypass": False,
                },
            }
        )

    def assignees(self, board: str) -> Object:
        """List native profile and Kanban assignees within the profile allowlist."""
        kanban = importlib.import_module("hermes_cli.kanban_db")
        with self.board_connection(board) as conn:
            source: object = kanban.known_assignees(conn)
            items = [project(item, ("name", "on_disk", "counts")) for item in rows(json_value(source))]
        items = [x for x in items if "*" in self.config.profiles or x.get("name") in self.config.profiles]
        return object_json(
            {
                "source": "native_kanban",
                "board": board,
                "assignees": items,
                "meaning": "Profiles on disk union task assignees; not running workers",
            }
        )

    def workers(self, board: str) -> Object:
        """Read active native worker leases without claiming process liveness."""
        with self.board_connection(board) as conn:
            found = conn.execute(
                "SELECT r.id AS run_id, r.task_id, t.status AS task_status, "
                "t.assignee AS task_assignee, r.profile, r.worker_pid, r.started_at, "
                "r.claim_expires, r.last_heartbeat_at, r.max_runtime_seconds FROM task_runs r "
                "JOIN tasks t ON t.id=r.task_id WHERE r.ended_at IS NULL AND t.status='running' "
                "AND r.worker_pid IS NOT NULL ORDER BY r.started_at LIMIT 100"
            ).fetchall()
        items = [project(dict(row), WORKER_FIELDS) for row in found]
        return object_json(
            {
                "source": "native_kanban",
                "board": board,
                "workers": items,
                "count": len(items),
                "checked_at": int(time.time()),
                "meaning": "Native active leases; PID liveness is not inferred",
            }
        )

    def worker(self, run_id: int, board: str) -> Object:
        """Read metadata for a native worker attempt by its durable ID."""
        bounded(run_id, 1, 2**63 - 1)
        kanban = importlib.import_module("hermes_cli.kanban_db")
        with self.board_connection(board) as conn:
            run = kanban.get_run(conn, run_id)
            if run is None:
                msg = "worker_not_found"
                raise BridgeError(msg, "Native worker run does not exist")
            result = WorkerResults(self).project(
                object_json(asdict(run)), (*WORKER_FIELDS, "id", "ended_at", "status", "outcome")
            )
        return object_json({"source": "native_kanban", "run": result, "result": WorkerResults.result(result)})

    def kanban_boards(self) -> Object:
        """List allowlisted boards from the native Desktop registry."""
        kanban = importlib.import_module("hermes_cli.kanban_db")
        with self.home_scope(self.config.board_profile):
            source: object = kanban.list_boards(include_archived=False)
        items = [
            project(x, ("slug", "name", "archived", "project_id"))
            for x in rows(json_value(source))
            if x.get("slug") in self.config.boards
        ]
        return object_json({"boards": items, "profile": self.config.board_profile})

    def kanban_get(self, board: str, limit: int = 100) -> Object:
        """Read native tasks and aggregate statistics for one board."""
        bounded(limit, 1, 100)
        kanban = importlib.import_module("hermes_cli.kanban_db")
        with self.board_connection(board) as conn:
            conn.execute("BEGIN")
            items: list[Object] = []
            for task in kanban.list_tasks(conn, limit=limit):
                raw_runs = [object_json(asdict(run)) for run in kanban.list_runs(conn, str(task.id))]
                item = WorkerResults(self).task(object_json(asdict(task)), raw_runs)
                item["revision"] = kanban_revision.revision(conn, str(task.id), board)
                items.append(item)
            stats: object = kanban.board_stats(conn)
        return object_json(
            {"source": "native_kanban", "board": board, "tasks": items, "stats": object_json(stats)}
        )

    def kanban_task(self, task_id: str, board: str) -> Object:
        """Read a task and projected worker attempts from its native board."""
        safe_id(task_id)
        kanban = importlib.import_module("hermes_cli.kanban_db")
        with self.board_connection(board) as conn:
            conn.execute("BEGIN")
            task = kanban.get_task(conn, task_id)
            if task is None:
                msg = "task_not_found"
                raise BridgeError(msg, "Native Kanban task does not exist")
            raw_runs = [object_json(asdict(run)) for run in kanban.list_runs(conn, task_id)]
            task_data = WorkerResults(self).task(object_json(asdict(task)), raw_runs)
            task_data["revision"] = kanban_revision.revision(conn, task_id, board)
            return object_json(
                {
                    "board": board,
                    "task": task_data,
                    "runs": [
                        WorkerResults(self).project(
                            raw, (*WORKER_FIELDS, "id", "ended_at", "status", "outcome")
                        )
                        for raw in raw_runs
                    ],
                }
            )

    def sessions(self, profile: str, limit: int, offset: int, *, active_only: bool) -> Object:
        """Page native metadata and optionally select recent unended sessions."""
        bounded(limit, 1, 100)
        bounded(offset, 0, 100000)
        home = self.home(profile)
        path = home / "state.db"
        if not path.is_file():
            return object_json(
                {
                    "source": "native_session_db",
                    "profile": profile,
                    "sessions": [],
                    "storage": "not_created",
                    "next_offset": None,
                }
            )
        state = importlib.import_module("hermes_state")
        db = state.SessionDB(db_path=path, read_only=True)
        try:
            source: object = db.list_sessions_rich(
                limit=limit, offset=offset, order_by_last_active=True, compact_rows=True
            )
            items = [project(item, SESSION_FIELDS) for item in rows(json_value(source))]
            for item in items:
                last = item.get("last_active") or item.get("started_at")
                item["is_active"] = (
                    item.get("ended_at") is None
                    and isinstance(last, (int, float))
                    and time.time() - last < ACTIVE_SESSION_SECONDS
                )
                item["profile"] = profile
            return object_json(
                {
                    "source": "native_session_db",
                    "profile": profile,
                    "sessions": [x for x in items if not active_only or x["is_active"]],
                    "activity_definition": "unended and active within 300 seconds; not an execution lease",
                    "next_offset": offset + limit if len(items) == limit else None,
                }
            )
        finally:
            db.close()

    def session(self, session_id: str, profile: str) -> Object:
        """Read native session metadata without transcript content."""
        safe_id(session_id)
        path = self.home(profile) / "state.db"
        if not path.is_file():
            msg = "session_not_found"
            raise BridgeError(msg, "Native session store does not exist")
        state = importlib.import_module("hermes_state")
        db = state.SessionDB(db_path=path, read_only=True)
        try:
            source: object = db.get_session(session_id)
            if source is None:
                msg = "session_not_found"
                raise BridgeError(msg, "Native session does not exist")
            return object_json({"profile": profile, **project(source, SESSION_FIELDS)})
        finally:
            db.close()

    def cron(self, profile: str) -> Object:
        """Read native cron metadata without invoking store repair."""
        jobs_module = importlib.import_module("cron.jobs")
        with self.home_scope(profile) as home, jobs_module.use_cron_store(home):
            # Native repair-free reader: list_jobs/load_jobs may repair and save the store.
            source: object = jobs_module._peek_jobs_unlocked()  # noqa: SLF001 — Native repair-free reader has no public equivalent.
        if source is None:
            msg = "storage_corrupt"
            raise BridgeError(msg, "Native cron store is unreadable; no automatic repair performed")
        jobs = [project(item, CRON_FIELDS) for item in rows(json_value(source))]
        return object_json({"source": "native_cron", "profile": profile, "jobs": jobs, "count": len(jobs)})

    def cron_job(self, job_id: str, profile: str) -> Object:
        """Find one scheduled job in the repair-free native projection."""
        safe_id(job_id)
        for job in rows(self.cron(profile)["jobs"]):
            if job.get("id") == job_id:
                return object_json({"profile": profile, **job})
        msg = "job_not_found"
        raise BridgeError(msg, "Native cron job does not exist")

    def workspaces(self, profile: str) -> Object:
        """Read native project records and cached repositories without scanning."""
        projects = importlib.import_module("hermes_cli.projects_db")
        with self.home_scope(profile):
            path = Path(projects.projects_db_path())
            if not path.is_file():
                return object_json(
                    {"source": "native_projects_db", "profile": profile, "projects": [], "repos": []}
                )
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                items = [object_json(x.to_dict()) for x in projects.list_projects(conn)]
                repos: object = projects.list_discovered_repos(conn)
            finally:
                conn.close()
        return object_json(
            {
                "source": "native_projects_db",
                "profile": profile,
                "projects": items,
                "repos": json_value(repos),
                "scan_performed": False,
            }
        )

    def workspace(self, workspace_id: str, profile: str) -> Object:
        """Resolve a project or cached repository by native identity."""
        data = self.workspaces(profile)
        for item in rows(data["projects"]) + rows(data["repos"]):
            if workspace_id in (item.get("id"), item.get("slug"), item.get("path"), item.get("primary_path")):
                return object_json({"profile": profile, "workspace": item})
        msg = "workspace_not_found"
        raise BridgeError(msg, "Native workspace identity does not exist")

    def memory_status(self, profile: str) -> Object:
        """Project native memory provider availability without memory contents."""
        profile_scope = importlib.import_module("hermes_cli.web_server_profiles")
        providers = importlib.import_module("hermes_cli.web_server_memory")
        self.home(profile)
        with profile_scope._config_profile_scope(profile):  # noqa: SLF001 — Native context-local credential scope prevents profile leakage.
            source: object = providers._discover_memory_provider_statuses()  # noqa: SLF001 — Native registry discovery is the authoritative provider inventory.
            config_module = importlib.import_module("hermes_cli.config")
            native_config = object_json(config_module.load_config())
        memory = native_config.get("memory")
        selected = memory.get("provider") if isinstance(memory, dict) else None
        items = [
            project(item, ("name", "available", "configured", "status")) for item in rows(json_value(source))
        ]
        return object_json(
            {
                "source": "native_memory_registry",
                "profile": profile,
                "selected_provider": selected,
                "providers": items,
                "hindsight": next(
                    (item for item in items if item.get("name") == "hindsight"),
                    object_json({"available": False, "status": "not_registered"}),
                ),
                "contents_exported": False,
            }
        )

    def _kanban_events(
        self, profile: str, board: str, task_id: str, session_id: str, position: int, cursor: str, limit: int
    ) -> list[Object]:
        self.config.board(board)
        if profile != self.config.board_profile or session_id:
            msg = "invalid_input"
            raise BridgeError(msg, "Kanban events require the configured board profile and no session_id")
        if task_id:
            safe_id(task_id)
        kanban = importlib.import_module("hermes_cli.kanban_db")
        with self.home_scope(profile):
            path = Path(kanban.kanban_db_path(board=board))
        if not path.is_file():
            msg = "kanban_unavailable"
            raise BridgeError(msg, "Native board store does not exist")
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            conn.row_factory = sqlite3.Row
            clause = " AND task_id = ?" if task_id else ""
            params: list[str | int] = [position]
            if task_id:
                params.append(task_id)
            params.append(limit)
            order = "ASC" if cursor else "DESC"
            found = conn.execute(
                "SELECT id, task_id, run_id, kind, created_at FROM task_events "  # nosec B608 # noqa: S608 - fixed SQL fragments and bound values
                f"WHERE id > ?{clause} ORDER BY id {order} LIMIT ?",
                params,
            ).fetchall()
            items = [object_json(dict(row)) for row in found]
            if not cursor:
                items.reverse()
        finally:
            conn.close()
        return items

    def _session_events(
        self, profile: str, task_id: str, session_id: str, position: int, limit: int
    ) -> tuple[list[Object], Json]:
        if not session_id or task_id:
            msg = "invalid_input"
            raise BridgeError(msg, "Session events require session_id and no task_id")
        self.session(session_id, profile)
        state = importlib.import_module("hermes_state")
        timeline = importlib.import_module("hermes_state_timeline")
        db = state.SessionDB(db_path=self.home(profile) / "state.db", read_only=True)
        try:
            data = object_json(
                timeline.get_session_timeline(db, session_id, limit=limit, after_row_id=position)
            )
            items = [project(x, ("row_id", "timestamp")) for x in rows(data["entries"])]
            for item in items:
                item["kind"], item["session_id"] = "user_turn", session_id
            pagination = object_json(data["pagination"])
            session_position = pagination.get("next_cursor") if pagination.get("has_more") else position
            if items and not pagination.get("has_more"):
                # Compaction may copy a logical turn to a newer physical row.
                # Polling uses native display order even at the end of a page.
                with timeline._snapshot(db) as conn:  # noqa: SLF001 — Native snapshot keeps display-row lookup consistent.
                    timeline._register_functions(db, conn)  # noqa: SLF001 — Native display SQL requires its registered functions.
                    sql = timeline._display_rows_sql(conn, session_id, users_only=True)  # noqa: SLF001 — Native display order preserves compaction lineage semantics.
                    selected = conn.execute(
                        sql + " SELECT sort_id FROM display_rows WHERE row_id = :row_id",  # nosec B608 # noqa: S608 - native SQL builder with bound values
                        {"sid": session_id, "row_id": items[-1]["row_id"]},
                    ).fetchone()
                    if selected is None:
                        msg = "events_changed"
                        raise BridgeError(msg, "Session events changed; retry this cursor")
                    session_position = selected[0]
        finally:
            db.close()
        return items, json_value(session_position)

    def events(
        self, subsystem: str, profile: str, board: str, task_id: str, session_id: str, cursor: str, limit: int
    ) -> Object:
        """Read scoped durable events and return a cursor in native logical order."""
        bounded(limit, 1, 100)
        self.home(profile)
        scope = {
            "subsystem": subsystem,
            "profile": profile,
            "board": board,
            "task_id": task_id,
            "session_id": session_id,
        }
        position = decode_cursor(cursor, scope)
        if subsystem == "kanban":
            items = self._kanban_events(profile, board, task_id, session_id, position, cursor, limit)
            last = items[-1].get("id") if items else position
        elif subsystem == "session":
            items, last = self._session_events(profile, task_id, session_id, position, limit)
        else:
            msg = "unsupported_subsystem"
            raise BridgeError(
                msg,
                "Durable events support kanban and session; no generic runtime journal exists",
            )
        if not isinstance(last, int):
            msg = "invalid_response"
            raise BridgeError(msg, "Native event cursor is invalid")
        return object_json(
            {
                "source": "native_event_store",
                "scope": scope,
                "events": items,
                "cursor": encode_cursor(scope, last),
                "page_full": len(items) == limit,
                "payloads_exported": False,
            }
        )
