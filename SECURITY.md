# Security

This server controls native profiles, tasks, schedules and configuration, and
may invoke agents with the same privileges as Hermes. Bind only to loopback.
Authentication is enabled by default. Disable it only when the local transport
is intentionally trusted. Foreign browser origins and hosts remain rejected.

Tokens and configuration snapshots are private owned files. The bridge checks
file type, ownership, permissions and symlinks; messages returned to clients
are sanitized. Keep secret-bearing backups outside a public repository. Do not
include provider credentials, `.env`, MCP keys or tunnel control-plane keys in
bug reports. Model output and native task content are untrusted data.

Full settings application and rollback write the native configuration and `.env`
as two files. Each replacement is atomic; a machine crash between them can leave
an intermediate pair. Durable before/target snapshots permit recovery. See
[settings persistence](docs/settings.md). Do not delete those snapshots before
confirming recovery. No automatic snapshot pruning is performed.

Report vulnerabilities through the repository's
[private vulnerability reporting form](https://github.com/alvnukov/hermes-link/security/advisories/new).
Do not publish exploit details or secret-bearing logs in public issues. Include
the affected version, reproduction steps and a description of the impact; omit
actual provider, MCP and tunnel credentials.

Dependency auditing covers the bridge's locked runtime. The separate native
Hermes installation and its optional provider/desktop dependencies need their
own update and security policy; this project does not replace that policy.
