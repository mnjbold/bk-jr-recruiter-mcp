"""
SMS Agent FastAPI Server
Exposes:
  POST /webhook/quo       — Quo inbound SMS webhook
  POST /webhook/retell    — Retell screening call events (call_analyzed writeback)
  POST /api/trigger-call  — Chat agent's trigger_followup_call → place screening call
  POST /api/send          — Send SMS (Hermes/OpenClaw tool endpoint)
  POST /api/relay         — Manual relay (BK types → SMS from his number)
  POST /api/bulk          — Bulk outreach (legacy templates)
  POST /api/bulk_job      — Generalized bulk outreach for any flows/jobs/*.yaml
  GET  /api/jobs          — List job postings
  GET  /api/candidates    — List tracked candidates
  GET  /health            — Health check
  POST /api/tool          — OpenClaw/Hermes unified tool endpoint
  POST /mcp               — MCP streamable-HTTP (Claude Code, OpenClaw, Hermes, etc.)
"""
# NOTE: do NOT add `from __future__ import annotations` here — FastAPI
# inspects the `lifespan` parameter at runtime, and stringified annotations
# break it. (The MCP server in mcp_server.py has the same caveat.)

import json
import os

import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from flows.jobs import list_jobs, load_job

from .agent import SMSRecruitmentAgent
from .retell_client import RetellClient

log = structlog.get_logger(__name__)

# CRITICAL: instantiate the agent AFTER env is loaded. The mcp_server mount
# below will reference module-level FastMCP `mcp` whose first @mcp.tool()
# decorator calls `agent` — so SMSRecruitmentAgent() must exist first.
agent = SMSRecruitmentAgent()

QUO_WEBHOOK_SECRET = os.environ.get("QUO_WEBHOOK_SECRET", "")
AGENT_API_KEY = os.environ.get("SMS_AGENT_API_KEY", "sms-agent-changeme-2026")
RETELL_API_KEY = os.environ.get("RETELL_API_KEY", "")


def _verify_api_key(authorization: str | None) -> bool:
    if not authorization:
        return False
    token = authorization.replace("Bearer ", "").strip()
    return token == AGENT_API_KEY


# (Lifespan is disabled — FastAPI 0.111 + Starlette 1.3 has a deprecation
# conflict that breaks APIRouter init. The startup/shutdown logs aren't
# critical for this MVP.)


app = FastAPI(title="Quo SMS Recruitment Agent", version="1.0.0")

# CORS so BK's browser board (board.html) can poll /api/candidates cross-origin.
# ponytail: allow_origins=["*"] is fine for a bearer-token-protected read API;
# tighten to the board's actual origin if it's ever served somewhere fixed.
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Log every incoming request body so we can see what the LLM is sending when
# it hallucinates param names. Critical for debugging 422s.
@app.middleware("http")
async def log_request_body(request, call_next):
    if request.url.path.startswith(("/api/", "/webhook/")):
        body = await request.body()
        if body:
            import sys
            print(f">>> {request.method} {request.url.path} body={body.decode('utf-8', errors='replace')[:500]}", file=sys.stderr, flush=True)
    return await call_next(request)


# ── Health ────────────────────────────────────────────────────────────────────

# ── Static widget (served from the project so the public URL is one-stop) ─────
_WIDGET_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "retell-screening", "widget-demo.html")
)


@app.get("/widget-demo.html")
def widget_demo():
    """Serve the BK JR recruiting console widget. Same file the local :8766 server serves."""
    if not os.path.exists(_WIDGET_PATH):
        raise HTTPException(status_code=404, detail="widget-demo.html not found")
    return FileResponse(_WIDGET_PATH, media_type="text/html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "agent": "sms-recruitment",
        "quo_configured": bool(os.environ.get("QUO_API_KEY") or os.environ.get("OPENPHONE_API_KEY")),
        "number_id_set": bool(os.environ.get("QUO_BK_NUMBER_ID")),
        "gchat_configured": bool(os.environ.get("GCHAT_WEBHOOK_URL")),
        "whatsapp_configured": bool(os.environ.get("WHATSAPP_TOKEN")),
        "gemini_available": bool(os.environ.get("GEMINI_API_KEY")),
        "retell_configured": bool(RETELL_API_KEY) and bool(os.environ.get("RETELL_SCREENING_FROM_NUMBER")),
        "sheets_configured": bool(agent.sheets and agent.sheets.configured),
        "jobs_available": list_jobs(),
    }


