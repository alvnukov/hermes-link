# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Render localized native plugin metadata without changing Hermes settings."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import TypedDict

import yaml

from .core import BridgeError

LANGUAGES = (
    "en",
    "zh",
    "zh-hant",
    "ja",
    "de",
    "es",
    "fr",
    "tr",
    "uk",
    "af",
    "ko",
    "it",
    "ga",
    "pt",
    "ru",
    "hu",
    "ar",
)


# Hermes 0.21.5 spellings; presentation works without importing the agent runtime.
_ALIASES: dict[str, str] = {
    "english": "en",
    "en-us": "en",
    "en-gb": "en",
    "chinese": "zh",
    "mandarin": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-sg": "zh",
    "traditional-chinese": "zh-hant",
    "traditional_chinese": "zh-hant",
    "zh-tw": "zh-hant",
    "zh-hk": "zh-hant",
    "zh-mo": "zh-hant",
    "japanese": "ja",
    "jp": "ja",
    "ja-jp": "ja",
    "german": "de",
    "deutsch": "de",
    "de-de": "de",
    "de-at": "de",
    "de-ch": "de",
    "spanish": "es",
    "espa\u00f1ol": "es",
    "espanol": "es",
    "es-es": "es",
    "es-mx": "es",
    "es-ar": "es",
    "french": "fr",
    "fran\u00e7ais": "fr",
    "france": "fr",
    "fr-fr": "fr",
    "fr-be": "fr",
    "fr-ca": "fr",
    "fr-ch": "fr",
    "ukrainian": "uk",
    "ukrainisch": "uk",
    "\u0443\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430": "uk",
    "uk-ua": "uk",
    "ua": "uk",
    "turkish": "tr",
    "t\u00fcrk\u00e7e": "tr",
    "tr-tr": "tr",
    "afrikaans": "af",
    "af-za": "af",
    "korean": "ko",
    "\ud55c\uad6d\uc5b4": "ko",
    "ko-kr": "ko",
    "italian": "it",
    "italiano": "it",
    "it-it": "it",
    "it-ch": "it",
    "irish": "ga",
    "gaeilge": "ga",
    "ga-ie": "ga",
    "portuguese": "pt",
    "portugu\u00eas": "pt",
    "portugues": "pt",
    "pt-pt": "pt",
    "pt-br": "pt",
    "brazilian": "pt",
    "brasileiro": "pt",
    "russian": "ru",
    "\u0440\u0443\u0441\u0441\u043a\u0438\u0439": "ru",
    "ru-ru": "ru",
    "hungarian": "hu",
    "magyar": "hu",
    "hu-hu": "hu",
    "arabic": "ar",
    "\u0627\u0644\u0639\u0631\u0628\u064a\u0629": "ar",
    "ar-sa": "ar",
    "ar-eg": "ar",
    "ar-ae": "ar",
    "ar-ma": "ar",
    "ar-dz": "ar",
}


class Labels(TypedDict):
    """The translated strings owned by one native settings field."""

    label: str
    description: str


class Catalog(TypedDict):
    """Complete plugin presentation for one language."""

    description: str
    fields: dict[str, Labels]


