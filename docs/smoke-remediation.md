# ChatGPT smoke report: remediation in 0.7.0

The October 4, 2026 report exercised all ten compact tools and demonstrated that
write actions were available to ChatGPT. Its execution and secret-export findings
were reproduced separately; discovery success did not imply runtime correctness.

## Secret exports

The native classifier recognized singular suffixes such as `_token`, but missed
`A2A_PEER_TOKENS`. The bridge now supplements it with the native secret-variable
registry and recursive credential-name/value checks. Cookie headers, credential
containers, URL credentials and credential-bearing stdio arguments use the same
preserve-marker mechanism. Export and apply resolve the same locations, retaining
private values on disk. Tests scan complete MCP result serialization, schema,
apply/update/restore responses and version metadata for sentinel credentials.

Full native settings remain supported as requested. `get` with `view="editable"`
returns the small convenience view and its revision. `schema.fields` documents
convenience updates, rather than limiting full `apply`. Process environment is
not exported; persisted `.env` assignments are returned with credential masking.
Never store credentials in unrelated free-text fields: semantic identification of
arbitrary opaque text is not possible. Previously exposed peer credentials require
rotation at their source; updating the plugin cannot retract an existing chat.

## Direct execution and sessions

The first `run_agent` import entered the native bootstrap. On this installation it
replaced the entire MCP process with managed Python; isolated Python then could
not import `hermes_bridge`. The embedded runtime now uses the native
`HERMES_DISABLE_LAZY_INSTALLS` opt-out before worker threads start. Dependency
maintenance remains an explicit Hermes operation.

Native SessionDB persistence happens before an accepted session ID is returned.
Failed persistence returns `session_id: null` and false retention flags. Legacy
tasks with absent sessions also report the truth and cannot silently continue a
nonexistent conversation. Results include owner PID/liveness, runtime backend,
accepted/start/end timestamps and session persistence. An unknown historical
exit time or exit code is not fabricated.

The subprocess regression uses the actual NativeEngine, AIAgent, conversation
loop and SessionDB. Only remote provider/model boundaries are replaced with a
fixture response. It verifies immediate session lookup, completion, continuation,
native messages, session events and usage. Rejected session/thread startup also
uses the existing terminal-status repair handle: a temporary journal failure
cannot turn an unstarted task into a permanently queued reservation.

## Native profile resolution

The production retest reproduced the `devops` failure in two attempts on a
disposable test card. Both native crash events identify the dispatcher as
`host.example:1234` (an example hostname and PID). That process belongs to the legacy API-only gateway service
`ai.hermes.gateway.legacy-api`, rather than the Desktop backend.
Its `HERMES_HOME` is `~/.hermes/legacy-api-runtime`. The old launcher also
resolves `~/.local/bin/hermes` into a separate separate maintenance worktree running
Python 3.11, while Desktop uses the main checkout and managed Python 3.14.7.

Native board and registry readers treat a directory nested under `~/.hermes`
as sharing that root. The worker selector treats this custom `HERMES_HOME`
literally, so it searches `legacy-api-runtime/profiles/devops`. That directory
does not exist. A read-only subprocess reproduced the contradiction:
`profile_exists("devops")` succeeds, the board resolves to `~/.hermes/kanban.db`,
but `resolve_profile_env("devops")` raises `FileNotFoundError`. The legacy
API-only setup omitted `kanban.dispatch_in_gateway=false`; the native default
enabled its dispatcher and allowed it to claim the user's shared board.

Disabling Kanban dispatch in the legacy API-only gateway prevents it from
claiming cards, but does not release the host-wide gateway role. After that
change, the restarted legacy process published itself as serving `custom` and
`default`, blocking the correct root gateway from starting. Native Hermes has
retired the `gateway.multiplex_profiles=false` / `GATEWAY_MULTIPLEX_PROFILES=false`
opt-out: those values are parsed but ignored. This behavior is explicit in the
native `gateway_multiplex_mode` implementation.

