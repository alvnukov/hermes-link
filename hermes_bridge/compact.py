# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Small MCP catalog over the existing native operations, with on-demand schemas."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, func_metadata
from mcp.types import CallToolResult, TextContent
from pydantic import Field, ValidationError

from .core import BridgeError, Config, Object, object_json

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.context import CallNext, HandlerResult, ServerRequestContext


class CompactEnvelope:
    """Validate wire arguments before SDK errors can echo input or discard fields."""

    def __init__(self, tools: frozenset[str]) -> None:
        """Restrict this middleware to the registered compact management tools."""
        self.tools = tools

    async def __call__(self, ctx: ServerRequestContext[object, object], call_next: CallNext) -> HandlerResult:
        """Reject malformed envelopes before any native action or SDK parsing."""
        if ctx.method == "tools/call" and ctx.params is not None and ctx.params.get("name") in self.tools:
            arguments = ctx.params.get("arguments")
            if not (
                isinstance(arguments, dict)
                and not set(arguments) - {"action", "params"}
                and isinstance(arguments.get("action"), str)
                and (arguments.get("params") is None or isinstance(arguments.get("params"), dict))
            ):
                data = BridgeError(
                    "invalid_input", "Expected action and optional params object only"
                ).payload()
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(data))],
                    structured_content=data,
                    is_error=True,
                )
        return await call_next(ctx)


@dataclass(frozen=True)
class Group:
    """One domain tool and its stable operation routing table."""

    name: str
    description: str
    actions: tuple[tuple[str, str], ...]


GROUPS = (
    Group(
        "hermes_system",
        "Hermes runtime, capabilities, agents and memory status.",
        (
            ("info", "hermes_info"),
            ("capabilities", "hermes_capabilities"),
            ("agents", "hermes_agents"),
            ("agent", "hermes_agent"),
            ("memory_status", "hermes_memory_status"),
        ),
    ),
    Group(
        "hermes_profiles",
        "Native profiles: list, inspect, create or delete inactive profiles.",
        (
            ("list", "hermes_profiles"),
            ("get", "hermes_profile"),
            ("create", "hermes_profile_create"),
            ("delete", "hermes_profile_delete"),
        ),
    ),
    Group(
        "hermes_tasks",
        "Agent tasks: run, status, result, continue or cancel.",
        (
            ("run", "hermes_run"),
            ("status", "hermes_status"),
            ("result", "hermes_result"),
            ("continue", "hermes_continue"),
            ("cancel", "hermes_cancel"),
        ),
    ),
    Group(
        "hermes_kanban",
        "Native boards, cards and assignees; create, update or archive cards.",
        (
            ("boards", "hermes_kanban_boards"),
            ("get", "hermes_kanban_get"),
            ("task", "hermes_kanban_task"),
            ("assignees", "hermes_assignees"),
            ("create", "hermes_kanban_create"),
            ("update", "hermes_kanban_update"),
            ("archive", "hermes_kanban_archive"),
        ),
    ),
    Group(
        "hermes_workers",
        "Native Kanban worker leases and attempts.",
        (
            ("list", "hermes_workers"),
            ("get", "hermes_worker"),
        ),
    ),
    Group(
        "hermes_sessions",
        "Persisted session metadata; excludes message text.",
        (
            ("list", "hermes_sessions"),
            ("get", "hermes_session"),
        ),
    ),
    Group(
        "hermes_cron",
        "Native scheduled jobs: list, inspect, create, pause/resume or delete.",
        (
            ("list", "hermes_cron"),
            ("get", "hermes_cron_job"),
            ("create", "hermes_cron_create"),
            ("set_paused", "hermes_cron_set_paused"),
            ("delete", "hermes_cron_delete"),
        ),
    ),
    Group(
        "hermes_workspaces",
        "Native projects and cached repositories.",
        (
            ("list", "hermes_workspaces"),
            ("get", "hermes_workspace"),
        ),
    ),
    Group(
        "hermes_settings",
        "Read/edit full config/env; disk versions and rollback. Secrets stay redacted.",
        (
            ("schema", "hermes_settings_schema"),
            ("get", "hermes_settings_get"),
            ("update", "hermes_settings_update"),
            ("apply", "hermes_settings_apply"),
            ("versions", "hermes_settings_versions"),
            ("restore", "hermes_settings_restore"),
        ),
    ),
    Group(
        "hermes_events", "Durable Kanban/session events with scoped cursors.", (("list", "hermes_events"),)
    ),
)


