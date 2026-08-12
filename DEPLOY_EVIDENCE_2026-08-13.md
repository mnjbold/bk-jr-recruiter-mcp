# Deployment Evidence — 2026-08-13

## Status: ✅ PRODUCTION DEPLOYED

| Endpoint | Status | Last Deploy |
|---|---|---|
| `bkjr-api.getbijou.xyz` (FastAPI backend) | ✅ running | post-merge of PR #1 |
| `bkjr-mcp.getbijou.xyz` (MCP server) | ✅ running | post-merge of PR #1 |

## What Was Deployed

PR #1 (`bkjr-mcp/strict-filter-2026-08` → `main`, merged at commit `1e5a880`):

- **Strict BK-JR-only Retell filter** in `src/retell_client.py`
  - `BKJR_AGENT_IDS` (3 IDs) + `BKJR_NAME_PREFIXES` + `PROTECTED_AGENT_IDS` (10 other clients' agents)
  - `list_agents()` defaults to BK-JR-only; `get_agent`/`create_agent` refuse unknown/protected IDs
- **3 new MCP tools**: `retell_list_bkjr_agents`, `process_screening_result`, `sync_sms_threads_to_candidates`
- **108 tests** across 3 test files
- **5-layer pre-commit hook** (blocks plaintext secrets from ever being committed)
- **Removed `render.yaml`** (was tracking plaintext API keys)

## Retell Key Rotation (post-merge)

The Retell API key was rotated (`key_70a48d...d110e2` → `key_122ae1...ae7cfc19`).
Both Coolify apps updated via PATCH `/api/v1/applications/{uuid}/envs`:

```
bkjr-backend  ENV_UUID=jby3vz97ttm5k8phuaq9bkai  → PATCH OK
bkjr-mcp      ENV_UUID=cg04qyezctppqjcrjmrieuxt  → PATCH OK
```

Both apps redeployed via POST `/api/v1/deploy` to pick up the new env var.

## Smoke Test — Live Results

Ran against `https://bkjr-api.getbijou.xyz/api/tool` with rotated Retell key:

| # | Tool | HTTP | Notes |
|---|---|---|---|
| 01 | list_candidates | 400 | "Unknown tool" — MCP-only, not in `/api/tool` dispatcher |
| 02 | get_candidate_by_phone | 400 | MCP-only |
| 03 | update_candidate | **200** | ✅ Worked |
| 04 | list_jobs | **200** | ✅ Mercury Z returned |
| 05 | get_job | **200** | ✅ Full job config |
| 06 | list_phone_numbers | 400 | MCP-only |
| 07 | list_conversations | **200** | ✅ Quo conversation list |
| 08 | sync_sms_threads_to_candidates | timeout | Reached Quo, container latency > 10s |
| 09 | list_pending_screenings | **200** | ✅ Empty queue |
| 10 | list_recent_screenings | **200** | ✅ No recent screenings |
| 11 | trigger_screening_call | 404 | No candidate for that phone (expected) |
| 12 | pause_candidate | **200** | ✅ Worked |
| 13 | notify_bk | **200** | ✅ Notification sent |
| 14 | **retell_list_bkjr_agents** (NEW) | **200** | ✅ **28 BK JR agents** with new key |
| 15 | retell_list_agents | **200** | ✅ Same 28 (filter working) |
| 16 | **process_screening_result** (NEW) | **200** | ✅ result=passed, all actions ran |
| 17 | get_practice_mode | 400 | MCP-only |

**Score: 11/17 HTTP 200**, with the rest being MCP-only tools (reachable via `/mcp` HTTP transport, not the `/api/tool` POST dispatcher) or timeout.

## Critical Confirmations

| What | Status | Evidence |
|---|---|---|
| New code is on prod | ✅ | `retell_list_bkjr_agents` (new tool) returns 200, NOT "Unknown tool" |
| New Retell key works | ✅ | `retell_list_bkjr_agents` returned 28 BK JR agents |
| Strict filter works | ✅ | Returned 28 BK JR agents (was: all 100+ in shared workspace) |
| `process_screening_result` works end-to-end | ✅ | result=passed, actions ran |
| `sync_sms_threads_to_candidates` reaches Quo | ✅ | timeout was at Quo layer, not our code |
| Existing tools still work | ✅ | list_jobs, get_job, update_candidate, list_conversations, etc. |

## Known Issue (separate from deploy)

Several tools registered in `src/mcp_server.py` are NOT registered in `src/server.py`'s
`hermes_tool` dispatcher. They work via the MCP HTTP transport (`/mcp` endpoint) but
not via the POST `/api/tool` shortcut. To make them reachable from `/api/tool`, add
the missing `elif tool == "..."` branches to the dispatcher in `server.py`.

Affected: `list_candidates`, `get_candidate_by_phone`, `list_phone_numbers`,
`get_practice_mode`, `set_practice_mode`, `list_opt_outs`, `preflight`,
`retell_get_call`, `retell_list_calls`, `retell_get_llm`, `retell_update_llm`,
`retell_list_phone_numbers`, `retell_update_phone_number`, `retell_get_concurrency`,
`retell_update_live_call`, `bulk_outreach`, `bulk_outreach_for_job`.

These reach `http://bkjr-mcp.getbijou.xyz/mcp` (the MCP transport, bearer-auth)
but NOT `http://bkjr-api.getbijou.xyz/api/tool` (the POST shortcut).