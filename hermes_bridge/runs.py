# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Durable native run identity and bounded background execution."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .core import BridgeError, bounded, object_json, safe_id
from .runs_state import ensure_session, owner_alive, session_fields

if TYPE_CHECKING:
    from collections.abc import Callable

    from .core import Json, Object
    from .native import Native

TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
_TASK_ID_PARTS = 3
_TASK_NONCE_LENGTH = 32
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "api_calls",
    "estimated_cost_usd",
)


def _execution_response(result: object, session_id: str) -> Object:
    response = object_json(result)
    response["session_id"] = session_id
    usage: Object = {}
    for key in _USAGE_FIELDS:
        value = response.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[key] = value
    response["usage"] = usage or None
    return response


def _execution_error(error: BaseException | None = None) -> Object:
    if isinstance(error, SystemExit):
        return {
            "code": "native_runtime_exit",
            "message": "Native runtime stopped the execution thread",
            "exit_code": error.code if isinstance(error.code, int) else None,
        }
    return {
        "code": "native_execution_failed",
        "message": (
            "Native Hermes execution failed; check provider login/configuration and local Hermes logs"
        ),
    }


class AgentControl(Protocol):
    """Cancellation capability exposed by a live native agent."""

    def interrupt(self, *, hard_cancel: bool = False) -> bool:
        """Request cancellation and report whether it was accepted."""
        ...


class Engine(Protocol):
    """Execute one turn while publishing the native cancellation handle."""

    def execute(
        self,
        profile: str,
        session_id: str,
        message: str,
        publish: Callable[[AgentControl], None],
        cancelled: threading.Event,
    ) -> Object:
        """Run one turn and return the native result envelope."""
        ...


class Journal(Protocol):
    """Native storage contract for request identity and recoverable status."""

    @property
    def durable(self) -> bool:
        """Whether request identity survives a process restart."""
        ...

    def reserve(
        self,
        scope: str,
        key: str,
        fingerprint: str,
        run_id: str,
        status: Object,
        *,
        owner_pid: int = 0,
        owner_started: int = 0,
    ) -> tuple[str, object]:
        """Atomically create or replay a scoped request reservation."""
        ...

    def status_for_run(self, scope: str, run_id: str) -> object:
        """Read the durable status and execution owner for one run."""
        ...

    def lookup(self, scope: str, key: str, fingerprint: str) -> tuple[str, object]:
        """Classify a request as missing, reused, or conflicting."""
        ...

    def update_status(self, run_id: str, status: Object) -> None:
        """Persist the complete public status of a reserved run."""
        ...

    def close(self) -> None:
        """Release the native journal connection."""
        ...


