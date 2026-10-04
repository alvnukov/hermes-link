# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Scoped state ETags and transaction-local fences over native Kanban storage.

Tokens include the native event sequence high-water, so normal audited writers
cannot restore an old token through an ABA edit or subsequent event retention.
An unrelated board audit write conservatively invalidates the token. Arbitrary
SQL ABA without audit, database restoration and sequence tampering are outside
this state ETag contract; no durable version counter is invented by the bridge.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .core import BridgeError, Json, json_value

if TYPE_CHECKING:
    from collections.abc import Iterator

_TOKEN = re.compile(r"kr1_[0-9a-f]{64}\Z")
_CONFLICT = "mcp_revision_conflict"
_READ = (
    "SELECT t.*, "
    "(SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=t.id) AS __mcp_event_cursor, "
    "(SELECT COALESCE(MAX(seq),0) FROM sqlite_sequence WHERE name='task_events') AS __mcp_event_high_water "
    "FROM tasks AS t WHERE t.id=?"
)


def validate_expected(expected: str | None) -> None:
    """Reject malformed preconditions without opening a writable native store."""
    if expected is not None and _TOKEN.fullmatch(expected) is None:
        msg = "invalid_input"
        raise BridgeError(msg, "expected_revision must be an opaque Kanban revision from a task read")


def _cell(value: object) -> Json:
    """Preserve SQLite value identity when hashing even damaged native text."""
    return {"sqlite_blob": value.hex()} if isinstance(value, bytes) else json_value(value)


def _database_identity(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and isinstance(row[2], str) and row[2]:
            return str(row[2])
    msg = "kanban_revision_unavailable"
    raise BridgeError(msg, "Kanban revisions require the native persistent board database")


def snapshot(conn: sqlite3.Connection, task_id: str, board: str) -> tuple[sqlite3.Row, str]:
    """Read the task preimage and its scoped ETag in one SQLite statement."""
    row = conn.execute(_READ, (task_id,)).fetchone()
    if row is None:
        msg = "task_not_found"
        raise BridgeError(msg, "Native Kanban task does not exist")
    columns = row.keys()
    values = {key: _cell(row[key]) for key in columns}
    encoded = json.dumps(
        {"database": _database_identity(conn), "board": board, "task_id": task_id, "state": values},
        sort_keys=True,
    ).encode()
    return row, "kr1_" + hashlib.sha256(encoded).hexdigest()


def revision(conn: sqlite3.Connection, task_id: str, board: str) -> str:
    """Return only the opaque revision of one atomically observed native task."""
    return snapshot(conn, task_id, board)[1]


def _conflict() -> BridgeError:
    return BridgeError(
        "revision_conflict",
        "Kanban state changed since expected_revision; read the task again",
        {"applied_fields": [], "state_changed": False, "compare_and_swap": True},
    )


@dataclass(slots=True)
class MutationGuard:
    """One-operation fence; native helpers retain their own transaction ownership."""

    conn: sqlite3.Connection
    task_id: str
    board: str
    expected: str | None
    revision_before: str
    validated: bool = False

    def check(self) -> int:
        """Compare the first write's actual preimage inside the native transaction."""
        if self.validated:
            return 1
        if not self.conn.in_transaction or self.expected is None:
            return 0
        try:
            current = revision(self.conn, self.task_id, self.board)
        except BridgeError:
            return 0
        if not hmac.compare_digest(current, self.expected):
            return 0
        # Subsequent statements and post-commit cleanup belong to the same native
        # operation. Rechecking the original ETag would reject our own audit write.
        self.validated = True
        return 1

    def install(self) -> None:
        """Install only TEMP triggers on this connection, never durable schema."""
        self.conn.create_function("mcp_revision_target", 0, lambda: self.task_id)
        self.conn.create_function("mcp_revision_check", 0, self.check)
        self.conn.execute(
            "CREATE TEMP TRIGGER mcp_revision_task BEFORE UPDATE ON main.tasks "
            "WHEN OLD.id=mcp_revision_target() AND mcp_revision_check()=0 "
            "BEGIN SELECT RAISE(ABORT, 'mcp_revision_conflict'); END"
        )
        self.conn.execute(
            "CREATE TEMP TRIGGER mcp_revision_event BEFORE INSERT ON main.task_events "
            "WHEN NEW.task_id=mcp_revision_target() AND mcp_revision_check()=0 "
            "BEGIN SELECT RAISE(ABORT, 'mcp_revision_conflict'); END"
        )

    def remove(self) -> None:
        """Release the connection-local fence after callbacks have completed."""
        self.conn.execute("DROP TRIGGER IF EXISTS temp.mcp_revision_task")
        self.conn.execute("DROP TRIGGER IF EXISTS temp.mcp_revision_event")
        self.conn.create_function("mcp_revision_target", 0, None)
        self.conn.create_function("mcp_revision_check", 0, None)


@contextmanager
def mutation_guard(
    conn: sqlite3.Connection, task_id: str, board: str, expected: str | None
) -> Iterator[MutationGuard]:
    """Fence a native operation without holding an outer transaction over it."""
    validate_expected(expected)
    before = revision(conn, task_id, board)
    if expected is not None and not hmac.compare_digest(before, expected):
        raise _conflict()
    guard = MutationGuard(conn, task_id, board, expected, before)
    try:
        if expected is not None:
            guard.install()
        yield guard
    except sqlite3.IntegrityError as error:
        if str(error) == _CONFLICT:
            raise _conflict() from error
        raise
    finally:
        if expected is not None:
            guard.remove()
