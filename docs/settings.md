# Native settings and persistent versions

`hermes_settings(action="get", params={"profile":"default"})` returns the complete native configuration with defaults (`config`), persisted overrides (`config_overrides`), persisted `.env` assignments (`env`), a convenience `settings` view, and a revision. Reads use the selected profile's native home; they leave process environment variables unchanged and return `${VARIABLE}` references without expansion. The revision covers the raw bytes and presence of **both** `config.yaml` and `.env`.

For a short response, pass `view="editable"` to `get`: only profile, revision, convenience settings and secrecy flags are returned. `view="full"` is the compatible default. The small `schema.fields` map describes convenience `update` fields; it does not limit the full configuration accepted by `apply`. No process environment is exported: `env` contains persisted assignments only, with credentials masked.

All settings actions use the same compact MCP tool. Call
`hermes_settings(action="help")` for enabled actions or
`hermes_settings(action="help", params={"action":"apply"})` for the exact
parameters of an action. Omitted `params.profile` uses the consuming profile
for a native profile connection, or the HTTP server's configured default.

Credentials identified by the native classifier or secret registry, credential-shaped keys (including plural tokens, auth, passwords and cookies), credential-bearing URLs/headers, and credential-bearing stdio arguments appear as the stable marker `{"$hermes_secret":"preserve"}`. Keep that marker at the same location to retain the current secret. Set a new literal value to replace a secret. Omit its key from a full replacement to remove it. A marker at a location without an existing secret is rejected. Secret values and native validator messages containing submitted values are not returned; validation reports contain error and warning counts.

## Apply all settings in one call

1. Call `hermes_settings(action="get", params={"profile":"default"})` for the intended profile.
2. Edit its `config` or `config_overrides` tree and, optionally, its `env` object.
3. Call `hermes_settings(action="apply", params={...})`, passing `config`, `expected_revision`, optional `env` and `profile` with the revision from the read.
4. Use the returned revision for the next write. Save `version_id` if this version may be needed later.

`config` is the complete desired persisted tree: omitted keys are deliberately removed. Use `config_overrides` to retain sparse overrides and inherit future native default changes. Applying the full `config` tree explicitly persists the displayed defaults. Configuration keys accepted by the native structure validator, including dynamic extension keys, are supported. JSON objects and arrays retain their nested shape. Legacy scalar `model` settings normalize to `model.default`.

`env=null` preserves `.env` exactly. An environment object replaces its assignments; omitted names are removed and an empty object clears assignments. Values must be single-line strings and names must be valid environment identifiers. Native quoting preserves spaces, quotes, backslashes and `#`. Administrator-managed settings remain protected by Hermes's native policy.

`hermes_settings(action="update", params={...})` accepts `changes`,
`expected_revision` and `profile` for the validated convenience fields listed
by `hermes_settings(action="schema")`. Every successful convenience update
also creates persistent versions.

## List and restore versions

Call `hermes_settings(action="versions", params={"profile":"default","limit":50})` to obtain newest-first version metadata. Each item contains `id`, `created_at`, `reason` and `revision`; `count` is the total number of versions and `has_more` indicates truncation. The default limit is 50, with a maximum of 1000.

To restore, first read the current revision and then call
`hermes_settings(action="restore", params={...})` with `version_id`,
`expected_revision` and `profile`. Restoration saves the current state first,
validates the chosen version, and restores the exact saved bytes of both files,
including whether a file was absent. The result contains
`saved_current_version_id`, `restored_version_id`, the refreshed revision,
and redacted settings. Versions are bound to their original profile and native
home; they cannot be restored into another profile.

Versions live in the selected profile's native `backups/config/` directory. Hermes's native `backup_config` and `list_config_backups` manage the paired `config.yaml` and `.env` backup files; private JSON sidecars under `bridge-versions/` bind each pair to its profile, revision and timestamp. Directories use `0700` and backup/metadata files use `0600`. Secrets are present only in these private backup files, as needed for restoration. Version metadata contains no secret values. History persists across bridge restarts, and the bridge keeps no separate runtime or board database. No automatic history pruning is performed.

Both the before-state and prepared after-state backups are saved and verified before an apply changes the live files. Storage failure refuses the change. A failed publication can leave its prepared version in history; it remains a valid saved configuration to select later. Paths containing symlinks, non-private history, damaged backups, invalid version IDs and stale revisions are rejected.

The native writer lock protects writes in this process, and each file is published with the native atomic writer. Config and environment remain two separate files: a process crash between their publications can leave a mixed pair, recoverable from the saved version. A write error triggers automatic restoration when possible; `settings_recovery_required` includes the saved current version ID if automatic restoration fails. The revision check is best effort against another process editing files at the same instant; Hermes exposes no cross-process compare-and-swap transaction for the pair. Existing agents may retain their current settings; new turns load the saved configuration.