class NativeEngine:
    """Resolve native credentials, resume state, and execute a Hermes turn."""

    def __init__(self, native: Native) -> None:
        """Bind execution to the configured native Hermes checkout."""
        self.native = native
        # This is an embedded server, not a Hermes CLI startup. Native lazy
        # installation may os.execv the entire host when run_agent is imported;
        # use Hermes's supported opt-out before any worker thread can import it.
        os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

    def execute(
        self,
        profile: str,
        session_id: str,
        message: str,
        publish: Callable[[AgentControl], None],
        cancelled: threading.Event,
    ) -> Object:
        """Run in the profile home and close both agent and database afterward."""
        # Credential resolver and scoped .env handling are Hermes's own; secrets
        # stay inside these objects and are never included in MCP responses.
        scope = importlib.import_module("hermes_cli.web_server_profiles")
        state = importlib.import_module("hermes_state")
        config_module = importlib.import_module("hermes_cli.config")
        provider_module = importlib.import_module("hermes_cli.runtime_provider")
        tools_module = importlib.import_module("hermes_cli.tools_config")
        fallback_module = importlib.import_module("hermes_cli.fallback_config")
        session_runtime_module = importlib.import_module("hermes_cli.cli_model_switch_mixin")
        agent_module = importlib.import_module("run_agent")
        # Hermes has no public replacement for its native credential profile scope.
        with (
            self.native.home_scope(profile) as home,
            scope._config_profile_scope(profile),  # noqa: SLF001 — native Hermes profile scope API
        ):
            config = config_module.load_config()
            model_config = config.get("model", "")
            model = model_config.get("default", "") if isinstance(model_config, dict) else str(model_config)
            provider = model_config.get("provider", "auto") if isinstance(model_config, dict) else "auto"
            db = state.SessionDB(db_path=home / "state.db")
            try:
                history = None
                stored_base_url = None
                stored_api_mode = None
                if db.get_session(session_id):
                    session_id = db.resolve_resume_session_id(session_id) or session_id
                    db.assert_resume_safe(session_id, tip_only=True)
                    history = db.get_messages_as_conversation(session_id, repair_alternation=True)
                    db.reopen_session(session_id)
                    route = session_runtime_module.stored_session_route(
                        db.get_session(session_id), current_model=model, current_provider=provider
                    )
                    if route is not None:
                        model, stored_provider, route_url, stored_api_mode, provider_changed = route
                        if provider_changed:
                            provider, stored_base_url = stored_provider, route_url
                runtime, fallback = provider_module.resolve_runtime_with_fallback(
                    config, requested=provider, target_model=model or None, explicit_base_url=stored_base_url
                )
                if fallback is not None:
                    model = fallback["model"]
                elif stored_api_mode:
                    runtime["api_mode"] = stored_api_mode
                agent = agent_module.AIAgent(
                    model=model,
                    provider=runtime.get("provider"),
                    requested_provider=runtime.get("requested_provider"),
                    api_key=runtime.get("api_key"),
                    base_url=runtime.get("base_url"),
                    api_mode=runtime.get("api_mode"),
                    command=runtime.get("command"),
                    args=runtime.get("args"),
                    credential_pool=runtime.get("credential_pool"),
                    enabled_toolsets=sorted(
                        tools_module._get_platform_tools(config, "cli")  # noqa: SLF001 — native Hermes CLI toolset resolver
                    ),
                    disabled_toolsets=config.get("agent", {}).get("disabled_toolsets"),
                    max_iterations=config.get("agent", {}).get("max_turns") or 100,
                    request_overrides=runtime.get("request_overrides"),
                    reasoning_config=self.native.constants.resolve_reasoning_config(config, model),
                    fallback_model=fallback_module.get_fallback_chain(config) or None,
                    session_id=session_id,
                    session_db=db,
                    platform="mcp-bridge",
                    quiet_mode=True,
                    load_soul_identity=True,
                )
                try:
                    publish(agent)
                    if cancelled.is_set():
                        return {"interrupted": True, "final_response": ""}
                    result: object = agent.run_conversation(
                        message, conversation_history=history, task_id=session_id
                    )
                    return _execution_response(result, str(agent.session_id))
                finally:
                    agent.close()
            finally:
                db.close()


@dataclass
class Handle:
    """Live cancellation state and any terminal status awaiting journal repair."""

    status: Object
    cancel: threading.Event = field(default_factory=threading.Event)
    agent: AgentControl | None = None
    thread: threading.Thread | None = None
    pending_status: bool = False


