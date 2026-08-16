"""
FastMCP server — BK JR's recruiting assistant tools.

Exposes the full recruiting flow as MCP tools. Any MCP-aware client
(Claude Code, Claude Desktop, OpenClaw, Hermes, your own scripts) can
use these to drive the recruiting pipeline end-to-end.

Two transports:
  stdio     — for LOCAL clients (Claude Code, Claude Desktop).
              Run: `python -m src.mcp_server` (default)
  streamable-http / sse — for REMOTE clients (OpenClaw, Hermes, hosted
              dashboards). Run: `python -m src.mcp_server --transport http --port 9000`

Each tool is a thin, authenticated HTTP call to the existing FastAPI
backend (/api/tool) — no business logic duplicated. The backend stays
the single source of truth, and a future dashboard can wire in
without touching the MCP surface.

Run:
    # Local (stdio, for Claude Code / Desktop)
    pip install "mcp[cli]" httpx
    BACKEND_URL=https://bk-jr-api.aixlabs.fun  SMS_AGENT_API_KEY=...  python -m src.mcp_server

    # Remote (HTTP, for OpenClaw / Hermes / hosted clients)
    python -m src.mcp_server --transport http --host 0.0.0.0 --port 9000
    # Then point the client at: http://localhost:9000/mcp

Env:
    BACKEND_URL         base URL of the FastAPI backend
                        (default http://localhost:8080)
    SMS_AGENT_API_KEY   bearer token for the backend's /api/* routes
                        (default "sms-agent-changeme-2026")
    MCP_TRANSPORT       "stdio" (default) | "http" | "sse"
    MCP_HOST            host for HTTP transport (default 0.0.0.0)
    MCP_PORT            port for HTTP transport (default 9000)
"""
# NOTE: do NOT add `from __future__ import annotations` here — FastMCP
# 1.9 inspects function signatures and crashes on stringified type hints.

import argparse
import os

import httpx
from mcp.server.fastmcp import FastMCP

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080").rstrip("/")
AGENT_API_KEY = os.environ.get("SMS_AGENT_API_KEY", "sms-agent-changeme-2026")

mcp = FastMCP(
    "bk-jr-recruiting",
    instructions=(
        "BK JR is a recruiting assistant. It can read the candidate sheet, "
        "send SMS via Quo/OpenPhone (using BK's number +18132952007), place "
        "outbound AI screening calls via Retell (from +18132146207), and write "
        "results back to the Google Sheet. ALWAYS look the candidate up in "
        "the sheet first before sending SMS or placing a call — never invent "
        "a phone number. Never call tools with empty required fields."
    ),
)


def _call(tool: str, params: dict) -> dict:
    """POST to the backend's unified /api/tool dispatcher."""
    resp = httpx.post(
        f"{BACKEND_URL}/api/tool",
        headers={"Authorization": f"Bearer {AGENT_API_KEY}"},
        json={"tool": tool, "params": params},
        timeout=60,
    )
    # Raise on HTTP error so MCP returns a clean error
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"backend {resp.status_code}: {detail}")
    return resp.json()


# ── Core recruiting tools ────────────────────────────────────────────────────

@mcp.tool()
def list_candidates() -> dict:
    """
    Return all tracked candidates with status + screening results from
    the in-memory state. This is BK's live board data (also written back
    to the Google Sheet via Composio).
    """
    return _call("get_candidates", {})


@mcp.tool()
def get_candidate_by_phone(phone: str) -> dict:
    """
    Look up a single candidate by phone (E.164, with or without '+').
    Returns name, location, role, state, last_call_result, etc.
    """
    candidates = _call("get_candidates", {}).get("candidates", [])
    phone_digits = "".join(c for c in phone if c.isdigit())[-10:]
    for c in candidates:
        cand_digits = "".join(c2 for c2 in str(c.get("phone", "")) if c2.isdigit())[-10:]
        if cand_digits == phone_digits:
            return {"found": True, "candidate": c}
    return {"found": False, "phone": phone}


