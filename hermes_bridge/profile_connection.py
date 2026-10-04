# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Connect a native Hermes profile to Hermes Link with versioned settings."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

from .core import BridgeError, Config, Object, json_value, object_json
from .native import Native
from .settings import Settings

SERVER = "hermes-link"
_IDENTITY_FIELDS = ("command", "args", "cwd", "env")
_WORKER_BOARD_PINS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)


def _preflight(config: Config, plugin_dir: Path) -> None:
    interpreter = config.hermes_repo / "venv" / "bin" / "python"
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        msg = "hermes_unavailable"
        raise BridgeError(msg, "The native Hermes venv/bin/python runtime is missing or not executable")
    if not (plugin_dir / "hermes_bridge" / "main.py").is_file():
        msg = "hermes_unavailable"
        raise BridgeError(msg, "The installed Hermes Link plugin is missing hermes_bridge/main.py")


def _require_unmanaged_disconnect(settings: Settings) -> None:
    prefix = f"mcp_servers.{SERVER}"
    keys: object = settings.module.managed_scope.managed_config_keys()
    if settings.module.is_managed() or (
        isinstance(keys, set)
        and any(
            isinstance(key, str)
            and (key == prefix or key.startswith(prefix + ".") or prefix.startswith(key + "."))
            for key in keys
        )
    ):
        msg = "access_denied"
        raise BridgeError(
            msg, "An administrator manages the Hermes Link connection; it cannot be disconnected"
        )


def _server_entry(native: Native, config_path: Path, profile: str, plugin_dir: Path, trust: str) -> Object:
    root = Path(native.constants.get_default_hermes_root()).resolve()
    env: Object = {"HERMES_HOME": str(root), **dict.fromkeys(_WORKER_BOARD_PINS, "")}
    managed_scope = importlib.import_module("hermes_cli.managed_scope")
    explicit_managed = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    managed = Path(explicit_managed).expanduser() if explicit_managed else managed_scope.get_managed_dir()
    if isinstance(managed, Path):
        env["HERMES_MANAGED_DIR"] = str(managed.resolve())
    return {
        "command": str(native.config.hermes_repo / "venv" / "bin" / "python"),
        "args": [
            "-m",
            "hermes_bridge.main",
            "--config",
            str(config_path),
            "--transport",
            "stdio",
            "--profile",
            profile,
        ],
        "cwd": str(plugin_dir),
        "env": env,
        "enabled": True,
        "trust": trust,
    }


def _server_map(config: Object) -> Object:
    value = config.get("mcp_servers", {})
    if not isinstance(value, dict):
        msg = "invalid_settings"
        raise BridgeError(msg, "mcp_servers must be an object before configuring Hermes Link")
    return object_json(value)


def _check_collision(servers: Object, entry: Object) -> None:
    if SERVER not in servers:
        return
    existing = servers[SERVER]
    if not isinstance(existing, dict) or any(existing.get(key) != entry[key] for key in _IDENTITY_FIELDS):
        msg = "connection_conflict"
        raise BridgeError(msg, "The hermes-link server name belongs to another connection; rename it first")


def _cli_selection(config: Object) -> list[str] | None:
    platforms = object_json(config.get("platform_toolsets", {}))
    validation = importlib.import_module("hermes_cli.toolset_validation")
    value: object = validation.parse_platform_toolsets_value(platforms.get("cli"))
    if value is None:
        return None
    return [str(name) for name in value] if isinstance(value, list) else None


def _cli_available(config: Object) -> bool:
    module = importlib.import_module("hermes_cli.tools_config")
    tools: object = module._get_platform_tools(config, "cli")  # noqa: SLF001 - Native worker selection.
    return isinstance(tools, set) and SERVER in tools


def _enabled_servers(config: Object) -> set[str]:
    module = importlib.import_module("hermes_cli.tools_config")
    enabled: object = module.enabled_mcp_server_names(config)
    return {str(name) for name in enabled} if isinstance(enabled, set) else set()


def _set_cli_selection(config: Object, names: list[str]) -> None:
    platforms = object_json(config.get("platform_toolsets", {}))
    platforms["cli"] = json_value(names)
    config["platform_toolsets"] = platforms


