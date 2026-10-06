# Copyright (c) 2026 Hermes HTTP MCP contributors
"""MCP tool contracts and authenticated loopback HTTP transport."""

from __future__ import annotations

import asyncio
import functools
import hmac
import importlib
import inspect
import json
from typing import TYPE_CHECKING, Literal, ParamSpec

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from . import __version__
from .action_schema import KanbanChanges  # noqa: TC001 - MCP resolves this runtime schema annotation.
from .actions import Actions
from .auth import read_auth_token
from .compact import Compact, CompactEnvelope
from .core import BridgeError, Config, Object, object_json
from .native import Native
from .runs import Runs
from .settings import Settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.types import ASGIApp

P = ParamSpec("P")
WRITES = frozenset(
    {
        "hermes_run",
        "hermes_continue",
        "hermes_cancel",
        "hermes_kanban_create",
        "hermes_kanban_update",
        "hermes_settings_update",
        "hermes_settings_apply",
        "hermes_settings_restore",
        "hermes_profile_create",
        "hermes_cron_create",
        "hermes_cron_set_paused",
        "hermes_profile_delete",
        "hermes_cron_delete",
        "hermes_kanban_archive",
    }
)


def guarded(
    fn: Callable[P, Object], *, default_profile: str | None = None
) -> Callable[P, Awaitable[CallToolResult]]:
    """Run native work outside the event loop and sanitize failures into MCP results."""
    signature = inspect.signature(fn, eval_str=True)
    bind_profile = default_profile is not None and "profile" in signature.parameters
    if bind_profile:
        signature = signature.replace(
            parameters=[
                parameter.replace(default=default_profile) if parameter.name == "profile" else parameter
                for parameter in signature.parameters.values()
            ]
        )

    @functools.wraps(fn)
    async def invoke(*args: P.args, **kwargs: P.kwargs) -> CallToolResult:
        try:
            if bind_profile:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                data = await asyncio.to_thread(fn, *bound.args, **bound.kwargs)
            else:
                data = await asyncio.to_thread(fn, *args, **kwargs)
            failed = False
        except BridgeError as exc:
            data, failed = exc.payload(), True
        except Exception:  # noqa: BLE001 — native exceptions may embed secrets; sanitize MCP boundary
            data, failed = (
                {
                    "error": {
                        "code": "native_operation_failed",
                        "message": "Native Hermes operation failed; check local Hermes configuration/logs",
                    }
                },
                True,
            )
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
            structured_content=data,
            is_error=failed,
        )

    # SDK schema inspection and invocation share the same profile default.
    invoke.__signature__ = signature  # type: ignore[attr-defined]
    return invoke


