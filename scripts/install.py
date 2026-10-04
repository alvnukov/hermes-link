# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Prepare a private Hermes directory plugin and local config; never changes a running tunnel."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import secrets
import shutil
import sys
import uuid
from dataclasses import replace
from pathlib import Path

# Installer can be invoked directly from the source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_bridge.auth import read_auth_token
from hermes_bridge.core import BridgeError, Config, read_token
from hermes_bridge.presentation import LANGUAGES, localize_plugin, select_language

_RUNTIME_ROOT_FILES = (
    "__init__.py",
    "plugin.yaml",
    "LICENSE",
    "assets/icon.svg",
    "assets/mark.svg",
    "assets/cover.svg",
    "desktop/plugin.js",
    *(f"hermes_bridge/locales/{language}.json" for language in LANGUAGES),
)
_DEVELOPMENT_DIRECTORIES = frozenset({"venv", "env", "build", "dist", "tests", "__pycache__", "htmlcov"})


def _installation_paths(hermes_home: Path, runtime: Path) -> tuple[Path, Path, Path]:
    source = Path(__file__).resolve().parents[1]
    home, state = hermes_home.resolve(), runtime.resolve()
    if home.is_relative_to(source) or state.is_relative_to(source):
        code = "invalid_install"
        raise BridgeError(code, "Hermes home and runtime must be outside the plugin source checkout")
    return source, home, state


def _runtime_exclusions(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        path = Path(directory) / name
        if path.is_dir():
            if (
                not name.isidentifier()
                or name in _DEVELOPMENT_DIRECTORIES
                or not path.joinpath("__init__.py").is_file()
            ):
                ignored.add(name)
        elif not (name.endswith(".py") and not name.startswith(".")) and name != "py.typed":
            ignored.add(name)
    return ignored


def _copy_runtime(source: Path, staging: Path) -> None:
    """Deploy the native runtime and explicitly allowed presentation files."""
    staging.mkdir(mode=0o700)
    shutil.copytree(source / "hermes_bridge", staging / "hermes_bridge", ignore=_runtime_exclusions)
    for name in _RUNTIME_ROOT_FILES:
        (staging / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, staging / name)


def seed_ui_settings(config: Config, hermes_home: Path, plugin_dir: Path) -> Config:
    """Show the effective policy on first install without resetting saved choices."""
    from hermes_bridge.main import read_saved_plugin_settings  # noqa: PLC0415 - startup dependency boundary
    from hermes_bridge.native import Native  # noqa: PLC0415 - keep dependency diagnostics at command entry
    from hermes_bridge.server import Surface  # noqa: PLC0415 - keep dependency diagnostics at command entry

    native = Native(config)
    owner = (
        hermes_home if config.board_profile == "default" else hermes_home / "profiles" / config.board_profile
    )
    if not owner.is_dir():
        msg = "invalid_install"
        raise BridgeError(msg, "HTTP MCP settings owner profile does not exist")
    scope = native.constants.set_hermes_home_override(owner)
    try:
        module = importlib.import_module("hermes_cli.config")
        saved = read_saved_plugin_settings(module)
        available = {name for name in dir(Surface) if name.startswith("hermes_")}
        policy = config.with_plugin_settings(saved).with_tool_settings(saved, available)
        values = {
            "writes": policy.writes,
            "auth_enabled": policy.auth_enabled,
            **{"tool_" + name: name not in policy.disabled_tools for name in sorted(available)},
        }
        settings = importlib.import_module("hermes_cli.plugins_settings")
        settings.save_plugin_settings(
            "http-mcp", plugin_dir, {key: value for key, value in values.items() if key not in saved}
        )
        return replace(policy, token_env_file=module.get_env_path())
    finally:
        native.constants.reset_hermes_home_override(scope)


def install(repo: Path, hermes_home: Path, runtime: Path, *, language: str = "auto") -> None:
    """Prepare a private directory plugin, preserving existing local policy and tokens."""
    source, hermes_home, runtime = _installation_paths(hermes_home, runtime)
    if not (repo / "mcp_serve.py").is_file() or not (repo / "venv/bin/python").is_file():
        msg = "hermes_unavailable"
        raise BridgeError(msg, "Installed Hermes repository/runtime not found")
    if language != "auto":
        select_language(language, hermes_home)
    plugin_root = hermes_home / "plugins"
    plugin_root.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    token_file = runtime / "mcp.key"
    if not token_file.exists():
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48) + "\n")
    read_token(token_file)
    config_file = runtime / "config.json"
    if not config_file.exists():
        runtime_document = {
            "hermes_repo": str(repo),
            "token_file": str(token_file),
            "profiles": ["*"],
            "boards": ["default"],
            "board_profile": "default",
            "writes": True,
            "auth_enabled": True,
            "disabled_tools": [],
            "host": "127.0.0.1",
            "port": 18788,
            "max_concurrent_runs": 2,
        }
        fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(runtime_document, stream, indent=2)
            stream.write("\n")
    config = Config.load(config_file)
    destination = plugin_root / "http-mcp"
    if destination.is_symlink():
        msg = "invalid_install"
        raise BridgeError(msg, "Plugin destination must not be a symlink")
    staging = runtime / ("stage-" + uuid.uuid4().hex)
    _copy_runtime(source, staging)
    owner = (
        hermes_home if config.board_profile == "default" else hermes_home / "profiles" / config.board_profile
    )
    chosen_language = select_language(language, owner)
    localize_plugin(staging, chosen_language)
    if destination.exists():
        previous = runtime / "previous"
        previous.mkdir(mode=0o700, exist_ok=True)
        destination.rename(previous / ("http-mcp-" + uuid.uuid4().hex))
    staging.rename(destination)
    effective = seed_ui_settings(config, hermes_home, destination)
    token = read_auth_token(effective) if effective.auth_enabled else read_token(token_file)
    header_file = runtime / "mcp-authorization.header"
    fd = os.open(header_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("Bearer " + token + "\n")
    header_file.chmod(0o600)
    print("Prepared Hermes plugin:", destination, "(language: " + chosen_language + ")")
    print("Configuration:", config_file)
    print(
        "Start from plugin directory with:",
        repo / "venv/bin/python",
        "-m hermes_bridge.main --config",
        repr(str(config_file)),
    )
    print("No service or tunnel was changed. Saved native plugin settings override config.json.")


def main() -> None:
    """Parse installation locations and report safe domain failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-repo", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument(
        "--runtime", type=Path, default=Path.home() / "Library/Application Support/HermesHTTPMCP"
    )
    parser.add_argument("--language", default="auto", help="auto or one of: " + ", ".join(LANGUAGES))
    args = parser.parse_args()
    try:
        install(
            args.hermes_repo.resolve(),
            args.hermes_home.resolve(),
            args.runtime.resolve(),
            language=args.language,
        )
    except BridgeError as exc:
        print(json.dumps(exc.payload()), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
