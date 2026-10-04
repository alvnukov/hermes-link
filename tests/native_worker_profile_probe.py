# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Check native Kanban spawning and CLI profile resolution without running a model."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

from hermes_bridge.core import Object, object_json

MARKER = "WORKER_PROFILE_PROBE:"
CHILD = """import json, os, runpy, sys
sys.path.insert(0, {repo})
profile = sys.argv[sys.argv.index("-p") + 1]
sys.argv.append("--help")
try:
    runpy.run_module("hermes_cli.main", run_name="__main__")
except SystemExit as exc:
    if exc.code not in (None, 0):
        raise
print({marker} + json.dumps({{"profile": profile, "home": os.environ.get("HERMES_HOME")}}))
"""


def wait_child(pid: int) -> int:
    """Bound a real spawned worker's lifetime and reap it even after a timeout."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        child, status = os.waitpid(pid, os.WNOHANG)
        if child:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    msg = "Native profile-only worker exceeded its timeout"
    raise RuntimeError(msg)


def prepare(
    sandbox: Path, repo: Path, names: list[str], launch_profile: str, shared_install: Path | None
) -> Path:
    """Bind empty disposable profiles; never copy user configuration or credentials."""
    root = sandbox / ".hermes"
    for name in ["default", launch_profile, *names]:
        home = root if name == "default" else root / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        home.joinpath("config.yaml").write_text('{"platform_toolsets":{"cli":["file"]}}\n', encoding="utf-8")
        home.joinpath(".env").write_text("", encoding="utf-8")
    if shared_install is not None:
        installs = root / "installs"
        installs.mkdir()
        installs.joinpath(shared_install.name).symlink_to(shared_install, target_is_directory=True)
    shim = sandbox / "profile-only-hermes"
    shim.write_text(
        f"#!{sys.executable}\n" + CHILD.format(repo=repr(str(repo)), marker=repr(MARKER)),
        encoding="utf-8",
    )
    shim.chmod(0o700)
    safe = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
    os.environ.clear()
    os.environ.update(safe)
    os.environ.update(
        {
            "HOME": str(sandbox),
            "HERMES_HOME": str(root if launch_profile == "default" else root / "profiles" / launch_profile),
            "HERMES_BIN": str(shim),
            "HERMES_RUNTIME_DIR": str(sandbox / "runtime"),
            "HERMES_MANAGED_DIR": str(sandbox / "managed"),
            "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_STATE_DB_GUARD_BYPASS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
        }
    )
    sys.path.insert(0, str(repo))
    os.chdir(sandbox)
    return root


def verify(repo: Path, names: list[str], launch_profile: str, shared_install: Path | None) -> Object:
    """Run the real native spawn path; stop each CLI at help before agent execution."""
    with tempfile.TemporaryDirectory(prefix="hermes-worker-profile-") as directory:
        sandbox = Path(directory)
        root = prepare(sandbox, repo, names, launch_profile, shared_install)
        native = importlib.import_module("hermes_cli.kanban_db")
        dispatch = importlib.import_module("hermes_cli.kanban_db_dispatch")
        checks: list[Object] = []
        for index, name in enumerate(names):
            task = native.Task(
                id=f"profile_probe_{index}",
                title="Isolated worker profile verification",
                body=None,
                assignee=name,
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
            pid = dispatch._default_spawn(task, str(sandbox), board="default")
            if not isinstance(pid, int):
                msg = "Native dispatcher returned no child process"
                raise TypeError(msg)
            exit_code = wait_child(pid)
            log = (root / "kanban" / "logs" / f"{task.id}.log").read_text(encoding="utf-8")
            records = [
                object_json(json.loads(line[len(MARKER) :]))
                for line in log.splitlines()
                if line.startswith(MARKER)
            ]
            wanted = root if name == "default" else root / "profiles" / name
            resolved = len(records) == 1 and records[0] == {"profile": name, "home": str(wanted)}
            checks.append(
                {
                    "profile": name,
                    "exit_code": exit_code,
                    "profile_resolved": resolved,
                    "help_reached": "usage:" in log.lower(),
                    "profile_missing": "does not exist" in log,
                }
            )
        return object_json(
            {
                "ok": all(
                    row["exit_code"] == 0 and row["profile_resolved"] and row["help_reached"]
                    for row in checks
                ),
                "isolated": True,
                "model_turns": 0,
                "native_spawn": True,
                "execution_stop": "CLI --help before agent construction",
                "launch_profile": launch_profile,
                "shared_dependency_runtime": shared_install is not None,
                "checks": checks,
            }
        )


def main() -> None:
    """Emit only the safe profile-resolution report, keeping native output separate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-repo", type=Path, required=True)
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--launch-profile", default="default")
    parser.add_argument("--shared-install-state", action="store_true")
    args = parser.parse_args()
    names: list[str] = args.profile
    if not names or any(
        not name or not all(c.isalnum() or c in "_-" for c in name) for name in [*names, args.launch_profile]
    ):
        parser.error("Profiles must be nonempty identifiers")
    repo = args.hermes_repo.resolve()
    shared_install: Path | None = None
    if args.shared_install_state:
        sys.path.insert(0, str(repo))
        environments = importlib.import_module("pm.environments")
        shared_install = Path(environments.install_state_dir(repo))
    with redirect_stdout(sys.stderr):
        result = verify(repo, names, args.launch_profile, shared_install)
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
