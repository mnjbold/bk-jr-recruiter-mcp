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

# ── Agent allowlist + denylist ──────────────────────────────────────────────
# This Retell workspace is SHARED across clients (Bold Connect / Pearl / NNC /
# Mercury Z / IT Support / BK JR / National Neuropathy etc.). Mutating any
# non-BK-JR agent would take down a live production system belonging to another
# client. Hardcoded on purpose — env vars can be misconfigured; missing config
# must not silently mean "no protection".
#
# BK JR agents: identified by EITHER agent_id OR name prefix. The name-prefix
# filter catches future BK JR agents added via the dashboard before their IDs
# land in this allowlist — defense in depth.
BKJR_AGENT_IDS: frozenset[str] = frozenset({
    "agent_c530f8a10e8a502798c49a30c6",   # BK JR Call Assistant (screener)
    "agent_6e853673d0d33f480c2164415e",   # BK JR Connect-Now (live connect)
    "agent_c0d49f55c814a63197ce06b3d8",   # BK JR Outreach (Interest-Only)
})

# BK JR agents are also grouped in Retell under the "BK JR" folder. The MCP
# checks name prefix on every agent we touch — case-insensitive — so a new
# BK JR agent appears automatically without a code deploy.
BKJR_NAME_PREFIXES: tuple[str, ...] = (
    "bk jr",
    "outbound screening agent",
)

# Other clients' production agents — REFUSE to read or mutate any of these.
# Even reads are blocked: the project rule is "never touch these agents", and
# a read is the first step of an edit-by-hand mistake.
PROTECTED_AGENT_IDS: frozenset[str] = frozenset({
    "agent_9a2cd6a1d680fd1fca6aeddad2",   # Pearl Health
    "agent_d2f34508b3c9677064ed705e1b",   # NNC Patient Services
    "agent_29f7eca77618d86036402b0035",   # National Neuropathy (after-hours)
    "agent_36f403199385903ade511361b1",   # Bold Connect IT Support
    "agent_cd73312c434362ae1d9d074299",   # Bold Connect IT Support (Tagalog)
    "agent_3223fb0f1d1d0fca2deabc9189",   # Bold Connect-Mercury Z Live
                                           # (same end client as BK JR but a
                                           # DIFFERENT agent — do NOT touch)
    "agent_da562b934e0241be688e7b2c45",   # Bold Connect -Bold Business Live
    "agent_afbb14aef83e3312dee84a01be",   # Ed Voice / ED JR (BK-adjacent scratch)
    "agent_cb0e6af95c7a7e575cb3498e32",   # Outreach Dialer (template)
    "agent_95fb2b3bbb3c5c281553bdcecb",   # Event/Webinar Reminder (template)
})


class ProtectedAgentError(RuntimeError):
    """Raised when an operation targets another client's production agent."""

    def __init__(self, agent_id: str | None = None, name: str | None = None):
        self.agent_id = agent_id
        self.name = name
        details = []
        if agent_id:
            details.append(f"agent_id={agent_id!r}")
        if name:
            details.append(f"name={name!r}")
        super().__init__(
            "refusing to touch another client's production agent "
            f"({', '.join(details) or 'unknown'}). "
            f"Only BK JR agents are allowed "
            f"(ids: {sorted(BKJR_AGENT_IDS)}, "
            f"name prefixes: {BKJR_NAME_PREFIXES})."
        )


def _is_bkjr_name(name: str | None) -> bool:
    """True if `name` matches a BK JR naming pattern (case-insensitive)."""
    if not name:
        return False
    lower = name.strip().lower()
    return any(lower.startswith(p) for p in BKJR_NAME_PREFIXES)


def _is_bkjr_agent(agent_id: str | None, name: str | None = None) -> bool:
    """True if the agent is BK JR-owned, by id OR name prefix."""
    return bool(
        (agent_id and agent_id in BKJR_AGENT_IDS)
        or (name and _is_bkjr_name(name))
    )


