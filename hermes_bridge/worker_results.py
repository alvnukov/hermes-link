# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Safe projections of durable native Kanban attempt handoffs and diagnostics."""

from __future__ import annotations

import importlib
import json
import sqlite3
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, unquote, urlsplit

from .core import BridgeError, Json, Object, json_value, object_json, project, safe_id
from .settings import Settings
from .settings_history import read_state, safe_path

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from .native import Native

_TEXT_LIMIT = 65_536
_DIAGNOSTIC_LIMIT = 4096
_UNAVAILABLE = "[redaction-unavailable]"
_EXIT_KINDS = frozenset(
    {"clean_exit", "nonzero_exit", "signaled", "unknown", "rate_limited", "terminal_provider"}
)
_FAILED_STATUSES = frozenset({"crashed", "failed", "timeout", "timed_out"})
_TASK_TEXT_FIELDS = ("title", "body", "result", "last_failure_error", "completion_contract")


def _url_secrets(settings: Settings, value: str) -> set[str]:
    """Extract encoded credential components from an already-classified URL."""
    url = urlsplit(value)
    found = {unquote(item) for item in (url.username, url.password) if item}
    for key, item in parse_qsl(url.query):
        if settings._is_secret(key, item):  # noqa: SLF001 - One existing credential-name classifier.
            found.add(item)
    return found


def _credential_parts(settings: Settings, value: str) -> set[str]:
    """Find scalar values in classified argv, header, bearer, and URL credentials."""
    found = {value}
    name, equal, tail = value.partition("=")
    if equal and name.startswith("-"):
        found.update(_credential_parts(settings, tail))
    header, colon, tail = value.partition(":")
    if colon and settings._is_secret(header.strip(), tail):  # noqa: SLF001 - Shared header classifier.
        found.update(_credential_parts(settings, tail.strip()))
    scheme, space, token = value.partition(" ")
    if space and scheme.lower() in {"bearer", "basic"}:
        found.add(token.strip())
    if "://" in value:
        found.update(_url_secrets(settings, value))
    return {item for item in found if item}


def _container_secrets(settings: Settings, value: str) -> set[str]:
    """Include values inside native JSON credential containers such as A2A_PEER_TOKENS."""
    try:
        parsed = json_value(json.loads(value))
    except (ValueError, BridgeError):
        return _credential_parts(settings, value)
    if isinstance(parsed, (dict, list)):
        return {value} | _secret_strings(settings, parsed, secret=True)
    return _credential_parts(settings, value)


def _secret_strings(settings: Settings, value: Json, *, secret: bool = False) -> set[str]:
    """Collect exact persisted secrets with the settings boundary's existing classifier."""
    if isinstance(value, str):
        return _container_secrets(settings, value) if secret and value else set()
    if secret and isinstance(value, (int, float, bool)):
        return {str(value), json.dumps(value)}
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            classified = (
                settings._is_secret(key, item)  # noqa: SLF001 - Reuse the export/apply secret classifier.
                or settings._credential_value(item)  # noqa: SLF001 - Reuse native argv/header/URL classification.
            )
            found.update(_secret_strings(settings, item, secret=secret or classified))
    elif isinstance(value, list):
        for item in value:
            found.update(_secret_strings(settings, item, secret=secret))
    return found


def _clean_text(redactor: ModuleType, secrets: tuple[str, ...], value: str) -> str:
    """Apply exact-value and native unconditional redaction before limiting any text."""
    text = value
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    text = redactor.redact_sensitive_text(text, force=True, redact_url_credentials=True)
    source: object = redactor.redact_for_egress(text)
    if not isinstance(source, str) or source == _UNAVAILABLE:
        msg = "redaction_unavailable"
        raise BridgeError(msg, "Native redaction is unavailable")
    return source


