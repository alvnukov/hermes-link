# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Native presentation translations preserve the settings contract."""

from __future__ import annotations

import ast
import importlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from hermes_bridge.core import BridgeError
from hermes_bridge.presentation import localize_plugin, main, select_language
from tests.support import REPO

PROJECT = Path(__file__).resolve().parents[1]


class CatalogTests(unittest.TestCase):
    def test_catalogs_cover_every_native_language_and_setting_without_losing_tool_ids(self) -> None:
        native = (REPO / "apps/desktop/src/i18n/languages.ts").read_text()
        desktop_ids = set(re.findall(r"id: '([^']+)'", native.split("] as const", 1)[0]))
        core = (REPO / "agent/i18n.py").read_text().split("SUPPORTED_LANGUAGES:", 1)[1]
        locale_ids = set(re.findall(r'"([^"]+)"', core.split("DEFAULT_LANGUAGE", 1)[0]))
        self.assertTrue(desktop_ids <= locale_ids)
        catalogs = PROJECT / "hermes_bridge/locales"
        self.assertEqual({path.stem for path in catalogs.glob("*.json")}, locale_ids)
        manifest = yaml.safe_load(PROJECT.joinpath("plugin.yaml").read_text())
        fields = manifest["config_schema"]
        for locale in sorted(locale_ids):
            with self.subTest(locale=locale):
                document = json.loads(catalogs.joinpath(locale + ".json").read_text())
                self.assertEqual(set(document), {"description", "fields"})
                self.assertTrue(document["description"].strip())
                self.assertEqual(set(document["fields"]), set(fields))
                for key, field in document["fields"].items():
                    self.assertEqual(set(field), {"label", "description"})
                    self.assertTrue(all(isinstance(value, str) and value.strip() for value in field.values()))
                    if key.startswith("tool_"):
                        self.assertIn(key.removeprefix("tool_"), field["description"])
                for token in ("32\u2013256", "A\u2013Z", "a\u2013z", "0\u20139", "Bearer"):
                    self.assertIn(token, document["fields"]["auth_token"]["description"])
                self.assertIn("127.0.0.1", document["fields"]["auth_enabled"]["description"])


class PresentationTests(unittest.TestCase):
    def test_auto_uses_profile_language_and_handles_native_region_aliases(self) -> None:

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertEqual(select_language("auto", home), "en")
            for value, expected in (
                ("ru-RU", "ru"),
                ("fr_CA", "fr"),
                ("ar-SA", "ar"),
                ("zh-TW", "zh-hant"),
                ("zh_Hant_HK", "zh-hant"),
                ("zh-Hans-CN", "zh"),
                ("pt-br", "pt"),
                ("unknown", "en"),
                (None, "en"),
            ):
                with self.subTest(value=value):
                    home.joinpath("config.yaml").write_text(yaml.safe_dump({"display": {"language": value}}))
                    self.assertEqual(select_language("auto", home), expected)
            self.assertEqual(select_language("de", home), "de")

    def test_explicit_unknown_language_and_invalid_config_fail_clearly(self) -> None:

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with self.assertRaises(BridgeError) as error:
                select_language("../../outside", home)
            self.assertEqual(error.exception.code, "unsupported_language")
            for contents in ("[invalid", "[a, b]", "[]", "0", "display: text\n"):
                home.joinpath("config.yaml").write_text(contents)
                with self.assertRaises(BridgeError) as error:
                    select_language("auto", home)
                self.assertEqual(error.exception.code, "invalid_config")

    def test_native_form_translates_all_fields_without_changing_policy_or_credentials(self) -> None:

        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary) / "http-mcp"
            plugin.mkdir()
            plugin.joinpath("desktop").mkdir()
            for name in ("plugin.yaml", "desktop/plugin.js"):
                shutil.copy2(PROJECT / name, plugin / name)
            plugin.joinpath("config.yaml").write_text("secret: user policy\n")
            plugin.joinpath(".env").write_text("TOKEN=do-not-touch\n")
            original = yaml.safe_load(plugin.joinpath("plugin.yaml").read_text())
            sys.path.insert(0, str(REPO))
            settings = importlib.import_module("hermes_cli.plugins_settings")
            native = importlib.import_module("hermes_constants")
            scope = native.set_hermes_home_override(plugin)
            self.addCleanup(native.reset_hermes_home_override, scope)
            catalogs = PROJECT / "hermes_bridge/locales"
            for language in [*[path.stem for path in sorted(catalogs.glob("*.json"))], "en"]:
                localize_plugin(plugin, language)
                actual = yaml.safe_load(plugin.joinpath("plugin.yaml").read_text())
                self.assertEqual(actual["name"], "http-mcp")
                self.assertEqual(actual["version"], original["version"])
                self.assertEqual(set(actual["config_schema"]), set(original["config_schema"]))
                for key, field in original["config_schema"].items():
                    for property_name in set(field) - {"label", "description"}:
                        self.assertEqual(actual["config_schema"][key][property_name], field[property_name])
                expected = json.loads(
                    PROJECT.joinpath("hermes_bridge/locales", language + ".json").read_text()
                )
                fields = {
                    field["key"]: field for field in settings.plugin_settings_fields("http-mcp", plugin)
                }
                self.assertEqual(fields["writes"]["label"], expected["fields"]["writes"]["label"])
                self.assertNotIn("value", fields["auth_token"])
                self.assertEqual(fields["auth_token"]["env"], "HERMES_HTTP_MCP_TOKEN")
                self.assertEqual(actual["description"], expected["description"])
                self.assertEqual(
                    actual["config_schema"]["writes"]["label"], expected["fields"]["writes"]["label"]
                )
                self.assertIn(
                    json.dumps(expected["description"], ensure_ascii=True),
                    plugin.joinpath("desktop/plugin.js").read_text(),
                )
            self.assertEqual(plugin.joinpath("config.yaml").read_text(), "secret: user policy\n")
            self.assertEqual(plugin.joinpath(".env").read_text(), "TOKEN=do-not-touch\n")

    def test_mismatched_schema_or_symlink_is_refused_before_writing(self) -> None:

        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary)
            plugin.joinpath("desktop").mkdir()
            manifest = {"name": "http-mcp", "config_schema": {"custom": {"type": "str"}}}
            plugin.joinpath("plugin.yaml").write_text(yaml.safe_dump(manifest))
            plugin.joinpath("desktop/plugin.js").write_text("unchanged")
            before = plugin.joinpath("plugin.yaml").read_bytes()
            with self.assertRaises(BridgeError):
                localize_plugin(plugin, "en")
            self.assertEqual(plugin.joinpath("plugin.yaml").read_bytes(), before)
            self.assertEqual(plugin.joinpath("desktop/plugin.js").read_text(), "unchanged")
            plugin.joinpath("plugin.yaml").unlink()
            plugin.joinpath("plugin.yaml").symlink_to(PROJECT / "plugin.yaml")
            with self.assertRaises(BridgeError):
                localize_plugin(plugin, "en")
            self.assertEqual(plugin.joinpath("desktop/plugin.js").read_text(), "unchanged")


class PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = Path(self.enterContext(tempfile.TemporaryDirectory())) / "http-mcp"
        self.plugin.mkdir()
        self.plugin.joinpath("desktop").mkdir()
        for name in ("plugin.yaml", "desktop/plugin.js"):
            shutil.copy2(PROJECT / name, self.plugin / name)
        self.before = {
            name: self.plugin.joinpath(name).read_bytes() for name in ("plugin.yaml", "desktop/plugin.js")
        }

    def test_failed_manifest_publish_restores_companion_and_removes_temporary_files(self) -> None:

        real_replace = Path.replace
        failed = False

        def fail_once(path: Path, target: Path) -> Path:
            nonlocal failed
            if target.name == "plugin.yaml" and not failed:
                failed = True
                message = "disk unavailable"
                raise OSError(message)
            return real_replace(path, target)

        with (
            patch("hermes_bridge.presentation.Path.replace", side_effect=fail_once, autospec=True),
            self.assertRaises(BridgeError) as error,
        ):
            localize_plugin(self.plugin, "en")
        self.assertEqual(error.exception.code, "presentation_update_failed")
        for name, contents in self.before.items():
            self.assertEqual(self.plugin.joinpath(name).read_bytes(), contents)
        self.assertEqual(list(self.plugin.rglob(".link-presentation-*")), [])

    def test_missing_catalog_fails_without_touching_the_installed_plugin(self) -> None:

        empty = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (
            patch("hermes_bridge.presentation.files", return_value=empty),
            self.assertRaises(BridgeError) as error,
        ):
            localize_plugin(self.plugin, "en")
        self.assertEqual(error.exception.code, "localization_unavailable")
        for name, contents in self.before.items():
            self.assertEqual(self.plugin.joinpath(name).read_bytes(), contents)

    def test_invalid_catalog_strings_are_refused_without_touching_metadata(self) -> None:

        empty = Path(self.enterContext(tempfile.TemporaryDirectory()))
        empty.joinpath("locales").mkdir()
        for bad in (
            "[not json",
            "[]",
            '{"description": "", "fields": {}}',
            '{"description": "test", "fields": {"writes": {"label": "", "description": "test"}}}',
        ):
            with self.subTest(catalog=bad):
                empty.joinpath("locales/en.json").write_text(bad)
                with (
                    patch("hermes_bridge.presentation.files", return_value=empty),
                    self.assertRaises(BridgeError) as error,
                ):
                    localize_plugin(self.plugin, "en")
                self.assertEqual(error.exception.code, "localization_unavailable")
                for name, contents in self.before.items():
                    self.assertEqual(self.plugin.joinpath(name).read_bytes(), contents)

    def test_cli_applies_the_profile_language_and_reports_unsupported_choice(self) -> None:

        self.plugin.joinpath("config.yaml").write_text("display:\n  language: ja\n")
        options = ["presentation", "--hermes-home", str(self.plugin), "--plugin-dir", str(self.plugin)]
        output = io.StringIO()
        with patch("sys.argv", options), redirect_stdout(output):
            main()
        self.assertIn("language: ja", output.getvalue())
        expected = json.loads(PROJECT.joinpath("hermes_bridge/locales/ja.json").read_text())
        actual = yaml.safe_load(self.plugin.joinpath("plugin.yaml").read_text())
        self.assertEqual(actual["config_schema"]["writes"]["label"], expected["fields"]["writes"]["label"])
        errors = io.StringIO()
        with (
            patch("sys.argv", [*options, "--language", "not-supported"]),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as exit_status,
        ):
            main()
        self.assertEqual(exit_status.exception.code, 1)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "unsupported_language")


class NativeAliasTests(unittest.TestCase):
    def test_profile_language_matches_every_bundled_native_alias(self) -> None:
        tree = ast.parse((REPO / "agent/i18n.py").read_text())
        entry = next(
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_LANGUAGE_ALIASES"
        )
        assert isinstance(entry, ast.AnnAssign)
        assert entry.value is not None
        aliases = ast.literal_eval(entry.value)
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for alias, expected in aliases.items():
            with self.subTest(alias=alias):
                self.assertEqual(select_language(alias, home), expected)
                home.joinpath("config.yaml").write_text(yaml.safe_dump({"display": {"language": alias}}))
                self.assertEqual(select_language("auto", home), expected)
