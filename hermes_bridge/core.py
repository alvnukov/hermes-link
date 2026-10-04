# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Validated JSON, local policy and sanitized domain errors."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeAlias

Json: TypeAlias = str | int | float | bool | list["Json"] | dict[str, "Json"] | None
Object: TypeAlias = dict[str, Json]
MAX_DISABLED_TOOLS = 64


class BridgeError(Exception):
    """A safe domain error whose public payload never contains a traceback."""

    def __init__(self, code: str, message: str, details: Object | None = None) -> None:
        """Store the stable error code and a sanitized message."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def payload(self) -> Object:
        """Return the JSON error envelope used by every MCP operation."""
        return {"error": {"code": self.code, "message": self.message, **self.details}}


def json_value(value: object) -> Json:
    """Validate data arriving from legacy untyped Hermes APIs or a JSON decoder."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {str(k): json_value(v) for k, v in value.items()}
    msg = "invalid_response"
    raise BridgeError(msg, "Native API returned non-JSON data")


def object_json(value: object) -> Object:
    """Validate a native response and require a JSON object."""
    result = json_value(value)
    if not isinstance(result, dict):
        msg = "invalid_response"
        raise BridgeError(msg, "Native API must return an object")
    return result


def rows(value: Json) -> list[Object]:
    """Require a JSON list containing only objects."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        msg = "invalid_response"
        raise BridgeError(msg, "Native API must return a list of objects")
    return [object_json(item) for item in value]


def project(value: object, fields: tuple[str, ...]) -> Object:
    """Export only explicitly allowed native response fields."""
    source = object_json(value)
    return {key: source[key] for key in fields if key in source}


def safe_id(value: str) -> str:
    """Validate an identifier that cannot escape a native profile or board path."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        msg = "invalid_input"
        raise BridgeError(msg, "ID must be 1-160 letters, digits, underscores or hyphens")
    return value


def bounded(value: object, minimum: int, maximum: int) -> int:
    """Require an integer in the inclusive interval; reject booleans and fractions."""
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        msg = "invalid_input"
        raise BridgeError(msg, f"Value must be {minimum}-{maximum}")
    return value


def string_list(value: Json) -> tuple[str, ...]:
    """Require a nonempty JSON list of strings."""
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        msg = "invalid_config"
        raise BridgeError(msg, "Nonempty string list required")
    return tuple(str(x) for x in value)