class Runs:
    """Only live handles are private; durable identity/status use Hermes's journal."""

    def __init__(self, native: Native, engine: Engine | None = None) -> None:
        """Initialize a bounded run owner with the native durable journal."""
        self.native = native
        self.engine = engine or NativeEngine(native)
        self._lock = threading.RLock()
        self._journals: dict[str, Journal] = {}
        self._handles: dict[str, Handle] = {}
        self._sessions: set[str] = set()
        self._closing = False
        status = importlib.import_module("gateway.status")
        started: object = status.get_process_start_time(os.getpid())
        self._started = int(started) if isinstance(started, (int, float)) else 0

    def journal(self, profile: str) -> Journal:
        """Open a profile journal, refusing an in-memory fallback."""
        with self._lock:
            if profile not in self._journals:
                module = importlib.import_module("gateway.platforms.api_server_run_idempotency")
                journal: Journal = module.RunIdempotencyStore(
                    str(self.native.home(profile) / "runs_idempotency.db")
                )
                if not journal.durable:
                    journal.close()
                    code = "storage_unavailable"
                    raise BridgeError(code, "Native durable run journal is unavailable")
                self._journals[profile] = journal
            return self._journals[profile]

    def parse(self, task_id: str) -> str:
        """Validate a bridge handle and return its authorized profile."""
        safe_id(task_id)
        parts = task_id.split("_", 2)
        if len(parts) != _TASK_ID_PARTS or parts[0] != "hm3" or len(parts[1]) != _TASK_NONCE_LENGTH:
            code = "invalid_task_id"
            raise BridgeError(code, "Use a task_id from this plugin; hermes_v2 handles belong to hermes_v2")
        return self.native.config.profile(parts[2])

    def status(self, task_id: str) -> Object:
        """Read durable status, repair pending writes, and detect exited owners."""
        profile = self.parse(task_id)
        with self._lock:
            record = self.journal(profile).status_for_run("mcp-bridge:" + profile, task_id)
            if record is None:
                code = "task_not_found"
                raise BridgeError(code, "Native run journal has no such plugin task")
            row = object_json(record)
            result = object_json(row["status"])
            handle = self._handles.get(task_id)
            if handle is not None and handle.pending_status:
                if result.get("status") not in TERMINAL:
                    try:
                        self.journal(profile).update_status(task_id, handle.status)
                    except Exception as exc:
                        code = "storage_unavailable"
                        raise BridgeError(
                            code, "Native run journal could not save the finished task; retry status"
                        ) from exc
                    result = dict(handle.status)
                self._handles.pop(task_id, None)
            alive = (
                True if handle is not None else owner_alive(row.get("owner_pid"), row.get("owner_started"))
            )
            result.update(
                owner_pid=row.get("owner_pid"),
                owner_alive=alive,
                owner_type="thread",
                runtime_backend="python_imports",
            )
            session_fields(self.native, profile, result)
            if result.get("status") not in TERMINAL and alive is False:
                result.update(
                    status="interrupted",
                    error={"code": "owner_exited", "message": "Execution owner exited"},
                )
                self.journal(profile).update_status(task_id, result)
            return result

    def submit(self, agent: str, task: str, request_id: str, session_id: str = "") -> Object:
        """Reserve request identity before starting one bounded native turn."""
        self.native.config.require_writes()
        profile = self.native.config.profile(agent)
        self.native.home(profile)
        safe_id(request_id)
        bounded(len(task), 1, 100000)
        if session_id:
            safe_id(session_id)
        fingerprint = hashlib.sha256(json.dumps([profile, task, session_id]).encode()).hexdigest()
        scope = "mcp-bridge:" + profile
        with self._lock:
            journal = self.journal(profile)
            outcome, record = journal.lookup(scope, request_id, fingerprint)
            if outcome == "conflict":
                code = "idempotency_conflict"
                raise BridgeError(code, "request_id was already used with different input")
            if outcome == "reused":
                replay = object_json(object_json(record)["status"])
                return self.status(str(replay["task_id"]))
            if self._closing:
                code = "shutting_down"
                raise BridgeError(code, "Server is shutting down")
            if len(self._sessions) >= self.native.config.max_concurrent_runs:
                code = "busy"
                raise BridgeError(code, "Native run concurrency limit reached")
            sid = session_id or "mcp_" + uuid.uuid4().hex
            if sid in self._sessions:
                code = "session_busy"
                raise BridgeError(code, "Session already has an active plugin turn")
            task_id = "hm3_" + uuid.uuid4().hex + "_" + profile
            result: Object = {
                "task_id": task_id,
                "session_id": sid,
                "agent": profile,
                "status": "queued",
                "output": None,
                "error": None,
                "usage": None,
                "retry_retention_seconds": 86400,
                "owner_pid": os.getpid(),
                "owner_alive": True,
                "owner_type": "thread",
                "runtime_backend": "python_imports",
                "accepted_at": time.time(),
                "started_at": None,
                "ended_at": None,
                "session_persisted": False,
                "session_retained": False,
            }
            outcome, record = journal.reserve(
                scope,
                request_id,
                fingerprint,
                task_id,
                result,
                owner_pid=os.getpid(),
                owner_started=self._started,
            )
            if outcome != "created":
                if outcome == "conflict":
                    code = "idempotency_conflict"
                    raise BridgeError(code, "Concurrent request_id conflict")
                replay = object_json(object_json(record)["status"])
                return self.status(str(replay["task_id"]))
            return self._start(profile, task_id, sid, task, result)

    def _start(self, profile: str, task_id: str, sid: str, task: str, result: Object) -> Object:
        """Persist the accepted session and launch its owned worker under the admission lock."""
        try:
            ensure_session(self.native, profile, sid)
        except Exception:  # noqa: BLE001 — never export native storage paths or error contents
            result.update(
                status="failed",
                session_id=None,
                ended_at=time.time(),
                error={
                    "code": "session_storage_unavailable",
                    "message": "Native session could not be persisted; no execution started",
                },
            )
            self._finish(profile, task_id, sid, Handle(dict(result)))
            return result
        result.update(session_persisted=True, session_retained=True)
        handle = Handle(dict(result))
        self._handles[task_id] = handle
        self._sessions.add(sid)
        thread = threading.Thread(
            target=self._execute, args=(profile, task_id, sid, task, handle), daemon=True
        )
        handle.thread = thread
        try:
            thread.start()
        except Exception:  # noqa: BLE001 — preserve accepted identity when the worker cannot start
            handle.status.update(
                status="failed",
                ended_at=time.time(),
                error={"code": "native_start_failed", "message": "Native Hermes worker could not start"},
            )
            self._finish(profile, task_id, sid, handle)
            return dict(handle.status)
        return result

    def _execute(self, profile: str, task_id: str, session_id: str, message: str, handle: Handle) -> None:
        journal = self.journal(profile)

        def publish(agent: AgentControl) -> None:
            with self._lock:
                handle.agent = agent
                if handle.cancel.is_set():
                    agent.interrupt(hard_cancel=True)

        try:
            with self._lock:
                handle.status.update(status="running", started_at=time.time())
                journal.update_status(task_id, handle.status)
            if handle.cancel.is_set():
                result: Object = {"interrupted": True, "final_response": ""}
            else:
                result = self.engine.execute(profile, session_id, message, publish, handle.cancel)
            final: Json = result.get("final_response")
            with self._lock:
                cancelled = handle.cancel.is_set() or bool(result.get("interrupted"))
                failed = (
                    bool(result.get("failed"))
                    or result.get("completed") is False
                    or bool(result.get("error"))
                )
                handle.status.update(
                    status="cancelled" if cancelled else "failed" if failed else "completed",
                    output=None if failed else final if isinstance(final, str) else "",
                    usage=result.get("usage"),
                )
                if failed and not cancelled:
                    handle.status["error"] = _execution_error()
                effective_session = result.get("session_id")
                if isinstance(effective_session, str):
                    handle.status["session_id"] = safe_id(effective_session)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — sanitize vendor failures and thread exits
            # Provider exceptions can contain credential-bearing URLs. Return a
            # stable actionable error; no prompt, traceback or raw exception leaks.
            with self._lock:
                handle.status.update(
                    status="cancelled" if handle.cancel.is_set() else "failed",
                    error=_execution_error(exc),
                )
        finally:
            with self._lock:
                handle.status["ended_at"] = time.time()
                self._finish(profile, task_id, session_id, handle)

    def _finish(self, profile: str, task_id: str, session_id: str, handle: Handle) -> None:
        """Release execution capacity and retain failed terminal writes for status repair."""
        try:
            self.journal(profile).update_status(task_id, handle.status)
        except Exception:  # noqa: BLE001 — keep terminal evidence until native journal repair
            handle.pending_status = True
            self._handles[task_id] = handle
        finally:
            self._sessions.discard(session_id)
            handle.agent = None
            handle.thread = None
            if not handle.pending_status:
                self._handles.pop(task_id, None)

    def continue_task(self, task_id: str, message: str, request_id: str) -> Object:
        """Submit a follow-up using the terminal task's effective session."""
        previous = self.status(task_id)
        if previous.get("status") not in TERMINAL:
            code = "session_busy"
            raise BridgeError(code, "Wait for the previous task to finish")
        session_id = previous.get("session_id")
        if not isinstance(session_id, str) or not previous.get("session_persisted"):
            code = "session_not_found"
            raise BridgeError(code, "Task has no retained native session; start a new task")
        return self.submit(str(previous["agent"]), message, request_id, session_id)

    def cancel(self, task_id: str) -> Object:
        """Request cancellation only when this process owns the live handle."""
        self.native.config.require_writes()
        previous = self.status(task_id)
        if previous.get("status") in TERMINAL:
            return previous
        with self._lock:
            handle = self._handles.get(task_id)
            if handle is None:
                code = "owner_unavailable"
                raise BridgeError(
                    code,
                    "Active run belongs to another server process; cannot claim cancellation",
                )
            handle.cancel.set()
            handle.status["status"] = "stopping"
            self.journal(self.parse(task_id)).update_status(task_id, handle.status)
            if handle.agent is not None:
                handle.agent.interrupt(hard_cancel=True)
            return dict(handle.status)

    def wait_idle(self) -> None:
        """Wait briefly for each currently owned worker to finish."""
        with self._lock:
            threads = [x.thread for x in self._handles.values() if x.thread is not None]
        for thread in threads:
            thread.join(3)

    def close(self) -> None:
        """Stop admission, cancel live turns, and close journals after workers exit."""
        with self._lock:
            self._closing = True
            for handle in self._handles.values():
                handle.cancel.set()
                if handle.agent is not None:
                    handle.agent.interrupt(hard_cancel=True)
        self.wait_idle()
        with self._lock:
            if not self._handles:
                for journal in self._journals.values():
                    journal.close()
                self._journals.clear()
