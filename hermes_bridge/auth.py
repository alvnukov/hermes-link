# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Read the native Desktop secret without exporting or caching its value."""

from __future__ import annotations

import importlib
import os
import stat
from typing import TYPE_CHECKING

from .core import BridgeError, object_json, read_token, validate_token

if TYPE_CHECKING:
    from pathlib import Path

    from .core import Config

TOKEN_ENV = "HERMES_HTTP_MCP_TOKEN"  # nosec B105 # noqa: S105 - environment variable name, not a secret
MAX_ENV_BYTES = 1024 * 1024


def _read_env(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        code = "invalid_token"
        raise BridgeError(code, "Cannot read native MCP authentication secret") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            code = "invalid_token"
            raise BridgeError(code, "Native secret file must be owned, regular and private")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_ENV_BYTES + 1)
        if len(raw) > MAX_ENV_BYTES:
            code = "invalid_token"
            raise BridgeError(code, "Native secret file is too large")
        return raw
    finally:
        os.close(fd)


def _native_token(raw: bytes) -> str | None:
    native = importlib.import_module("agent.secret_scope")
    # Reuse the native persisted .env grammar; process environment is intentionally ignored.
    values = object_json(native._parse_env_text(native._decode_env_bytes(raw)))  # noqa: SLF001
    if TOKEN_ENV not in values:
        return None
    value = values[TOKEN_ENV]
    if not isinstance(value, str):
        code = "invalid_token"
        raise BridgeError(code, "Native MCP token must be a string")
    return validate_token(value)


def read_auth_token(config: Config) -> str:
    """Prefer the owning profile's UI token; fail closed on invalid stored secrets."""
    if config.token_env_file is not None:
        try:
            raw = _read_env(config.token_env_file)
            token = _native_token(raw) if raw is not None else None
            if token is not None:
                return token
        except BridgeError:
            raise
        except Exception:  # noqa: BLE001 - native parser failures may contain secret values
            code = "invalid_token"
            raise BridgeError(code, "Cannot parse native MCP authentication secret") from None
    return read_token(config.token_file)
