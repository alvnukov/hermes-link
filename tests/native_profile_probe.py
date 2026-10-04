# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Verify real Hermes chat/Kanban MCP tools in disposable profiles, without model turns."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING

from hermes_bridge.core import Object, object_json, rows

if TYPE_CHECKING:
    from types import ModuleType

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SERVER = "hermes-link"
PROFILES = ("profile_a", "profile_b")
PROFILE_SEQUENCE = ("profile_a", "profile_b", "profile_a")
EXPECTED_TOOL_COUNT = 10
OWNER_CARD = "Owner profile default board card"
WORKER_CARD = "Worker different board card"
WORKER_BOARD = "worker-other"


def require(condition: bool, message: str) -> None:
    """Fail the verification at the first broken observable contract."""
    if not condition:
        raise RuntimeError(message)


def prepare_profiles(sandbox: Path, repo: Path) -> Path:
    """Create bare disposable profiles and a custom administrator policy."""
    root = sandbox / ".hermes"
    root.mkdir()
    root.joinpath("config.yaml").write_text("agent:\n  max_turns: 7\n", encoding="utf-8")
    managed = sandbox / "admin"
    managed.mkdir()
    managed.joinpath("config.yaml").write_text("model:\n  default: managed-test\n", encoding="utf-8")
    config_path = sandbox / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "hermes_repo": str(repo),
                "token_file": str(sandbox / "unused-token"),
                "profiles": ["*"],
                "boards": ["default"],
                "writes": True,
                "auth_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    for index, profile in enumerate(PROFILES):
        home = root / "profiles" / profile
        home.mkdir(parents=True)
        home.joinpath(".env").write_text("", encoding="utf-8")
        home.joinpath(".env").chmod(0o600)
        # JSON is valid YAML, and this file is fixture data rather than a native user config writer.
        home.joinpath("config.yaml").write_text(
            json.dumps(
                {
                    "agent": {"max_turns": 17 + index, "disabled_toolsets": []},
                    "platform_toolsets": {"cli": ["file"]},
                }
            ),
            encoding="utf-8",
        )
    return root


def bind_environment(sandbox: Path, root: Path, repo: Path) -> None:
    """Discard ambient credentials before importing any native Hermes module."""
    safe = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
    os.environ.clear()
    os.environ.update(safe)
    os.environ.update(
        {
            "HOME": str(sandbox),
            "HERMES_HOME": str(root),
            "HERMES_RUNTIME_DIR": str(sandbox / "runtime"),
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_MANAGED_DIR": str(sandbox / "admin"),
            "HERMES_STATE_DB_GUARD_BYPASS": "1",
            "NO_COLOR": "1",
        }
    )
    sys.path.insert(0, str(repo))
    os.chdir(sandbox)


def prepare_connections(sandbox: Path) -> None:
    """Consume the real connector setup rather than maintaining a parallel fixture stanza."""
    connections = importlib.import_module("hermes_bridge.profile_connection")
    for profile in PROFILES:
        result = connections.configure_connection(sandbox / "bridge.json", profile, plugin_dir=PACKAGE_ROOT)
        require(result["connected"] and result["cli_available"], "Generated connector is unavailable")


def prepare_boards() -> Path:
    """Initialize distinguishable owner and worker boards under the disposable default home."""
    board = importlib.import_module("hermes_cli.kanban_db")
    database = importlib.import_module("hermes_cli.kanban_db_connect")
    for name, title in (("default", OWNER_CARD), (WORKER_BOARD, WORKER_CARD)):
        connection = database.connect(board=name)
        try:
            board.create_task(connection, title=title, board=name, initial_status="blocked")
        finally:
            connection.close()
    return Path(board.kanban_db_path(board=WORKER_BOARD))


