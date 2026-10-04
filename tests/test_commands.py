# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import argparse
import asyncio
import importlib
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import patch

import httpx

from hermes_bridge import main
from hermes_bridge.actions import Actions
from hermes_bridge.core import object_json, rows
from hermes_bridge.server import create_server, http_app
from scripts.smoke import smoke
from tests.test_settings import fixture


class CommandTests(unittest.TestCase):
    def test_stdio_command_closes_execution_resources(self) -> None:
        with fixture() as (settings, home):
            server, surface = create_server(settings.native.config)
            with (
                patch("hermes_bridge.main.load_effective_config", return_value=settings.native.config),
                patch("hermes_bridge.server.create_server", return_value=(server, surface)),
                patch.object(server, "run_stdio_async"),
                patch.object(surface.runs, "close", wraps=surface.runs.close) as close,
            ):
                main.command(argparse.Namespace(config=home / "unused", transport="stdio"))
                self.assertEqual(close.call_count, 1)

    def test_http_listener_failure_closes_resources_and_sanitizes_error(self) -> None:
        with fixture() as (settings, home):
            key = settings.native.config.token_file
            key.write_text("a" * 48)
            key.chmod(0o600)
            server, surface = create_server(settings.native.config)
            stderr = io.StringIO()
            with (
                patch("hermes_bridge.main.load_effective_config", return_value=settings.native.config),
                patch("hermes_bridge.server.create_server", return_value=(server, surface)),
                patch("uvicorn.run", side_effect=OSError("SECRET_VALUE")),
                patch.object(surface.runs, "close", wraps=surface.runs.close) as close,
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as error,
            ):
                main.command(argparse.Namespace(config=home / "unused", transport="http"))
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(close.call_count, 1)
            self.assertNotIn("SECRET_VALUE", stderr.getvalue())

    def test_missing_sdk_has_explicit_safe_startup_error(self) -> None:
        stderr = io.StringIO()
        with (
            patch("hermes_bridge.main.load_effective_config", side_effect=ImportError("SECRET_VALUE")),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as error,
        ):
            main.command(argparse.Namespace(config="unused", transport="http"))
        self.assertEqual(error.exception.code, 1)
        self.assertIn("dependencies are unavailable", stderr.getvalue())
        self.assertNotIn("SECRET_VALUE", stderr.getvalue())


class SmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 10

    async def test_smoke_runs_with_restricted_owner_and_board_without_devops(self) -> None:
        with fixture() as (settings, home):
            key = settings.native.config.token_file
            key.write_text("a" * 48)
            key.chmod(0o600)
            Actions(settings.native).profile_create("work", "test", {})
            config = replace(
                settings.native.config, profiles=("work",), boards=("team",), board_profile="work"
            )
            secret_scope = importlib.import_module("agent.secret_scope")
            launch_policy = importlib.import_module("tui_gateway.launch_profile_policy")
            previous_mode = secret_scope.is_multiplex_active()
            self.enterContext(patch.object(launch_policy, "_snapshot", launch_policy._snapshot))
            server, surface = create_server(config)
            with surface.native.board_connection("team", write=True):
                pass
            app = http_app(server, config)
            output = home / "report.json"

            def client_factory(headers: dict[str, str]) -> httpx.AsyncClient:
                return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers)

            try:
                with (
                    patch("scripts.smoke.load_effective_config", return_value=config),
                    patch("scripts.smoke.create_mcp_http_client", side_effect=client_factory),
                    redirect_stdout(io.StringIO()),
                ):
                    async with app.router.lifespan_context(app):
                        await smoke(home / "unused.json", output)
                report = object_json(json.loads(output.read_text()))
                self.assertEqual(len(rows(report["checks"])), 17)
                tools = report["tools"]
                self.assertIsInstance(tools, list)
                if isinstance(tools, list):
                    self.assertEqual(len(tools), 10)
                self.assertTrue(all(check.get("ok") for check in rows(report["checks"])))
                self.assertNotIn("a" * 48, json.dumps(report))
            finally:
                surface.runs.close()
                # Native secondary-profile reads intentionally activate a process-wide host mode.
                # Restore that process state after the isolated integration test, never during it.
                secret_scope.set_multiplex_active(previous_mode)