@dataclass(frozen=True)
class Operation:
    """An enabled native handler and its SDK-derived parameter model."""

    handler: Callable[..., Object]
    parameters: type[ArgModelBase]
    description: str
    write: bool

    def invoke(self, params: dict[str, object]) -> Object:
        """Validate nested arguments strictly without echoing rejected input."""
        if set(params) - self.parameters.model_fields.keys():
            code = "invalid_input"
            raise BridgeError(code, "Unknown action parameters; use help for the parameter schema")
        try:
            validated = self.parameters.model_validate(params, strict=True)
        except ValidationError:
            code = "invalid_input"
            raise BridgeError(code, "Invalid action parameters; use help for the parameter schema") from None
        return self.handler(**validated.model_dump_one_level())


class Compact:
    """Route a small domain catalog while retaining per-operation access controls."""

    def __init__(
        self,
        config: Config,
        handlers: dict[str, Callable[..., Object]],
        writes: frozenset[str],
        schema_handler: Callable[[Callable[..., Object]], Callable[..., object]],
    ) -> None:
        """Compile only enabled operations using the same defaults as the native handlers."""
        self._groups: dict[str, Group] = {}
        self._operations: dict[str, dict[str, Operation]] = {}
        for group in GROUPS:
            operations = {
                action: Operation(
                    handlers[name],
                    func_metadata(schema_handler(handlers[name]), structured_output=False).arg_model,
                    handlers[name].__doc__ or "",
                    name in writes,
                )
                for action, name in group.actions
                if name not in config.disabled_tools and (config.writes or name not in writes)
            }
            if operations:
                self._groups[group.name] = group
                self._operations[group.name] = operations

    def catalog(self) -> Object:
        """Report only actions actually callable under the current local policy."""
        return object_json({name: list(actions) for name, actions in self._operations.items()})

    def registrations(self) -> list[tuple[Group, Callable[..., Object], bool]]:
        """Return the enabled tools and truthful mutation annotations."""
        return [
            (
                group,
                self._handler(group.name),
                any(operation.write for operation in self._operations[name].values()),
            )
            for name, group in self._groups.items()
        ]

    def _operation(self, name: str, action: str) -> Operation:
        if action not in dict(self._groups[name].actions):
            code = "invalid_action"
            raise BridgeError(code, "Unknown action; use help for available actions")
        operation = self._operations[name].get(action)
        if operation is None:
            code = "access_denied"
            raise BridgeError(code, "Action disabled by local settings")
        return operation

    def _help(self, name: str, params: dict[str, object]) -> Object:
        if set(params) - {"action"}:
            code = "invalid_input"
            raise BridgeError(code, "Help accepts only an optional action name")
        action = params.get("action")
        if action is None:
            return object_json(
                {
                    "tool": name,
                    "actions": {
                        key: {"summary": operation.description.splitlines()[0], "write": operation.write}
                        for key, operation in self._operations[name].items()
                    },
                }
            )
        if not isinstance(action, str):
            code = "invalid_input"
            raise BridgeError(code, "Help action must be a string")
        operation = self._operation(name, action)
        return object_json(
            {
                "tool": name,
                "action": action,
                "description": operation.description,
                "write": operation.write,
                "params_schema": {**operation.parameters.model_json_schema(), "additionalProperties": False},
            }
        )

    def _handler(self, name: str) -> Callable[..., Object]:
        def invoke(action: str, params: dict[str, object] | None = None) -> Object:
            arguments = params if params is not None else {}
            if action == "help":
                return self._help(name, arguments)
            result = self._operation(name, action).invoke(arguments)
            if name == "hermes_system" and action == "capabilities":
                result = {**result, "mcp_actions": self.catalog(), "tool_surface": "compact"}
            return result

        invoke.__name__ = name
        # A short enum advertises permitted actions; routing separately enforces policy.
        invoke.__annotations__["action"] = Annotated[
            str, Field(json_schema_extra={"enum": [*self._operations[name], "help"]})
        ]
        return invoke
