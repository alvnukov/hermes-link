# Kanban regression follow-up in 0.8.0

The October 4, 2026 follow-up report confirmed the previous execution and
secret-redaction fixes. Native attempt 23 completed, its handoff summary was
`KANBAN_RESULT_OK`, and the stamped worker session existed in the assigned
profile's SessionDB. The test card was archived after terminal-state inspection.

## Stored attempt data

The missing result link and crash diagnostics were projection defects. Native
`Run.summary`, `Run.error`, and the specific `worker_session_id`, `exit_code`,
`exit_kind`, and `worker_output` metadata fields were already durable. The MCP
projection omitted them. Attempt reads now expose these fields through the
existing worker and card tools, without a parallel result database.

Card `session_id` identifies the originating conversation. It is not repurposed
as a worker ID. Attempt results expose the validated worker link separately and
do not search for a recent session by timestamp. Missing, invalid or inaccessible
links stay explicitly unavailable. Arbitrary metadata is not returned.

`result.output` is the stored handoff with
`output_source="native_run_summary"`. The native attempt table does not persist
a final assistant-message watermark. Reading the latest assistant message would
misattribute a later continuation to an earlier attempt, so the bridge does not
claim that behavior. The native per-attempt usage record is also unavailable;
session-wide counters are not presented as attempt usage.

Error and output text is bounded and passes native unconditional egress
redaction. A failed redactor withholds raw text. Historical diagnostics come from
the attempt itself; a task log appended across retries is not a valid substitute.

## Conditional updates

The existing `kanban/update` action accepts an optional `expected_revision` from
a fresh `task.revision`. The opaque revision describes a native card/store
snapshot. Its verification uses the native mutation connection and transaction
boundary, including native audit writes. No outer transaction wraps post-commit
callbacks, and no revision table, core patch or process monkeypatch is required.

Two updates from the same snapshot must have exactly one successful conditional
mutation. A stale request reports `revision_conflict` without applying its patch.
Unconditional updates retain their previous contract. Full patch validation
still happens before writes, and failures after a native commit describe the
observed state rather than claiming rollback.

Native audit high-water information makes the token conservative: unrelated
board events can also invalidate it. Re-read and reconsider the desired change
after a conflict. The token is not a monotonically increasing per-card integer.
Manual SQL rewrites, replacement of the database, and restoring an identical
historical database are outside this concurrency contract.

## Preserved interface

There are still ten compact tools and 39 actions. Full settings reads remain
the default, as requested; `settings/get` with `view="editable"` provides the
small convenience view. Both retain revision checks and secret masking.
Authentication, tool controls, stored settings and the existing tunnel remain
part of the current installation contract.

## Verification

The completed local suite passed 258 tests and 315 subtests, with 95.37%
combined line/branch coverage. Ruff `ALL`, formatting, strict mypy, all-level
Bandit and the locked runtime dependency audit passed; source and wheel builds
passed. Independent review covered the native transaction fence, worker
attribution and redaction boundaries, including contradictory zero-duration
records and post-commit receipt failures. The documented native limitations
remain explicit rather than being replaced with inferred state.

The installed 0.8.0 server passed authenticated HTTP checks without model calls:
attempt 23 returned `KANBAN_RESULT_OK` with its persisted worker session,
attempts 17 and 18 returned their stored nonzero-exit diagnostics, and two
concurrent conditional updates produced one winner and one revision conflict.
The existing direct-task result remained available. A separate check through the
installed `hermes_v3` connector created a card, updated it conditionally, rejected
a stale update and archived the card. Both test cards were archived afterward.

The upgrade kept the existing tunnel, all ten tools and 39 actions. Authentication
remained enabled; native configuration, environment, runtime configuration and
credential files were checked byte-for-byte after installation. A private
rollback snapshot was retained before replacing the plugin.
