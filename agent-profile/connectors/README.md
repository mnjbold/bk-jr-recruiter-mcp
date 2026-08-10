# Connectors

Auth is a bearer token on every request. The important split is whether your
client speaks remote MCP natively or needs a stdio bridge.

| Client | File | Shape |
|---|---|---|
| Claude Desktop | `claude-desktop.json` | stdio via `mcp-remote` bridge |
| Claude Code / Cursor / Continue | `generic-mcp.json` | native `url` + `headers` |
| Dashboard / custom backend | raw HTTP, below | direct |

## Claude Desktop gotchas (all three cost real debugging time)
1. A `url` + `headers` entry is **invalid** - Desktop only accepts `command`/`args`,
   so it reports "server could not be loaded" and skips it without ever connecting.
2. Use an **absolute path** to `npx.cmd`. Desktop does not reliably inherit PATH on Windows.
3. Put the space **inside** the env var (`AUTH_HEADER="Bearer xyz"`), because
   `mcp-remote` splits `--header` arguments on spaces and would mangle the value.

## Raw HTTP (for the recruiter dashboard)

    curl -X POST https://bkjr-mcp.getbijou.xyz/mcp \
      -H "Authorization: Bearer $SMS_AGENT_API_KEY" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

Responses are SSE-framed (`data: {...}`) even for single calls - parse accordingly.

## Verify a connector
`GET /health` -> `{"status":"ok","tools":24,"auth":"bearer-required"}`

Auth check - the pass signal is **401 / 401 / 200**:
no token -> 401, wrong token -> 401, correct token -> 200.
Rejecting a *wrong* token is what proves it is really comparing.

A bare **403** is not an auth failure - that is Cloudflare blocking a
non-browser user-agent (e.g. python-urllib). curl and real clients are fine.
