# BK JR MCP — Deployment

BK JR's recruitment MCP server, deployed on Coolify.

## Live services

| Service | URL | Mode |
|---|---|---|
| Backend (FastAPI) | `https://bkjr-api.getbijou.xyz` | `MODE=backend` |
| MCP server (FastMCP) | `https://bkjr-mcp.getbijou.xyz` | `MODE=mcp` |

## Architecture

```
┌─────────────────┐     ┌──────────────────┐
│ MCP clients     │────▶│ bkjr-mcp (port   │
│ - Claude Desktop│     │ 8080, FastMCP    │
│ - Hermes        │     │ bearer auth)     │
│ - OpenClaw      │     └────────┬─────────┘
│ - any MCP client│              │ /api/tool (POST)
└─────────────────┘              ▼
                        ┌──────────────────┐
                        │ bkjr-backend     │
                        │ (port 8080,      │
                        │  FastAPI, real    │
                        │  business logic) │
                        └────────┬─────────┘
                                 │
                  ┌──────────────┼──────────────┐
                  ▼              ▼              ▼
              Quo/OpenPhone   Retell AI      Composio
              (SMS)           (voice calls)  (Sheets/GCal/Gmail/Drive)
```

## One-command deploy

Coolify auto-deploys on push to `main`:

```bash
git push origin main
# → Coolify webhook fires
# → Coolify pulls latest
# → Docker build (Dockerfile, MODE=backend for backend app, MODE=mcp for mcp app)
# → Both services restart with new image
# → Live within ~2 minutes
```

For staging:

```bash
git checkout bkjr-mcp/staging
git push origin bkjr-mcp/staging
# → GitHub Actions fires (.github/workflows/deploy-staging.yml)
# → Slack notification + smoke test
```

## Local development

```bash
cd retell-quo-server
uv venv .venv --python 3.12
source .venv/Scripts/activate   # or .venv/bin/activate on Linux
uv pip install -r requirements.txt
uv pip install "mcp>=1.9.0,<2.0.0" "starlette<0.38" pytest pytest-asyncio ruff httpx

# Set dummy env vars (tests use mocks — no real network)
export QUO_API_KEY=dummy
export RETELL_API_KEY=dummy
export SMS_AGENT_API_KEY=dummy
export BACKEND_URL=http://localhost:8080

pytest tests/ -v
ruff check src tests
```

## MCP tools (26 total)

Core recruiting:
- `list_candidates`, `get_candidate_by_phone`, `update_candidate`
- `list_jobs`, `get_job`
- `bulk_outreach`, `bulk_outreach_for_job`

SMS / phone:
- `send_sms`, `list_phone_numbers`, `list_conversations`
- `sync_sms_threads_to_candidates` *(new — see `src/sms_sync.py`)*

Voice / screening:
- `trigger_screening_call`, `list_pending_screenings`, `list_recent_screenings`
- `pause_candidate`, `process_screening_result` *(new — see `src/screening_automation.py`)*

Retell agent lifecycle (BK-JR-only filter enforced — see `src/retell_client.py`):
- `retell_list_agents` *(updated: defaults to BK JR only)*
- `retell_list_bkjr_agents` *(new — explicit safe-by-default)*
- `retell_get_agent`, `retell_create_agent`, `retell_place_call`
- `retell_get_call`, `retell_list_calls`, `retell_get_llm`, `retell_update_llm`
- `retell_list_phone_numbers`, `retell_update_phone_number`
- `retell_get_concurrency`, `retell_update_live_call`

Notifications + Google services:
- `notify_bk`
- `gmail_send`, `gcal_list_events`, `gcal_create_event`
- `gdrive_create_folder`, `gdrive_share`

Safety:
- `preflight`, `get_practice_mode`, `set_practice_mode`
- `list_opt_outs`

## Security

- All secrets live in Coolify env vars (encrypted at rest). Repo never
  contains plaintext keys — see `SECURITY-NOTES.md` for the historical
  exposure and recommended rotations.
- MCP HTTP transport requires `Authorization: Bearer <MCP_AUTH_TOKEN>`.
- Retell allowlist enforced at the client layer
  (`src/retell_client.py::assert_agent_allowed`) — every operation that
  targets an agent checks it against `BKJR_AGENT_IDS` /
  `BKJR_NAME_PREFIXES` / `PROTECTED_AGENT_IDS`.

## CI/CD

- `.github/workflows/test.yml` — pytest + ruff on every PR / push to main
- `.github/workflows/deploy-staging.yml` — staging-branch deploy +
  smoke test (requires `STAGING_MCP_URL`, `STAGING_MCP_TOKEN`,
  `DEPLOY_WEBHOOK` secrets in GitHub)
- Coolify auto-deploy on push to `main` (production) — no workflow
  needed; Coolify watches the repo directly.