def worker_selection(profile: str, home: Path) -> list[str]:
    """Pass native Kanban worker argv through the real shared CLI parser, without spawning it."""
    board = importlib.import_module("hermes_cli.kanban_db")
    dispatch = importlib.import_module("hermes_cli.kanban_db_dispatch")
    task = board.Task(
        id="profile_tools_probe",
        title="Isolated MCP verification",
        body=None,
        assignee=profile,
        status="running",
        priority=0,
        created_by="verification",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    # The public dispatch command would claim/spawn work; this builder is its actual consumer seam.
    argv = dispatch._worker_argv(task, profile, str(home))
    profile_flag = argv.index("-p")
    require(argv[profile_flag + 1] == profile, "Kanban worker selected another profile")
    parser_module = importlib.import_module("hermes_cli._parser")
    parser, _, _ = parser_module.build_top_level_parser()
    arguments = parser.parse_args(argv[profile_flag + 2 :])
    require(arguments.command == "chat" and arguments.query is not None, "Worker did not use one-shot chat")
    require(arguments.cli is True, "Worker did not select the CLI")
    return [name.strip() for name in arguments.toolsets.split(",") if name.strip()]


def dispatch_result(model_tools: ModuleType, name: str, arguments: Object, selected: list[str]) -> Object:
    """Call the discovered tool through native dispatch and unwrap its actual MCP result."""
    raw = model_tools.handle_function_call(name, arguments, enabled_toolsets=selected)
    envelope = object_json(json.loads(raw))
    if "error" in envelope:
        error = envelope["error"]
        return object_json(json.loads(error)) if isinstance(error, str) else envelope
    structured = envelope.get("structuredContent")
    if isinstance(structured, dict):
        return object_json(structured)
    text = envelope.get("result")
    require(isinstance(text, str), f"Missing MCP result: {name}")
    if not isinstance(text, str):  # Type narrowing; require above always raises.
        raise TypeError(name)
    start = text.find("{")
    require(start >= 0, f"Missing JSON MCP result: {name}")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return object_json(value)


def dispatch_tool(model_tools: ModuleType, name: str, arguments: Object, selected: list[str]) -> Object:
    """Require a successful native MCP tool result."""
    result = dispatch_result(model_tools, name, arguments, selected)
    require("error" not in result, f"MCP tool failed: {name}")
    return result


def verify_policy(model_tools: ModuleType, selected: list[str], revision: str) -> Object:
    """Observe administrator enforcement and owner-board reads through the generated stdio client."""
    schema = importlib.import_module("tools.mcp_tool_schema")
    write_name = schema.mcp_prefixed_tool_name(SERVER, "hermes_settings")
    board_name = schema.mcp_prefixed_tool_name(SERVER, "hermes_kanban")
    denied = dispatch_result(
        model_tools,
        write_name,
        {
            "action": "update",
            "params": {"changes": {"model.default": "unmanaged-write-test"}, "expected_revision": revision},
        },
        selected,
    )
    owner = dispatch_tool(
        model_tools, board_name, {"action": "get", "params": {"board": "default"}}, selected
    )
    outside = dispatch_result(
        model_tools, board_name, {"action": "get", "params": {"board": WORKER_BOARD}}, selected
    )
    return {
        "managed_write_denied": object_json(denied.get("error", {})).get("code") == "access_denied",
        "owner_board_read": [item["title"] for item in rows(owner["tasks"])] == [OWNER_CARD],
        "worker_board_denied": object_json(outside.get("error", {})).get("code") == "access_denied",
    }


def verify_calls(
    model_tools: ModuleType, selected: list[str], profile: str, root: Path, target: int
) -> Object:
    """Prove omitted-profile reads and revision-checked writes affect only the selected profile."""
    schema = importlib.import_module("tools.mcp_tool_schema")
    settings_name = schema.mcp_prefixed_tool_name(SERVER, "hermes_settings")
    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=selected, quiet_mode=True, skip_tool_search_assembly=True
    )
    # Hermes also registers four generic resource/prompt utilities for this MCP connection.
    prefix = schema.mcp_prefixed_tool_name(SERVER, "hermes_")
    names = {row["function"]["name"] for row in definitions if row["function"]["name"].startswith(prefix)}
    require(
        len(names) == EXPECTED_TOOL_COUNT,
        f"The bridge exposed {len(names)} native MCP schemas; expected all {EXPECTED_TOOL_COUNT}",
    )
    require(settings_name in names, "The settings tool was absent from native schemas")
    snapshots = {
        name: (root / "profiles" / name / "config.yaml").read_bytes() for name in PROFILES if name != profile
    }
    default_before = root.joinpath("config.yaml").read_bytes()
    before = dispatch_tool(model_tools, settings_name, {"action": "get"}, selected)
    require(before.get("profile") == profile, "Omitted profile read used another profile")
    managed_model = object_json(before["settings"])["model.default"]
    policy = verify_policy(model_tools, selected, str(before["revision"]))
    # An unfixed connector may allow the managed write. Refresh the revision so the ordinary write
    # remains an independent check and both isolation failures appear in the same evidence report.
    before = dispatch_tool(model_tools, settings_name, {"action": "get"}, selected)
    after = dispatch_tool(
        model_tools,
        settings_name,
        {
            "action": "update",
            "params": {"changes": {"agent.max_turns": target}, "expected_revision": before["revision"]},
        },
        selected,
    )
    reread = dispatch_tool(model_tools, settings_name, {"action": "get"}, selected)
    require(
        after.get("profile") == profile and reread.get("profile") == profile, "Write used another profile"
    )
    require(object_json(reread["settings"])["agent.max_turns"] == target, "Settings write was not persisted")
    require(before["revision"] != reread["revision"], "The settings revision did not change")
    require(root.joinpath("config.yaml").read_bytes() == default_before, "The default profile was changed")
    require(
        all(
            (root / "profiles" / name / "config.yaml").read_bytes() == raw for name, raw in snapshots.items()
        ),
        "Another profile was changed",
    )
    return {
        "profile": profile,
        "read_profile": before["profile"],
        "write_profile": reread["profile"],
        "tool_count": len(names),
        "revision_changed": True,
        "other_profiles_unchanged": True,
        "managed_model": managed_model,
        **policy,
    }


