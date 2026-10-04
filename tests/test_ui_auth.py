# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from hermes_bridge.core import BridgeError
from hermes_bridge.main import load_effective_config
from hermes_bridge.server import create_server, http_app
from tests.test_settings import fixture


class UIAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 10
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous)

    async def test_native_ui_token_authorizes_http_without_secret_export(self) -> None:
        with fixture("model: original\n") as (settings, home):
            home.joinpath(".env").chmod(0o600)
            settings.module.save_env_value("HERMES_HTTP_MCP_TOKEN", "u" * 48)
            key = settings.native.config.token_file
            key.write_text("f" * 48)
            key.chmod(0o600)
            path = home / "bridge.json"
            path.write_text(
                json.dumps(
                    {
                        "hermes_repo": str(settings.native.config.hermes_repo),
                        "token_file": str(key),
                        "profiles": ["*"],
                        "boards": ["default"],
                    }
                )
            )
            config = load_effective_config(path)
            server, surface = create_server(config)
            try:
                app = http_app(server, config)
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://127.0.0.1:18788",
                    ) as client,
                ):
                    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                    headers = {"Accept": "application/json, text/event-stream"}
                    new = await client.post(
                        "/mcp", json=payload, headers={**headers, "Authorization": "Bearer " + "u" * 48}
                    )
                    old = await client.post(
                        "/mcp", json=payload, headers={**headers, "Authorization": "Bearer " + "f" * 48}
                    )
                    self.assertEqual(new.status_code, 200)
                    self.assertEqual(old.status_code, 401)
                exported = surface.settings.get("default")
                self.assertNotIn("u" * 48, json.dumps(exported))
                self.assertNotIn("u" * 48, repr(config))
                self.assertNotIn("u" * 48, home.joinpath("config.yaml").read_text())
            finally:
                surface.runs.close()

    async def test_invalid_stored_ui_token_blocks_fallback_but_auth_off_can_start(self) -> None:
        with fixture("model: original\n") as (settings, home):
            home.joinpath(".env").chmod(0o600)
            key = settings.native.config.token_file
            key.write_text("f" * 48)
            key.chmod(0o600)
            path = home / "bridge.json"
            runtime: dict[str, object] = {
                "hermes_repo": str(settings.native.config.hermes_repo),
                "token_file": str(key),
                "profiles": ["*"],
                "boards": ["default"],
            }
            path.write_text(json.dumps(runtime))
            for token in ("", "short", "u" * 257):
                with self.subTest(token_length=len(token)):
                    settings.module.save_env_value("HERMES_HTTP_MCP_TOKEN", token)
                    config = load_effective_config(path)
                    server, surface = create_server(config)
                    try:
                        with self.assertRaises(BridgeError) as error:
                            http_app(server, config)
                        self.assertEqual(error.exception.code, "invalid_token")
                    finally:
                        surface.runs.close()
            runtime["auth_enabled"] = False
            path.write_text(json.dumps(runtime))
            config = load_effective_config(path)
            server, surface = create_server(config)
            try:
                http_app(server, config)
            finally:
                surface.runs.close()

    async def test_ui_secret_file_must_be_private_regular_file(self) -> None:
        with fixture("model: original\n") as (settings, home):
            key = settings.native.config.token_file
            key.write_text("f" * 48)
            key.chmod(0o600)
            path = home / "bridge.json"
            path.write_text(
                json.dumps(
                    {
                        "hermes_repo": str(settings.native.config.hermes_repo),
                        "token_file": str(key),
                        "profiles": ["*"],
                        "boards": ["default"],
                    }
                )
            )
            settings.module.save_env_value("HERMES_HTTP_MCP_TOKEN", "u" * 48)
            env = home / ".env"
            env.chmod(0o644)
            config = load_effective_config(path)
            server, surface = create_server(config)
            try:
                with self.assertRaises(BridgeError):
                    http_app(server, config)
                env.chmod(0o600)
                with tempfile.TemporaryDirectory() as root:
                    target = Path(root) / "env"
                    env.rename(target)
                    env.symlink_to(target)
                    with self.assertRaises(BridgeError):
                        http_app(server, config)
                    env.unlink()
                    os.mkfifo(env, 0o600)
                    with self.assertRaises(BridgeError):
                        http_app(server, config)
            finally:
                surface.runs.close()


if __name__ == "__main__":
    unittest.main()
