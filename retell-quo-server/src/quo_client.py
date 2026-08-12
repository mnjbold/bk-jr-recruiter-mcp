"""
Quo (OpenPhone) API Client
Wraps the Quo REST API for SMS send/receive, contacts, conversations.
API docs: https://quo.com/docs/mdx/api-reference/introduction
"""
from __future__ import annotations

import os

import httpx
import structlog

log = structlog.get_logger(__name__)

QUO_API_BASE = "https://api.openphone.com/v1"


class QuoClient:
    def __init__(self, api_key: str | None = None):
        # Same OpenPhone key powers both projects; the Apps Script side calls it
        # OPENPHONE_API_KEY, so accept either name rather than force one env var.
        self.api_key = api_key or os.environ.get("QUO_API_KEY", "") or os.environ.get("OPENPHONE_API_KEY", "")
        if not self.api_key:
            raise ValueError("QUO_API_KEY (or OPENPHONE_API_KEY) not set. Get it from Quo/OpenPhone Settings → Integrations → API.")
        self.headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    # ── Messages ─────────────────────────────────────────────────────────────

    def send_sms(self, from_number_id: str, to: str, text: str) -> dict:
        """
        Send an SMS from BK's Quo number to a candidate.
        from_number_id: the phoneNumberId of BK's Quo number (get from list_numbers())
        to: E.164 format e.g. +15551234567 or +54911234567 (international)
        """
        payload = {
            "content": text,
            "from": from_number_id,
            "to": [to],
        }
        resp = httpx.post(
            f"{QUO_API_BASE}/messages",
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        if resp.status_code >= 400:
            import sys
            print(f"OPENPHONE_SEND_FAILED status={resp.status_code} from={from_number_id} to={to} body={resp.text[:500]}", file=sys.stderr, flush=True)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        log.info("sms_sent", to=to, message_id=data.get("id"), status=data.get("status"))
        return data

    def list_messages(self, phone_number_id: str, participant: str | None = None, limit: int = 25) -> list[dict]:
        """List messages for a phone number, optionally filtered to a specific contact."""
        params = {"phoneNumberId": phone_number_id, "maxResults": limit}
        if participant:
            params["participants"] = participant
        resp = httpx.get(f"{QUO_API_BASE}/messages", headers=self.headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_message(self, message_id: str) -> dict:
        resp = httpx.get(f"{QUO_API_BASE}/messages/{message_id}", headers=self.headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", {})

    # ── Conversations ─────────────────────────────────────────────────────────

    def list_conversations(self, phone_number_id: str, limit: int = 20) -> list[dict]:
        """List active conversations on BK's Quo number."""
        params = {"phoneNumberId": phone_number_id, "maxResults": limit}
        resp = httpx.get(f"{QUO_API_BASE}/conversations", headers=self.headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])

    # ── Contacts ─────────────────────────────────────────────────────────────

    def list_contacts(self, limit: int = 100) -> list[dict]:
        params = {"maxResults": limit}
        resp = httpx.get(f"{QUO_API_BASE}/contacts", headers=self.headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_contact_by_phone(self, phone: str) -> dict | None:
        """Find a contact by phone number."""
        contacts = self.list_contacts(limit=200)
        for c in contacts:
            for pn in c.get("phoneNumbers", []):
                if pn.get("value", "").replace(" ", "") == phone.replace(" ", ""):
                    return c
        return None

    def create_contact(self, name: str, phone: str, tags: list[str] | None = None) -> dict:
        payload = {
            "firstName": name.split()[0] if name else "Unknown",
            "lastName": " ".join(name.split()[1:]) if len(name.split()) > 1 else "",
            "phoneNumbers": [{"value": phone}],
            "tags": tags or [],
        }
        resp = httpx.post(f"{QUO_API_BASE}/contacts", headers=self.headers, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", {})

    # ── Phone Numbers ─────────────────────────────────────────────────────────

    def list_numbers(self) -> list[dict]:
        """List all phone numbers in BK's Quo account."""
        resp = httpx.get(f"{QUO_API_BASE}/phone-numbers", headers=self.headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_default_number_id(self) -> str | None:
        """Get the first available phone number ID."""
        numbers = self.list_numbers()
        if numbers:
            return numbers[0].get("id")
        return None

    # ── Webhooks ─────────────────────────────────────────────────────────────

    def create_message_webhook(self, url: str, phone_number_id: str | None = None) -> dict:
        """
        Register a webhook to receive inbound SMS notifications.
        url: Your public webhook endpoint (e.g. Railway service URL)
        """
        payload = {"url": url}
        if phone_number_id:
            payload["phoneNumberId"] = phone_number_id
        resp = httpx.post(
            f"{QUO_API_BASE}/webhooks/message",
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def list_webhooks(self) -> list[dict]:
        resp = httpx.get(f"{QUO_API_BASE}/webhooks", headers=self.headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", [])

    # ── Calls ────────────────────────────────────────────────────────────────

    def list_calls(self, phone_number_id: str, participant: str, max_results: int = 100,
                    page_token: str | None = None) -> dict:
        """
        List calls between BK's Quo number and a single participant.
        NOTE: /v1/calls requires exactly one participant (1:1 only) — no bulk/no-filter mode.
        Returns the raw response dict (data, totalItems, nextPageToken) so callers can paginate.
        """
        params = {"phoneNumberId": phone_number_id, "participants": participant, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        resp = httpx.get(f"{QUO_API_BASE}/calls", headers=self.headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def list_all_calls(self, phone_number_id: str, participant: str) -> list[dict]:
        """Paginate through every call for a single participant."""
        return self._paginate_participant("/calls", phone_number_id, participant)

    # ── Generic pagination helpers ───────────────────────────────────────────

    def _paginate(self, path: str, params: dict) -> list[dict]:
        """Follow nextPageToken until exhausted. Retries once on 429 with backoff."""
        import time
        out: list[dict] = []
        page_token = None
        while True:
            p = dict(params)
            if page_token:
                p["pageToken"] = page_token
            resp = httpx.get(f"{QUO_API_BASE}{path}", headers=self.headers, params=p, timeout=15)
            if resp.status_code == 429:
                time.sleep(2)
                resp = httpx.get(f"{QUO_API_BASE}{path}", headers=self.headers, params=p, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("data", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
            time.sleep(0.12)  # stay under rate limit across pages
        return out

    def _paginate_participant(self, path: str, phone_number_id: str, participant: str) -> list[dict]:
        return self._paginate(path, {
            "phoneNumberId": phone_number_id,
            "participants": participant,
            "maxResults": 100,
        })

    def list_all_contacts(self) -> list[dict]:
        # OpenPhone caps /contacts at maxResults=50 (other endpoints allow 100)
        return self._paginate("/contacts", {"maxResults": 50})

    def list_all_conversations(self, phone_number_id: str) -> list[dict]:
        return self._paginate("/conversations", {"phoneNumbers": phone_number_id, "maxResults": 100})

    def list_all_messages(self, phone_number_id: str, participant: str) -> list[dict]:
        return self._paginate_participant("/messages", phone_number_id, participant)

    def has_any_message(self, phone_number_id: str, participant: str) -> bool:
        """
        Cheap existence check (single request, maxResults=1) — was this
        number EVER texted from this Quo number? Used as the dedup gate before
        bulk outreach so we don't re-contact someone already in a thread.
        Much cheaper than list_all_messages() at 1000+-candidate scale since
        it doesn't paginate the full history for every candidate.
        """
        params = {"phoneNumberId": phone_number_id, "participants": participant, "maxResults": 1}
        resp = httpx.get(f"{QUO_API_BASE}/messages", headers=self.headers, params=params, timeout=10)
        resp.raise_for_status()
        return len(resp.json().get("data", [])) > 0
