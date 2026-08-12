"""
Post-screening automation — branches on Retell's call_analyzed result.

This module wires the result of an AI screening call into the downstream
workflow. The webhook at /webhook/retell handles the same logic on
inbound events; this function is also exposed as the
`process_screening_result` MCP tool for re-processing, manual triggers,
and external clients.

Idempotency: each action records an `executed_actions` list on the
candidate dict. Re-running with the same call_id short-circuits the
already-completed actions.

Pure-Python where possible. The HTTP/SDK calls (Quo SMS, GCal, Gmail,
Drive, Retell `get_call`) are isolated in try/except blocks so a single
provider failure doesn't roll back the whole branch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

log = structlog.get_logger(__name__)

VALID_RESULTS = ("passed", "needs_follow_up", "failed")


def _candidate_phone(params: dict, agent) -> str | None:
    """Resolve a candidate phone from params OR from the call's metadata."""
    phone = params.get("phone")
    if phone:
        return phone
    call_id = params.get("call_id")
    if call_id and call_id != params.get("screening_result"):  # heuristic: skip if call_id == result
        try:
            from src.retell_client import RetellClient
            call = RetellClient().get_call(call_id)
            md = call.get("metadata") or {}
            return md.get("candidate_phone") or md.get("phone")
        except Exception as e:  # noqa: BLE001
            log.warning("process_screening_get_call_failed", call_id=call_id, error=str(e))
    return None


def _custom_fields(params: dict, call_id: str | None) -> dict:
    """Resolve the custom_analysis_fields from params OR from the Retell call."""
    if params.get("custom_analysis_fields"):
        return params["custom_analysis_fields"]
    if not call_id:
        return {}
    try:
        from src.retell_client import RetellClient
        call = RetellClient().get_call(call_id)
        return (call.get("call_analysis") or {}).get("custom_analysis_fields") or {}
    except Exception as e:  # noqa: BLE001
        log.warning("process_screening_get_analysis_failed", call_id=call_id, error=str(e))
        return {}


def _send_sms_safe(g, to: str, message: str, from_number_id: str = "") -> tuple[bool, str]:
    """Send an SMS via Quo and return (ok, message_id_or_error). Never raises."""
    if not to or not message:
        return False, "missing to/message"
    try:
        if hasattr(g, "send_sms"):
            res = g.send_sms(to=to, message=message, from_number_id=from_number_id or None)
            if isinstance(res, dict):
                return bool(res.get("ok")), res.get("message_id") or res.get("error", "")
            return True, str(res)
        return False, "send_sms not available on g"
    except Exception as e:  # noqa: BLE001
        return False, f"send_sms raised: {e}"


def _notify_safe(agent, message: str) -> tuple[bool, str]:
    """Notify BK via the configured channel. Never raises."""
    if not message:
        return False, "empty message"
    try:
        if hasattr(agent, "_notify"):
            agent._notify(message)
            return True, "notified"
        return False, "_notify not available on agent"
    except Exception as e:  # noqa: BLE001
        return False, f"_notify raised: {e}"


def _update_candidate_safe(agent, phone: str, fields: dict) -> tuple[bool, dict]:
    """Update candidate state. Never raises."""
    try:
        cand = agent.candidate_states.get(phone, {"phone": phone})
        cand.update(fields)
        agent.candidate_states[phone] = cand
        if hasattr(agent, "_sync_to_sheets"):
            try:
                agent._sync_to_sheets(cand)
            except Exception as e:  # noqa: BLE001
                log.warning("sheets_sync_failed", phone=phone, error=str(e))
        return True, cand
    except Exception as e:  # noqa: BLE001
        return False, {"phone": phone, "error": str(e)}


