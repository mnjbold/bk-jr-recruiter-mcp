"""
Retell AI Client — outbound AI voice screening calls.

Used for the "second screening" step: after a candidate confirms interest +
equipment over SMS (Quo), we place an AI voice call from a DEDICATED Retell
number (NOT BK's Quo number — see docs/plans/outbound_telephony_screening_agent_requirements.md
for why the numbers must stay separate) to run a structured screening
conversation and extract structured results via post-call analysis.

API reference (verified against docs.retellai.com, July 2026):
  - POST /v2/create-phone-call   — single outbound call
  - POST /create-batch-call      — bulk outbound campaign
  - GET  /v2/get-call/{id}       — fetch a call + call_analysis
  - Webhook signature: header "x-retell-signature" = "v=<ts>,d=<hex>",
    hex = HMAC-SHA256(raw_body + ts, key=webhook-designated API key)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import httpx
import structlog

log = structlog.get_logger(__name__)

RETELL_API_BASE = "https://api.retellai.com"


class RetellClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("RETELL_API_KEY", "")
        if not self.api_key:
            raise ValueError("RETELL_API_KEY not set. Get it from Retell dashboard → API Keys.")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ── Calls ────────────────────────────────────────────────────────────────

    def create_phone_call(
        self,
        from_number: str,
        to_number: str,
        agent_id: str | None = None,
        dynamic_variables: dict | None = None,
        metadata: dict | None = None,
        agent_override: dict | None = None,
    ) -> dict:
        """
        Place a single outbound screening call.
        from_number: the DEDICATED Retell/imported number for screening calls
                     (RETELL_SCREENING_FROM_NUMBER) — never BK's Quo number.
        dynamic_variables: injected into the agent's prompt, e.g. candidate name,
                     role, location, requirements — this is what makes ONE Retell
                     agent reusable across every job posting instead of one agent
                     per role.
        agent_override: full inline override (voice, model, prompt, etc.) for
                     this call only — this is how you'd apply BK's cloned voice
                     if it's set per-job rather than baked into the shared agent
                     (voice cloning solves "sounds like BK", it does NOT solve
                     "shows BK's number" — those are separate asks, see
                     docs/plans/outbound_telephony_screening_agent_requirements.md § voice cloning).
        """
        payload: dict = {"from_number": from_number, "to_number": to_number}
        if agent_id:
            payload["override_agent_id"] = agent_id
        if dynamic_variables:
            payload["retell_llm_dynamic_variables"] = {k: str(v) for k, v in dynamic_variables.items()}
        if metadata:
            payload["metadata"] = metadata
        if agent_override:
            payload["agent_override"] = agent_override
        resp = httpx.post(f"{RETELL_API_BASE}/v2/create-phone-call", headers=self.headers, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        log.info("retell_call_placed", to=to_number, call_id=data.get("call_id"))
        return data

    def create_batch_call(
        self,
        from_number: str,
        tasks: list[dict],
        name: str | None = None,
        trigger_timestamp: int | None = None,
        call_time_window: dict | None = None,
        reserved_concurrency: int | None = None,
    ) -> dict:
        """
        Bulk screening call campaign.
        tasks: [{"to_number": "+1...", "retell_llm_dynamic_variables": {...}, "metadata": {...}}, ...]
        call_time_window: e.g. {"start": "09:00", "end": "18:00", "timezone": "America/New_York"}
                          — enforce quiet hours so candidates aren't cold-called at night.
        """
        payload: dict = {"from_number": from_number, "tasks": tasks}
        if name:
            payload["name"] = name
        if trigger_timestamp:
            payload["trigger_timestamp"] = trigger_timestamp
        if call_time_window:
            payload["call_time_window"] = call_time_window
        if reserved_concurrency is not None:
            payload["reserved_concurrency"] = reserved_concurrency
        resp = httpx.post(f"{RETELL_API_BASE}/create-batch-call", headers=self.headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        log.info("retell_batch_call_created", batch_call_id=data.get("batch_call_id"), tasks=len(tasks))
        return data

    def get_call(self, call_id: str) -> dict:
        resp = httpx.get(f"{RETELL_API_BASE}/v2/get-call/{call_id}", headers=self.headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Agent lifecycle (Ed's "spin up any agent on demand" ask) ────────────

    def list_agents(self) -> list[dict]:
        """List all Retell voice agents on the workspace.

        Ed's vision: BK Jr. can see the existing agents (screener, follow-up,
        scheduler) and decide which one to dispatch a call to. Or spin up a
        new one for a specific role.
        """
        # NOTE: Retell's list endpoint is /list-agents (no /v2/ prefix).
        # /v2/agents, /v2/agent, and /v2/list-agents ALL 404. Verified live
        # 2026-08-06 — only /list-agents returns 200.
        resp = httpx.get(f"{RETELL_API_BASE}/list-agents", headers=self.headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        agents = data if isinstance(data, list) else data.get("agents", [])
        return [
            {
                "agent_id": a.get("agent_id"),
                "name": a.get("agent_name") or a.get("name"),
                "voice_id": a.get("voice_id"),
                "llm_id": (a.get("response_engine") or {}).get("llm_id") if isinstance(a.get("response_engine"), dict) else None,
                "last_modified": a.get("last_modified") or a.get("updated_at"),
            }
            for a in agents
        ]

    def get_agent(self, agent_id: str) -> dict:
        """Fetch full agent config (prompt, voice, tools, dynamic vars)."""
        # NOTE: like list_agents, the read endpoints live at /get-agent/{id},
        # /list-agents, /create-agent — NOT under /v2/. /v2/agent/{id} 404s.
        # The /v2/ prefix is only for create-phone-call + a few other write ops.
        # Verified live 2026-08-06.
        resp = httpx.get(f"{RETELL_API_BASE}/get-agent/{agent_id}", headers=self.headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def create_agent(
        self,
        name: str,
        system_prompt: str,
        voice_id: str = "",
        llm_id: str = "gpt-4.1",
        from_number: str = "",
        dynamic_variables: list[dict] | None = None,
    ) -> dict:
        """Create a new Retell voice agent on the workspace.

        Ed's ask: "build me an agent that interviews for X and asks about Y".
        BK JR will call this with a role description and the screening
        questions — Retell creates the agent, returns the ID, and BK JR
        can immediately place a call to it.

        `dynamic_variables` is a list of {"name", "type", "default_value"}
        matching the placeholders in `system_prompt` (e.g. candidate_name,
        role, requirements).
        """
        payload: dict = {
            "agent_name": name,
            "response_engine": {
                "type": "retell-llm",
                "llm_id": llm_id,
            },
            "voice_id": voice_id or "11labs-Adrian",
            "system_prompt": system_prompt,
        }
        if dynamic_variables:
            payload["dynamic_variables"] = dynamic_variables
        if from_number:
            payload["inbound_number"] = from_number  # bind to a number if provided
        resp = httpx.post(
            f"{RETELL_API_BASE}/create-agent",
            headers=self.headers,
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        log.info("retell_agent_created", agent_id=data.get("agent_id"), name=name)
        return data

    # ── Webhook verification ─────────────────────────────────────────────────

    @staticmethod
    def verify_webhook(raw_body: bytes, signature_header: str, api_key: str, max_skew_seconds: int = 300) -> bool:
        """
        Verify Retell's x-retell-signature header: "v=<epoch_ms>,d=<hex_hmac>".
        Must use the RAW body bytes (not re-serialized JSON) and the workspace's
        webhook-designated API key (only one key per workspace holds that role).
        """
        try:
            parts = dict(p.split("=", 1) for p in signature_header.split(","))
            ts = int(parts["v"])
            digest = parts["d"]
        except (KeyError, ValueError):
            return False

        now_ms = int(time.time() * 1000)
        if abs(now_ms - ts) > max_skew_seconds * 1000:
            log.warning("retell_webhook_stale", ts=ts, now=now_ms)
            return False

        signed_payload = raw_body + str(ts).encode()
        expected = hmac.new(api_key.encode(), signed_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, digest)
