# Compact MCP tools (0.8.0)

Hermes Link exposes 10 domain tools with 39 operations. Every
tool accepts `action` and an optional object `params`. The action's parameter
names are documented by help. Task handles and native storage remain compatible.

## Discover parameters

Call a tool with `action="help"` and empty `params` to list its enabled actions.
Request a precise action schema by passing `params={"action":"create"}`:

```json
{"tool":"hermes_kanban","arguments":{"action":"help","params":{"action":"create"}}}
```

Pass action arguments inside `params`, rather than at the top level. Omitted
`params.profile` keeps the configured profile default; a native profile
connection binds this default to the consuming profile, including help schemas.

## Migration

| Compact tool | Action → previous operation |
| --- | --- |
| `hermes_system` | `info` → `hermes_info`; `capabilities` → `hermes_capabilities`; `memory_status` → `hermes_memory_status`; `agents` → `hermes_agents`; `agent` → `hermes_agent` |
| `hermes_profiles` | `list` → `hermes_profiles`; `get` → `hermes_profile`; `create` → `hermes_profile_create`; `delete` → `hermes_profile_delete` |
| `hermes_tasks` | `run` → `hermes_run`; `status` → `hermes_status`; `result` → `hermes_result`; `continue` → `hermes_continue`; `cancel` → `hermes_cancel` |
| `hermes_kanban` | `boards` → `hermes_kanban_boards`; `get` → `hermes_kanban_get`; `task` → `hermes_kanban_task`; `assignees` → `hermes_assignees`; `create` → `hermes_kanban_create`; `update` → `hermes_kanban_update`; `archive` → `hermes_kanban_archive` |
| `hermes_workers` | `list` → `hermes_workers`; `get` → `hermes_worker` |
| `hermes_sessions` | `list` → `hermes_sessions`; `get` → `hermes_session` |
| `hermes_cron` | `list` → `hermes_cron`; `get` → `hermes_cron_job`; `create` → `hermes_cron_create`; `set_paused` → `hermes_cron_set_paused`; `delete` → `hermes_cron_delete` |
| `hermes_workspaces` | `list` → `hermes_workspaces`; `get` → `hermes_workspace` |
| `hermes_settings` | `schema` → `hermes_settings_schema`; `get` → `hermes_settings_get`; `update` → `hermes_settings_update`; `apply` → `hermes_settings_apply`; `versions` → `hermes_settings_versions`; `restore` → `hermes_settings_restore` |
| `hermes_events` | `list` → `hermes_events` |

The old operation names remain configuration keys, not callable MCP aliases.
For example, disabling `hermes_kanban_create` blocks `hermes_kanban/create`.
Already saved UI switches and `disabled_tools` keep their meaning. Disabled
actions are omitted from help and cannot be invoked directly; a group with no
enabled actions is omitted from discovery. With `writes: false`, write actions
are unavailable while the allowed read actions remain.

## Examples

Read a board:

```json
{"tool":"hermes_kanban","arguments":{"action":"get","params":{"board":"default","limit":10}}}
```

Read a profile's settings before a revision-checked change:

```json
{"tool":"hermes_settings","arguments":{"action":"get","params":{"profile":"developer"}}}
{"tool":"hermes_settings","arguments":{"action":"update","params":{"profile":"developer","changes":{"agent.max_turns":25},"expected_revision":"<revision from get>"}}}
```

Use `hermes_settings/versions` and `hermes_settings/restore` to restore a saved
version. Read the current revision before restoration. Persistent history and
secret preservation work as described in [settings.md](settings.md).

For everyday convenience settings, request the smaller view explicitly:

```json
{"tool":"hermes_settings","arguments":{"action":"get","params":{"profile":"developer","view":"editable"}}}
```

The default remains `view="full"`, preserving full configuration and environment
round trips. Both views return the same revision and use the same secret boundary.

## Kanban attempt results

Read a native attempt by its integer run ID:

```json
{"tool":"hermes_workers","arguments":{"action":"get","params":{"run_id":23,"board":"default"}}}
```

The response contains the native `run` and a normalized `result`. Its `output`
is the attempt's durable handoff summary, with
`output_source="native_run_summary"`. It is not a claim about the final assistant
message: continuing a session later cannot replace the historical handoff.
`result.session_id` refers to the validated worker session, while
`task.session_id` retains the card's originating session. Missing or inaccessible
links report `session_link_status` explicitly; no latest-session guess is made.

Failure diagnostics expose only the native attempt's stored error, exit code,
exit kind and bounded worker output. Arbitrary metadata and current task logs are
not exported. Text passes the unconditional native egress redactor; failed
redaction withholds raw text. Unknown exit codes stay null. Native Hermes does
not store exact usage for every Kanban attempt, so `result.usage` is null and
`usage_scope="unavailable"`; cumulative session totals are not substituted.

## Conditional Kanban updates

Read `task.revision` from a card response, then use that opaque token:

```json
{"tool":"hermes_kanban","arguments":{"action":"update","params":{"task_id":"t_example","expected_revision":"<task.revision from a fresh read>","changes":{"title":"Reviewed title"}}}}
```

The revision is bound to the native card and board store. The conditional check
runs inside the same native write transaction as the first mutation, including
native audit writes. A stale token returns `revision_conflict` before applying
the change. Omitting `expected_revision` preserves unconditional behavior.
Single-operation validation still rejects mixed mutation phases.

This is a scoped ETag rather than a persistent integer counter. It includes
native state and audit sequence information; another board event may therefore
invalidate a token conservatively. Read the card again and reconsider the patch
after a conflict. No new native tables or shadow revision store are created.
The guarantee covers normal native writers. External database replacement,
manual SQL rewrites and restoration of an identical historical database are
outside the revision contract. Native post-commit callbacks retain their own
semantics; their failures do not imply rollback of an already committed change.

## Updating clients

Install the update and restart the MCP server. Keep the same Secure MCP Tunnel
ID and target. Refresh the plugin's tools in ChatGPT, then start a new chat with
that plugin. Existing chats and Hermes workers may retain the previous catalog;
start a new chat or worker to discover the compact contract.

Groups containing enabled write actions use write-capable MCP annotations;
a client may therefore request confirmation for a read action in such a group.
The server still checks the specific action, allowlists and write policy. The
smaller catalog does not prove that ChatGPT's action filtering or permissions
have changed; verify available groups and write actions in the actual chat.