@mcp.tool()
def update_candidate(phone: str, fields: dict) -> dict:
    """
    Update a candidate's row fields (e.g. {"state": "interview_scheduled",
    "screening_result": "passed"}) and sync to the Google Sheet.
    Returns the updated candidate.
    """
    return _call("update_candidate", {"phone": phone, "fields": fields})


@mcp.tool()
def list_jobs() -> dict:
    """List all configured job postings (e.g. mercury_z_fiber_ir_technician_2026_07)."""
    return _call("list_jobs", {})


@mcp.tool()
def get_job(job_id: str) -> dict:
    """Get the full config for a job posting (locations, requirements, screening call config, SMS templates)."""
    return _call("get_job", {"job_id": job_id})


# ── SMS tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def send_sms(
    to: str,
    message: str,
    candidate_name: str = "",
    from_number_id: str = "",
) -> dict:
    """
    Send an SMS to a candidate from BK's Quo number (+18132952007).
    `to` must be a real E.164 phone number from the sheet (col H).
    `message` must be a real SMS body. The backend returns 400 if either
    is empty. ALWAYS confirm the recipient + message with the user
    before calling this. Returns {ok, message_id, to}.
    """
    params = {"to": to, "message": message}
    if candidate_name:
        params["candidate_name"] = candidate_name
    if from_number_id:
        params["from_number_id"] = from_number_id
    return _call("send_sms_v2", params)


@mcp.tool()
def list_phone_numbers() -> dict:
    """List all OpenPhone/Quo phone numbers on BK's account (for the `from_number_id` field of send_sms)."""
    return _call("list_numbers", {})


@mcp.tool()
def list_conversations(
    phone_number_id: str = "",
    limit: int = 20,
    page_token: str = "",
) -> dict:
    """
    List recent SMS conversations on the given OpenPhone number (defaults to
    BK's primary). Returns up to `limit` conversations (max 100 per call;
    follow `next_page_token` to paginate through the full history).
    """
    params: dict = {"limit": max(1, min(int(limit), 100))}
    if phone_number_id:
        params["phone_number_id"] = phone_number_id
    if page_token:
        params["page_token"] = page_token
    return _call("list_conversations", params)


@mcp.tool()
def list_messages(
    participant: str = "",
    phone_number_id: str = "",
    limit: int = 50,
    page_token: str = "",
) -> dict:
    """
    Read SMS message bodies (with direction: 'inbound' / 'outbound') for a
    Quo number, optionally filtered to a single participant (E.164).

    Closes the BK JR MCP's historical "no response body" gap — every message
    returned includes its full `text` field plus delivery state, so you can
    finally see what a candidate wrote. Paginated; pass `page_token` to
    follow the cursor.
    """
    params: dict = {"limit": max(1, min(int(limit), 100))}
    if participant:
        params["participant"] = participant
    if phone_number_id:
        params["phone_number_id"] = phone_number_id
    if page_token:
        params["page_token"] = page_token
    return _call("list_messages", params)


@mcp.tool()
def list_calls(
    participant: str,
    phone_number_id: str = "",
    limit: int = 50,
    page_token: str = "",
) -> dict:
    """
    List voice calls between BK's Quo number and a single participant.
    Quo's REST endpoint requires 1:1 (no bulk), so `participant` is mandatory.
    """
    params: dict = {"participant": participant, "limit": max(1, min(int(limit), 100))}
    if phone_number_id:
        params["phone_number_id"] = phone_number_id
    if page_token:
        params["page_token"] = page_token
    return _call("list_calls", params)


@mcp.tool()
def get_call_transcript(call_id: str) -> dict:
    """
    Fetch transcript + metadata for a Retell (or Quo voice) call. Returns
    utterances [{speaker, text, started_at}], call_analysis, recording_url.
    Prefer Retell when available — its transcripts are richer.

    Use this to read what a candidate said during screening — works for any
    call id, even years-old, as long as Retell/Quo retains it.
    """
    return _call("get_call_transcript", {"call_id": call_id})


