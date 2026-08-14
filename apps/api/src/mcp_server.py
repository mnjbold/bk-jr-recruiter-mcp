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
import sys

import httpx
from mcp.server.fastmcp import FastMCP

# Load .env so SMS_AGENT_API_KEY reaches this process. .mcp.json is git-tracked,
# so the key must NOT be inlined there — and without it this module silently fell
# back to the changeme default below, which made every /api/tool call 401 while
# preflight's unauthenticated /health probe stayed green. That combination is
# exactly the failure this import prevents.
try:
    from dotenv import load_dotenv

    load_dotenv(".env", override=False)
except ImportError:  # dotenv is optional for hosted deploys that inject real env
    pass

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080").rstrip("/")
AGENT_API_KEY = os.environ.get("SMS_AGENT_API_KEY", "sms-agent-changeme-2026")

if AGENT_API_KEY == "sms-agent-changeme-2026":
    # Not fatal — a dev backend may genuinely use the default — but never silent,
    # because the resulting 401 looks like a network fault.
    print("WARNING: SMS_AGENT_API_KEY is the placeholder default; every /api/tool "
          "call will 401 unless the backend uses the same placeholder.", file=sys.stderr)

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
        except Exception:
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
    Uses the same digit-stripping + last-10 comparison as the backend's
    normalize_phone() so masked numbers (e.g. +181****3579) match their
    full-E.164 counterparts (e.g. +18138223579).
    """
    candidates = _call("get_candidates", {}).get("candidates", [])
    # Normalize the search phone: strip non-digits, keep last 10 digits (NANP default)
    import re
    search_digits = re.sub(r"\D", "", phone)[-10:]
    for c in candidates:
        cand_digits = re.sub(r"\D", "", str(c.get("phone", "")))[-10:]
        if cand_digits == search_digits:
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
def list_conversations(phone_number_id: str = "") -> dict:
    """List recent SMS conversations on the given OpenPhone number (defaults to BK's primary)."""
    params = {}
    if phone_number_id:
        params["phone_number_id"] = phone_number_id
    return _call("list_conversations", params)


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
    attendees: list = None,
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
def retell_list_agents(include_versions: bool = False) -> dict:
    """
    List all Retell voice agents on BK's workspace. Use to see what
    specialists are available (screener, follow-up, scheduler, etc.)
    before dispatching a call.

    One row per agent, showing the highest version and how many versions
    exist. Retell's raw endpoint returns one row per agent VERSION (~100 rows
    for ~15 agents, ~4k wasted tokens) — set include_versions=True only if you
    genuinely need every version listed separately.
    """
    return _call("retell_list_agents", {"include_versions": include_versions})


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
    dynamic_variables: dict = None,
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


# ── Safety: practice mode + opt-outs ─────────────────────────────────────────

@mcp.tool()
def preflight() -> dict:
    """
    RUN THIS BEFORE EVERY DEMO. One read-only green/red checklist covering
    everything that has to be true for a live telephony demo to work:

      backend reachable, SQLite state DB writable, practice-mode state,
      calling-window config, OpenPhone auth + the Quo number's restrictions,
      Retell auth + concurrency headroom, sheet read (tab, header row, how many
      rows have a usable phone in column H), each BK JR agent's is_published /
      version / webhook_events (including the four transfer_* events), Retell
      number bindings (and whether they pin latest_published rather than
      latest), and whether the practice number is a tracked candidate.

    Each check is {name, status: pass|warn|fail, detail, fix_hint}. The report
    carries `ok` (no failures), `counts`, `blocking` and `warnings`.
    Mutates nothing — safe to call as often as you like.
    """
    return _call("preflight", {})


@mcp.tool()
def get_practice_mode() -> dict:
    """
    Is practice mode on? When ON, every outbound SMS and call is redirected to
    the practice number and SMS bodies are prefixed [PRACTICE], so no real
    candidate can be reached. Defaults ON for a fresh deploy.
    """
    return _call("get_practice_mode", {})


@mcp.tool()
def set_practice_mode(on: bool) -> dict:
    """
    Turn practice mode on or off.

    set_practice_mode(False) ARMS LIVE SENDING: SMS and calls will reach real
    candidates. Confirm with the user in plain words before ever calling it
    with False. set_practice_mode(True) is always safe.
    """
    return _call("set_practice_mode", {"on": bool(on)})


@mcp.tool()
def list_opt_outs() -> dict:
    """
    Every phone number that has sent STOP/UNSUBSCRIBE/etc, with the reason and
    timestamp. These numbers are refused at the lowest send/dial level — no
    tool can text or call them until the person replies START.
    """
    return _call("list_opt_outs", {})


# ── Retell calls, LLMs, numbers, concurrency ─────────────────────────────────

@mcp.tool()
def retell_get_call(call_id: str, full: bool = False) -> dict:
    """
    Fetch one Retell call. Returns a COMPACT projection by default — call_id,
    call_status, disconnection_reason, duration_ms, transcript, call_analysis.

    full=True returns the raw object, which embeds per-word timings and
    recording URLs and is large enough to blow up the context window. Only ask
    for it when you specifically need a field the compact view omits.
    """
    return _call("retell_get_call", {"call_id": call_id, "full": full})


@mcp.tool()
def retell_list_calls(filters: dict = None, limit: int = 20) -> dict:
    """
    List recent Retell calls. `filters` is Retell's filter_criteria, e.g.
    {"agent": [{"agent_id": "agent_c530f8a10e8a502798c49a30c6"}]} or
    {"call_status": ["ended"]}. Use this to find a call_id, then
    retell_get_call for the detail.
    """
    return _call("retell_list_calls", {"filters": filters or {}, "limit": limit})


@mcp.tool()
def retell_get_llm(llm_id: str) -> dict:
    """
    Fetch a Retell LLM (response engine) — the prompt, tools and states behind
    an agent. Get the llm_id from retell_list_agents or retell_get_agent.
    """
    return _call("retell_get_llm", {"llm_id": llm_id})


@mcp.tool()
def retell_update_llm(llm_id: str, patch: dict) -> dict:
    """
    Patch a Retell LLM's config (prompt, tools, states).

    MUTATING. An LLM is shared by every agent referencing it, so this refuses
    if any denylisted agent (another client's production system) uses it —
    checked server-side, not here. Confirm the change with the user first.
    """
    return _call("retell_update_llm", {"llm_id": llm_id, "patch": patch})


@mcp.tool()
def retell_list_phone_numbers() -> dict:
    """
    List Retell phone numbers with their inbound/outbound agent bindings and
    each binding's agent_version. Bindings should pin 'latest_published' so an
    unpublished draft can never answer a live call — preflight() flags this.
    """
    return _call("retell_list_phone_numbers", {})


@mcp.tool()
def retell_update_phone_number(number: str, patch: dict) -> dict:
    """
    Rebind a Retell number. `number` is E.164, `patch` uses Retell's fields:
    inbound_agents / outbound_agents as [{"agent_id": ..., "agent_version":
    "latest_published", "weight": 1}].

    MUTATING and high-risk — rebinding is how you point another client's
    traffic at BK JR. Every agent_id in the patch is denylist-checked
    server-side. Confirm with the user before calling.
    """
    return _call("retell_update_phone_number", {"number": number, "patch": patch})


@mcp.tool()
def retell_get_concurrency() -> dict:
    """
    Current concurrent-call usage and the org limit. Headroom of zero means the
    next outbound call will be rejected — check this before a batch.
    """
    return _call("retell_get_concurrency", {})


@mcp.tool()
def retell_update_live_call(call_id: str, body: dict) -> dict:
    """
    Update a call that is happening RIGHT NOW — override dynamic variables or
    metadata mid-conversation. MUTATING, and only meaningful while the call is
    in progress. Get the call_id from retell_list_calls or the webhook.
    """
    return _call("retell_update_live_call", {"call_id": call_id, "body": body})


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
