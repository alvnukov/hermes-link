# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Native plugin registration and transport lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .core import BridgeError, Config, Object, object_json

if TYPE_CHECKING:
    from types import ModuleType


class CLIContext(Protocol):
    """The native Hermes plugin registration interface."""

    def register_cli_command(
        self,
        *,
        name: str,
        help: str,  # noqa: A002 - native registration keyword contract
        setup_fn: Callable[[argparse.ArgumentParser], None],
        handler_fn: Callable[[argparse.Namespace], None],
        description: str = "",
    ) -> object:
        """Register one native command without starting it."""
        ...


def setup_cli(parser: argparse.ArgumentParser) -> None:
    """Declare the required configuration and supported transports."""
    parser.add_argument("--config", type=Path, required=True, help="Local bridge config.json")
    parser.add_argument("--transport", choices=("http", "stdio"), default="http")
    parser.add_argument("--profile", help="Default target profile for this MCP client")


def read_saved_plugin_settings(config_module: ModuleType) -> Object:
    """Read explicit policy values; the caller binds the native owning home."""
    state_module = importlib.import_module("hermes_cli.plugins_state")
    try:
        raw = config_module.require_readable_config_before_write(config_module.get_config_path())
    except Exception:  # noqa: BLE001 — native parser errors may contain configuration values
        msg = "invalid_config"
        raise BridgeError(msg, "Native configuration cannot be read safely") from None
    section: Mapping[str, object] = raw
    for key in ("plugins", "entries", "http-mcp"):
        if key not in section:
            break
        value = section[key]
        if not isinstance(value, Mapping):
            msg = "invalid_config"
            raise BridgeError(msg, "Native HTTP MCP plugin settings sections must be objects")
        section = value
    entry = state_module._plugin_settings_entry(raw, "http-mcp")  # noqa: SLF001 - native saved settings grammar
    settings: object = (entry or {}).get("settings", {})
    if not isinstance(settings, Mapping):
        msg = "invalid_config"
        raise BridgeError(msg, "Native HTTP MCP settings must be an object")
    return object_json(
        {
            key: value
            for key, value in settings.items()
            if isinstance(key, str)
            and (key in ("writes", "auth_enabled", "disabled_tools") or key.startswith("tool_"))
        }
    )


def load_effective_config(path: Path) -> Config:
    """Apply saved native plugin settings from the bridge's owning profile."""
    from .native import Native  # noqa: PLC0415 - report missing native dependencies before opening a listener

    config = Config.load(path)
    native = Native(config)
    with native.home_scope(config.board_profile):
        config_module = importlib.import_module("hermes_cli.config")
        saved = read_saved_plugin_settings(config_module)
        token_env_file = config_module.get_env_path()
    from .server import Surface  # noqa: PLC0415 - keep dependency errors at startup

    available = {name for name in dir(Surface) if name.startswith("hermes_")}
    effective = config.with_plugin_settings(saved).with_tool_settings(saved, available)
    return replace(effective, token_env_file=token_env_file)


def command(args: argparse.Namespace) -> None:
    """Run the selected transport and always close native execution handles."""
    try:
        config = load_effective_config(args.config)
        # Missing dependency is reported before any listener is opened.
        from .server import (  # noqa: PLC0415 - report missing native dependencies before opening a listener
            create_server,
            http_app,
        )

        server, surface = create_server(config, default_profile=getattr(args, "profile", None))
        try:
            logging.getLogger().setLevel(logging.WARNING)
            if args.transport == "stdio":
                asyncio.run(server.run_stdio_async())
            else:
                import uvicorn  # noqa: PLC0415 - report missing native dependencies before opening a listener

                uvicorn.run(
                    http_app(server, config),
                    host=config.host,
                    port=config.port,
                    log_level="warning",
                    access_log=False,
                )
        finally:
            surface.runs.close()
    except BridgeError as exc:
        print(json.dumps(exc.payload()), file=sys.stderr)
        raise SystemExit(1) from None
    except ImportError:
        print(
            "Hermes MCP dependencies are unavailable. "
            "Run this plugin with the installed Hermes Python runtime.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except OSError:
        print(
            "Cannot start local MCP listener; "
            "check configured port, file permissions and Hermes installation.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def register(ctx: CLIContext) -> None:
    """Expose the HTTP MCP command through the native plugin loader."""
    ctx.register_cli_command(
        name="http-mcp",
        help="Start Hermes Link with local MCP authentication",
        setup_fn=setup_cli,
        handler_fn=command,
        description="Hermes Link: native agent management over local HTTP MCP or stdio.",
    )


def main() -> None:
    """Parse standalone command arguments and report sanitized startup failures."""
    parser = argparse.ArgumentParser(description="Hermes Link — connect your assistant to Hermes")
    setup_cli(parser)
    command(parser.parse_args())


if __name__ == "__main__":
    main()