# ── Quo Webhook (inbound SMS from candidates) ─────────────────────────────────

@app.post("/webhook/quo")
async def quo_webhook(request: Request):
    """Quo calls this when BK receives an inbound SMS."""
    body = await request.body()
    payload = json.loads(body)

    log.info("quo_webhook_received", evt=payload.get("event", "unknown"))

    result = agent.handle_inbound(payload)
    return {"ok": True, "result": result}


# ── Retell Webhook (screening call events) ────────────────────────────────────

@app.post("/webhook/retell")
async def retell_webhook(request: Request, x_retell_signature: str = Header(None)):
    """Retell calls this on call_started / call_ended / call_analyzed events."""
    raw_body = await request.body()

    if RETELL_API_KEY:
        if not x_retell_signature or not RetellClient.verify_webhook(raw_body, x_retell_signature, RETELL_API_KEY):
            log.warning("retell_webhook_bad_signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    else:
        log.warning("retell_webhook_unverified", reason="RETELL_API_KEY not set — skipping signature check")

    payload = json.loads(raw_body)
    log.info("retell_webhook_received", evt=payload.get("event", "unknown"))

    result = agent.handle_retell_webhook(payload)
    return {"ok": True, "result": result}


# ── Trigger Call (chat agent's trigger_followup_call custom function) ──────────

@app.post("/api/trigger-call")
async def trigger_call(request: Request, x_retell_signature: str = Header(None),
                       authorization: str = Header(None)):
    """
    The Retell CHAT agent's `trigger_followup_call` custom function POSTs here to
    ask the VOICE agent to place a screening call. Also reachable from the
    recruiting console widget and from Hermes/OpenClaw tools.

    Auth (priority order):
      1. `X-Retell-Signature` HMAC  (Retell chat agent's custom-function calls)
      2. `Authorization: Bearer <SMS_AGENT_API_KEY>`  (widget / Hermes / OpenClaw)
      3. If RETELL_API_KEY is unset, signature check is skipped (dev mode)
    """
    raw_body = await request.body()

    if RETELL_API_KEY:
        retell_ok = (x_retell_signature
                     and RetellClient.verify_webhook(raw_body, x_retell_signature, RETELL_API_KEY))
        bearer_ok = _verify_api_key(authorization)
        if not (retell_ok or bearer_ok):
            log.warning("trigger_call_auth_failed",
                        has_retell_sig=bool(x_retell_signature),
                        has_bearer=bool(authorization))
            raise HTTPException(status_code=401, detail="Invalid webhook signature or API key")
    # else: dev mode — no auth check

    payload = json.loads(raw_body)
    # Retell custom-function payloads carry the LLM-extracted args under "args"
    # (older shape: "arguments"); fall back to the whole body for direct calls.
    args = payload.get("args") or payload.get("arguments") or payload

    # Defensive validation: the LLM sometimes hallucinates this call with empty
    # args (e.g. when the user is just asking a question). Return a 400 with a
    # clear message so the agent retries with the right data instead of looping.
    phone = args.get("phone_number") or args.get("candidate_phone") or args.get("phone") or args.get("to_number")
    name = args.get("candidate_name") or args.get("name")
    if not phone or not str(phone).strip():
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "phone_number is required and cannot be empty. Look the candidate up in the sheet first, then retry with the real phone number in E.164 format (e.g. +18138223579)."},
        )
    if not name or not str(name).strip():
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "candidate_name is required. Look the candidate up in the sheet first, then retry with their real name."},
        )

    result = agent.trigger_followup_call(args)

    if result.get("error"):
        return JSONResponse(status_code=422, content={"ok": False, **result})
    return {"ok": True, "call_id": result.get("call_id"), "call_status": result.get("status")}


# ── Send SMS (direct API) ─────────────────────────────────────────────────────