The deployment correction therefore unloads and disables the obsolete
`ai.hermes.gateway.legacy-api` LaunchAgent, retaining its plist, runtime,
configuration and keys. Dispatch moves to the native gateway launched from the
canonical main-checkout entry point with `HERMES_HOME=~/.hermes`. The obsolete
PATH wrapper must not be used to start that gateway. The modern HTTP MCP and
tunnel services are independent and remain active. Restoring the old service
requires stopping the correct root gateway first, then enabling and loading
the retained legacy LaunchAgent: the native host singleton permits only one
gateway. No native Hermes core patch is needed.
Before the service handover, the shared board had no ready/running/scheduled
cards or active claims; all twelve profiles had zero cron jobs. No enabled
messaging platforms or recognized messaging credential variables were found in
their persisted configuration. After handover, a production smoke card
assigned to `devops` reached `ready → running → done`. Its native attempt reports
`status: done` and `outcome: completed`. The successful and failed smoke cards
were archived without changing existing cards.

Fresh registry-to-worker resolution from the correct root matches for all
twelve installed profiles. The native `_default_spawn → python -m hermes_cli.main`
branch also resolved all twelve names in disposable homes under Python 3.14.7;
the only argument instrumentation appended `--help` to stop before a model call.
A subprocess regression additionally covers an assigned profile launched from a
different named profile. These no-op checks validate profile selection, while
the production task test separately verifies actual dispatch and execution.

## Kanban writes and cleanup

`update` has a closed typed `changes` schema. Native `edit_task` atomically
changes title/body/priority together. Assignment, state transitions and overrides
are separate native operations: mixed-phase requests are rejected before writes.
If a native post-commit callback fails, the error reports observed `state_changed`
and does not promise rollback. Native CAS is unavailable and explicitly reported
as unsupported; a preflight comparison is not presented as a transaction.

The existing tools add `profiles/delete`, `cron/delete` and `kanban/archive`.
Default/current/active profiles and active resources are protected. Cron removal
uses the native dispatch fence followed by the reentrant native jobs writer lock.
The exact-ID check and deletion share that writer section, preventing a racing
removal from making native ID-or-name lookup delete a sibling job. Native global
jobs locking degrades to in-process serialization on flock timeout or unavailable
cross-process locking; Hermes exposes no acquisition signal. The plugin therefore
does not claim a fail-closed cross-process guarantee in that degraded mode.
Kanban archive guards against a concurrent worker
claim inside its transaction. Profile deletion is subject to native preflight
limitations: the native API provides no atomic profile activity lease.

Arbitrary stdout/stderr access and a generic runtime event journal remain outside
the plugin boundary. Native task/session/worker metadata and durable native events
are available; the plugin does not invent a separate logging database.

## Verification and installation

The complete suite passed: 231 tests and 286 subtests, with 95.79% combined
statement/branch coverage. Ruff `ALL`, formatting, strict mypy and Bandit passed.
The source distribution and wheel built successfully. The legacy API launchers
have separate targeted Go integration tests executing their actual shell scripts
in a disposable home; those tests, `go vet` and shell syntax checks passed.

Installed HTTP MCP reports extension version 0.7.0, ten domain tools and 39
actions. Production `settings/get` masks `A2A_PEER_TOKENS`; the compact editable
view retains its revision. Two real provider/model turns completed with
`SMOKE_OK` in the same immediately resolvable native session. Kanban execution
was verified separately after the dispatcher handover described above.

ChatGPT's existing `hermes_v3` connection was refreshed through **Update tools**.
The application dialog shows five write and five read tools, including updated
descriptions for profile deletion, cron deletion and card archiving. No new
tunnel is needed. Server authentication and existing keys remain in place;
private rollback snapshots contain the previous installation/configuration.

An independent read-only review found and verified fixes for custom credential
header arguments, failed-start journal recovery and concurrent cron deletion.
The final scoped review approved the implementation within the documented native
locking and profile-activity limits. Connected MCP tool calls independently
confirmed version 0.7.0, the retained `SMOKE_OK` result, the completed/archived
`devops` attempt, secret masking and discovery of the new cleanup action.

Machine-readable local evidence is in `docs/live-smoke-0.7.0.json`,
`docs/install-0.7.0.json`, `docs/connector-smoke-0.7.0.json` and
`docs/coverage-smoke-remediation.json`. These local
reports are not packaged into the public source distribution.