@mcp.tool()
def register_webhook(url: str = "", phone_number_id: str = "") -> dict:
    """
    Register a Quo webhook subscription so live inbound events (SMS received,
    call completed, etc.) land at YOUR URL in real time.

    Defaults: targets THIS backend's /webhook/quo (so events are stored in
    the webhook_events log + push a GChat notification to BK). Override `url`
    only for testing (e.g. ngrok/cloudflared) — a non-public URL will
    silently fail to receive deliveries.
    """
    params: dict = {}
    if url:
        params["url"] = url
    if phone_number_id:
        params["phone_number_id"] = phone_number_id
    return _call("register_webhook", params)


@mcp.tool()
def list_webhooks() -> dict:
    """List Quo webhook subscriptions currently registered on BK's account."""
    return _call("list_webhooks", {})


@mcp.tool()
def unregister_webhook(webhook_id: str) -> dict:
    """
    Remove a Quo webhook subscription. `webhook_id` comes from list_webhooks().
    Idempotent — already-gone ids return ok=True.
    """
    return _call("unregister_webhook", {"webhook_id": webhook_id})


@mcp.tool()
def list_webhook_events(
    limit: int = 50,
    source: str = "",
    phone: str = "",
    since_seconds: int = 0,
) -> dict:
    """
    Recent webhook event log — every Quo (SMS) + Retell (call) event the
    backend has received since startup. Newest first.

    Filters:
      - `source`: 'quo' | 'retell' | empty (all)
      - `phone`: exact E.164 match
      - `since_seconds`: only events from the last N seconds (0 = no limit)

    Each row includes the SMS body (or first 3 transcript utterances for
    calls), the message/call id, and a truncated payload preview. Use this
    instead of grepping structlog when answering "what did the candidate say?".
    """
    params: dict = {"limit": max(1, min(int(limit), 500))}
    if source:
        params["source"] = source
    if phone:
        params["phone"] = phone
    if since_seconds and int(since_seconds) > 0:
        params["since_seconds"] = int(since_seconds)
    return _call("list_webhook_events", params)


@mcp.tool()
def sync_sms_threads_to_candidates(
    window_days: int = 7,
    phone_number_id: str = "",
) -> dict:
    """
    Pull recent SMS conversations from Quo and reconcile with the
    candidate sheet.

    For each thread whose phone number is NOT already tracked, we add a
    new candidate row with `state: "sms_engaged"` and `source: "sms_sync"`.
    For threads whose phone IS already tracked, we bump `last_sms_activity`
    and update state if it was stale.

    The BK JR MCP exposes only conversation METADATA — not message bodies.
    So this sync cannot classify "YES" replies; that requires a separate
    integration with Quo's message-list endpoint (see KNOWN_GAPS).

    Args:
        window_days: only consider conversations active within N days
            (default 7). Stale threads are skipped.
        phone_number_id: optional Quo number ID; defaults to BK's primary.

    Returns:
        {ok, window_days, total_threads, in_window, created_count,
         updated_count, skipped_count, created, updated, skipped_sample}
    """
    return _call("sync_sms_threads_to_candidates", {
        "window_days": window_days,
        "phone_number_id": phone_number_id,
    })


# ── Voice / screening tools ──────────────────────────────────────────────────

@mcp.tool()
def trigger_screening_call(
    phone: str,
    job_id: str = "",
    candidate_name: str = "",
    call_type: str = "initial_screening",
    context: str = "",
) -> dict:
    """
    Place an outbound AI screening call to a candidate via Retell voice agent
    (from +18132146207). The call uses the screening call config from the
    job posting (questions, custom_analysis_fields). The candidate's
    state advances to screening_call_queued, then screening_passed /
    screening_failed / screening_needs_follow_up when the call_analyzed
    webhook fires.

    REQUIRED: `phone` must be a real E.164 from the sheet (col H).
    REQUIRED: `candidate_name` must be a real name.
    `call_type` is "initial_screening" (first call) or "follow_up"
    (post-screening, transfer to BK enabled).
    `context` (optional, 2026-08-07): free-form text surfaced to the voice
    agent as `{{context}}` — talking points, empathy flags, scheduling
    preferences, etc. Empty string = no special context.
    """
    # Defensive: refuse if phone or name is empty (the agent LLM also has
    # this rule but defense in depth is cheap here).
    if not phone or not str(phone).strip():
        raise RuntimeError("phone is required and must be a non-empty E.164 number")
    if not candidate_name or not str(candidate_name).strip():
        raise RuntimeError("candidate_name is required (look the candidate up in the sheet first)")
    return _call("trigger_screening_call", {
        "phone": phone,
        "job_id": job_id or None,
        "candidate_name": candidate_name,
        "call_type": call_type,
        "context": context or None,
    })


