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
"""
from __future__ import annotations

import os

import httpx
import structlog

log = structlog.get_logger(__name__)

GCHAT_WEBHOOK_URL = os.environ.get("GCHAT_WEBHOOK_URL", "")
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


def _send_gchat(text: str) -> dict:
    """POST to a Google Chat incoming webhook. No OAuth required."""
    try:
        resp = httpx.post(GCHAT_WEBHOOK_URL, json={"text": text}, timeout=5)
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