@dataclass(frozen=True)
class Config:
    """Immutable local policy and native repository connection settings."""

    hermes_repo: Path
    token_file: Path
    profiles: tuple[str, ...]
    boards: tuple[str, ...]
    writes: bool = True
    board_profile: str = "default"
    host: str = "127.0.0.1"
    port: int = 18788
    max_concurrent_runs: int = 2
    auth_enabled: bool = True
    disabled_tools: tuple[str, ...] = ()
    token_env_file: Path | None = None

    def with_tool_settings(self, settings: Object, available: set[str]) -> Config:
        """Overlay explicit native UI choices on the legacy tool exclusions."""
        disabled = set(self.disabled_tools)
        for key, enabled in settings.items():
            if not key.startswith("tool_"):
                continue
            name = key.removeprefix("tool_")
            if name not in available or not isinstance(enabled, bool):
                msg = "invalid_config"
                raise BridgeError(msg, "Tool controls must name available tools and contain booleans")
            if enabled:
                disabled.discard(name)
            else:
                disabled.add(name)
        return replace(self, disabled_tools=tuple(sorted(disabled)))

    def with_plugin_settings(self, settings: Object) -> Config:
        """Validate and overlay persisted native plugin policy."""
        writes = settings.get("writes", self.writes)
        auth_enabled = settings.get("auth_enabled", self.auth_enabled)
        disabled = settings.get("disabled_tools", list(self.disabled_tools))
        if not isinstance(writes, bool) or not isinstance(auth_enabled, bool):
            msg = "invalid_config"
            raise BridgeError(msg, "writes and auth_enabled must be booleans")
        if (
            not isinstance(disabled, list)
            or len(disabled) > MAX_DISABLED_TOOLS
            or not all(isinstance(name, str) and re.fullmatch(r"hermes_[a-z_]+", name) for name in disabled)
        ):
            msg = "invalid_config"
            raise BridgeError(msg, "disabled_tools must be a list of Hermes MCP tool names")
        return replace(
            self,
            writes=writes,
            auth_enabled=auth_enabled,
            disabled_tools=tuple(str(name) for name in disabled),
        )

    @classmethod
    def load(cls, path: Path) -> Config:
        """Read a strict JSON configuration with explicit loopback and allowlist policy."""
        try:
            data = object_json(json.loads(path.read_bytes()))
        except (OSError, ValueError) as exc:
            msg = "invalid_config"
            raise BridgeError(msg, "Cannot read configuration JSON") from exc
        allowed = {
            "hermes_repo",
            "token_file",
            "profiles",
            "boards",
            "writes",
            "board_profile",
            "host",
            "port",
            "max_concurrent_runs",
            "auth_enabled",
            "disabled_tools",
        }
        if set(data) - allowed:
            msg = "invalid_config"
            raise BridgeError(msg, "Unknown configuration fields")
        repo, token = data.get("hermes_repo"), data.get("token_file")
        if (
            not isinstance(repo, str)
            or not Path(repo).is_absolute()
            or not isinstance(token, str)
            or not Path(token).is_absolute()
        ):
            msg = "invalid_config"
            raise BridgeError(msg, "Absolute Hermes repository and token file paths required")
        profiles = string_list(data.get("profiles"))
        boards = string_list(data.get("boards"))
        for name in (*profiles, *boards):
            if name != "*":
                safe_id(name)
        if "*" in boards:
            msg = "invalid_config"
            raise BridgeError(msg, "Boards require an explicit allowlist")
        writes = data.get("writes", True)
        board_profile = data.get("board_profile", "default")
        port, concurrency = data.get("port", 18788), data.get("max_concurrent_runs", 2)
        if data.get("host", "127.0.0.1") != "127.0.0.1":
            msg = "invalid_config"
            raise BridgeError(msg, "MCP must bind to 127.0.0.1")
        if (
            not isinstance(writes, bool)
            or not isinstance(board_profile, str)
            or not isinstance(port, int)
            or not isinstance(concurrency, int)
        ):
            msg = "invalid_config"
            raise BridgeError(msg, "Invalid write, profile, port or concurrency setting")
        result = cls(
            Path(repo),
            Path(token),
            profiles,
            boards,
            writes,
            safe_id(board_profile),
            "127.0.0.1",
            bounded(port, 1024, 65535),
            bounded(concurrency, 1, 8),
        )
        result.profile(board_profile)
        return result.with_plugin_settings(data)

    def profile(self, name: str) -> str:
        """Validate a profile identifier and enforce its local allowlist."""
        safe_id(name)
        if "*" not in self.profiles and name not in self.profiles:
            msg = "access_denied"
            raise BridgeError(msg, "Profile is not allowlisted")
        return name

    def board(self, name: str) -> str:
        """Validate a board identifier and enforce its local allowlist."""
        safe_id(name)
        if name not in self.boards:
            msg = "access_denied"
            raise BridgeError(msg, "Board is not allowlisted")
        return name

    def require_writes(self) -> None:
        """Reject mutations when local policy disables writes."""
        if not self.writes:
            msg = "access_denied"
            raise BridgeError(msg, "Write tools are disabled by local configuration")


def read_token(path: Path) -> str:
    """Read a bounded, owned, private regular token file without following links."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                msg = "invalid_token"
                raise BridgeError(msg, "Token must be an owned regular file with permissions 0600")
            token = os.read(fd, 1024).decode("ascii").strip()
        finally:
            os.close(fd)
    except (OSError, UnicodeError) as exc:
        msg = "invalid_token"
        raise BridgeError(msg, "Cannot read local MCP authentication token") from exc
    return validate_token(token)


def validate_token(token: str) -> str:
    """Validate a URL-safe bearer token without echoing its contents."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        msg = "invalid_token"
        raise BridgeError(msg, "Local MCP token must contain 32-256 URL-safe characters")
    return token