def _followup_message(name: str, failed_field: str) -> str:
    """Build a personalized follow-up SMS based on which field failed."""
    name = name.strip() or "there"
    if failed_field == "has_iphone_11_plus":
        return (
            f"Hey {name}, thanks for chatting! We need to confirm you have an "
            f"iPhone 11 or newer for the Mercury Z role. Reply YES or NO when "
            f"you can — thanks!"
        )
    if failed_field == "has_vehicle_with_ladder_rack":
        return (
            f"Hey {name}, thanks for chatting! We need to confirm you have a "
            f"truck or work van with a ladder rack + 28ft ladder for the "
            f"Mercury Z role. Reply YES or NO when you can — thanks!"
        )
    if failed_field == "can_commit_schedule":
        return (
            f"Hey {name}, thanks for chatting! Quick follow-up — the role "
            f"needs 6 days/week (Mon-Sat) for 2+ years. Can you confirm you "
            f"can commit to that schedule? Reply YES or NO — thanks!"
        )
    return (
        f"Hey {name}, thanks for chatting! We had a couple of details to "
        f"clarify for the Mercury Z role. Reply when you can and we'll get "
        f"back to you. Thanks!"
    )


def process_screening_result(agent, params: dict, g) -> dict:
    """
    Run post-screening automation. See server.py for the full docstring;
    this function is the implementation.

    Returns a dict that the MCP layer passes back to the caller.
    """
    call_id: str | None = params.get("call_id")
    result: str = (params.get("screening_result") or "").strip()
    if result not in VALID_RESULTS:
        return {"ok": False, "error": f"invalid screening_result: {result!r} (must be one of {VALID_RESULTS})"}

    phone = params.get("phone") or _candidate_phone(params, agent)
    if not phone:
        return {"ok": False, "error": "phone or call_id (with metadata.candidate_phone) is required"}

    cand = agent.candidate_states.get(phone, {"phone": phone})
    name = cand.get("name") or params.get("candidate_name") or "there"
    custom = _custom_fields(params, call_id)

    # Idempotency: record executed actions PER call_id on the candidate.
    # Keys are call_ids (or "manual:<result>:<ts>" for non-call invocations),
    # values are lists of action names already executed under that key.
    # Same call_id → skip; different call_id → run independently.
    call_key: str = call_id or f"manual:{result}:{params.get('processed_at', '')}"
    executed_per_key: dict[str, list[str]] = dict(cand.get("executed_actions_per_key", {}))
    executed: list[str] = list(executed_per_key.get(call_key, []))
    actions_taken: list[str] = []
    actions_skipped: list[str] = []

    def _do(action: str, fn):
        if action in executed:
            actions_skipped.append(action)
            return
        try:
            fn()
            executed.append(action)
            actions_taken.append(action)
        except Exception as e:  # noqa: BLE001
            log.error("process_screening_action_failed", action=action, error=str(e))
            actions_skipped.append(f"{action} (error: {e})")

    # ── Branch 1: passed ─────────────────────────────────────────────────
    if result == "passed":
        # Update state
        def _state_passed():
            _update_candidate_safe(agent, phone, {
                "state": "screening_passed",
                "screening_result": "passed",
                "preferred_location": custom.get("preferred_location"),
            })
        _do("update_candidate:passed", _state_passed)

        # SMS follow-up
        def _sms_passed():
            _send_sms_safe(
                g, phone,
                f"Great news {name}! You've passed the initial screen for "
                f"the Mercury Z Fiber I&R role. {os_environ_default('BOLD_BUSINESS_RECRUITER_NAME', 'the recruiting team')} "
                f"will text you within 24h to lock in an interview. Thanks!"
            )
        _do("send_sms:passed", _sms_passed)

        # Notify BK
        def _notify_passed():
            _notify_safe(
                agent,
                f"✅ Passed screening: {name} ({phone}). "
                f"Pref: {custom.get('preferred_location', 'unknown')}. "
                f"Interview slot next."
            )
        _do("notify_bk:passed", _notify_passed)

        # GCal event stub — caller usually wants to schedule themselves;
        # we create a tentative 30-min slot at +7d at 10am LOCAL. Refine in
        # a follow-up tool call once BK confirms a time.
        def _gcal():
            try:
                if hasattr(g, "gcal_create_event"):
                    start = (datetime.now(timezone.utc) + timedelta(days=7)).replace(
                        hour=10, minute=0, second=0, microsecond=0
                    )
                    end = start + timedelta(minutes=30)
                    g.gcal_create_event(
                        summary=f"Interview: {name} (Fiber I&R)",
                        start=start.isoformat(),
                        end=end.isoformat(),
                        description="Auto-scheduled after screening pass. "
                                    "Confirm time with candidate via SMS.",
                    )
            except Exception as e:
                log.warning("gcal_create_event_failed", error=str(e))
                raise
        _do("gcal_create_event:tentative", _gcal)

        # Drive folder for the candidate
        def _drive():
            try:
                if hasattr(g, "gdrive_create_folder"):
                    g.gdrive_create_folder(
                        name=f"{name} - Fiber I&R - {datetime.now(timezone.utc).date().isoformat()}",
                        parent_id=os_environ_default("BK_DRIVE_PARENT_ID", ""),
                    )
            except Exception as e:
                log.warning("gdrive_create_folder_failed", error=str(e))
                raise
        _do("gdrive_create_folder:candidate", _drive)

        # Gmail packet
        def _gmail():
            if hasattr(g, "gmail_send"):
                g.gmail_send(
                    to=os_environ_default("BOLD_BUSINESS_RECRUITER_EMAIL", "recruiting@boldbusiness.com"),
                    subject=f"New hire packet: {name} (Fiber I&R)",
                    body=(
                        f"{name} ({phone}) passed AI screening for the "
                        f"Mercury Z Fiber I&R role.\n\n"
                        f"Preferred location: {custom.get('preferred_location', 'unknown')}\n"
                        f"Confirm time: see GCal event stub.\n\n"
                        f"Next steps: send onboarding packet, schedule in-person, "
                        f"order equipment."
                    ),
                )
        _do("gmail_send:packet", _gmail)

    # ── Branch 2: needs_follow_up ────────────────────────────────────────
    elif result == "needs_follow_up":
        # Identify which field failed so the SMS can be specific
        failed_field = None
        for field in ("has_iphone_11_plus", "has_vehicle_with_ladder_rack", "can_commit_schedule"):
            if custom.get(field) is False:
                failed_field = field
                break

        def _state_followup():
            _update_candidate_safe(agent, phone, {
                "state": "screening_needs_follow_up",
                "screening_result": "needs_follow_up",
                "failed_field": failed_field,
            })
        _do("update_candidate:needs_follow_up", _state_followup)

        # Personalized follow-up SMS
        def _sms_followup():
            _send_sms_safe(g, phone, _followup_message(name, failed_field or ""))
        _do("send_sms:needs_follow_up", _sms_followup)

        # Pause candidate (auto-resume on next positive reply)
        def _pause():
            if hasattr(agent, "pause_candidate"):
                agent.pause_candidate(phone, reason=f"needs_follow_up:{failed_field or 'unknown'}")
        _do("pause_candidate:48h", _pause)

        # Notify BK with what to clarify
        def _notify_followup():
            _notify_safe(
                agent,
                f"⚠️ Needs follow-up: {name} ({phone}). "
                f"Failed: {failed_field or 'unknown'}. "
                f"SMS sent; awaiting reply."
            )
        _do("notify_bk:needs_follow_up", _notify_followup)

    # ── Branch 3: failed ─────────────────────────────────────────────────
    else:  # failed
        def _state_failed():
            _update_candidate_safe(agent, phone, {
                "state": "screening_failed",
                "screening_result": "failed",
                "failed_field": next(
                    (f for f in ("has_iphone_11_plus", "has_vehicle_with_ladder_rack", "can_commit_schedule")
                     if custom.get(f) is False),
                    "unspecified",
                ),
            })
        _do("update_candidate:failed", _state_failed)

        def _pause_failed():
            if hasattr(agent, "pause_candidate"):
                agent.pause_candidate(phone, reason="screening_failed")
        _do("pause_candidate:permanent", _pause_failed)

    # Persist per-call_id executed_actions onto the candidate
    executed_per_key[call_key] = executed
    cand["executed_actions_per_key"] = executed_per_key
    agent.candidate_states[phone] = cand

    return {
        "ok": True,
        "result": result,
        "phone": phone,
        "candidate_name": name,
        "actions_taken": actions_taken,
        "actions_skipped": actions_skipped,
        "idempotency_key": call_key,
    }


def os_environ_default(key: str, default: str) -> str:
    """Tiny helper to dodge a top-level `import os` for one line."""
    import os
    return os.environ.get(key, default)