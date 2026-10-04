# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Installer source isolation and runtime bundle boundaries."""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hermes_bridge.core import BridgeError, Config
from scripts.install import install
from tests.support import REPO

PROJECT = Path(__file__).resolve().parents[1]


def checkout(root: Path) -> Path:
    source = root / "checkout"
    source.mkdir()
    for name in ("__init__.py", "plugin.yaml", "LICENSE"):
        shutil.copy2(PROJECT / name, source / name)
    assets = source / "assets"
    assets.mkdir()
    for name in ("icon.svg", "mark.svg", "cover.svg"):
        assets.joinpath(name).write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    assets.joinpath("private.key").write_text("private-value", encoding="utf-8")
    desktop = source / "desktop"
    desktop.mkdir()
    desktop.joinpath("plugin.js").write_text(
        'export default { id: "http-mcp", name: "Hermes Link", register() {} };\n', encoding="utf-8"
    )
    desktop.joinpath(".env").write_text("TOKEN=private-value", encoding="utf-8")
    scripts = source / "scripts"
    scripts.mkdir()
    scripts.joinpath("install.py").write_text("# Installer entry point\n", encoding="utf-8")
    package = source / "hermes_bridge"
    package.mkdir()
    for name in ("__init__.py", "main.py", "core.py", "py.typed"):
        shutil.copy2(PROJECT / "hermes_bridge" / name, package / name)
    locales = package / "locales"
    locales.mkdir()
    for catalog in PROJECT.joinpath("hermes_bridge/locales").glob("*.json"):
        shutil.copy2(catalog, locales / catalog.name)
    locales.joinpath("private.json").write_text('"private-value"', encoding="utf-8")
    child = package / "subpackage"
    child.mkdir()
    child.joinpath("__init__.py").write_text("# Runtime subpackage\n", encoding="utf-8")
    child.joinpath("reader.py").write_text("VALUE = 1\n", encoding="utf-8")
    return source


class InstallSafetyTests(unittest.TestCase):
    def test_source_contained_destinations_are_refused_before_creating_or_changing_files(self) -> None:
        cases = (
            ("runtime", "child"),
            ("home", "child"),
            ("runtime", "source"),
            ("home", "source"),
            ("runtime", "alias"),
            ("home", "alias"),
        )
        for field, location in cases:
            with self.subTest(field=field, location=location), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                source = checkout(root)
                contained = source / "state" if location == "child" else source
                if location == "alias":
                    alias = root / "source-alias"
                    alias.symlink_to(source, target_is_directory=True)
                    contained = alias
                home = contained if field == "home" else root / "home"
                runtime = contained if field == "runtime" else root / "runtime"
                before = sorted(root.rglob("*"))
                permissions = source.stat().st_mode
                copy_message = "unsafe copy boundary reached"
                reached_copy = RuntimeError(copy_message)
                error: Exception | None = None
                with (
                    patch("scripts.install.__file__", str(source / "scripts/install.py")),
                    # Protect the old implementation from recursively copying its own stage.
                    patch("scripts.install.shutil.copytree", side_effect=reached_copy),
                    redirect_stdout(io.StringIO()),
                ):
                    try:
                        install(REPO, home, runtime)
                    except (BridgeError, RuntimeError) as caught:
                        error = caught
                self.assertIsInstance(error, BridgeError)
                assert isinstance(error, BridgeError)
                self.assertEqual(error.code, "invalid_install")
                self.assertEqual(sorted(root.rglob("*")), before)
                self.assertEqual(source.stat().st_mode, permissions)

    def test_runtime_bundle_keeps_native_entry_point_manifest_and_package_without_local_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = checkout(root)
            package = source / "hermes_bridge"
            for location in (source, package, package / "subpackage"):
                location.joinpath(".env").write_text("API_KEY=private-value\n", encoding="utf-8")
                location.joinpath(".env.local").write_text("TOKEN=private-value\n", encoding="utf-8")
                location.joinpath("local.key").write_text("private-value\n", encoding="utf-8")
                for artifact in ("venv", ".venv", ".quality-venv", "build", "dist", "__pycache__", "tests"):
                    folder = location / artifact
                    folder.mkdir()
                    folder.joinpath("__init__.py").write_text("# Development artifact\n", encoding="utf-8")
                    folder.joinpath("artifact.py").write_text("SECRET = 'private-value'\n", encoding="utf-8")
            home, runtime = root / "home", root / "runtime"
            with (
                patch("scripts.install.__file__", str(source / "scripts/install.py")),
                patch("pathlib.Path.home", return_value=root),
                redirect_stdout(io.StringIO()),
            ):
                install(REPO, home, runtime)
            deployed = home / "plugins/http-mcp"
            deployed_files = {
                path.relative_to(deployed).as_posix() for path in deployed.rglob("*") if path.is_file()
            }
            self.assertEqual(
                deployed_files,
                {
                    "__init__.py",
                    "plugin.yaml",
                    "LICENSE",
                    "assets/icon.svg",
                    "assets/mark.svg",
                    "assets/cover.svg",
                    "desktop/plugin.js",
                    "hermes_bridge/__init__.py",
                    "hermes_bridge/main.py",
                    "hermes_bridge/core.py",
                    "hermes_bridge/py.typed",
                    "hermes_bridge/subpackage/__init__.py",
                    "hermes_bridge/subpackage/reader.py",
                    *{
                        "hermes_bridge/locales/" + path.name
                        for path in PROJECT.joinpath("hermes_bridge/locales").glob("*.json")
                    },
                },
            )
            for name in ("__init__.py", "LICENSE", "hermes_bridge/main.py"):
                self.assertEqual((deployed / name).read_bytes(), (source / name).read_bytes())
            self.assertNotIn("private-value", "".join(path.read_text() for path in deployed.rglob("*.py")))
            self.assertEqual(Config.load(runtime / "config.json").auth_enabled, True)
            config = Config.load(runtime / "config.json")
            self.assertEqual(config.hermes_repo, REPO)
            self.assertTrue(runtime.joinpath("mcp.key").is_file())
