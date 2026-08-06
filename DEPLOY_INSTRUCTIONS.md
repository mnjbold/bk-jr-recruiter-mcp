# Deploy BK Jr. MCP to Render — Step by Step

The GitHub repo is ready: **https://github.com/mnjbold/bk-jr-recruiter-mcp**

## Option A: Render Blueprint (1 click after setup) — RECOMMENDED

1. Open https://dashboard.render.com
2. **New** → **Blueprint**
3. Connect the `mnjbold/bk-jr-recruiter-mcp` repo (one-time GitHub auth if first time)
4. Render reads `render.yaml` automatically and shows the service plan
5. Click **Apply** — service starts building

That's it for the basic deploy. Then set the secrets:

6. Go to the service's **Environment** tab
7. Add these **Secret Files** (one per env var, the values are in `quo-sms-agent/.env`):

| Key | Value source |
|---|---|
| `MCP_AUTH_TOKEN` | pick a strong random string (32+ chars) |
| `OPENPHONE_API_KEY` | from `quo-sms-agent/.env` |
| `RETELL_API_KEY` | from `.env` |
| `COMPOSIO_API_KEY` | the `ak_…` one |
| `COMPOSIO_USER_ID` | from `.env` |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | from `.env` |
| `GOOGLE_SHEETS_ID` | from `.env` |
| `SMS_AGENT_API_KEY` | from `.env` (used as bearer for the backend) |

8. Save. Render auto-redeploys with the secrets.
9. The service gets a URL like `https://bk-jr-recruiter-mcp.onrender.com`

## Option B: Render Web Service (no Blueprint, more manual)

1. Render → **New** → **Web Service**
2. Connect `mnjbold/bk-jr-recruiter-mcp` repo
3. Runtime: **Docker**
4. Region: **Singapore** (closest to your team)
5. Plan: **Starter** ($7/mo, persistent — free tier sleeps after 15min)
6. Click **Create Web Service**
7. Add secrets as in step 7 above

## Test after deploy

```bash
# Health check
curl https://bk-jr-recruiter-mcp.onrender.com/health
# {"status":"ok","server":"bk-jr-recruiting","tools":24,"auth":"bearer-required"}

# No auth -> 401
curl -X POST https://bk-jr-recruiter-mcp.onrender.com/mcp
# {"error":"unauthorized",...}

# With auth -> 200
curl -X POST https://bk-jr-recruiter-mcp.onrender.com/mcp \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Claude Desktop config (for the team)

After deploy, share the URL + auth token with the team. Their Claude Desktop config:

```json
"bk-jr-recruiter": {
  "type": "http",
  "url": "https://bk-jr-recruiter-mcp.onrender.com/mcp",
  "headers": {
    "Authorization": "Bearer <paste the MCP_AUTH_TOKEN here>"
  }
}
```

Restart Claude Desktop. They have all 24 tools with auth.

## Why auth matters

Without bearer auth, anyone with the URL can:
- Send SMS from BK's Quo number (real money per message)
- Place Retell screening calls (real money per minute)
- Read/write the candidate Sheet
- Send emails from BK's Gmail

Bearer auth keeps the public URL safe.

## Cost

Render's Starter plan: $7/mo for the Docker service. Free tier sleeps after 15min inactivity (bad for an MCP server that needs to respond instantly). The Starter plan keeps 1 machine always running.

## Fallback: local install (already working)

If you don't want to pay Render right now, the local install at `https://bk-jr-mcp.aixlabs.fun/mcp` works (no auth — only safe because the URL is obscure). The team can use it via Claude Desktop config:

```json
"bk-jr-recruiter": {
  "type": "http",
  "url": "https://bk-jr-mcp.aixlabs.fun/mcp"
}
```

The BK-JR-MCP scheduled task on your machine auto-restarts the MCP server if it crashes. The cloudflared tunnel gives it a public URL.
