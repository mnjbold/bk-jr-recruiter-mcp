"""
Composio-backed Google services — Gmail, Calendar, Drive.

Mirrors src/composio_sheets.py but for the three Google services the BK JR
"agentic" layer needs in addition to Sheets:
  - GMAIL_SEND_EMAIL          for sending interview confirmations, document links
  - GOOGLECALENDAR_LIST_EVENTS for checking BK's availability
  - GOOGLECALENDAR_CREATE_EVENT for booking interviews
  - GOOGLEDRIVE_CREATE_FOLDER  for per-candidate document storage
  - GOOGLEDRIVE_SHARE_FILE     for sending upload links to candidates

Auth model is identical to Sheets: same COMPOSIO_API_KEY (platform key, ak_),
same COMPOSIO_USER_ID, same COMPOSIO_CONNECTED_ACCOUNT_ID. The user must
have authorized the additional scopes (gmail, calendar, drive) on the
connected Google account at app.composio.dev — otherwise the call returns
Composio's "scope not granted" error and the LLM should fall back gracefully.
"""
from __future__ import annotations

import os

import httpx
import structlog

log = structlog.get_logger(__name__)

COMPOSIO_BASE = "https://backend.composio.dev/api/v3"


class ComposioGoogleClient:
    def __init__(self) -> None:
        self.api_key = os.environ.get("COMPOSIO_API_KEY", "")
        self.entity_id = os.environ.get("COMPOSIO_USER_ID", "") or os.environ.get("COMPOSIO_ENTITY_ID", "")
        self.connected_account_id = os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.entity_id)

    def _execute(self, tool_slug: str, arguments: dict) -> dict:
        if not self.configured:
            return {"error": "composio not configured (need COMPOSIO_API_KEY + COMPOSIO_USER_ID)"}
        body: dict = {"entity_id": self.entity_id, "arguments": arguments}
        if self.connected_account_id:
            body["connected_account_id"] = self.connected_account_id
        try:
            resp = httpx.post(
                f"{COMPOSIO_BASE}/tools/execute/{tool_slug}",
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                json=body,
                timeout=20,
            )
            resp.raise_for_status()
            out = resp.json()
            if out.get("successful") is False:
                err = (out.get("data") or {}).get("message") or out.get("error") or "unknown"
                return {"error": f"Composio {tool_slug} failed: {err}"}
            return out.get("data", out)
        except Exception as e:
            log.error("composio_google_failed", tool=tool_slug, error=str(e))
            return {"error": f"{tool_slug} request failed: {e}"}

    # ── Gmail ────────────────────────────────────────────────────────────────

    def gmail_send(self, to: str, subject: str, body: str) -> dict:
        """Send an email from BK's connected Gmail account.

        `to` may be a single address or comma-separated list.
        `body` may contain plain text or simple HTML — Composio handles both.

        NOTE: Composio's GMAIL_SEND_EMAIL tool uses 'recipient_email' as the
        arg name (not 'to'). Verified live 2026-08-06 — using 'to' returns
        "missing fields: {recipient_email}".
        """
        return self._execute("GMAIL_SEND_EMAIL", {
            "recipient_email": to,
            "subject": subject,
            "body": body,
        })

    # ── Google Calendar ──────────────────────────────────────────────────────

    def gcal_list_events(self, time_min: str | None, time_max: str | None, max_results: int = 10) -> dict:
        """List events on BK's primary calendar between time_min and time_max.

        time_min / time_max should be ISO 8601 (e.g. "2026-08-07T00:00:00Z").
        If omitted, defaults to "now" through "+7 days".

        NOTE: Composio's slug is GOOGLECALENDAR_EVENTS_LIST (not
        GOOGLECALENDAR_LIST_EVENTS — that's a 404). Verified live 2026-08-06.
        """
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        if not time_min:
            time_min = now.isoformat()
        if not time_max:
            time_max = (now + timedelta(days=7)).isoformat()
        result = self._execute("GOOGLECALENDAR_EVENTS_LIST", {
            "calendarId": "primary",
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        })
        return result if isinstance(result, dict) else {"events": result}

    def gcal_create_event(
        self,
        summary: str,
        start: str,
        end: str,
        attendees: list[str] | None = None,
        description: str = "",
    ) -> dict:
        """Create a calendar event. `start` / `end` are ISO 8601.

        If `attendees` is provided, Composio sends invitations to each address.

        NOTE: Composio's slug is GOOGLECALENDAR_EVENTS_INSERT (not
        GOOGLECALENDAR_CREATE_EVENT — that's a 404). Verified live 2026-08-06.
        """
        args = {
            "summary": summary,
            "start_datetime": start,
            "end_datetime": end,
            "description": description,
        }
        if attendees:
            args["attendees"] = attendees
        return self._execute("GOOGLECALENDAR_EVENTS_INSERT", args)

    # ── Google Drive ─────────────────────────────────────────────────────────

    def gdrive_create_folder(self, name: str, parent_id: str | None = None) -> dict:
        """Create a folder in BK's Drive. `parent_id` is optional (defaults to root).

        NOTE: Composio's GOOGLEDRIVE_CREATE_FOLDER tool uses 'folder_name'
        as the arg name (not 'name'). Verified live 2026-08-06.
        """
        args = {"folder_name": name}
        if parent_id:
            args["parent_folder_id"] = parent_id
        return self._execute("GOOGLEDRIVE_CREATE_FOLDER", args)

    def gdrive_share(self, file_id: str, email: str, role: str = "reader") -> dict:
        """Share a Drive file/folder with `email` at the given permission role.

        `role` is one of: reader, commenter, writer, organizer.
        """
        return self._execute("GOOGLEDRIVE_SHARE_FILE", {
            "file_id": file_id,
            "email": email,
            "role": role,
        })
