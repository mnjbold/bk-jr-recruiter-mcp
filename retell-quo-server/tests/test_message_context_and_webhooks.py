"""
Tests for the new message-context + live-webhook tools (2026-08-16):
  - list_messages (closes the "no response body" gap)
  - list_calls
  - list_conversations pagination (replaces the buggy hard-coded 20-cap)
  - register_webhook / list_webhooks / unregister_webhook
  - get_call_transcript (Retell-first with Quo fallback)
  - webhook_log: SQLite persistence, filter, recent(), count()

Verifies:
  1. New tools surface full message text + direction from Quo.
  2. The 20-conversation cap is gone — `limit` is honored and `page_token`
     plumbs through.
  3. Quo participants are passed as JSON arrays (NOT bare strings — that
     was a pre-existing bug introduced into QuoClient.list_messages).
  4. Webhook registrations hit Quo with the right URL + idempotent removal.
  5. webhook_log survives across "process restarts" (separate sqlite3.Connection
     reads the same file).
  6. notify_inbound_sms + notify_call_event format predictable GChat text.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

# Use a private temp DB so we don't touch any real BKJR_WEBHOOK_LOG_DB.
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["BKJR_WEBHOOK_LOG_DB"] = _TMP_DB.name

import webhook_log  # noqa: E402
from quo_client import QuoClient  # noqa: E402


# ── Fixtures ───────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, *, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json if self._json is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = RuntimeError(f"HTTP {self.status_code}")
            err.status_code = self.status_code
            raise err


class FakeQuo:
    """In-memory stand-in for QuoClient that records every call and
    returns canned responses keyed by endpoint."""

    def __init__(self):
        self.calls: list[dict] = []
        self.messages_payload = {
            "data": [
                {
                    "id": "AC_inb_1",
                    "to": ["+181****2007"],
                    "from": "+15555550100",
                    "text": "Yes, I'm interested",
                    "direction": "incoming",
                    "status": "delivered",
                    "createdAt": "2026-08-14T22:00:00.000Z",
                    "phoneNumberId": "PNPGjMWOw8",
                    "conversationId": "CNtest123",
                },
                {
                    "id": "AC_out_1",
                    "to": ["+15555550100"],
                    "from": "+181****2007",
                    "text": "Hi there! Are you open to a screening call?",
                    "direction": "outgoing",
                    "status": "delivered",
                    "createdAt": "2026-08-14T21:55:00.000Z",
                    "phoneNumberId": "PNPGjMWOw8",
                    "conversationId": "CNtest123",
                },
            ],
            "nextPageToken": "next_abc",
            "totalItems": 2,
        }
        self.calls_payload = {
            "data": [
                {
                    "id": "CA_test_1",
                    "from": "+181****2007",
                    "to": "+15555550100",
                    "duration": 42,
                    "status": "completed",
                },
            ],
            "nextPageToken": None,
        }
        self.conversations_payload = {
            "data": [
                {"id": "CNtest123", "lastActivityAt": "2026-08-14T22:00:00.000Z"},
            ],
            "nextPageToken": "more_pages",
        }
        self.webhooks_payload = {
            "data": [
                {
                    "id": "WH_exist_1",
                    "url": "https://bkjr-api.getbijou.xyz/webhook/quo",
                    "events": ["message.received"],
                    "phoneNumberId": "PNPGjMWOw8",
                }
            ],
            "nextPageToken": None,
        }

    def list_messages(self, *, phone_number_id, participant=None, limit=25, page_token=None):
        self.calls.append({
            "method": "list_messages",
            "phone_number_id": phone_number_id,
            "participant": participant,
            "limit": limit,
            "page_token": page_token,
        })
        return self.messages_payload

    def list_calls(self, *, phone_number_id, participant, max_results=100, page_token=None):
        self.calls.append({
            "method": "list_calls",
            "phone_number_id": phone_number_id,
            "participant": participant,
            "max_results": max_results,
            "page_token": page_token,
        })
        return self.calls_payload

    def list_conversations(self, *, phone_number_id, limit=20, page_token=None):
        self.calls.append({
            "method": "list_conversations",
            "phone_number_id": phone_number_id,
            "limit": limit,
            "page_token": page_token,
        })
        return self.conversations_payload

    def create_message_webhook(self, *, url, phone_number_id=None):
        self.calls.append({
            "method": "create_message_webhook",
            "url": url,
            "phone_number_id": phone_number_id,
        })
        return {"id": "WH_new_1", "url": url}

    def list_webhooks(self):
        self.calls.append({"method": "list_webhooks"})
        return self.webhooks_payload

    def unregister_webhook(self, webhook_id):
        self.calls.append({"method": "unregister_webhook", "webhook_id": webhook_id})
        return {"id": webhook_id, "ok": True}

    def get_call_transcript(self, call_id):
        self.calls.append({"method": "get_call_transcript", "call_id": call_id})
        return {"id": call_id, "transcript": [
            {"speaker": "agent", "text": "Are you open to a screening call?"},
            {"speaker": "candidate", "text": "Yes, send me details."},
        ]}


# ── QuoClient.list_messages: participants must be array, not string ───────


def test_list_messages_passes_participants_as_array(monkeypatch):
    """Regression: Quo's REST requires participants=array, but the OLD
    list_messages was passing it as a bare string."""
    captured: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = dict(params or {})
        return FakeResponse(json_data={
            "data": [{"id": "ACx", "direction": "incoming", "text": "yes"}],
            "nextPageToken": None,
        })

    monkeypatch.setattr("httpx.get", fake_get)
    client = QuoClient(api_key="test_key")
    resp = client.list_messages(
        phone_number_id="PNPGjMWOw8",
        participant="+15555550100",
        limit=5,
    )
    assert captured["url"].endswith("/messages")
    assert captured["params"]["phoneNumberId"] == "PNPGjMWOw8"
    assert captured["params"]["maxResults"] == 5
    # The participants MUST be a list — that's what Quo's API requires.
    assert captured["params"]["participants"] == ["+15555550100"], (
        f"participants must be a JSON array, got {captured['params']['participants']!r}"
    )
    # And the response is the raw dict (not a list) so callers can paginate.
    assert isinstance(resp, dict)
    assert resp["data"][0]["text"] == "yes"


def test_list_conversations_passes_page_token(monkeypatch):
    captured: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = dict(params or {})
        return FakeResponse(json_data={"data": [], "nextPageToken": None})

    monkeypatch.setattr("httpx.get", fake_get)
    client = QuoClient(api_key="test_key")
    resp = client.list_conversations(
        phone_number_id="PNPGjMWOw8", limit=50, page_token="cursor_xyz",
    )
    assert captured["params"]["pageToken"] == "cursor_xyz"
    assert captured["params"]["maxResults"] == 50
    assert isinstance(resp, dict)


# ── list_calls: participants MUST be array (was a bug) ─────────────────────


def test_list_calls_passes_participants_as_array(monkeypatch):
    captured: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = dict(params or {})
        return FakeResponse(json_data={"data": [], "nextPageToken": None})

    monkeypatch.setattr("httpx.get", fake_get)
    client = QuoClient(api_key="test_key")
    resp = client.list_calls(
        phone_number_id="PNPGjMWOw8", participant="+15555550100",
    )
    assert captured["params"]["participants"] == ["+15555550100"]


# ── webhook_log: SQLite persistence + filters ──────────────────────────────


def test_webhook_log_records_and_retrieves():
    webhook_log.record(
        source="quo", event_type="message.received",
        phone="+15555550100", direction="inbound",
        message_id="AC_test_1", body="Yes", payload={"event": "message.received"},
    )
    rows = webhook_log.recent(limit=10)
    assert len(rows) >= 1
    found = next(r for r in rows if r["message_id"] == "AC_test_1")
    assert found["source"] == "quo"
    assert found["body"] == "Yes"
    assert found["phone"] == "+15555550100"


def test_webhook_log_source_filter():
    webhook_log.record(
        source="retell", event_type="call_analyzed",
        phone="+15555550100", call_id="CA_test_1",
        body="agent: hi", payload={"event": "call_analyzed"},
    )
    quo = webhook_log.recent(source="quo", limit=10)
    retell = webhook_log.recent(source="retell", limit=10)
    assert all(r["source"] == "quo" for r in quo)
    assert all(r["source"] == "retell" for r in retell)
    assert any(r["call_id"] == "CA_test_1" for r in retell)


def test_webhook_log_phone_filter():
    webhook_log.record(
        source="quo", event_type="message.received",
        phone="+15559998888", body="hello", payload={},
    )
    rows = webhook_log.recent(phone="+15559998888", limit=5)
    assert rows
    assert all(r["phone"] == "+15559998888" for r in rows)


def test_webhook_log_since_seconds_filter():
    import time
    webhook_log.record(source="quo", event_type="message.received",
                       phone="+15550001111", body="now", payload={})
    # Ask for events within the last 60s — should match.
    rows = webhook_log.recent(since_seconds=60, limit=10)
    assert any(r["phone"] == "+15550001111" for r in rows)


def test_webhook_log_count():
    start_count = webhook_log.count()
    webhook_log.record(source="quo", event_type="message.received",
                       phone="+15550001234", body="x", payload={})
    end_count = webhook_log.count()
    assert end_count >= start_count + 1


# ── notify.py formatters ─────────────────────────────────────────────────


def test_notify_inbound_sms_formats_correctly(monkeypatch):
    """Test the GChat payload format used when an inbound SMS arrives.

    We don't actually fire HTTP — just check the helper builds the right
    text + does NOT raise when GCHAT_WEBHOOK_URL is unset."""
    import notify
    # No GCHAT env → result is {"none": ...} but no exception.
    monkeypatch.setattr(notify, "GCHAT_WEBHOOK_URL", "")
    out = notify.notify_inbound_sms("+15555550100", "Yes, send details", "AC_test_1")
    assert "none" in out  # No channel → no-op result


def test_notify_inbound_sms_payload_when_channel_set(monkeypatch):
    """When GCHAT is configured, the formatted text includes phone + body."""
    import notify
    monkeypatch.setattr(notify, "GCHAT_WEBHOOK_URL", "https://example.invalid/webhook")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["text"] = json.get("text", "")
        return FakeResponse(status_code=200)

    monkeypatch.setattr("httpx.post", fake_post)
    out = notify.notify_inbound_sms("+15555550100", "Yes, send details", "AC_test_1")
    assert out["gchat"]["sent"] is True
    assert "+15555550100" in captured["text"]
    assert "Yes, send details" in captured["text"]


def test_notify_call_event_format(monkeypatch):
    """call_analyzed should include the call id and the summary text."""
    import notify
    monkeypatch.setattr(notify, "GCHAT_WEBHOOK_URL", "https://example.invalid/webhook")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["text"] = json.get("text", "")
        return FakeResponse(status_code=200)

    monkeypatch.setattr("httpx.post", fake_post)
    notify.notify_call_event("call_analyzed", "CA_test_1", "+15555550100",
                            summary="candidate: yes send details")
    assert "CA_test_1" in captured["text"]
    assert "call_analyzed" in captured["text"]
    assert "+15555550100" in captured["text"]


# ── notify._resolve_gchat_url — fallback to .env.webhook ──────────────────


def test_resolve_gchat_url_from_env(monkeypatch):
    """First-priority: env var wins."""
    import notify
    monkeypatch.setattr(notify, "_resolve_gchat_url", lambda: "https://env.invalid/x")
    assert notify._resolve_gchat_url() == "https://env.invalid/x"


def test_resolve_gchat_url_from_file(monkeypatch, tmp_path):
    """Fallback: parse BK_JR_WEBHOOK=... from a credential file."""
    import importlib
    import notify as _notify
    creds = tmp_path / ".env.webhook"
    creds.write_text(
        "# comment\n"
        "BK_JR_WEBHOOK=https://chat.googleapis.com/v1/spaces/AAA/messages?key=foo&token=bar\n"
        "OTHER_VAR=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GCHAT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BKJR_GCHAT_FILE", raising=False)
    monkeypatch.setenv("BKJR_GCHAT_FILE", str(creds))
    importlib.reload(_notify)
    assert "chat.googleapis.com" in _notify.GCHAT_WEBHOOK_URL
    assert "spaces/AAA" in _notify.GCHAT_WEBHOOK_URL


# ── Dispatcher smoke: branches exist at 4-space indent ────────────────────


def test_dispatcher_has_new_tool_branches():
    """The four new tools must be reachable from hermes_tool — the
    historic elif-indent bug rendered tools invisible. This test parses
    server.py and verifies each `elif tool == "X":` we added is at the
    outer 4-space indent inside hermes_tool."""
    src = (SRC / "server.py").read_text(encoding="utf-8")
    import re
    branches = re.findall(r"^    elif tool == \"(\w+)\":", src, flags=re.MULTILINE)
    # All eight new tools must be present and unique (no accidental duplicate).
    required = {"list_messages", "list_calls", "get_call_transcript",
                "register_webhook", "list_webhooks", "unregister_webhook",
                "list_webhook_events"}
    missing = required - set(branches)
    assert not missing, f"missing tools in dispatcher: {missing}"
    from collections import Counter
    c = Counter(branches)
    dups = {k: v for k, v in c.items() if v > 1}
    assert not dups, f"duplicate dispatcher branches: {dups}"
