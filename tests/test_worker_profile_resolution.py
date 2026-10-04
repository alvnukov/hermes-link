# Copyright (c) 2026 Hermes HTTP MCP contributors
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
NATIVE_PYTHON = REPO / "venv" / "bin" / "python"


@unittest.skipUnless(NATIVE_PYTHON.is_file(), "Native Hermes interpreter is unavailable")
class WorkerProfileResolutionTests(unittest.TestCase):
    def test_spawned_native_cli_resolves_the_assigned_profile(self) -> None:
        """A worker HOME/root mismatch must fail at the real CLI profile selector."""
        process = subprocess.run(  # noqa: S603 - trusted local probe and selected interpreter
            [
                str(NATIVE_PYTHON),
                "-B",
                "-m",
                "tests.native_worker_profile_probe",
                "--hermes-repo",
                str(REPO),
                "--profile",
                "default",
                "--profile",
                "worker_probe",
                "--launch-profile",
                "launcher_profile",
            ],
            cwd=PACKAGE_ROOT,
            env={key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr[-2500:] + process.stdout[-2500:])
        report = json.loads(process.stdout)
        self.assertTrue(report["ok"])
        self.assertTrue(report["isolated"])
        self.assertTrue(report["native_spawn"])
        self.assertEqual(report["launch_profile"], "launcher_profile")
        self.assertEqual(report["model_turns"], 0)
        self.assertEqual([row["profile"] for row in report["checks"]], ["default", "worker_probe"])
        for row in report["checks"]:
            self.assertEqual(row["exit_code"], 0)
            self.assertTrue(row["profile_resolved"])
            self.assertTrue(row["help_reached"])
            self.assertFalse(row["profile_missing"])
