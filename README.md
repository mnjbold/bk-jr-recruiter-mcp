# BK Jr. MCP Server

A hosted MCP (Model Context Protocol) server for the BK Jr. recruiter agent. Wires together Retell AI (voice), Quo/OpenPhone (SMS), Google Sheets/Calendar/Gmail/Drive (via Composio).

## Why this exists

The user-facing agent (BK Jr.) runs in Claude Desktop. Claude Desktop talks to the MCP server over HTTPS. This server:
- Adds **bearer-token auth** (the raw FastMCP server has none)
- Strips the **trailing-slash redirect** that breaks clients behind TLS-terminating proxies (Cloudflare, Fly, Render)
- Exposes a `/health` endpoint for the platform's health check
- Runs in **stateless mode** (no long-lived sessions) so it survives restarts cleanly

## Endpoints

| Path | Auth | Description |
|---|---|---|
| `GET /health` | none | Returns server status. Use for Render/Render health checks. |
| `POST /mcp` | Bearer | JSON-RPC: `tools/list`, `tools/call`, `initialize`, etc. |
| `POST /mcp/` | Bearer | Same as `/mcp` (the trailing-slash alias). |

## Auth

Every MCP request must include:
```
Authorization: Bearer <MCP_AUTH_TOKEN>
```

The server reads `MCP_AUTH_TOKEN` from env, falling back to `SMS_AGENT_API_KEY` for backward compat. If neither is set, the server refuses ALL MCP requests with 401.

## Local development

```bash
docker build -t bk-jr-mcp:local .
docker run --rm -p 8090:8080 \
  -e MCP_AUTH_TOKEN=test-token \
  -e BACKEND_URL=http://host.docker.internal:8080 \
  -e OPENPHONE_API_KEY=... \
  -e RETELL_API_KEY=... \
  -e COMPOSIO_API_KEY=... \
  -e COMPOSIO_USER_ID=... \
  -e COMPOSIO_CONNECTED_ACCOUNT_ID=... \
  -e GOOGLE_SHEETS_ID=... \
  bk-jr-mcp:local
```

Then test:
```bash
# No auth -> 401
curl -X POST http://localhost:8090/mcp

# With auth -> 200
curl -X POST http://localhost:8090/mcp \
  -H "Authorization: Bearer test-token" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Deploy to Render

This repo has a `render.yaml` blueprint. The simplest path:
1. Push to GitHub
2. Render dashboard → New → Blueprint → point at this repo
3. Render reads `render.yaml` and creates the service
4. Set `MCP_AUTH_TOKEN` as a secret in Render
5. Set the Quo/Retell/Composio API keys as secrets too
6. The service deploys and gets a public URL like `https://bk-jr-recruiter-mcp.onrender.com/mcp`

## Config

Required env vars (set as Render secrets):
- `MCP_AUTH_TOKEN` — bearer token clients send
- `BACKEND_URL` — the FastAPI backend (defaults to `https://bk-jr-api.aixlabs.fun`)
- `OPENPHONE_API_KEY` — Quo/OpenPhone
- `RETELL_API_KEY` — Retell AI
- `COMPOSIO_API_KEY` (and `COMPOSIO_USER_ID`, `COMPOSIO_CONNECTED_ACCOUNT_ID`, `GOOGLE_SHEETS_ID`)
- `SMS_AGENT_API_KEY` — bearer token for the backend's `/api/*` routes (fallback for `MCP_AUTH_TOKEN`)

## 24 tools exposed

| Domain | Tools |
|---|---|
| Candidates | list_candidates, get_candidate_by_phone, update_candidate |
| Jobs | list_jobs, get_job |
| SMS (Quo) | send_sms, list_phone_numbers, list_conversations |
| Voice (Retell) | trigger_screening_call, list_pending_screenings, list_recent_screenings |
| Retell agents | retell_list_agents, retell_get_agent, retell_create_agent, retell_place_call |
| Bulk | bulk_outreach, bulk_outreach_for_job |
| Google | gmail_send, gcal_list_events, gcal_create_event, gdrive_create_folder, gdrive_share |
| Control | pause_candidate, notify_bk |
