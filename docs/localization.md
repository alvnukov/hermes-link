# Localization

Hermes Link 0.4.2 contains 79 UI strings per language: one plugin description
and labels/descriptions for all 39 native settings fields. The proper name
Hermes Link, 36 machine tool identifiers, environment variable names, token
constraints and network addresses stay unchanged. MCP protocol responses and
technical API descriptions keep their existing contracts; the translations
cover the plugin's native configuration UI rather than rewriting Hermes or
translating its conversations.

The source of supported languages is `agent/i18n.py` in Hermes 0.21.5. Its 17
bundled core/TUI languages are all covered: `en`, `zh`, `zh-hant`, `ja`, `de`,
`es`, `fr`, `tr`, `uk`, `af`, `ko`, `it`, `ga`, `pt`, `ru`, `hu`, `ar`.
This includes the Desktop application's nine bundled languages. The completeness
test compares the catalog files with both native lists and the settings schema.

## Selecting a language

`scripts/install.py --language auto` reads `display.language` from the profile
that owns the bridge settings (`board_profile` in its runtime configuration).
Region/script tags resolve to the bundled catalog, for example `de-DE` to `de`,
`pt-BR` to `pt`, `zh-TW` and `zh-Hant-HK` to `zh-hant`. An absent or additional
pack-only language falls back to English. An explicit unsupported language fails
with `unsupported_language`; malformed profile YAML fails with `invalid_config`.

A locale can be selected explicitly during installation (`--language fr`) or
applied later from the installed plugin directory:

```sh
~/.hermes/hermes-agent/venv/bin/python -m hermes_bridge.presentation --language auto
~/.hermes/hermes-agent/venv/bin/python -m hermes_bridge.presentation --language ar
```

The command defaults to `~/.hermes/plugins/http-mcp`. To follow another profile:

```sh
~/.hermes/hermes-agent/venv/bin/python -m hermes_bridge.presentation \
  --hermes-home ~/.hermes/profiles/reviewer \
  --plugin-dir ~/.hermes/plugins/http-mcp --language auto
```

Then rescan Plugins in Hermes Desktop. The translated form is available while
the plugin is disabled. `python -m hermes_bridge.presentation --help` also works
in a wheel installation; pass `--plugin-dir` to the native directory package.

## Native limitation and safety

Hermes 0.21.5's manifest reader coerces `label` and `description` directly to
strings; its form renderer displays those strings. There is no locale-aware
manifest field or plugin contribution that replaces the native settings form.
The plugin-scoped Desktop i18n SDK translates a plugin's own React contributions,
not this backend-owned form. Hermes Link therefore renders presentation files
at install/apply time rather than patching core, intercepting requests, or
creating a second settings UI. Switching Desktop language alone does not reapply
the native form; the explicit command and rescan are required. A shared plugin
directory has one applied presentation language across profiles.

Only `plugin.yaml` display strings and the inert `desktop/plugin.js` metadata
are rewritten. Field keys, types, defaults, secret storage, permissions and
activation are preserved. Config/env/runtime files, settings version history,
services and tunnels are untouched. The command rejects symlink presentation
paths and incompatible manifests, validates catalogs before writing, replaces
individual files atomically, and restores the previous companion if publishing
the manifest fails. Installation renders in its private staging directory.

Catalogs ship in source releases, wheels and native directory installations.
Installation copies only the 17 named JSON files, excluding unrelated data.
Brand SVGs remain language-neutral except for the existing English cover tagline.
Translations were checked for completeness and technical meaning; independent
native-speaker review and visual review of every language remain release follow-up
work. The native form owns text direction and layout.