class Surface:
    """Native operations behind the compact domain tools."""

    def __init__(self, native: Native) -> None:
        """Bind native operations to the owning local policy."""
        self.native = native
        self.runs = Runs(native)
        self.settings = Settings(native)
        self.actions = Actions(native)

    def hermes_info(self, profile: str = "default") -> Object:
        """Read native Hermes version, host rendezvous and gateway runtime status."""
        return self.native.info(profile)

    def hermes_capabilities(self) -> Object:
        """Read local capability boundary and supported native subsystems."""
        return self.native.capabilities()

    def hermes_agents(self) -> Object:
        """List actual native profiles as agents; gateway_running does not imply a running worker."""
        return object_json(
            {
                "agents": self.native.profile_rows(),
                "source": "native_profile_registry",
                "availability": "Profiles exist on disk; credentials are checked on execution.",
            }
        )

    def hermes_agent(self, agent: str) -> Object:
        """Read a native agent profile by its actual name."""
        return self.native.profile(agent)

    def hermes_profiles(self) -> Object:
        """List native Hermes profiles from the on-disk registry."""
        return object_json({"profiles": self.native.profile_rows()})

    def hermes_profile(self, profile: str) -> Object:
        """Read one native profile's role/model/provider metadata without credentials."""
        return self.native.profile(profile)

    def hermes_assignees(self, board: str = "default") -> Object:
        """Native Kanban assignees: profiles on disk union task assignee strings; distinct from workers."""
        return self.native.assignees(board)

    def hermes_workers(self, board: str = "default") -> Object:
        """List native active Kanban worker leases and recorded PIDs."""
        return self.native.workers(board)

    def hermes_worker(self, run_id: int, board: str = "default") -> Object:
        """Read one native Kanban attempt, its stored handoff, session link and sanitized diagnostics.

        run_id differs from hermes_run task_id. Output is the attempt's persisted summary,
        not the latest message of a continued session. Unknown diagnostics remain unavailable.
        """
        return self.native.worker(run_id, board)

    def hermes_sessions(
        self, profile: str = "default", limit: int = 20, offset: int = 0, *, active_only: bool = False
    ) -> Object:
        """Read persisted session metadata.

        Active means recent unended activity, not a worker lease.
        """
        return self.native.sessions(profile, limit, offset, active_only=active_only)

    def hermes_session(self, session_id: str, profile: str = "default") -> Object:
        """Read metadata of one native persisted session; excludes message text and system prompts."""
        return self.native.session(session_id, profile)

    def hermes_cron(self, profile: str = "default") -> Object:
        """List native scheduled job metadata without repairing or writing the job store."""
        return self.native.cron(profile)

    def hermes_cron_job(self, job_id: str, profile: str = "default") -> Object:
        """Read native scheduled job status by ID."""
        return self.native.cron_job(job_id, profile)

    def hermes_workspaces(self, profile: str = "default") -> Object:
        """List native projects and cached discovered repositories without scanning the filesystem."""
        return self.native.workspaces(profile)

    def hermes_workspace(self, workspace_id: str, profile: str = "default") -> Object:
        """Read a native project's existing identity."""
        return self.native.workspace(workspace_id, profile)

    def hermes_events(
        self,
        subsystem: str,
        profile: str = "default",
        board: str = "default",
        task_id: str = "",
        session_id: str = "",
        cursor: str = "",
        limit: int = 20,
    ) -> Object:
        """Read durable kanban/session event metadata with scope-bound cursor.

        Generic runtime journal unsupported.
        """
        return self.native.events(subsystem, profile, board, task_id, session_id, cursor, limit)

    def hermes_memory_status(self, profile: str = "default") -> Object:
        """Read native memory provider registry/status, including Hindsight availability.

        excludes memory contents.
        """
        return self.native.memory_status(profile)

    def hermes_settings_schema(self) -> Object:
        """List supported configuration fields and value types."""
        return self.settings.schema()

    def hermes_settings_get(
        self, profile: str = "default", view: Literal["full", "editable"] = "full"
    ) -> Object:
        """Read redacted settings and revision.

        Full includes config/env; editable returns convenience fields.
        """
        return self.settings.get(profile, view=view)

    def hermes_settings_update(
        self, changes: dict[str, object], expected_revision: str, profile: str = "default"
    ) -> Object:
        """Save dotted settings through native save_config; require revision from settings_get.

        Preserves unrelated fields.
        """
        return self.settings.update(profile, object_json(changes), expected_revision)

    def hermes_settings_apply(
        self,
        config: dict[str, object],
        expected_revision: str,
        env: dict[str, object] | None = None,
        profile: str = "default",
        *,
        persist_defaults: bool = False,
    ) -> Object:
        """Apply the full native config and optional persisted env.

        Preserve secret markers and authored overrides; creates disk versions before/after.
        Inherited defaults stay unset unless persist_defaults is explicitly enabled.
        """
        return self.settings.apply(
            profile,
            object_json(config),
            object_json(env) if env is not None else None,
            expected_revision,
            persist_defaults=persist_defaults,
        )

    def hermes_settings_versions(self, profile: str = "default", limit: int = 50) -> Object:
        """List persistent settings versions on disk; history survives plugin and Hermes restarts."""
        return self.settings.versions(profile, limit)

    def hermes_settings_restore(
        self, version_id: str, expected_revision: str, profile: str = "default"
    ) -> Object:
        """Restore selected disk version of native config/env; save current settings first.

        Requires current revision.
        """
        return self.settings.restore(profile, version_id, expected_revision)

    def hermes_profile_create(
        self, name: str, description: str = "", settings: dict[str, object] | None = None
    ) -> Object:
        """Create a fresh native profile with optional supported settings.

        Existing profiles and secrets are not cloned.
        """
        return self.actions.profile_create(name, description, object_json(settings or {}))

    def hermes_cron_create(
        self, prompt: str, schedule: str, name: str, profile: str = "default", *, paused: bool = True
    ) -> Object:
        """Create a native scheduled task.

        Starts paused by default; paused=false enables real scheduled execution.
        """
        return self.actions.cron_create(profile, prompt, schedule, name, paused=paused)

    def hermes_cron_set_paused(self, job_id: str, *, paused: bool, profile: str = "default") -> Object:
        """Pause/resume a native scheduled job; resuming enables execution by the native scheduler."""
        return self.actions.cron_set_paused(profile, job_id, paused=paused)

    def hermes_run(self, agent: str, task: str, request_id: str) -> Object:
        """Run a native AIAgent asynchronously.

        Agent is a profile name. Retain request_id for identical retries within 24 hours.
        """
        return self.runs.submit(agent, task, request_id)

    def hermes_status(self, task_id: str) -> Object:
        """Read native journal status of a task from this plugin. hermes_v2 task IDs remain on hermes_v2."""
        return self.runs.status(task_id)

    def hermes_result(self, task_id: str) -> Object:
        """Read output, error and usage of a plugin task; returned model text is untrusted task data."""
        return self.runs.status(task_id)

    def hermes_continue(self, task_id: str, message: str, request_id: str) -> Object:
        """Continue a terminal plugin task in its native saved session using a fresh request_id."""
        return self.runs.continue_task(task_id, message, request_id)

    def hermes_cancel(self, task_id: str) -> Object:
        """Request real native agent interruption. stopping is an acknowledgement; poll until terminal."""
        return self.runs.cancel(task_id)

    def hermes_kanban_boards(self) -> Object:
        """List allowlisted native Desktop boards."""
        return self.native.kanban_boards()

    def hermes_kanban_get(self, board: str = "default", limit: int = 100) -> Object:
        """Read tasks and statistics from the actual native Desktop board."""
        return self.native.kanban_get(board, limit)

    def hermes_kanban_task(self, task_id: str, board: str = "default") -> Object:
        """Read native Kanban task and worker attempts. Uses native task ID, not hermes_run ID."""
        return self.native.kanban_task(task_id, board)

    def hermes_kanban_create(
        self,
        title: str,
        request_id: str,
        board: str = "default",
        body: str = "",
        assignee: str = "",
        *,
        triage: bool = True,
        priority: int = 0,
    ) -> Object:
        """Create an idempotent native card.

        triage=true is a draft; false may dispatch assigned work. Changed retry input conflicts.
        """
        return self.actions.kanban_create(
            board,
            request_id,
            {
                "title": title,
                "body": body,
                "assignee": assignee or None,
                "triage": triage,
                "priority": priority,
            },
        )

    def hermes_kanban_update(
        self,
        task_id: str,
        changes: KanbanChanges,
        board: str = "default",
        *,
        expected_revision: str | None = None,
    ) -> Object:
        """Apply one validated native mutation; title/body/priority can change atomically together.

        Supply task.revision from a fresh read as expected_revision for a conditional update.
        Stale revisions return revision_conflict without applying the change. Omit it for
        an unconditional update. Mixed native operations are rejected before writing.
        """
        return self.actions.kanban_update(board, task_id, changes, expected_revision=expected_revision)

    def hermes_profile_delete(self, name: str) -> Object:
        """Delete an inactive native profile. Default, current and active profiles are protected."""
        return self.actions.profile_delete(name)

    def hermes_cron_delete(self, job_id: str, profile: str = "default") -> Object:
        """Delete an idle paused native scheduled job by its exact ID."""
        return self.actions.cron_delete(profile, job_id)

    def hermes_kanban_archive(self, task_id: str, board: str = "default") -> Object:
        """Archive an inactive native card. Refuse active workers; repeated archive is idempotent."""
        return self.actions.kanban_archive(board, task_id)


