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
class ProfileToolsTests(unittest.TestCase):
    def test_native_consumers_keep_default_reads_and_writes_in_the_selected_profile(self) -> None:
        """A missing profile binding or worker MCP pin must break this real transport check."""
        process = subprocess.run(  # noqa: S603 - fixed local script and explicitly selected interpreter
            [
                str(NATIVE_PYTHON),
                "-m",
                "tests.native_profile_probe",
                "--hermes-repo",
                str(REPO),
            ],
            cwd=PACKAGE_ROOT,
            env={key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr[-4000:] + process.stdout[-4000:])
        report = json.loads(process.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["model_turns"], 0)
        self.assertTrue(report["isolated"])
        for consumer in ("chat", "kanban"):
            checks = [row for row in report["checks"] if row["consumer"] == consumer]
            self.assertEqual([row["profile"] for row in checks], ["profile_a", "profile_b", "profile_a"])
            self.assertTrue(all(row["read_profile"] == row["profile"] for row in checks))
            self.assertTrue(all(row["write_profile"] == row["profile"] for row in checks))
            self.assertTrue(
                all(row["revision_changed"] and row["other_profiles_unchanged"] for row in checks)
            )
            self.assertTrue(all(row["tool_count"] == 10 for row in checks))
            self.assertTrue(all(row["managed_write_denied"] for row in checks))
            self.assertTrue(all(row["managed_model"] == "managed-test" for row in checks))
            self.assertTrue(all(row["owner_board_read"] for row in checks))
            self.assertTrue(all(row["worker_board_denied"] for row in checks))
        self.assertTrue(report["generated_connection"])
