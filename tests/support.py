# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Portable discovery of the optional native Hermes integration checkout."""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(os.environ.get("TEST_HERMES_REPO", str(Path.home() / ".hermes" / "hermes-agent")))