class SendSMSRequest(BaseModel):
    """Accepts the canonical `to` field plus every alias the LLM has ever
    hallucinated. The first non-empty one wins. Same defensive pattern as
    /api/trigger-call.
    """
    to: str | None = None
    phone: str | None = None
    phone_number: str | None = None
    candidate_phone: str | None = None
    to_number: str | None = None
    recipient: str | None = None
    number: str | None = None
    mobile: str | None = None
    phoneNumber: str | None = None
    toPhone: str | None = None
    message: str
    from_number_id: str | None = None
    candidate_name: str | None = None
    track_state: bool | None = True

    def resolved_to(self) -> str:
        for v in (self.to, self.phone, self.phone_number, self.candidate_phone,
                  self.to_number, self.recipient, self.number, self.mobile,
                  self.phoneNumber, self.toPhone):
            if v and str(v).strip():
                return str(v).strip()
        return ""


@app.post("/api/send")
async def send_sms(req: SendSMSRequest, authorization: str = Header(None)):
    if not _verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")

    to = req.resolved_to()
    if not to:
        raise HTTPException(status_code=400, detail="'to' (or phone/phone_number/candidate_phone/recipient/number/mobile) is required and must be a non-empty E.164 phone number.")
    if not req.message or not str(req.message).strip():
        raise HTTPException(status_code=400, detail="'message' is required and must be non-empty.")

    # If the LLM stuffed extra text into the phone field (e.g. "Candidate 1 Test at +18138223579"),
    # pull the first E.164-looking substring out. Better than 500-ing.
    # IMPORTANT: keep ALL the digits, don't truncate. E.164 is variable-length
    # (10-15 digits) — using `[-10:]` would strip the country code from US (+1…)
    # numbers. Just preserve whatever digit count the input has.
    import re
    m = re.search(r"\+?\d[\d\s\-\(\)]{7,}\d", to)
    if m:
        extracted = m.group(0)
        digits = re.sub(r"\D", "", extracted)
        # Rebuild: + prefix if the original had it, plus the digits
        to = ("+" if extracted.startswith("+") else "") + digits

    number_id = req.from_number_id or os.environ.get("QUO_BK_NUMBER_ID") or agent.quo.get_default_number_id()
    if not number_id:
        raise HTTPException(status_code=400, detail="No phone number ID. Set QUO_BK_NUMBER_ID env var.")

    result = agent.quo.send_sms(number_id, to, req.message)
    return {"ok": True, "message_id": result.get("id"), "to": to}


# ── Telegram Relay ────────────────────────────────────────────────────────────

class RelayRequest(BaseModel):
    to: str           # Candidate phone number
    message: str      # What BK typed in Telegram
    from_number_id: str | None = None


@app.post("/api/relay")
async def relay_message(req: RelayRequest, authorization: str = Header(None)):
    if not _verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")

    result = agent.relay_from_bk(req.to, req.message, req.from_number_id)
    return {"ok": True, **result}


# ── Bulk Outreach ─────────────────────────────────────────────────────────────

class Candidate(BaseModel):
    name: str
    phone: str
    role: str
    location: str = ""

class BulkRequest(BaseModel):
    candidates: list[Candidate]
    template: str = "mercury_initial"
    extra_vars: dict = {}


@app.post("/api/bulk")
async def bulk_outreach(req: BulkRequest, authorization: str = Header(None)):
    if not _verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")

    candidates_data = [c.dict() for c in req.candidates]
    results = agent.send_bulk_outreach(candidates_data, req.template, **req.extra_vars)
    return {"ok": True, "results": results, "total": len(results)}


class JobCandidate(BaseModel):
    name: str
    phone: str
    location: str | None = None


class BulkJobRequest(BaseModel):
    job_id: str
    candidates: list[JobCandidate]
    skip_if_contacted: bool = True
    dry_run: bool = False
    max_per_run: int | None = None


@app.post("/api/bulk_job")
async def bulk_outreach_for_job(req: BulkJobRequest, authorization: str = Header(None)):
    """
    Generalized bulk outreach — works for ANY job posting in flows/jobs/*.yaml,
    not just the hardcoded Mercury Z templates. This is the endpoint an NLP
    command like "text everyone for the Fiber I&R role" should hit.

    Set dry_run=true first to get a preview of rendered messages + dedup
    results for BK/Mel to approve before anything actually sends — matches
    the team's "controlled pilot" rollout plan (prove it on a small batch
    before trusting it at 1000+/session).
    """
    if not _verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")

    candidates_data = [c.dict() for c in req.candidates]
    results = agent.send_bulk_outreach_for_job(
        candidates_data, req.job_id,
        skip_if_contacted=req.skip_if_contacted,
        dry_run=req.dry_run,
        max_per_run=req.max_per_run,
    )
    return {"ok": True, "results": results, "total": len(results)}