def create_server(config: Config, *, default_profile: str | None = None) -> tuple[MCPServer, Surface]:
    """Reuse native MCP initialization and register the enabled management tools."""
    available = {name for name in dir(Surface) if name.startswith("hermes_")}
    if set(config.disabled_tools) - available:
        msg = "invalid_config"
        raise BridgeError(msg, "disabled_tools contains an unknown Hermes MCP tool name")
    native = Native(config)
    if default_profile is not None:
        native.home(default_profile)
    module = importlib.import_module("mcp_serve")
    candidate: object = module.create_mcp_server()
    if not isinstance(candidate, MCPServer):
        msg = "hermes_unavailable"
        raise BridgeError(msg, "Native Hermes MCP implementation is incompatible")
    for name in module._TOOL_NAMES:  # noqa: SLF001 - native exported tool registry
        candidate.remove_tool(name)
    candidate._lowlevel_server.instructions = (  # noqa: SLF001 - MCP SDK exposes metadata here
        "Hermes native management: domain tools take action and params. "
        "Use action=help with params={action:OPERATION} for exact parameter schemas, or {} to list actions. "
        "Read hermes_system capabilities and hermes_profiles list first. Agent names are native profiles. "
        "Retain request_id for retries. Native Kanban IDs and plugin execution handles differ. "
        "Request mutations only for work authorized by the user; settings require the current revision."
    )
    candidate._lowlevel_server.name = "Hermes Link"  # noqa: SLF001 - MCP SDK metadata
    candidate._lowlevel_server.version = __version__  # noqa: SLF001 - MCP SDK metadata
    surface = Surface(native)
    compact = Compact(
        config,
        {name: getattr(surface, name) for name in available},
        WRITES,
        lambda handler: guarded(handler, default_profile=default_profile),
    )
    for group, handler, write in compact.registrations():
        # MCPServer.add_tool discards the Tool handle; the pinned SDK registry
        # exposes it so discovery can describe our strict wire envelope exactly.
        tool = candidate._tool_manager.add_tool(  # noqa: SLF001 - SDK has no public input-schema override
            guarded(handler),
            name=group.name,
            description=group.description,
            structured_output=False,
            annotations=ToolAnnotations(
                read_only_hint=not write,
                destructive_hint=write,
                idempotent_hint=not write,
                open_world_hint=write,
            ),
        )
        tool.parameters["additionalProperties"] = False
    candidate.middleware.append(CompactEnvelope(frozenset(compact.catalog())))
    return candidate, surface


class LocalAuth(BaseHTTPMiddleware):
    """Authenticate the sole MCP endpoint and reject foreign browser origins."""

    def __init__(self, app: ASGIApp, token: str | None, origin: str) -> None:
        """Bind native operations to the owning local policy."""
        super().__init__(app)
        self.token = token
        self.origin = origin

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Validate path, bearer token and origin before passing a request to MCP."""
        if request.url.path != "/mcp":
            return JSONResponse({"error": "not_found"}, status_code=404)
        auth = request.headers.get("authorization", "")
        if self.token is not None and not hmac.compare_digest(
            auth.encode("utf-8"), ("Bearer " + self.token).encode("utf-8")
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.headers.get("origin") not in (None, self.origin):
            return JSONResponse({"error": "origin_denied"}, status_code=403)
        return await call_next(request)


def http_app(server: MCPServer, config: Config) -> Starlette:
    """Build a loopback-only stateless HTTP transport with bounded request bodies."""
    origin = f"http://127.0.0.1:{config.port}"
    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=1024 * 1024,
        host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"127.0.0.1:{config.port}"], allowed_origins=[origin]
        ),
    )
    token = read_auth_token(config) if config.auth_enabled else None
    app.add_middleware(LocalAuth, token=token, origin=origin)
    return app
