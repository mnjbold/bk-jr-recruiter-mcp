"""
BK notification layer — a BACKUP alert channel only.

BK works mainly through the chat widget now, so these are just push alerts for
when a candidate needs human judgment, a screening call finishes, or a bulk run
completes. Two channels, either or both:

  - Google Chat incoming webhook (PRIMARY — zero OAuth, BK creates one webhook
    URL per Space: Space settings → Apps & integrations → Webhooks → paste into
    GCHAT_WEBHOOK_URL).
  - WhatsApp Business Cloud API (SECONDARY — heavier setup: needs
    WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, and WHATSAPP_TO=BK's number).

No Telegram — BK does not use it. Do not reintroduce it.

Resolves GCHAT_WEBHOOK_URL from three sources, in order:
  1. Env var `GCHAT_WEBHOOK_URL` (highest priority — Coolify/Render env)
  2. File at `BKJR_GCHAT_FILE` (default `CREDENTIALS/.env.webhook`), parsed
     as `KEY=VALUE` lines, taking the `BK_JR_WEBHOOK=...` line as the URL.
     Loaded once at import time so we don't re-read the file on every notify.
  3. Empty (no GChat channel) — caller falls back to logs only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)


def _resolve_gchat_url() -> str:
    """Pick the best GChat URL we can find."""
    env_url = os.environ.get("GCHAT_WEBHOOK_URL", "").strip()
    if env_url:
        return env_url

    # Fall back to file-based credential. The point of the `BKJR_GCHAT_FILE`
    # env var is to keep test runs / staging from accidentally loading the
    # production GChat URL. We try multiple locations because the deploy
    # target may vary (Windows local vs Linux container vs Fly/Render):
    #   1. $BKJR_GCHAT_FILE directly (if set)
    #   2. <repo>/CREDENTIALS/.env.webhook (relative, both Windows + POSIX)
    #   3. /workspace/CREDENTIALS/.env.webhook (Coolify docker default)
    #   4. /app/CREDENTIALS/.env.webhook (containerized older style)
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    explicit = os.environ.get("BKJR_GCHAT_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(here.parents[3] / "CREDENTIALS" / ".env.webhook")
    candidates.append(Path("/workspace/CREDENTIALS/.env.webhook"))
    candidates.append(Path("/app/CREDENTIALS/.env.webhook"))
    candidates.append(Path("/repo/CREDENTIALS/.env.webhook"))

    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key in ("BK_JR_WEBHOOK", "GCHAT_WEBHOOK_URL") and val:
                    log.info("gchat_url_loaded_from_file", path=str(candidate))
                    return val
        except Exception as e:  # noqa: BLE001
            log.warning("gchat_file_load_failed", path=str(candidate), error=str(e))
    return ""


GCHAT_WEBHOOK_URL = _resolve_gchat_url()
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_TO = os.environ.get("WHATSAPP_TO", "")


def notify_bk(text: str, chat_id: str | None = None) -> dict:
    """Fan out a notification to every configured channel. Returns per-channel status.

    `chat_id` is accepted for call-site compatibility but ignored (Telegram removed).
    """
    results = {}

    if GCHAT_WEBHOOK_URL:
        results["gchat"] = _send_gchat(text)

    if WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_TO:
        results["whatsapp"] = _send_whatsapp(text)

    if not results:
        log.warning("notify_bk_no_channel_configured", text=text[:80])
        results["none"] = {"sent": False, "reason": "no notification channel configured"}

    return results


def notify_inbound_sms(phone: str, body: str, message_id: str = "") -> dict:
    """
    Specialized formatter for inbound SMS — short, scannable for BK's phone.
    Posts to GChat with a quick 📨 + the first 140 chars of the message.
    """
    snippet = (body or "").replace("\n", " ").strip()[:140]
    text = f"📨 *New SMS* from `{phone}`:\n>{snippet}"
    if message_id:
        text += f"\n[message_id: {message_id}]"
    return notify_bk(text)


def notify_call_event(event_type: str, call_id: str, phone: str = "",
                      summary: str = "") -> dict:
    """GChat push for screening call events (started / ended / analyzed)."""
    icon = {
        "call_started": "📞",
        "call_ended": "📴",
        "call_analyzed": "📊",
    }.get(event_type, "🔔")
    text = f"{icon} *{event_type}* — `{phone or 'unknown'}`\n>{summary[:300] if summary else '(no summary)'}\n[call_id: {call_id}]"
    return notify_bk(text)


def _send_gchat(text: str) -> dict:
    """POST to a Google Chat incoming webhook. No OAuth required."""
    # GChat limits a single text payload to 4096 chars; truncate defensively.
    payload_text = text if len(text) <= 4000 else text[:3997] + "..."
    try:
        resp = httpx.post(
            GCHAT_WEBHOOK_URL,
            json={"text": payload_text},
            timeout=5,
        )
        return {"sent": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as e:
        log.error("gchat_notify_failed", error=str(e))
        return {"sent": False, "error": str(e)}


def _send_whatsapp(text: str) -> dict:
    """Send a text message via the WhatsApp Business Cloud API."""
    try:
        resp = httpx.post(
            f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": WHATSAPP_TO,
                "type": "text",
                "text": {"body": text},
            },
            timeout=5,
        )
        return {"sent": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as e:
        log.error("whatsapp_notify_failed", error=str(e))
        return {"sent": False, "error": str(e)}
