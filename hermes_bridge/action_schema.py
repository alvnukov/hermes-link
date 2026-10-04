# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Strict Kanban patch schema and the native transaction phases it can express."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .core import BridgeError, Object, object_json

Text = Annotated[str, Field(max_length=100000)]
ShortText = Annotated[str, Field(max_length=2000)]
Status = Literal["triage", "todo", "ready", "blocked", "scheduled", "review", "done"]
Phase = Literal["edit", "assignment", "status", "model", "reasoning"]


class KanbanChanges(BaseModel):
    """One native mutation; nulls and cross-operation combinations are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: ShortText | None = None
    body: Text | None = None
    priority: Annotated[int, Field(ge=-(2**63), lt=2**63)] | None = None
    assignee: Annotated[str, Field(max_length=160)] | None = None
    status: Status | None = None
    result: Text | None = None
    summary: Text | None = None
    block_reason: Text | None = None
    model_override: ShortText | None = None
    provider_override: ShortText | None = None
    clear_model_override: bool | None = None
    reasoning_effort: ShortText | None = None
    clear_reasoning_effort: bool | None = None


def invalid_patch(message: str, fields: list[str]) -> BridgeError:
    """Report a pre-mutation rejection without echoing caller values."""
    return BridgeError("invalid_input", message, object_json({"applied_fields": [], "failed_fields": fields}))


def validated_patch(changes: KanbanChanges | Object) -> tuple[KanbanChanges, Object, Phase]:
    """Validate every supplied value and permit only one native mutation phase."""
    supplied = changes.model_dump(exclude_unset=True) if isinstance(changes, KanbanChanges) else changes
    fields = sorted(supplied)
    try:
        parsed = KanbanChanges.model_validate(supplied, strict=True)
    except ValidationError as error:
        msg = "Invalid Kanban changes; use help for the typed schema"
        raise invalid_patch(msg, fields) from error
    values = object_json(parsed.model_dump(exclude_unset=True))
    if not values or any(value is None for value in values.values()):
        msg = "Supply non-null changes; omit unchanged fields"
        raise invalid_patch(msg, fields)
    if parsed.title is not None and not parsed.title.strip():
        msg = "Task title cannot be blank"
        raise invalid_patch(msg, fields)
    groups: dict[Phase, set[str]] = {
        "edit": {"title", "body", "priority"},
        "assignment": {"assignee"},
        "status": {"status", "result", "summary", "block_reason"},
        "model": {"model_override", "provider_override", "clear_model_override"},
        "reasoning": {"reasoning_effort", "clear_reasoning_effort"},
    }
    phases = [phase for phase, keys in groups.items() if keys.intersection(values)]
    if len(phases) != 1:
        msg = "Native Hermes cannot atomically combine these operations; submit one operation at a time"
        raise invalid_patch(
            msg,
            fields,
        )
    phase = phases[0]
    _validate_phase(parsed, values, phase)
    return parsed, values, phase


def _validate_phase(patch: KanbanChanges, values: Object, phase: Phase) -> None:
    """Reject native no-ops and conflicting clear/set requests before writing."""
    allowed: set[str] | None = None
    if phase == "status":
        allowed = {"status"}
        if patch.status == "done":
            allowed.update(("result", "summary"))
        elif patch.status == "review":
            allowed.add("summary")
        elif patch.status in {"blocked", "scheduled"}:
            allowed.add("block_reason")
        if patch.status is None or set(values) - allowed:
            msg = "Result, summary and reason require the corresponding status"
            raise invalid_patch(msg, sorted(values))
    elif phase in {"model", "reasoning"}:
        _validate_override(patch, values, phase)


def _validate_override(patch: KanbanChanges, values: Object, phase: Phase) -> None:
    """Require an explicit set or clear, excluding contradictory native knobs."""
    value = patch.model_override if phase == "model" else patch.reasoning_effort
    clear = patch.clear_model_override if phase == "model" else patch.clear_reasoning_effort
    if (value is None) == (clear is not True) or (value is not None and not value.strip()):
        msg = "Provide a nonblank override or its clear flag, exclusively"
        raise invalid_patch(msg, sorted(values))
    if (
        phase == "model"
        and patch.provider_override is not None
        and (clear or not patch.provider_override.strip())
    ):
        msg = "Provider requires a nonblank model override"
        raise invalid_patch(msg, sorted(values))