class WorkerResults:
    """Expose native attempt evidence without inferring linkage or reading task log files."""

    def __init__(self, native: Native) -> None:
        """Retain the native reader and its existing profile policy."""
        self.native = native

    def project(self, raw: Object, fields: tuple[str, ...]) -> Object:
        """Project metadata, sanitized handoff text, and a verified session pointer."""
        result = project(raw, fields)
        result.update(
            {
                "summary": None,
                "error": None,
                "exit_code": None,
                "exit_kind": None,
                "last_output": None,
                "worker_session_id": None,
                "session_link_status": "profile_unavailable",
                "redaction_status": "profile_unavailable",
                "text_truncated": False,
            }
        )
        profile = raw.get("profile")
        if not isinstance(profile, str):
            return result
        metadata = raw.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            with self.native.home_scope(profile) as home:
                result.update(self._scoped(raw, metadata, profile, home))
        except (BridgeError, OSError):
            # Missing historical profiles must not fail the entire task history or leak its text.
            return result
        return result

    def _scoped(self, raw: Object, metadata: Object, profile: str, home: Path) -> Object:
        session_id, link_status = self._link(metadata.get("worker_session_id"), profile, home, raw)
        result = self._redacted(raw, metadata, profile)
        exit_code = metadata.get("exit_code")
        exit_kind = metadata.get("exit_kind")
        result.update(
            {
                "worker_session_id": session_id,
                "session_link_status": link_status,
                "exit_code": (
                    exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None
                ),
                "exit_kind": exit_kind if isinstance(exit_kind, str) and exit_kind in _EXIT_KINDS else None,
            }
        )
        return result

    def _link(self, candidate: Json, profile: str, home: Path, raw: Object) -> tuple[str | None, str]:
        if candidate is None:
            return None, "unavailable"
        if not isinstance(candidate, str):
            return None, "invalid"
        try:
            safe_id(candidate)
        except BridgeError:
            return None, "invalid"
        return self._read_link(candidate, profile, home, raw)

    @staticmethod
    def _read_link(candidate: str, profile: str, home: Path, raw: Object) -> tuple[str | None, str]:
        path = home / "state.db"
        try:
            safe_path(path)
            if not path.is_file():
                return None, "not_found"
            state = importlib.import_module("hermes_state")
            db = state.SessionDB(db_path=path, read_only=True)
            try:
                session = object_json(db.get_session(candidate) or {})
            finally:
                db.close()
        except (BridgeError, OSError, sqlite3.Error):
            return None, "storage_unavailable"
        if not session:
            return None, "not_found"
        if session.get("source") != "kanban" or session.get("profile_name") != profile:
            return None, "scope_mismatch"
        for key, expected in (("task_id", raw.get("task_id")), ("run_id", raw.get("id"))):
            if session.get(key) is not None and session[key] != expected:
                return None, "scope_mismatch"
        return candidate, "persisted"

    def _redacted(self, raw: Object, metadata: Object, profile: str) -> Object:
        values: Object = {
            "summary": raw.get("summary"),
            "error": raw.get("error"),
            "last_output": metadata.get("worker_output"),
        }
        return self._safe_fields(values, (profile,))

    def task(self, task: Object, runs: list[Object]) -> Object:
        """Sanitize card prose against its allowed current and historical native profile owners."""
        values = {key: task.get(key) for key in _TASK_TEXT_FIELDS if key in task}
        profiles = {self.native.config.board_profile}
        assignee = task.get("assignee")
        if isinstance(assignee, str):
            profiles.add(assignee)
        for run in runs:
            profile = run.get("profile")
            # Native zero-duration terminal records on never-claimed cards
            # have no worker profile; their prose uses the board owner's scope.
            if (
                profile is None
                and run.get("outcome") in ("completed", "blocked", "review_requested", "scheduled")
                and run.get("status") == run.get("outcome")
                and run.get("ended_at") is not None
                and run.get("started_at") == run.get("ended_at")
                and run.get("claim_lock") is None
                and run.get("worker_pid") is None
                and run.get("last_heartbeat_at") is None
            ):
                continue
            if not isinstance(profile, str):
                return {
                    **task,
                    **self._withheld(values),
                    "text_redaction_status": "profile_unavailable",
                    "text_truncated": False,
                }
            profiles.add(profile)
        safe = self._safe_fields(values, tuple(sorted(profiles)))
        status = safe.pop("redaction_status")
        truncated = safe.pop("text_truncated")
        return {**task, **safe, "text_redaction_status": status, "text_truncated": truncated}

    @staticmethod
    def _withheld(values: Object) -> Object:
        return {key: _UNAVAILABLE if isinstance(value, str) else None for key, value in values.items()}

    def _safe_fields(self, values: Object, profiles: tuple[str, ...]) -> Object:
        values = dict(values)
        try:
            redactor = importlib.import_module("agent.redact")
            for profile in profiles:
                with self.native.home_scope(profile) as home:
                    secrets = self._persisted_secrets(home)
                    values = {
                        key: _clean_text(redactor, secrets, value) if isinstance(value, str) else None
                        for key, value in values.items()
                    }
            truncated = False
            for key, value in values.items():
                if isinstance(value, str):
                    limit = _DIAGNOSTIC_LIMIT if key in {"error", "last_output"} else _TEXT_LIMIT
                    truncated = truncated or len(value) > limit
                    values[key] = value[:limit]
        except BridgeError as exc:
            status = (
                "profile_unavailable" if exc.code in {"access_denied", "profile_not_found"} else "unavailable"
            )
            return {**self._withheld(values), "redaction_status": status, "text_truncated": False}
        except Exception:  # noqa: BLE001 - Every native/import failure must close this text egress boundary.
            # This is an egress boundary: even third-party redactor/import failures fail closed.
            return {**self._withheld(values), "redaction_status": "unavailable", "text_truncated": False}
        else:
            return {**values, "redaction_status": "applied", "text_truncated": truncated}

    def _persisted_secrets(self, home: Path) -> tuple[str, ...]:
        settings = Settings(self.native)
        snapshot = read_state(home)
        scope = importlib.import_module("agent.secret_scope")
        # Native load_env_file hides unreadable files; parse our strictly read snapshot instead.
        decoded = scope._decode_env_bytes(snapshot.env or b"")  # noqa: SLF001 - Native .env decoding.
        env = object_json(scope._parse_env_text(decoded))  # noqa: SLF001 - Native .env assignment tokenizer.
        config = settings._read_config(home / "config.yaml")  # noqa: SLF001 - Existing safe read boundary.
        effective = settings._effective(config)  # noqa: SLF001 - Include native managed secret overrides.
        found = _secret_strings(settings, env) | _secret_strings(settings, effective)
        return tuple(sorted(found, key=len, reverse=True))

    @staticmethod
    def result(run: Object) -> Object:
        """Normalize exact saved handoff evidence; per-session usage is not attempt usage."""
        message = run.get("error")
        exit_kind = run.get("exit_kind")
        status = run.get("status")
        error: Json = None
        if message or (isinstance(status, str) and status in _FAILED_STATUSES):
            kind = exit_kind if isinstance(exit_kind, str) else "failed"
            error = {"code": f"worker_{kind}", "message": message}
        output = run.get("summary") if run.get("redaction_status") == "applied" else None
        return {
            "run_id": run.get("id"),
            "task_id": run.get("task_id"),
            "profile": run.get("profile"),
            "status": status,
            "outcome": run.get("outcome"),
            "session_id": run.get("worker_session_id"),
            "worker_session_id": run.get("worker_session_id"),
            "session_link_status": run.get("session_link_status"),
            "output": output,
            "output_source": "native_run_summary" if output is not None else None,
            "usage": None,
            "usage_scope": "unavailable",
            "error": error,
            "redaction_status": run.get("redaction_status"),
            "text_truncated": run.get("text_truncated"),
        }