@app.get("/api/jobs")
async def get_jobs(authorization: str = Header(None)):
    if not _verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return {"jobs": list_jobs()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, authorization: str = Header(None)):
    if not _verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    try:
        job = load_job(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No job posting {job_id!r}")
    return {"job": job.__dict__}


# ── Candidate State ───────────────────────────────────────────────────────────

@app.get("/api/candidates")
async def list_candidates(authorization: str = Header(None), include_sheet: bool = True):
    """List tracked candidates.

    By default merges in-memory state (updated by inbound webhooks) with the
    Google Sheet (source of truth for the master list). After a fresh restart
    the in-memory dict is empty, so without the sheet read the endpoint would
    lie and return [] — that's the bug that made the chat agent hallucinate
    "John Doe / Jane Smith" placeholder data on 2026-07-31.
    """
    if not _verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")

    # 1. Always start with in-memory (richer: has state, screening_result, etc.)
    by_phone: dict[str, dict] = {}
    for c in agent.candidate_states.values():
        phone = str(c.get("phone", "")).strip()
        if phone:
            by_phone[phone] = dict(c)

    sheet_added = 0
    if include_sheet and agent.sheets and agent.sheets.configured:
        try:
            sheet = agent.sheets.list_all_candidates(limit=200)
            for c in sheet.get("candidates", []):
                phone = str(c.get("phone", "")).strip()
                if not phone:
                    continue
                if phone in by_phone:
                    # Backfill any missing fields from the sheet (name, role, etc.)
                    for k, v in c.items():
                        if not by_phone[phone].get(k) and v:
                            by_phone[phone][k] = v
                else:
                    by_phone[phone] = {
                        "phone": phone,
                        "name": c.get("name", ""),
                        "first_name": c.get("first_name", ""),
                        "last_name": c.get("last_name", ""),
                        "role": c.get("role", ""),
                        "location": c.get("location", ""),
                        "status": c.get("status", ""),
                        "email": c.get("email", ""),
                        "row": c.get("row"),
                        "source": "sheet",
                        "state": "new",
                    }
                    sheet_added += 1
        except Exception as e:
            log.warning("candidates_sheet_read_failed", error=str(e))

    out = list(by_phone.values())
    return {
        "candidates": out,
        "count": len(out),
        "in_memory": len(agent.candidate_states),
        "from_sheet": sheet_added,
    }


# ── OpenClaw / Hermes Unified Tool Endpoint ───────────────────────────────────

class ToolRequest(BaseModel):
    tool: str          # "send_sms" | "bulk_outreach" | "get_candidates" | "relay"
    params: dict = {}


@app.post("/api/tool")
async def hermes_tool(req: ToolRequest, authorization: str = Header(None)):
    """
    Unified tool endpoint for OpenClaw/Hermes integration.
    Called by FORGE, ATLAS, or MAVEN when they need to send/check SMS.
    """
    if not _verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")

    tool = req.tool
    params = req.params

    if tool == "send_sms":
        number_id = params.get("from_number_id") or os.environ.get("QUO_BK_NUMBER_ID")
        result = agent.quo.send_sms(number_id, params["to"], params["message"])
        return {"ok": True, "result": result}

    elif tool == "bulk_outreach":
        results = agent.send_bulk_outreach(
            params.get("candidates", []),
            params.get("template", "mercury_initial"),
        )
        return {"ok": True, "results": results}

    elif tool == "get_candidates":
        # Mirror the /api/candidates logic (in-memory + sheet) — the MCP
        # tool goes through /api/tool, not /api/candidates, so duplicate the
        # merge here. Without this, fresh-restart MCP sessions see an empty
        # list and the chat agent hallucinates placeholder names.
        by_phone: dict[str, dict] = {}
        for c in agent.candidate_states.values():
            phone = str(c.get("phone", "")).strip()
            if phone:
                by_phone[phone] = dict(c)
        sheet_added = 0
        if agent.sheets and agent.sheets.configured:
            try:
                sheet = agent.sheets.list_all_candidates(limit=200)
                for c in sheet.get("candidates", []):
                    phone = str(c.get("phone", "")).strip()
                    if not phone or phone in by_phone:
                        continue
                    by_phone[phone] = {
                        "phone": phone,
                        "name": c.get("name", ""),
                        "first_name": c.get("first_name", ""),
                        "last_name": c.get("last_name", ""),
                        "role": c.get("role", ""),
                        "location": c.get("location", ""),
                        "status": c.get("status", ""),
                        "email": c.get("email", ""),
                        "row": c.get("row"),
                        "source": "sheet",
                        "state": "new",
                    }
                    sheet_added += 1
            except Exception as e:
                log.warning("candidates_sheet_read_failed", error=str(e))
        return {"candidates": list(by_phone.values()),
                "count": len(by_phone),
                "in_memory": len(agent.candidate_states),
                "from_sheet": sheet_added}

    elif tool == "relay":
        result = agent.relay_from_bk(params["to"], params["message"])
        return {"ok": True, "result": result}

    elif tool == "list_numbers":
        numbers = agent.quo.list_numbers()
        return {"numbers": numbers}

    elif tool == "list_conversations":
        number_id = params.get("phone_number_id") or os.environ.get("QUO_BK_NUMBER_ID")
        convos = agent.quo.list_conversations(number_id)
        return {"conversations": convos}

    elif tool == "sync_sms_threads_to_candidates":
        """
        SMS thread → candidate sync. See sms_sync.py for details.

        Returns {ok, window_days, total_threads, in_window, created_count,
        updated_count, skipped_count, created, updated, skipped_sample}.
        """
        from src.sms_sync import sync_sms_threads
        return sync_sms_threads(
            agent,
            window_days=params.get("window_days", 7),
            phone_number_id=params.get("phone_number_id", ""),
            primary_number=os.environ.get("QUO_BK_NUMBER", ""),
        )

    elif tool == "create_contact":
        contact = agent.quo.create_contact(params["name"], params["phone"], params.get("tags", []))
        return {"ok": True, "contact": contact}

    elif tool == "bulk_outreach_for_job":
        results = agent.send_bulk_outreach_for_job(
            params.get("candidates", []),
            params["job_id"],
            skip_if_contacted=params.get("skip_if_contacted", True),
            dry_run=params.get("dry_run", False),
            max_per_run=params.get("max_per_run"),
        )
        return {"ok": True, "results": results}

    elif tool == "trigger_screening_call":
        candidate = agent.candidate_states.get(params["phone"])
        if not candidate:
            raise HTTPException(status_code=404, detail=f"No tracked candidate for {params['phone']}")
        # MCP callers can pass `context` per-call; merge it onto the candidate
        # before triggering the screening call so it ends up in the Retell
        # dynamic_variables payload (and surfaces as `{{context}}` to the LLM).
        if params.get("context"):
            candidate["context"] = params["context"]
        job = load_job(candidate.get("job_id") or params.get("job_id"))
        result = agent.trigger_screening_call(candidate, job)
        return {"ok": True, "result": result}

    elif tool == "trigger_batch_screening":
        job = load_job(params["job_id"])
        result = agent.trigger_batch_screening_calls(
            params.get("candidates", []),
            job,
            name=params.get("name"),
            call_time_window=params.get("call_time_window"),
        )
        return {"ok": True, "result": result}

    elif tool == "list_jobs":
        return {"jobs": list_jobs()}

    elif tool == "get_job":
        job = load_job(params["job_id"])
        return {"job": job.__dict__}

    elif tool == "update_candidate":
        phone = params["phone"]
        cand = agent.candidate_states.get(phone, {"phone": phone})
        cand.update(params.get("fields", {}))
        agent.candidate_states[phone] = cand
        agent._sync_to_sheets(cand)
        return {"ok": True, "candidate": cand}

    elif tool == "notify_bk":
        agent._notify(params["message"])
        return {"ok": True, "notified": True}

    # ── New tools for the proactive agentic flow + AI agent integration ─────

    elif tool == "send_sms_v2":
        # The chat-agent-facing send_sms (uses Quo/OpenPhone; honors from_number_id)
        to = params.get("to")
        message = params.get("message")
        if not to or not str(to).strip():
            raise HTTPException(status_code=400, detail="'to' is required and must be a non-empty E.164 phone number.")
        if not message or not str(message).strip():
            raise HTTPException(status_code=400, detail="'message' is required and must be non-empty.")
        number_id = (
            params.get("from_number_id")
            or os.environ.get("QUO_BK_NUMBER_ID")
            or agent.quo.get_default_number_id()
        )
        if not number_id:
            raise HTTPException(status_code=400, detail="No phone number ID. Set QUO_BK_NUMBER_ID env var.")
        result = agent.quo.send_sms(number_id, to, message)
        return {"ok": True, "message_id": result.get("id"), "to": to}

    elif tool == "list_pending_screenings":
        """Candidates in screening_call_queued / call_in_progress state."""
        pending = [
            {"phone": c.get("phone"), "name": c.get("name"), "state": c.get("state"),
             "job_id": c.get("job_id"), "last_call_id": c.get("last_call_id"),
             "queued_at": c.get("sms2_queued_at") or c.get("last_call_at")}
            for c in agent.candidate_states.values()
            if c.get("state") in ("screening_call_queued", "call_in_progress", "sms2_queued")
        ]
        return {"pending": pending, "count": len(pending)}

    elif tool == "list_recent_screenings":
        """Last N screening results (screening_passed / failed / needs_follow_up)."""
        limit = int(params.get("limit", 20))
        all_screens = [
            c for c in agent.candidate_states.values()
            if c.get("state") in ("screening_passed", "screening_failed", "screening_needs_follow_up", "screening_no_answer", "call_transferred")
        ]
        all_screens.sort(key=lambda c: c.get("last_call_at") or "", reverse=True)
        return {"screenings": all_screens[:limit], "count": len(all_screens[:limit])}

    elif tool == "pause_candidate":
        """BK's "stop contacting this person" override — flips state to 'paused'."""
        phone = params["phone"]
        cand = agent.candidate_states.get(phone, {"phone": phone})
        cand["state"] = "paused"
        cand["paused_at"] = str(__import__("datetime").datetime.utcnow())
        cand["paused_reason"] = params.get("reason", "manual pause")
        agent.candidate_states[phone] = cand
        agent._sync_to_sheets(cand)
        return {"ok": True, "candidate": cand}

    elif tool == "agent_chat":
        """
        Talk to the Retell BK JR chat agent. The agent has tools for the sheet
        (Composio), send_sms, and trigger_followup_call. The agent_id is read
        from the env (or passed). Useful for AI agents that want to drive the
        recruiting conversation programmatically.
        """
        from .retell_client import RetellClient
        retell = RetellClient()
        agent_id = params.get("agent_id") or os.environ.get("RETELL_CHAT_AGENT_ID", "agent_29eea2101e81cd761b7928dcd7")
        message = params.get("message")
        if not message:
            raise HTTPException(status_code=400, detail="'message' is required.")
        chat = retell.create_chat(agent_id, message=message)
        return {"ok": True, "chat": chat}

    # ── Google services via Composio (Ed's "Drive / Calendar / Gmail" ask) ──
    # All three use the same backend.composio.dev tool-execute endpoint as
    # the Sheets integration. Auth comes from COMPOSIO_API_KEY +
    # COMPOSIO_CONNECTED_ACCOUNT_ID (already in .env). If the connected
    # account hasn't authorized the additional Google services yet, the call
    # returns 400/401 from Composio — the user must reconnect that scope at
    # app.composio.dev.
    elif tool in ("gmail_send", "gcal_list_events", "gcal_create_event",
                  "gdrive_create_folder", "gdrive_share", "retell_list_agents",
                  "retell_list_bkjr_agents", "retell_create_agent",
                  "retell_get_agent", "retell_place_call", "process_screening_result"):
        from .composio_google import ComposioGoogleClient
        from .retell_client import RetellClient
        g = ComposioGoogleClient()

        if tool == "process_screening_result":
            # Post-screening automation. Branches on screening_result:
            #   - "passed":           update state -> screening_passed, send
            #                         SMS, notify BK, create GCal event stub,
            #                         create Drive folder, send Gmail packet.
            #   - "needs_follow_up":  update state -> screening_needs_follow_up,
            #                         personalized SMS, pause 48h, notify BK.
            #   - "failed":           update state -> screening_failed,
            #                         pause permanently.
            # Idempotent: same call_id -> same response, no duplicate side
            # effects. Caller may bypass the call lookup by passing
            # screening_result + custom_analysis_fields directly.
            from .screening_automation import process_screening_result as _psr
            return _psr(agent, params, g)

        if tool == "gmail_send":
            to = params.get("to")
            subject = params.get("subject", "")
            body = params.get("body", "")
            if not to or not body:
                raise HTTPException(status_code=400, detail="'to' and 'body' are required.")
            return g.gmail_send(to=to, subject=subject, body=body)

        elif tool == "gcal_list_events":
            return g.gcal_list_events(
                time_min=params.get("time_min"),
                time_max=params.get("time_max"),
                max_results=int(params.get("max_results", 10)),
            )

        elif tool == "gcal_create_event":
            summary = params.get("summary")
            start = params.get("start")
            end = params.get("end")
            attendees = params.get("attendees", [])
            if not summary or not start or not end:
                raise HTTPException(status_code=400, detail="'summary', 'start', 'end' are required (ISO 8601).")
            return g.gcal_create_event(summary=summary, start=start, end=end,
                                       attendees=attendees, description=params.get("description", ""))

        elif tool == "gdrive_create_folder":
            name = params.get("name")
            parent_id = params.get("parent_id")
            if not name:
                raise HTTPException(status_code=400, detail="'name' is required.")
            return g.gdrive_create_folder(name=name, parent_id=parent_id)

        elif tool == "gdrive_share":
            file_id = params.get("file_id")
            email = params.get("email")
            role = params.get("role", "reader")
            if not file_id or not email:
                raise HTTPException(status_code=400, detail="'file_id' and 'email' are required.")
            return g.gdrive_share(file_id=file_id, email=email, role=role)

        elif tool == "retell_list_bkjr_agents":
            retell = RetellClient()
            agents = retell.list_agents(include_other_clients=False)
            return {"agents": agents, "count": len(agents), "filter": "bkjr_only"}

        elif tool == "retell_list_agents":
            retell = RetellClient()
            include_other_clients = bool(params.get("include_other_clients", False))
            agents = retell.list_agents(include_other_clients=include_other_clients)
            payload = {"agents": agents, "count": len(agents)}
            if include_other_clients:
                payload["filter"] = "all_with_warn"
                payload["other_clients_count"] = sum(1 for a in agents if not a.get("is_bkjr"))
            else:
                payload["filter"] = "bkjr_only"
            return payload

        elif tool == "retell_get_agent":
            agent_id = params.get("agent_id")
            if not agent_id:
                raise HTTPException(status_code=400, detail="'agent_id' is required.")
            return RetellClient().get_agent(agent_id)

        elif tool == "retell_create_agent":
            """Spin up a new Retell voice agent on demand.
            Ed's vision: "build me an agent that interviews for X and asks about Y."
            """
            retell = RetellClient()
            name = params.get("name")
            system_prompt = params.get("system_prompt")
            voice_id = params.get("voice_id") or os.environ.get("RETELL_DEFAULT_VOICE_ID", "")
            llm_id = params.get("llm_id", "gpt-4.1")
            from_number = params.get("from_number") or os.environ.get("RETELL_SCREENING_FROM_NUMBER", "")
            if not name or not system_prompt:
                raise HTTPException(status_code=400, detail="'name' and 'system_prompt' are required.")
            return retell.create_agent(
                name=name, system_prompt=system_prompt, voice_id=voice_id,
                llm_id=llm_id, from_number=from_number,
                dynamic_variables=params.get("dynamic_variables"),
            )

        elif tool == "retell_place_call":
            """Place a call to ANY Retell agent (not just the default screener).
            This is the "spin up any agent and call" capability Ed asked for.
            """
            retell = RetellClient()
            to_number = params.get("to_number") or params.get("to")
            agent_id = params.get("agent_id") or os.environ.get("RETELL_SCREENING_AGENT_ID")
            from_number = params.get("from_number") or os.environ.get("RETELL_SCREENING_FROM_NUMBER", "")
            if not to_number or not agent_id:
                raise HTTPException(status_code=400, detail="'to_number' and 'agent_id' are required.")
            return retell.create_phone_call(
                from_number=from_number, to_number=to_number, agent_id=agent_id,
                dynamic_variables=params.get("dynamic_variables", {}),
            )

        # ── Retell call/llm/phone-number/concurrency/live-call tools ────────
        # These mirror the MCP tools in src/mcp_server.py so they're reachable
        # via BOTH the /mcp transport AND the POST /api/tool shortcut. The
        # handlers below either delegate to RetellClient or return a clear
        # "not yet wired" error so the dispatcher stops returning 400
        # "Unknown tool" for tools that are clearly defined on the MCP side.
        elif tool == "retell_get_call":
            call_id = params.get("call_id")
            if not call_id:
                raise HTTPException(status_code=400, detail="'call_id' is required.")
            retell = RetellClient()
            return retell.get_call(call_id)

        elif tool == "retell_list_calls":
            retell = RetellClient()
            return {"calls": retell.list_calls(
                filters=params.get("filters") or {},
                limit=int(params.get("limit", 20)),
            )}

        elif tool == "retell_get_llm":
            llm_id = params.get("llm_id")
            if not llm_id:
                raise HTTPException(status_code=400, detail="'llm_id' is required.")
            retell = RetellClient()
            return retell.get_llm(llm_id)

        elif tool == "retell_update_llm":
            llm_id = params.get("llm_id")
            patch = params.get("patch") or {}
            if not llm_id or not patch:
                raise HTTPException(status_code=400, detail="'llm_id' and 'patch' are required.")
            retell = RetellClient()
            return retell.update_llm(llm_id, patch)

        elif tool == "retell_list_phone_numbers":
            retell = RetellClient()
            return {"numbers": retell.list_phone_numbers()}

        elif tool == "retell_update_phone_number":
            number = params.get("number")
            patch = params.get("patch") or {}
            if not number or not patch:
                raise HTTPException(status_code=400, detail="'number' and 'patch' are required.")
            retell = RetellClient()
            return retell.update_phone_number(number, patch)

        elif tool == "retell_get_concurrency":
            retell = RetellClient()
            return retell.get_concurrency()

        elif tool == "retell_update_live_call":
            call_id = params.get("call_id")
            body = params.get("body") or {}
            if not call_id or not body:
                raise HTTPException(status_code=400, detail="'call_id' and 'body' are required.")
            retell = RetellClient()
            return retell.update_live_call(call_id, body)

        # ── Safety / practice-mode / preflight tools ───────────────────────
        elif tool == "preflight":
            if hasattr(agent, "preflight"):
                return agent.preflight()
            return {"ok": False, "error": "preflight() not implemented on agent",
                    "hint": "see mcp_server.py preflight docstring for the checklist"}

        elif tool == "get_practice_mode":
            return {"practice_mode": bool(getattr(agent, "practice_mode", True))}

        elif tool == "set_practice_mode":
            on = bool(params.get("on", True))
            if hasattr(agent, "set_practice_mode"):
                agent.set_practice_mode(on)
            elif hasattr(agent, "practice_mode"):
                agent.practice_mode = on
            else:
                # No state to mutate — return current value
                return {"ok": False, "error": "set_practice_mode not implemented",
                        "practice_mode": bool(getattr(agent, "practice_mode", True))}
            return {"ok": True, "practice_mode": on}

        elif tool == "list_opt_outs":
            # Opt-outs live in state — surface them as a list
            try:
                if hasattr(agent, "list_opt_outs"):
                    return {"opt_outs": agent.list_opt_outs()}
                if hasattr(agent, "opt_outs"):
                    return {"opt_outs": list(agent.opt_outs)}
            except Exception as e:  # noqa: BLE001
                log.warning("list_opt_outs_failed", error=str(e))
            return {"opt_outs": []}

        # ── Core recruiting (alias `list_candidates` for the MCP tool) ─────
        elif tool == "list_candidates":
            return agent.candidate_states

        elif tool == "get_candidate_by_phone":
            phone = params.get("phone", "")
            phone_digits = "".join(c for c in phone if c.isdigit())[-10:]
            for c in agent.candidate_states.values():
                cand_digits = "".join(c2 for c2 in str(c.get("phone", "")) if c2.isdigit())[-10:]
                if cand_digits == phone_digits:
                    return {"found": True, "candidate": c}
            return {"found": False, "phone": phone}

        # ── Phone-number alias (MCP uses `list_phone_numbers`, dispatcher
        #    already had `list_numbers`; support both) ─────────────────────
        elif tool == "list_phone_numbers":
            return {"numbers": agent.quo.list_numbers()}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown tool: {tool}")
