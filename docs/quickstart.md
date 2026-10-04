# Quick start

Hermes Link 0.8.0 needs an installed Hermes checkout and its managed Python
runtime. Tested together: Hermes 0.21.5, MCP 2.0.0 and Python 3.13. The HTTP
server stays on loopback with authentication enabled. Native profile connections
can use [stdio instead](profile-connection.md).

## Install and start the server

From a terminal on the machine running Hermes:

```sh
git clone https://github.com/alvnukov/hermes-link.git
cd hermes-link
export HERMES_REPO="$HOME/.hermes/hermes-agent"
"$HERMES_REPO"/venv/bin/python scripts/install.py --hermes-repo "$HERMES_REPO"
cd "$HOME/.hermes/plugins/http-mcp"
"$HERMES_REPO"/venv/bin/python -m hermes_bridge.main \
  --config "$HOME/Library/Application Support/HermesHTTPMCP/config.json"
```

Adjust `HERMES_REPO` if Hermes is installed elsewhere. The installer prints the
runtime configuration location and preserves existing settings and credentials.
It does not register a background service. Keep the server running while testing.
An unauthenticated request to `http://127.0.0.1:18788/mcp` returning HTTP 401
confirms that this authenticated listener is reachable.

## Create the tunnel connection

1. Open [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels).
   Create a tunnel, associate the intended ChatGPT workspace, and keep its ID.
2. Install the official [tunnel-client release](https://github.com/openai/tunnel-client/releases/latest).
   Creating tunnels requires Tunnels Read + Manage; running them requires
   Tunnels Read + Use. Keep the runtime API key in a private file with mode 0600.
3. Save this YAML outside the checkout. Replace the ID and absolute file paths.
   The installer generated `mcp-authorization.header` in its runtime directory.

```yaml
config_version: 1
control_plane:
  base_url: https://api.openai.com
  tunnel_id: tunnel_REPLACE_ME
  api_key: file:/absolute/path/openai-runtime.key
health:
  listen_addr: 127.0.0.1:18787
mcp:
  server_urls:
    - channel: main
      url: http://127.0.0.1:18788/mcp
  extra_headers:
    Authorization: file:/absolute/path/mcp-authorization.header
  discovery_extra_headers:
    Authorization: file:/absolute/path/mcp-authorization.header
```

```sh
tunnel-client doctor --config /absolute/path/hermes-tunnel.yaml --explain
tunnel-client run --config /absolute/path/hermes-tunnel.yaml
```

The OpenAI runtime key and the local MCP Bearer key have separate purposes.
Keep both processes running. See the [official tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
and [client configuration reference](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/configuration.md).

## Add Hermes Link to ChatGPT

Enable Developer mode under Settings → Security and login, if available for your
account. Open [ChatGPT Plugins](https://chatgpt.com/plugins/newchat), create a
connection, choose Tunnel and select or enter the ID. Begin a new conversation
with the connection enabled. These steps follow [OpenAI's connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt).

First ask for `hermes_system` with `action="info"`, then
`hermes_kanban` with `action="help"`. The default catalog has ten tools and
39 operations; authentication and writes are enabled. Tool calls that change
data can still require the client's approval. [Action examples](compact-tools.md)
show task execution, settings revisions and conditional card updates.

After upgrading, keep the same tunnel ID, restart the MCP server, refresh the
connection's metadata and start a new conversation. Existing processes may retain
the previous code. See [troubleshooting](troubleshooting.md) for common failures.