def _mapping(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BridgeError(code, "Expected a mapping with string keys")
    return {str(key): item for key, item in value.items()}


def _yaml_document(path: Path, code: str) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise BridgeError(code, "Cannot read the YAML document") from exc
    return _mapping({} if value is None else value, code)


def _language(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    tag = value.strip().lower().replace("_", "-")
    alias = _ALIASES.get(tag)
    if alias is not None:
        return alias
    if tag.startswith(("zh-hant", "zh-tw", "zh-hk", "zh-mo")):
        return "zh-hant"
    base = tag.split("-", 1)[0]
    return base if base in LANGUAGES else None


def select_language(language: str, hermes_home: Path) -> str:
    """Choose a bundled language, following the profile when ``auto`` is selected."""
    if language != "auto":
        chosen = _language(language)
        if chosen is None:
            code = "unsupported_language"
            raise BridgeError(code, "Supported languages: " + ", ".join(LANGUAGES))
        return chosen
    config_path = hermes_home / "config.yaml"
    if not config_path.exists():
        return "en"
    code = "invalid_config"
    config = _yaml_document(config_path, code)
    display = _mapping(config.get("display") or {}, code)
    return _language(display.get("language")) or "en"


def _catalog(language: str) -> Catalog:
    if language not in LANGUAGES:
        code = "unsupported_language"
        raise BridgeError(code, "Select a supported language before rendering metadata")
    code = "localization_unavailable"
    try:
        raw = files("hermes_bridge").joinpath("locales", language + ".json").read_text(encoding="utf-8")
        document = _mapping(json.loads(raw), code)
    except (OSError, UnicodeError, ValueError) as exc:
        raise BridgeError(code, "Cannot read the bundled translation catalog") from exc
    description = document.get("description")
    if not isinstance(description, str) or not description.strip():
        raise BridgeError(code, "Translation catalog has no plugin description")
    fields = {}
    for key, value in _mapping(document.get("fields"), code).items():
        field = _mapping(value, code)
        label, hint = field.get("label"), field.get("description")
        if not isinstance(label, str) or not isinstance(hint, str) or not label.strip() or not hint.strip():
            raise BridgeError(code, "Translation catalog has an incomplete settings field")
        fields[key] = Labels(label=label, description=hint)
    return Catalog(description=description, fields=fields)


def _desktop_metadata(description: str) -> str:
    # JSON escaping also handles JS line separators and quotes in translated copy.
    return (
        "// Copyright (c) 2026 Hermes HTTP MCP contributors. MIT license.\n"
        "// Generated presentation only. Apply another language with hermes_bridge.presentation.\n"
        "export default {\n"
        "  id: 'http-mcp',\n"
        "  name: 'Hermes Link',\n"
        f"  description: {json.dumps(description, ensure_ascii=True)},\n"
        "  defaultEnabled: false,\n"
        "  register() {}\n"
        "};\n"
    )


def _atomic_text(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".link-presentation-", dir=path.parent)
    pending = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def localize_plugin(plugin_dir: Path, language: str) -> None:
    """Update only native description/labels and the inert Desktop companion."""
    code = "invalid_plugin"
    manifest_path, desktop_path = plugin_dir / "plugin.yaml", plugin_dir / "desktop/plugin.js"
    if any(path.is_symlink() for path in (plugin_dir, manifest_path, desktop_path.parent, desktop_path)):
        raise BridgeError(code, "Plugin presentation paths must not be symlinks")
    catalog = _catalog(language)
    document = _yaml_document(manifest_path, code)
    schema = _mapping(document.get("config_schema"), code)
    if document.get("name") != "http-mcp" or set(schema) != set(catalog["fields"]):
        raise BridgeError(code, "Manifest is not a compatible Hermes Link plugin")
    for key, translated in catalog["fields"].items():
        schema[key] = {**_mapping(schema[key], code), **translated}
    document["description"], document["config_schema"] = catalog["description"], schema
    try:
        original_desktop = desktop_path.read_text(encoding="utf-8")
        rendered_manifest = yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=110)
        _atomic_text(desktop_path, _desktop_metadata(catalog["description"]))
        try:
            _atomic_text(manifest_path, rendered_manifest)
        except OSError:
            _atomic_text(desktop_path, original_desktop)
            raise
    except (OSError, UnicodeError) as exc:
        code = "presentation_update_failed"
        raise BridgeError(
            code, "Could not publish plugin presentation; rescan after repairing the files"
        ) from exc


def main() -> None:
    """Apply the chosen presentation language to an installed directory plugin."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--plugin-dir", type=Path)
    parser.add_argument("--language", default="auto", help="auto or one of: " + ", ".join(LANGUAGES))
    args = parser.parse_args()
    try:
        language = select_language(args.language, args.hermes_home)
        plugin_dir = args.plugin_dir or args.hermes_home / "plugins/http-mcp"
        localize_plugin(plugin_dir, language)
    except BridgeError as exc:
        print(json.dumps(exc.payload()), file=sys.stderr)
        raise SystemExit(1) from None
    print(f"Hermes Link language: {language}. Rescan Plugins in Hermes Desktop to refresh the native form.")


if __name__ == "__main__":
    main()
