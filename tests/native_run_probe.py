# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Run the real Hermes engine offline in a subprocess with disposable native stores."""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.core import Config, Object
from hermes_bridge.native import Native
from hermes_bridge.runs import TERMINAL, Runs


def finish(runs: Runs, accepted: Object) -> Object:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        runs.wait_idle()
        status = runs.status(str(accepted["task_id"]))
        if status["status"] in TERMINAL:
            return status
    message = "Native offline turn did not finish within 30 seconds"
    raise TimeoutError(message)


def main() -> None:
    repo, home = (Path(value) for value in sys.argv[1:])
    native = Native(Config(repo, home / "key", ("default",), ("default",), writes=True))
    if native.home("default").resolve() != home.resolve():
        message = "Native profile resolved outside the disposable test store"
        raise AssertionError(message)
    runs = Runs(native)
    agent = importlib.import_module("run_agent")
    completions = importlib.import_module("openai.types.chat")
    response = completions.ChatCompletion(
        id="offline-fixture",
        created=1,
        model="gpt-4o-mini",
        object="chat.completion",
        choices=[
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "SMOKE_OK"}}
        ],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )
    runtime = {
        "provider": "custom",
        "requested_provider": "custom",
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1",
        "api_mode": "chat_completions",
    }
    try:
        # Only remote credential/model boundaries are replaced. Native imports,
        # AIAgent construction, conversation loop, histories and DB writes are real.
        with (
            patch("hermes_cli.runtime_provider.resolve_runtime_with_fallback", return_value=(runtime, None)),
            patch.object(agent.AIAgent, "_interruptible_api_call", return_value=response),
            patch.object(agent.AIAgent, "_interruptible_streaming_api_call", return_value=response),
            patch("socket.socket.connect", side_effect=OSError("Network disabled in offline probe")),
        ):
            accepted = runs.submit("default", "Reply SMOKE_OK", "first")
            if accepted["status"] == "failed":
                message = "Native task was rejected before execution: " + json.dumps(accepted)
                raise AssertionError(message)
            immediate = native.session(str(accepted["session_id"]), "default")
            first = finish(runs, accepted)
            second = finish(runs, runs.continue_task(str(first["task_id"]), "Reply again", "second"))
            session = native.session(str(first["session_id"]), "default")
            events = native.events("session", "default", "default", "", str(first["session_id"]), "", 10)
            report = {
                "first": first,
                "second": second,
                "immediate": immediate,
                "session": session,
                "events": events,
            }
            sys.stdout.write("NATIVE_RUN_REPORT=" + json.dumps(report) + "\n")
    finally:
        runs.close()


if __name__ == "__main__":
    main()