def assert_agent_allowed(
    agent_id: str | None,
    name: str | None = None,
) -> None:
    """
    Refuse any denylisted agent. Called by every agent-targeting operation.

    Raises ProtectedAgentError if the agent is on PROTECTED_AGENT_IDS OR if
    `agent_id` is set but is neither BK JR-owned nor explicitly denylisted
    (the latter is a defensive refusal for unknown agents — we err on the side
    of not touching things we're not sure belong to BK JR).
    """
    # 1. Hard denylist — ALWAYS refuse these.
    if agent_id and agent_id in PROTECTED_AGENT_IDS:
        log.error("protected_agent_refused", agent_id=agent_id)
        raise ProtectedAgentError(agent_id=agent_id)

    # 2. If agent_id is provided but is NOT BK JR-owned, refuse.
    # This is the "defense in depth" rule: we never read or mutate an agent
    # we can't confirm belongs to BK JR. Wildcard agents (no id, just name)
    # fall through to the name-prefix check.
    if agent_id and not _is_bkjr_agent(agent_id, name):
        log.error("unknown_agent_refused", agent_id=agent_id, name=name)
        raise ProtectedAgentError(agent_id=agent_id, name=name)


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

    def list_agents(
        self,
        include_other_clients: bool = False,
        bkjr_only: bool | None = None,
    ) -> list[dict]:
        """
        List BK JR-owned Retell voice agents on the workspace (default).

        By default ONLY returns agents that are in BKJR_AGENT_IDS OR whose name
        matches BKJR_NAME_PREFIXES. Other clients' agents are filtered out at
        this layer — we don't even know they exist from the MCP's perspective.

        Args:
            include_other_clients: True to return ALL agents (use only for
                diagnostic/audit purposes — the MCP server never sets this).
                A WARN is logged every time it's used.
            bkjr_only: explicit override. None (default) returns only BK JR
                agents. Pass False to get all agents; True is identical to
                the default.

        Returns:
            List of agent dicts: {agent_id, name, voice_id, llm_id,
            last_modified, is_bkjr}. The is_bkjr flag is True when the row
            passed BK JR ownership check.
        """
        # NOTE: Retell's list endpoint is /list-agents (no /v2/ prefix).
        # /v2/agents, /v2/agent, and /v2/list-agents ALL 404. Verified live
        # 2026-08-06 — only /list-agents returns 200.
        resp = httpx.get(f"{RETELL_API_BASE}/list-agents", headers=self.headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        raw_agents = data if isinstance(data, list) else data.get("agents", [])

        rows: list[dict] = []
        for a in raw_agents:
            agent_id = a.get("agent_id")
            name = a.get("agent_name") or a.get("name")
            is_bkjr = _is_bkjr_agent(agent_id, name)
            rows.append({
                "agent_id": agent_id,
                "name": name,
                "voice_id": a.get("voice_id"),
                "llm_id": (a.get("response_engine") or {}).get("llm_id")
                          if isinstance(a.get("response_engine"), dict) else None,
                "last_modified": a.get("last_modified") or a.get("updated_at"),
                "is_bkjr": is_bkjr,
            })

        # Default: BK JR only. Opt-out requires explicit flag.
        if bkjr_only is False:
            include_other_clients = True
        if not include_other_clients:
            rows = [r for r in rows if r["is_bkjr"]]
        else:
            log.warning(
                "retell_list_agents_include_other_clients",
                count=sum(1 for r in rows if not r["is_bkjr"]),
                agent_ids=[r["agent_id"] for r in rows if not r["is_bkjr"]],
            )

        return rows

    def list_bkjr_agents(self) -> list[dict]:
        """
        Explicit BK-JR-only listing. Functionally identical to
        list_agents() with default args, but with a clearer intent at call
        sites. Use this from MCP tools that ONLY need BK JR agents.
        """
        return self.list_agents(include_other_clients=False)

    def get_agent(self, agent_id: str) -> dict:
        """
        Fetch full agent config (prompt, voice, tools, dynamic vars).

        Refuses if `agent_id` is on PROTECTED_AGENT_IDS or is not a known
        BK JR agent. Read access is also gated because the project rule is
        "never touch these agents", and a read is the first step of an
        edit-by-hand mistake.
        """
        assert_agent_allowed(agent_id)
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

        Refuses if `name` matches a BK JR prefix (we don't want to create
        agents that LOOK like BK JR agents but aren't in the allowlist — that
        would let a future caller bypass the gate by referring to the new
        agent by name). BK JR agents must be added to BKJR_AGENT_IDS
        explicitly.

        Ed's ask: "build me an agent that interviews for X and asks about Y".
        BK JR will call this with a role description and the screening
        questions — Retell creates the agent, returns the ID, and BK JR
        can immediately place a call to it.

        `dynamic_variables` is a list of {"name", "type", "default_value"}
        matching the placeholders in `system_prompt` (e.g. candidate_name,
        role, requirements).
        """
        # Refuse to create anything that LOOKS like a BK JR agent. The allowlist
        # is the only source of truth — a name-matching bypass would defeat it.
        if _is_bkjr_name(name):
            raise ProtectedAgentError(name=name)
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
