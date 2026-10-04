# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Read-only MCP smoke against a real loopback server; never submits a model turn."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import CallToolResult

from hermes_bridge.auth import read_auth_token
from hermes_bridge.core import Object, object_json, rows
from hermes_bridge.main import load_effective_config


async def smoke(config_path: Path, output: Path) -> None:
    """Exercise read tools against a configured loopback server without model turns."""
    config = await asyncio.to_thread(load_effective_config, config_path)
    token = await asyncio.to_thread(read_auth_token, config) if config.auth_enabled else None
    headers = {"Authorization": "Bearer " + token} if token is not None else {}
    async with (
        create_mcp_http_client(headers=headers) as http_client,
        streamable_http_client(f"http://127.0.0.1:{config.port}/mcp", http_client=http_client) as transport,
        ClientSession(transport[0], transport[1], read_timeout_seconds=20) as client,
    ):
        initialized = await client.initialize()
        discovery = await client.list_tools()
        report = object_json(
            {
                "version": initialized.server_info.version,
                "tools": [tool.name for tool in discovery.tools],
                "checks": [],
            }
        )
        calls: list[tuple[str, str, str, Object]] = [
            ("hermes_info", "hermes_system", "info", {"profile": config.board_profile}),
            ("hermes_capabilities", "hermes_system", "capabilities", {}),
            ("hermes_agents", "hermes_system", "agents", {}),
            ("hermes_profiles", "hermes_profiles", "list", {}),
            ("hermes_profile", "hermes_profiles", "get", {"profile": config.board_profile}),
            ("hermes_assignees", "hermes_kanban", "assignees", {"board": config.boards[0]}),
            ("hermes_workers", "hermes_workers", "list", {"board": config.boards[0]}),
            ("hermes_sessions", "hermes_sessions", "list", {"profile": config.board_profile, "limit": 1}),
            ("hermes_cron", "hermes_cron", "list", {"profile": config.board_profile}),
            ("hermes_workspaces", "hermes_workspaces", "list", {"profile": config.board_profile}),
            ("hermes_kanban_boards", "hermes_kanban", "boards", {}),
            ("hermes_kanban_get", "hermes_kanban", "get", {"board": config.boards[0], "limit": 1}),
            (
                "hermes_events",
                "hermes_events",
                "list",
                {"subsystem": "kanban", "board": config.boards[0], "profile": config.board_profile},
            ),
            ("hermes_settings_get", "hermes_settings", "get", {"profile": config.board_profile}),
            ("hermes_settings_schema", "hermes_settings", "schema", {}),
            ("hermes_settings_versions", "hermes_settings", "versions", {"profile": config.board_profile}),
            ("hermes_memory_status", "hermes_system", "memory_status", {"profile": config.board_profile}),
        ]
        checks: list[Object] = []
        for operation, name, action, arguments in calls:
            if operation in config.disabled_tools:
                checks.append(
                    {"tool": name, "action": action, "skipped": True, "reason": "disabled_by_configuration"}
                )
                continue
            result = await client.call_tool(name, {"action": action, "params": arguments})
            if not isinstance(result, CallToolResult) or result.is_error:
                msg = f"MCP read check failed: {name}/{action}"
                raise RuntimeError(msg)
            data = object_json(result.structured_content)
            check: Object = {"tool": name, "action": action, "ok": True}
            if operation == "hermes_agents":
                check["profiles"] = [x["name"] for x in rows(data["agents"])]
            if operation == "hermes_assignees":
                check["devops"] = next((x for x in rows(data["assignees"]) if x["name"] == "devops"), None)
            if operation == "hermes_info":
                check["native_version"] = data["version"]
            checks.append(check)
        report["checks"] = object_json({"items": checks})["items"]
    await asyncio.to_thread(output.write_text, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    passed = sum(check.get("ok") is True for check in checks)
    print(f"Validated {passed} native reads and {len(discovery.tools)} MCP tools; report: {output}")


def main() -> None:
    """Parse the smoke configuration and destination report path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.getLogger().setLevel(logging.WARNING)
    asyncio.run(smoke(args.config, args.output))


if __name__ == "__main__":
    main()
