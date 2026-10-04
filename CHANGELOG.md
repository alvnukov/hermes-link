# Changelog

## 0.8.0

- Export persisted Kanban attempt summaries, worker-session links and sanitized
  failure diagnostics through the existing compact worker and card tools.
- Keep originating card sessions distinct from worker sessions; historical
  results never follow the latest message of a subsequently continued session.
- Add optional revision-checked Kanban updates on native transaction boundaries,
  without new tables, a separate state store or native core patches.
- Retain ten compact tools, all existing action names, full settings exports,
  authentication and the existing tunnel connection.

## 0.7.0

- Close secret export gaps for peer tokens, auth/credential containers, registered
  secret variables, Cookie headers, credential URLs and stdio arguments. Preserve
  markers round-trip through full apply and persistent rollback.
- Add `settings/get` view `editable` for a compact response; retain full settings.
- Prevent embedded Hermes bootstrap from replacing the MCP process. Persist native
  sessions before accepting tasks and report truthful legacy session retention.
- Include execution owner, session persistence, runtime backend and timestamps in
  task diagnostics, using the existing native journal.
- Validate Kanban patches with a closed typed schema. Combine title/body/priority
  atomically through native `edit_task`; reject multiple native mutation phases
  before changes. Native compare-and-swap remains unsupported.
- Add profile delete, paused cron delete and inactive Kanban archive within the
  existing 10 tools. Protect active resources; localize all 39 operation controls.
- Add real native subprocess coverage for execution and profile launch resolution.
- Recover rejected startup status after a temporary journal failure, redact custom
  credential headers in split/equals argv forms and serialize exact cron removal
  against native writers.
- Diagnose and retire a legacy API-only gateway that claimed the shared Kanban
  board with the wrong profile root. Verify real `devops` execution through the
  canonical native gateway, preserving rollback files and the existing tunnel.

## 0.6.0

- Reject malformed or misplaced envelope fields before SDK parsing, without
  echoing settings values; publish matching strict envelope/action schemas.

- Replace the 36 individual MCP tools with 10 domain tools accepting `action`
  and `params`, without changing action results or native persistence.
- Add `help` to every group for enabled actions and precise per-action schemas.
- Preserve the existing 36 operation switches; disabled actions and write actions
  in read-only mode disappear from help and cannot be called through a group.
- Bind omitted profiles inside action parameters and help to the consuming profile.
- Update real HTTP and native chat/Kanban probes to the compact contract.
- Keep the existing tunnel ID; refresh ChatGPT tools and start a new chat after upgrading.
  Client-side write permissions still require independent verification.

## 0.5.0

- Connect any allowed native profile to Hermes Link using automatic stdio
  startup in both ordinary chats and Kanban worker launches.
- Bind omitted tool profile arguments and their schemas to the consuming
  profile, preserving explicit cross-profile access and existing HTTP defaults.
- Add versioned native connection/removal, collision checks and selective CLI
  toolset handling, with full local trust or consent-gated untrusted access.
- Verify actual native discovery and MCP reads/writes in isolated A/B/A
  profiles through chat and Kanban startup selection, without provider calls.

## 0.4.2

- Translate plugin descriptions and all 39 settings fields into all 17 bundled
  Hermes core/TUI languages, including the nine bundled Desktop languages.
- Select the settings-owner profile language during installation, supporting
  region/script aliases and English fallback for additional language packs.
- Add a presentation-only locale command with atomic file replacement and
  companion rollback; preserve IDs, policy, credentials and tool contracts.
- Package allowlisted locale catalogs and document native manifest/rescan limits.

## 0.4.1

- Introduce the Hermes Link display name, description, SVG icon and 2:1 cover.
- Add an inert native Desktop companion for the display name, preserving the
  `http-mcp` registry key, command, settings, credentials and tool contracts.
- Include allowlisted presentation assets in installations and source releases;
  package the SVG assets in the wheel and document local catalog limitations.

## 0.4.0

- Add MIT licensing, typed wheel/sdist packaging, a hashed uv lockfile and CI.
- Enable all stable Ruff rules, strict typing, security/dependency checks and
  a 95% line-and-branch coverage gate.
- Refactor native event readers, execution result normalization and settings
  publishing while preserving all 36 MCP tool contracts.
- Test isolated native stores, cancellation races, startup/auth failures,
  history corruption and rollback recovery, without model/provider requests.
- Reject fractional integer inputs; classify malformed cursor/history data
  consistently; verify rollback publication and protect interrupted env writes.
- Make test repository discovery and read-only smoke checks portable.

## 0.3.2

Native UI controls for authentication and all tools, owning-profile secret
storage, private disk versions and authenticated native management tools.