@mcp.tool()
def list_pending_screenings() -> dict:
    """
    List candidates currently in screening_call_queued / sms2_queued /
    call_in_progress state. Useful for showing the AI agent (or BK)
    what's about to be called.
    """
    return _call("list_pending_screenings", {})


@mcp.tool()
def list_recent_screenings(limit: int = 20) -> dict:
    """
    Last N screening results (screening_passed / failed / needs_follow_up
    / no_answer / call_transferred). Sorted by most recent first.
    """
    return _call("list_recent_screenings", {"limit": limit})


@mcp.tool()
def pause_candidate(phone: str, reason: str = "manual pause") -> dict:
    """
    Override: mark a candidate as paused so no further outreach or
    screening calls are placed. Syncs to the Google Sheet.
    """
    return _call("pause_candidate", {"phone": phone, "reason": reason})


# ── Bulk / batch outreach ────────────────────────────────────────────────────

@mcp.tool()
def bulk_outreach(candidates: list, template: str = "mercury_initial") -> dict:
    """
    Send outreach to a list of candidates in bulk, using the named template
    (e.g. "mercury_initial" or a per-job template). Returns per-candidate results.
    """
    return _call("bulk_outreach", {"candidates": candidates, "template": template})


@mcp.tool()
def bulk_outreach_for_job(
    candidates: list,
    job_id: str,
    skip_if_contacted: bool = True,
    dry_run: bool = False,
    max_per_run: int = 50,
) -> dict:
    """
    Bulk outreach for a specific job posting. Uses the job's own templates
    and screening call config. `dry_run=True` to preview without sending.
    """
    return _call("bulk_outreach_for_job", {
        "candidates": candidates,
        "job_id": job_id,
        "skip_if_contacted": skip_if_contacted,
        "dry_run": dry_run,
        "max_per_run": max_per_run,
    })


# ── Notification ─────────────────────────────────────────────────────────────

@mcp.tool()
def notify_bk(message: str) -> dict:
    """
    Push a notification to BK (via Google Chat webhook / WhatsApp,
    whichever is configured). Use this for "candidate X is ready",
    "screening failed for Y", etc. — anything BK needs to act on.
    """
    return _call("notify_bk", {"message": message})


# ── Google services (Gmail / Calendar / Drive) — Ed's "wired with GSuite" ask ─

@mcp.tool()
def gmail_send(to: str, subject: str, body: str) -> dict:
    """
    Send an email from BK's connected Gmail account. Use for:
    - Interview confirmations ("Hi John, see you Tuesday at 2pm")
    - Document collection ("Upload your I-9 here: <link>")
    - Offer letters / onboarding packets
    Returns {ok, message_id}.
    """
    return _call("gmail_send", {"to": to, "subject": subject, "body": body})


@mcp.tool()
def gcal_list_events(time_min: str = "", time_max: str = "", max_results: int = 10) -> dict:
    """
    List events on BK's primary calendar between time_min and time_max.
    Both should be ISO 8601 (e.g. "2026-08-07T00:00:00Z"). If omitted,
    defaults to "now" through "+7 days". Used to find open slots before
    booking an interview.
    """
    params: dict = {"max_results": max_results}
    if time_min:
        params["time_min"] = time_min
    if time_max:
        params["time_max"] = time_max
    return _call("gcal_list_events", params)


