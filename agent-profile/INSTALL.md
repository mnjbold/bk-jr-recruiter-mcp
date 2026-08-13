# Install BK Jr. — Bring the BK JR Recruiting Agent into Claude Desktop

This is the **portable BK Jr. persona package** — drop it into Claude Desktop
(or any MCP-aware client) and it acts as BK Jr., the recruiting ops agent for
Mercury Z / Bold Business hiring operations.

## ⚡ One-Step Install (Claude Desktop)

1. **Quit Claude Desktop** (so the config reload picks up the new server).
2. **Get your SMS Agent API key.** It's `SMS_AGENT_API_KEY` — same one already
   in the `.env.local` you copied earlier. (Visible only to operators.)
3. **Open Claude Desktop's config file** at:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
4. **Paste this block** into the top-level `mcpServers` field (merge with
   anything already there — keep your existing servers):

   ```json
   {
     "mcpServers": {
       "bk-jr": {
         "command": "C:\\Program Files\\nodejs\\npx.cmd",
         "args": [
           "-y", "mcp-remote",
           "https://bkjr-mcp.getbijou.xyz/mcp",
           "--header", "Authorization:***"
         ],
         "env": { "AUTH_HEADER": "Bearer sms-agent-bkjr-XhjK4yLtkZ5MdhNzGIlHRUKtYXdaqJJlx0qg7CuyXPo" }
       }
     }
   }
   ```

   (Substitute the actual bearer token at deployment time. Never commit it.)

5. **Save** the file, **relaunch** Claude Desktop.
6. **Ask Claude**: *"List the BK JR tools"* — Claude should see ~27 tools
   and respond with the persona from `agent-profile/SOUL.md`.

## 📂 What's in this folder

```
agent-profile/
├── AGENT.md                  # Tool inventory + transport + boundaries (load first)
├── USER.md                   # Who BK is + escalation rules
├── SOUL.md                   # Voice + principles + refusals (load second)
├── rules/operating-rules.md  # R1–R8 hard constraints (rules outrank skills)
├── skills/
│   ├── candidate-outreach.md # SMS + bulk + reply handling
│   ├── screening-calls.md    # Retell voice screening
│   └── agent-builder.md      # retell_create_agent playbook
├── connectors/
│   ├── claude-desktop.json   # ready-to-paste into claude_desktop_config.json
│   ├── generic-mcp.json      # for Claude Code / Cursor / Continue
│   └── README.md             # raw curl examples + auth-proving sequence
├── hooks/hooks.json          # lifecycle hooks (confirmation, write-back, halt)
└── INSTALL.md                # this file
```

### Load order (for any new MCP-capable client)

`AGENT.md` → `USER.md` → `SOUL.md` → `rules/operating-rules.md` → the relevant `skills/*.md`

Rules outrank skills. Skills outrank tone. Don't skip steps.

## 🔐 Auth model

The MCP server at `bkjr-mcp.getbijou.xyz/mcp` requires `Authorization: Bearer <SMS_A..._EY>`.
The token is the same one used by `bkjr-api.getbijou.xyz` for the `POST /api/tool` shortcut.

**Proof the auth is real** (not just a 200-anyway):
```
$ curl -i https://bkjr-mcp.getbijou.xyz/mcp     # no header   -> 401
$ curl -i -H 'Authorization: Bearer wrong-key'  ...           -> 401
$ curl -i -H 'Authorization: Bearer sms-agent-...' ...         -> 200/200
```

The pass signal is **`401 / 401 / 200`** (no token / wrong token / right token).
A bare `403` instead of `401` means Cloudflare is UA-blocking you — use real curl or set a browser User-Agent.

## 🛠️ Tools BK Jr. exposes (27, verified live on prod)

**Identity & jobs**
`list_candidates` · `get_candidate_by_phone` · `update_candidate` · `list_jobs` · `get_job`

**SMS / Quo / OpenPhone**
`send_sms` · `list_phone_numbers` · `list_conversations` · `sync_sms_threads_to_candidates`

**Voice / Retell AI** (with strict BK-JR-only filter — never touches other clients' agents)
`trigger_screening_call` · `retell_place_call` · `retell_list_bkjr_agents`
`retell_list_agents` · `retell_get_agent` · `retell_create_agent`
`list_pending_screenings` · `list_recent_screenings`

**Post-screening automation**
`process_screening_result` (branches on passed / needs_follow_up / failed)

**Workspace (via Composio — uses BK's Google login)**
`gmail_send` · `gcal_list_events` · `gcal_create_event`
`gdrive_create_folder` · `gdrive_share` · `notify_bk`

**Candidate state**
`bulk_outreach` · `bulk_outreach_for_job` · `pause_candidate`

**Safety**
`preflight` · `get_practice_mode` · `set_practice_mode` · `list_opt_outs`

## 🚦 Boundaries (R1–R8 — see rules/operating-rules.md)

R1 — Confirm before irreversible actions (any batch >1 recipient)
R2 — Resolve identity before contact (never type phone numbers from memory)
R3 — Verify at the user's layer ("sent" ≠ delivered; check `list_conversations`)
R4 — One follow-up max (then `update_candidate` → `no_response`, `pause_candidate`)
R5 — Honour opt-outs immediately (any STOP/UNSUBSCRIBE → `pause_candidate`)
R6 — Log every outcome (`update_candidate` after every send / call / reply)
R7 — New agents are scoped (one role, one job — copy from existing via `retell_get_agent`)
R8 — Auth failures stop everything (401/403 → halt, `notify_bk`)

## 🌍 Live endpoints

| Endpoint | URL | Notes |
|---|---|---|
| MCP (streamable HTTP) | `https://bkjr-mcp.getbijou.xyz/mcp` | 27 tools, bearer required |
| Backend (FastAPI)    | `https://bkjr-api.getbijou.xyz` | `/api/tool` shortcut, `/webhook/quo`, `/webhook/retell` |
| Health               | `https://bkjr-mcp.getbijou.xyz/health` | `{"tools":27,"status":"ok"}` |

## 🆘 If BK Jr. misbehaves

1. **First**, check the **rules/** file — every hard constraint is enumerated there.
2. **Then** check **SOUL.md** for voice/principles.
3. **Then** check the **skill** for the active task.
4. If Claude makes a tool call you don't want — say *"use pause_candidate first"*
   or *"escalate that to BK via notify_bk"*.
5. If Claude is being too chatty / asks too many questions — point it at
   `rules/operating-rules.md` again and say *"load rules before next action"*.

The persona is **opinionated** by design: it should never invent candidate
phone numbers, never message opt-outs, never promise things not in the
job record. If it does, that's a bug — report it as a bug.

---

**Source repo:** `mnjbold/bk-jr-recruiter-mcp` @ main
**Latest agent-profile commit:** see `git log -- agent-profile/`
**Live MCP version:** 1.9.0 (29 tools in workspace, 27 exposed via MCP)
**Last updated:** see commit history
