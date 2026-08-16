"""
SMS thread → candidate sync.

Pulls recent SMS conversations from Quo/OpenPhone and reconciles them
with the in-memory candidate state. For each thread:

  - If the phone number is NOT in the candidate dict, we mark it as
    a new candidate (`state: "sms_engaged"`, `source: "sms_sync"`).
  - If it IS in the candidate dict but the state is stale, we bump it
    to `sms_engaged` and record the last activity timestamp.
  - Stale threads (older than the window) are skipped.

We DO NOT read message bodies — the BK JR MCP exposes only conversation
metadata. A positive "YES" classification needs a follow-up integration
with Quo's message-list endpoint (see KNOWN_GAPS in this repo).

Pure-Python where possible. The Quo call is the only network I/O.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def _normalize_phone(raw: str | None) -> str | None:
    """E.164-normalize a phone number, returning None for empties."""
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if digits:
        return f"+{digits}"
    return None


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse ISO 8601 — tolerate the trailing 'Z' for UTC."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def sync_sms_threads(
    agent,
    window_days: int = 7,
    phone_number_id: str = "",
    primary_number: str = "",
) -> dict:
    """
    Reconcile SMS conversations with the candidate state.

    Args:
        agent: the SMSRecruitmentAgent (or any object exposing
            .quo.list_conversations() and .candidate_states).
        window_days: only consider conversations active within N days
            (default 7). Older threads are ignored.
        phone_number_id: the Quo number ID to pull conversations for.
            Defaults to BK's primary.
        primary_number: BK's own phone (E.164). Threads where the only
            participant is BK's number are skipped — those are outbound-
            only threads where we have no inbound signal.

    Returns:
        {
          "window_days": int,
          "total_threads": int,
          "in_window": int,
          "created": [...],   # new candidates added
          "updated": [...],   # existing candidates touched
          "skipped": [...],   # reasons for skipping (BK's own number, stale, etc.)
        }
    """
    window = max(1, int(window_days))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window)

    # Pull conversations from Quo — paginated so we don't silently cap at 20
    # (the old `list_conversations(limit=20)` behaviour hid 95% of older threads).
    convo_list: list[dict] = []
    try:
        convo_resp = agent.quo.list_conversations(
            phone_number_id or None, limit=100,
        ) if phone_number_id else agent.quo.list_conversations(
            os.environ.get("QUO_BK_NUMBER_ID") or agent.quo.get_default_number_id(),
            limit=100,
        )
    except Exception as e:  # noqa: BLE001
        log.error("sync_sms_quo_list_failed", error=str(e))
        return {
            "ok": False,
            "error": f"Quo list_conversations failed: {e}",
            "window_days": window,
        }

    # New return shape is a dict with `data` (list) + `nextPageToken` (opt).
    # Defensive: callers that still pass the old list-shaped Quo client must
    # not crash, so we accept either.
    if isinstance(convo_resp, dict):
        convo_list = list(convo_resp.get("data") or [])
    else:
        convo_list = list(convo_resp or [])
    if not convo_list and phone_number_id is None:
        # Most likely QUO_BK_NUMBER_ID env not set; fall back to first number.
        try:
            fallback = agent.quo.list_conversations(
                agent.quo.get_default_number_id(), limit=100,
            )
            convo_list = list((fallback or {}).get("data") or []) if isinstance(fallback, dict) else list(fallback or [])
        except Exception:
            pass

    created: list[dict] = []
    updated: list[dict] = []
    skipped: list[dict] = []
    in_window = 0

    for convo in convo_list:
        convo_id = convo.get("id")
        last_iso = convo.get("lastActivityAt")
        last_dt = _parse_iso(last_iso)
        if not last_dt:
            skipped.append({"conversation_id": convo_id, "reason": "no last_activity timestamp"})
            continue
        if last_dt < cutoff:
            skipped.append({"conversation_id": convo_id, "reason": f"stale (last activity {last_iso})"})
            continue
        in_window += 1

        # Identify the OTHER participant (skip BK's own number)
        participants = convo.get("participants") or []
        candidate_phones = [
            p for p in (_normalize_phone(p) for p in participants)
            if p and p != primary_number
        ]
        if not candidate_phones:
            skipped.append({"conversation_id": convo_id, "reason": "no candidate phone (only BK on thread)"})
            continue
        phone = candidate_phones[0]

        # Reconcile with candidate_states
        existing = agent.candidate_states.get(phone)
        if existing is None:
            # NEW candidate — add to state
            new_cand = {
                "phone": phone,
                "name": "",  # we don't know the name from metadata alone
                "state": "sms_engaged",
                "source": "sms_sync",
                "last_sms_activity": last_iso,
                "first_sms_engagement_at": last_iso,
            }
            agent.candidate_states[phone] = new_cand
            # Sync to sheets (best-effort)
            if hasattr(agent, "_sync_to_sheets"):
                try:
                    agent._sync_to_sheets(new_cand)
                except Exception as e:  # noqa: BLE001
                    log.warning("sheets_sync_failed", phone=phone, error=str(e))
            created.append({
                "phone": phone,
                "conversation_id": convo_id,
                "last_activity_at": last_iso,
                "state": "sms_engaged",
            })
        else:
            # EXISTING candidate — bump last_sms_activity; only mark
            # sms_engaged if state was older than outreach
            previous_state = existing.get("state")
            patch: dict[str, Any] = {"last_sms_activity": last_iso}
            if previous_state in (None, "new", "sms_sync"):
                patch["state"] = "sms_engaged"
                patch["source"] = existing.get("source") or "sms_sync"
            existing.update(patch)
            agent.candidate_states[phone] = existing
            if hasattr(agent, "_sync_to_sheets"):
                try:
                    agent._sync_to_sheets(existing)
                except Exception as e:  # noqa: BLE001
                    log.warning("sheets_sync_failed", phone=phone, error=str(e))
            updated.append({
                "phone": phone,
                "conversation_id": convo_id,
                "previous_state": previous_state,
                "new_state": existing.get("state"),
            })

    return {
        "ok": True,
        "window_days": window,
        "total_threads": len(convo_list),
        "in_window": in_window,
        "created_count": len(created),
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "created": created,
        "updated": updated,
        # Skipped truncated to first 20 to avoid huge responses on bad input
        "skipped_sample": skipped[:20],
    }