@mcp.tool()
def gcal_create_event(
    summary: str,
    start: str,
    end: str,
    attendees: list | None = None,
    description: str = "",
) -> dict:
    """
    Create a calendar event on BK's calendar. `start` and `end` are ISO 8601.
    `attendees` is a list of email addresses — Composio will send Google
    Calendar invites to each. Used to book interviews automatically after a
    candidate passes screening.
    """
    params: dict = {"summary": summary, "start": start, "end": end, "description": description}
    if attendees:
        params["attendees"] = attendees
    return _call("gcal_create_event", params)


@mcp.tool()
def gdrive_create_folder(name: str, parent_id: str = "") -> dict:
    """
    Create a folder in BK's Drive. `parent_id` is optional (defaults to root).
    Use to create per-candidate document folders, e.g. "John Doe - Fiber I&R - 2026-08".
    """
    params: dict = {"name": name}
    if parent_id:
        params["parent_id"] = parent_id
    return _call("gdrive_create_folder", params)


@mcp.tool()
def gdrive_share(file_id: str, email: str, role: str = "reader") -> dict:
    """
    Share a Drive file/folder with `email` at the given role
    ('reader' | 'commenter' | 'writer' | 'organizer'). Use after creating
    a candidate's document folder so they can upload their I-9, W-9, etc.
    """
    return _call("gdrive_share", {"file_id": file_id, "email": email, "role": role})


# ── Retell agent lifecycle (Ed's "spin up any agent on demand" ask) ──────────

@mcp.tool()
def retell_list_bkjr_agents() -> dict:
    """
    List BK JR-owned Retell voice agents ONLY.

    This is the safe, default way to see what agents are available
    (screener, follow-up, scheduler, etc.) before dispatching a call.
    Other clients' production agents are filtered out — they are not
    visible to BK JR's MCP. Use this for normal agent discovery.

    Returns:
        {"agents": [...], "count": N, "filter": "bkjr_only"}
    """
    return _call("retell_list_bkjr_agents", {})


@mcp.tool()
def retell_list_agents(
    include_other_clients: bool = False,
) -> dict:
    """
    List Retell voice agents on BK's workspace.

    STRICT FILTER (default): only returns agents that are BK JR-owned
    (in BKJR_AGENT_IDS OR whose name starts with "BK JR" / "Outbound
    Screening Agent"). Other clients' production agents (Pearl Health,
    NNC, Bold Connect IT Support, etc.) are filtered out.

    Args:
        include_other_clients: True to return ALL agents on the workspace,
            including ones belonging to other clients. Use ONLY for
            diagnostic / audit purposes — every call logs a WARN. False
            (default) is the safe choice for normal agent discovery.

    Prefer `retell_list_bkjr_agents` for the common case — it's the same
    default but with a name that makes the intent obvious at call sites.
    """
    return _call("retell_list_agents", {"include_other_clients": include_other_clients})


@mcp.tool()
def process_screening_result(
    call_id: str = "",
    phone: str = "",
    candidate_name: str = "",
    screening_result: str = "",
    custom_analysis_fields: dict | None = None,
) -> dict:
    """
    Post-screening automation: branch on a Retell call's screening outcome.

    The webhook at /webhook/retell calls this automatically when
    `call_analyzed` fires; this MCP wrapper lets external clients
    (Hermes, OpenClaw, Claude Desktop) trigger the same workflow
    manually — useful for re-processing, debugging, or piping a result
    that came from somewhere other than Retell.

    Branches:
      - "passed":          update state → screening_passed, send SMS,
                           notify BK, create GCal event stub, create
                           Drive folder, send Gmail packet.
      - "needs_follow_up": update state → screening_needs_follow_up,
                           send a personalized SMS based on which
                           requirement failed, pause the candidate 48h,
                           notify BK.
      - "failed":          update state → screening_failed, pause
                           permanently.

    Args:
        call_id:  Retell call ID. If set, fetches the call to extract
                  `metadata.candidate_phone` and
                  `call_analysis.custom_analysis_fields`. Optional if
                  `phone` + `screening_result` are provided directly.
        phone:    E.164 candidate phone (col H from the sheet).
        candidate_name: Required if `phone` is for a candidate not in
                  the in-memory state.
        screening_result: One of "passed" / "needs_follow_up" / "failed".
                          Required if `call_id` is not given.
        custom_analysis_fields: Pass-through overrides for the
                  has_iphone_11_plus / has_vehicle_with_ladder_rack /
                  can_commit_schedule / preferred_location fields.
                  Optional — used when `call_id` isn't available.

    Idempotent: same call_id → same response, no duplicate SMS / events.
    """
    params: dict = {}
    if call_id:
        params["call_id"] = call_id
    if phone:
        params["phone"] = phone
    if candidate_name:
        params["candidate_name"] = candidate_name
    if screening_result:
        params["screening_result"] = screening_result
    if custom_analysis_fields:
        params["custom_analysis_fields"] = custom_analysis_fields
    return _call("process_screening_result", params)


