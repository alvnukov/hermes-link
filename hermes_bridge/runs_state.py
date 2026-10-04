# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Native session persistence and conservative process ownership checks."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from .core import BridgeError

if TYPE_CHECKING:
    from .core import Object
    from .native import Native


def ensure_session(native: Native, profile: str, session_id: str) -> None:
    """Persist a native session before its ID is accepted by an MCP caller."""
    state = importlib.import_module("hermes_state")
    db = state.SessionDB(db_path=native.home(profile) / "state.db")
    try:
        if db.get_session(session_id) is None:
            db.create_session(session_id, "mcp-bridge", profile_name=profile)
    finally:
        db.close()


def session_fields(native: Native, profile: str, status: Object) -> None:
    """Report only an actually retained native session, including legacy tasks."""
    session_id = status.get("session_id")
    persisted = False
    if isinstance(session_id, str):
        try:
            native.session(session_id, profile)
            persisted = True
        except BridgeError as error:
            if error.code != "session_not_found":
                raise
    status["session_persisted"] = persisted
    status["session_retained"] = persisted
    if not persisted:
        status["session_id"] = None
        reported_error = status.get("error")
        if isinstance(reported_error, dict) and reported_error.get("code") == "owner_exited":
            status["error"] = {"code": "owner_exited", "message": "Execution owner exited"}


def owner_alive(pid: object, started: object) -> bool | None:
    """Distinguish a confirmed exit/PID reuse from an unavailable process probe."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    process = importlib.import_module("gateway.status")
    current: object = process.get_process_start_time(pid)
    if current is not None and started:
        return bool(current == started)
    # The native helper handles absent PIDs, zombies and permission failures on
    # all supported platforms; an unavailable start token cannot prove PID reuse.
    exists: object = process._pid_exists(pid)  # noqa: SLF001 — native cross-platform liveness helper
    return None if exists else False