def _enable_cli(desired: Object, effective: Object) -> None:
    selected = _cli_selection(effective)
    if selected is not None and "no_mcp" in selected:
        msg = "connection_disabled"
        raise BridgeError(msg, "Remove no_mcp from platform_toolsets.cli before connecting Hermes Link")
    agent = object_json(effective.get("agent", {}))
    skill_utils = importlib.import_module("agent.skill_utils")
    disabled: object = skill_utils.parse_config_string_list(agent.get("disabled_toolsets"))
    if isinstance(disabled, list) and SERVER in {str(name).strip() for name in disabled}:
        msg = "connection_disabled"
        raise BridgeError(
            msg, "Remove hermes-link from agent.disabled_toolsets before connecting Hermes Link"
        )
    if _cli_available(effective):
        return
    enabled = _enabled_servers(effective)
    if selected is not None and set(selected) & (enabled - {SERVER}):
        selected = [*selected, SERVER]
        _set_cli_selection(desired, selected)
        _set_cli_selection(effective, selected)
    if not _cli_available(effective):
        msg = "connection_disabled"
        raise BridgeError(
            msg,
            "Hermes Link is disabled for CLI workers; enable hermes-link in native Tools and "
            "remove its suppression from agent.disabled_toolsets before connecting",
        )


def _disable_cli(desired: Object, effective: Object, previously_enabled: set[str]) -> None:
    selected = _cli_selection(desired)
    if selected is None or SERVER not in selected:
        return
    remaining = [name for name in selected if name != SERVER]
    if (
        SERVER in previously_enabled
        and "no_mcp" not in remaining
        and not set(remaining) & _enabled_servers(effective)
    ):
        remaining.append("no_mcp")
    _set_cli_selection(desired, remaining)
    _set_cli_selection(effective, remaining)


def _desired_config(snapshot: Object, entry: Object, *, disconnect: bool) -> tuple[Object, Object]:
    desired = deepcopy(object_json(snapshot["config_overrides"]))
    effective = deepcopy(object_json(snapshot["config"]))
    _check_collision(_server_map(effective), entry)
    servers = _server_map(desired)
    effective_servers = _server_map(effective)
    previously_enabled = _enabled_servers(effective) if disconnect else set()
    if disconnect:
        servers.pop(SERVER, None)
        effective_servers.pop(SERVER, None)
    else:
        existing = servers.get(SERVER)
        servers[SERVER] = {**(existing if isinstance(existing, dict) else {}), **entry}
        effective_servers[SERVER] = servers[SERVER]
    if "mcp_servers" in desired or not disconnect:
        desired["mcp_servers"] = servers
    effective["mcp_servers"] = effective_servers
    if disconnect:
        _disable_cli(desired, effective, previously_enabled)
    else:
        _enable_cli(desired, effective)
    return desired, effective


def configure_connection(
    config_path: Path,
    profile: str,
    *,
    disconnect: bool = False,
    trust: str = "full",
    plugin_dir: Path | None = None,
) -> Object:
    """Add or remove only this connector using native backups and revision checks.

    The stdio child anchors its registry at the native default home. The consumer
    profile selects default tool arguments; the runtime config retains board authority.
    """
    if trust not in ("full", "untrusted"):
        msg = "invalid_input"
        raise BridgeError(msg, "trust must be full or untrusted")
    config_path = config_path.expanduser().resolve()
    config = Config.load(config_path)
    config.require_writes()
    plugin_dir = (plugin_dir or Path(__file__).resolve().parents[1]).expanduser().resolve()
    _preflight(config, plugin_dir)
    native = Native(config)
    settings = Settings(native)
    if disconnect:
        _require_unmanaged_disconnect(settings)
    snapshot = settings.get(profile)
    entry = _server_entry(native, config_path, profile, plugin_dir, trust)
    discovery = importlib.import_module("hermes_cli.plugins_discovery")
    with native.home_scope(profile), discovery.suppress_plugin_discovery():
        desired, effective = _desired_config(snapshot, entry, disconnect=disconnect)
        cli_available = not disconnect and _cli_available(effective)
    changed = desired != snapshot["config_overrides"]
    saved = settings.apply(profile, desired, None, str(snapshot["revision"])) if changed else snapshot
    result: Object = {
        "profile": profile,
        "server": SERVER,
        "transport": "stdio",
        "connected": not disconnect,
        "changed": changed,
        "trust": trust,
        "cli_available": cli_available,
        "revision": saved["revision"],
        "notice": "The connection selection takes effect in the next session, including new Kanban workers.",
    }
    for key in ("before_version_id", "version_id"):
        if key in saved:
            result[key] = saved[key]
    return result


def main(argv: list[str] | None = None) -> int:
    """Run explicit per-profile connection setup and report a safe JSON result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--disconnect", action="store_true")
    parser.add_argument("--trust", choices=("full", "untrusted"), default="full")
    parser.add_argument("--plugin-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = configure_connection(
            args.config,
            args.profile,
            disconnect=args.disconnect,
            trust=args.trust,
            plugin_dir=args.plugin_dir,
        )
    except BridgeError as exc:
        sys.stderr.write(json.dumps(exc.payload()) + "\n")
        return 1
    except (ImportError, OSError):
        unavailable = BridgeError(
            "hermes_unavailable", "Hermes Link setup could not load its native runtime or files"
        )
        sys.stderr.write(json.dumps(unavailable.payload()) + "\n")
        return 1
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