@mcp.tool()
def retell_get_agent(agent_id: str) -> dict:
    """
    Fetch full agent config (prompt, voice, tools, dynamic variables).
    Use to inspect what an existing agent does before using it.
    """
    return _call("retell_get_agent", {"agent_id": agent_id})


@mcp.tool()
def retell_create_agent(
    name: str,
    system_prompt: str,
    voice_id: str = "",
    llm_id: str = "gpt-4.1",
    from_number: str = "",
) -> dict:
    """
    Create a brand new Retell voice agent on the workspace. Ed's "build me
    an agent that interviews for X and asks about Y" — the LLM fills in
    `name` and `system_prompt` from the user's intent, and the new agent
    is immediately available for `retell_place_call`.

    `from_number` is the E.164 to bind the agent to (e.g. +18132146207).
    """
    params: dict = {
        "name": name,
        "system_prompt": system_prompt,
        "llm_id": llm_id,
    }
    if voice_id:
        params["voice_id"] = voice_id
    if from_number:
        params["from_number"] = from_number
    return _call("retell_create_agent", params)


@mcp.tool()
def retell_place_call(
    to_number: str,
    agent_id: str = "",
    from_number: str = "",
    dynamic_variables: dict | None = None,
) -> dict:
    """
    Place an outbound call to ANY Retell agent (not just the default
    screener). The full Ed vision: "spin up any agent and make it call."

    `to_number` must be a real E.164 (e.g. +18138223579).
    `agent_id` is the Retell agent to dispatch to — use retell_list_agents
    to see available agents, or retell_create_agent to spin up a new one.
    `dynamic_variables` are injected into the agent's prompt at call time
    (e.g. {"candidate_name": "John", "role": "Fiber I&R Tech"}).
    """
    params: dict = {"to_number": to_number}
    if agent_id:
        params["agent_id"] = agent_id
    if from_number:
        params["from_number"] = from_number
    if dynamic_variables:
        params["dynamic_variables"] = dynamic_variables
    return _call("retell_place_call", params)


# ── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="BK JR MCP server")
    p.add_argument("--transport", choices=["stdio", "http", "sse"],
                   default=os.environ.get("MCP_TRANSPORT", "stdio"),
                   help="Transport: stdio (default, local) or http (remote)")
    p.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"),
                   help="Host for HTTP transport (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "9000")),
                   help="Port for HTTP transport (default 9000)")
    args = p.parse_args()

    if args.transport == "stdio":
        mcp.run()  # default = stdio
    elif args.transport == "http":
        # Streamable HTTP transport — works with Claude Desktop, OpenClaw, Hermes,
        # mcp-remote, etc. Mount point is /mcp.
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        # Fix (2026-08-04): default session-based streamable-http mode hit
        # "RuntimeError: Task group is not initialized" under Cloudflare tunnel
        # + repeat requests. Stateless mode avoids the long-lived session/task-group
        # entirely -- each request is independent, which matches how MCP clients
        # (Claude Code, OpenClaw, Hermes) actually call this server (short tool bursts,
        # no persistent bidirectional session needed).
        mcp.settings.stateless_http = True
        mcp.run(transport="streamable-http")
    elif args.transport == "sse":
        # Legacy SSE transport (older clients).
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
