# Troubleshooting

Use `hermes_system/info` to identify the extension/native versions and
`hermes_system/capabilities` to inspect the effective policy. Use each tool's
`help` action for its current parameter schema. Redact credentials before sharing
diagnostics in an issue.

| Symptom | Check and recovery |
| --- | --- |
| `hermes_unavailable` at startup | Check the configured absolute Hermes checkout path, its `mcp_serve.py` and managed `venv/bin/python`. Use the tested native version from the README. |
| HTTP 401 | Authentication is enabled. Check the local MCP Bearer header and its private file. Both tunnel runtime and discovery headers must carry the effective MCP token. The OpenAI runtime key is a separate credential. |
| HTTP 403 | Check Host/Origin restrictions. Keep the listener on loopback and use the intended tunnel/client endpoint. |
| Tunnel missing in ChatGPT | Check its ChatGPT workspace association and the operator's Tunnels Read + Use permission. See the [official tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels). |
| Discovery or tunnel calls fail | Keep both server and `tunnel-client` running; use `tunnel-client doctor --config /absolute/path/hermes-tunnel.yaml --explain` and the local `/readyz` endpoint. |
| Old tools or old extension version | Restart the MCP process, refresh the ChatGPT connection and begin a new conversation. A new tunnel ID is not required. |
| Write action unavailable | Check `writes_enabled`, disabled operations and native plugin settings, then the client's write permission. Saved Hermes UI switches take precedence over `config.json`. |
| `revision_conflict` | Re-read the card/settings and reconsider the patch using the returned revision. Kanban revisions conservatively include native board audit progress; unrelated events may invalidate them. |
| Worker output or session absent | Inspect `output_source`, `session_link_status` and stored diagnostics through `hermes_workers/get`. Missing data stays unavailable; the bridge does not infer a session or read a shared retry log as historical output. |

Changing the UI token requires restarting the MCP process and updating the
client/tunnel header. Reinstalling regenerates the header from the effective
saved token. Read [plugin settings](plugin-settings.md) for precedence and
[settings history](settings.md) before restoring configuration.