def verify(repo: Path) -> Object:
    """Exercise profile A, B, then A through both native consumer selections and real stdio MCP."""
    require(repo.joinpath("mcp_serve.py").is_file(), "A native Hermes checkout is required")
    with tempfile.TemporaryDirectory(prefix="hermes-profile-tools-") as temporary:
        sandbox = Path(temporary).resolve()
        root = prepare_profiles(sandbox, repo)
        bind_environment(sandbox, root, repo)
        prepare_connections(sandbox)
        worker_board_path = prepare_boards()
        dispatch = importlib.import_module("hermes_cli.kanban_db_dispatch")
        secrets = importlib.import_module("agent.secret_scope")
        lifecycle = importlib.import_module("tools.mcp_tool_lifecycle")
        startup = importlib.import_module("hermes_cli.mcp_startup")
        model_tools = importlib.import_module("model_tools")
        config = importlib.import_module("hermes_cli.config")
        tools_config = importlib.import_module("hermes_cli.tools_config")
        profiles = importlib.import_module("hermes_cli.profiles")
        require(
            profiles.get_profile_dir("default").resolve() == root, "Native default root escaped the sandbox"
        )
        checks: list[Object] = []
        secrets.set_multiplex_active(True)
        try:
            for consumer in ("chat", "kanban"):
                if consumer == "kanban":
                    # A new worker starts fresh discovery while carrying its own board pins. Tear down
                    # chat connections so adoption cannot hide the worker's distinct spawn environment.
                    lifecycle.shutdown_mcp_servers(timeout=15)
                    os.environ["HERMES_KANBAN_DB"] = str(worker_board_path)
                    os.environ["HERMES_KANBAN_BOARD"] = WORKER_BOARD
                for index, profile in enumerate(PROFILE_SEQUENCE):
                    home = root / "profiles" / profile
                    # Bind the same complete scope the native dispatcher uses for its profile reads.
                    with dispatch._worker_profile_scope(str(home)):
                        selected = (
                            sorted(tools_config._get_platform_tools(config.load_config(), "cli"))
                            if consumer == "chat"
                            else worker_selection(profile, home)
                        )
                        require(
                            isinstance(selected, list) and SERVER in selected,
                            "MCP was missing from selection",
                        )
                        startup.set_mcp_server_filter(selected)
                        startup.ensure_mcp_discovery_before_agent_build(
                            logger=logging.getLogger(__name__), timeout=20, single_query=consumer == "kanban"
                        )
                        require(startup.join_mcp_discovery(timeout=20), "MCP discovery did not finish")
                        check = verify_calls(model_tools, selected, profile, root, 50 + len(checks) + index)
                        check["consumer"] = consumer
                        checks.append(check)
        finally:
            lifecycle.shutdown_mcp_servers(timeout=15)
            secrets.set_multiplex_active(False)
        policy_ok = all(
            item["managed_write_denied"]
            and item["managed_model"] == "managed-test"
            and item["owner_board_read"]
            and item["worker_board_denied"]
            for item in checks
        )
        return object_json(
            {
                "ok": policy_ok,
                "isolated": True,
                "generated_connection": True,
                "model_turns": 0,
                "checks": checks,
            }
        )


def main() -> None:
    """Run with a native Hermes interpreter and emit compact, secret-free evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-repo", type=Path, required=True)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    try:
        with redirect_stdout(sys.stderr):
            report = verify(arguments.hermes_repo.resolve())
    except (RuntimeError, ImportError, OSError, ValueError) as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")) + "\n")
        raise SystemExit(1) from None
    sys.stdout.write(json.dumps(report, separators=(",", ":")) + "\n")
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
