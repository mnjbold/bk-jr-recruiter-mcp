"""
Composio-backed Google Sheets writeback.

Replaces the old service-account approach (src/sheets_client.py, dropped) with
Composio's hosted tool execution, so the whole system runs off BK's single
Google login (connected at app.composio.dev) — no GOOGLE_SERVICE_ACCOUNT_JSON.

Live-verified contract (backend.composio.dev, July 2026 — confirmed against the
real connection, NOT just the SDK docs):
  POST https://backend.composio.dev/api/v3/tools/execute/{TOOL_SLUG}
  header  x-api-key: <COMPOSIO_API_KEY>        # the PLATFORM key (ak_...), not ck_
  body    {"entity_id": "<user>",              # the connected user id
           "connected_account_id": "ca_...",   # optional but the API asks for it
           "arguments": {...}}

Row model for the real sheet ("Positions for Assignment"):
  - candidate phone lives in column H
  - screening results are written to NEW columns starting at
    COMPOSIO_WRITEBACK_START_CELL (default AG), in COMPOSIO_WRITEBACK_COLUMNS order
  - the agent NEVER creates rows (a human owns the candidate list); if no row
    matches the phone, it's a logged no-op, not an append

find_row reads column H via GOOGLESHEETS_BATCH_GET and matches on normalized
digits — deterministic, avoids depending on LOOKUP's opaque response shape.
"""
from __future__ import annotations

import os
import re

import httpx
import structlog

log = structlog.get_logger(__name__)

COMPOSIO_BASE = "https://backend.composio.dev/api/v3"

DEFAULT_WRITEBACK_COLUMNS = ["status", "screening_result", "interested", "callback_time", "summary"]


def _digits(s: str) -> str:
    """Last 10 digits of a phone-ish string, for format-agnostic matching."""
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else d


def _col_to_index(col: str) -> int:
    """'A'->0, 'AG'->32."""
    n = 0
    for ch in col.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _index_to_col(i: int) -> str:
    s = ""
    i += 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


class ComposioSheetsClient:
    def __init__(self) -> None:
        self.api_key = os.environ.get("COMPOSIO_API_KEY", "")
        self.entity_id = os.environ.get("COMPOSIO_USER_ID", "") or os.environ.get("COMPOSIO_ENTITY_ID", "")
        self.connected_account_id = os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "")
        self.spreadsheet_id = os.environ.get("GOOGLE_SHEETS_ID", "")
        self.sheet_name = os.environ.get("COMPOSIO_SHEET_NAME", "Positions for Assignment")
        self.phone_col = os.environ.get("COMPOSIO_PHONE_COLUMN", "H")
        self.start_cell = os.environ.get("COMPOSIO_WRITEBACK_START_CELL", "")  # e.g. "AG"
        cols = os.environ.get("COMPOSIO_WRITEBACK_COLUMNS", "")
        self.columns = [c.strip() for c in cols.split(",") if c.strip()] or DEFAULT_WRITEBACK_COLUMNS

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.entity_id and self.spreadsheet_id)

    # ── Composio raw execute ──────────────────────────────────────────────────

    def _execute(self, tool_slug: str, arguments: dict) -> dict:
        body: dict = {"entity_id": self.entity_id, "arguments": arguments}
        if self.connected_account_id:
            body["connected_account_id"] = self.connected_account_id
        resp = httpx.post(
            f"{COMPOSIO_BASE}/tools/execute/{tool_slug}",
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json=body,
            timeout=20,
        )
        resp.raise_for_status()
        out = resp.json()
        # Composio returns HTTP 200 even on logical failures (e.g. grid limits,
        # bad range) with successful:false — treat that as an error, don't
        # silently pretend it worked.
        if out.get("successful") is False:
            raise RuntimeError(f"Composio {tool_slug} failed: {(out.get('data') or {}).get('message') or out.get('error')}")
        return out

    # ── Row lookup (deterministic: read col H, match normalized phone) ────────

    def find_row(self, phone: str) -> int | None:
        result = self._execute(
            "GOOGLESHEETS_BATCH_GET",
            {"spreadsheet_id": self.spreadsheet_id,
             "ranges": [f"'{self.sheet_name}'!{self.phone_col}:{self.phone_col}"]},
        )
        data = result.get("data", result) or {}
        value_ranges = data.get("valueRanges") or []
        if not value_ranges:
            return None
        target = _digits(phone)
        if not target:
            return None
        for idx, row in enumerate(value_ranges[0].get("values", [])):
            cell = row[0] if row else ""
            if _digits(cell) == target:
                return idx + 1  # values are 0-based from row 1; sheet rows are 1-based
        return None

    # ── Full sheet read (for /api/candidates) ────────────────────────────────

    # Default column map for the "Positions for Assignment" sheet — the
    # human-readable candidate columns BK's team uses. Override via env if
    # the sheet's column layout ever changes.
    # Real layout (from row 1 header dump 2026-07-31):
    #   A LOCATION: | B (BK) Category | C STATUS: | D (WENDY) Start Date:
    #   E (BK) Fallout Date | F (BK) First Name | G (BK) Last Name
    #   H (BK) Phone Number | I (BK) Personal Email
    DEFAULT_LIST_COLUMNS = {
        "status": os.environ.get("COMPOSIO_LIST_COL_STATUS", "C"),
        "first_name": os.environ.get("COMPOSIO_LIST_COL_FIRST_NAME", "F"),
        "last_name": os.environ.get("COMPOSIO_LIST_COL_LAST_NAME", "G"),
        "phone": os.environ.get("COMPOSIO_LIST_COL_PHONE", "H"),
        "email": os.environ.get("COMPOSIO_LIST_COL_EMAIL", "I"),
        "role": os.environ.get("COMPOSIO_LIST_COL_ROLE", "B"),
        "location": os.environ.get("COMPOSIO_LIST_COL_LOCATION", "A"),
        "start_date": os.environ.get("COMPOSIO_LIST_COL_START_DATE", "D"),
        "fallout_date": os.environ.get("COMPOSIO_LIST_COL_FALLOUT_DATE", "E"),
    }

    def list_all_candidates(self, limit: int = 50) -> dict:
        """Read every candidate row from the sheet. Returns the columns we
        need (name, phone, role, location, status) keyed by the sheet's row
        number so callers can write back. The in-memory `candidate_states`
        may be empty (e.g. fresh restart) — this is the source of truth.
        """
        if not self.configured:
            return {"error": "composio not configured", "candidates": []}
        cols = self.DEFAULT_LIST_COLUMNS
        # Build a single wide range covering every mapped column so we get
        # them in one batched read.
        first = min(cols.values(), key=lambda c: _col_to_index(c))
        last = max(cols.values(), key=lambda c: _col_to_index(c))
        col_range = f"{first}:{last}"
        try:
            result = self._execute(
                "GOOGLESHEETS_BATCH_GET",
                {"spreadsheet_id": self.spreadsheet_id,
                 "ranges": [f"'{self.sheet_name}'!{col_range}"]},
            )
        except Exception as e:
            log.error("composio_list_failed", error=str(e))
            return {"error": f"sheet read failed: {e}", "candidates": []}

        data = result.get("data", result) or {}
        value_ranges = data.get("valueRanges") or []
        if not value_ranges:
            return {"candidates": [], "count": 0, "source": "sheet"}
        rows = value_ranges[0].get("values", [])

        # Resolve column indices relative to the range start.
        start_idx = _col_to_index(first)
        idx = {name: _col_to_index(c) - start_idx for name, c in cols.items()}

        out = []
        for i, row in enumerate(rows):
            phone = ""
            # Use cell() in a closure that captures `idx` and `row`
            def cell(col_name):
                j = idx.get(col_name, -1)
                if j < 0 or j >= len(row):
                    return ""
                return (row[j] or "").strip()

            phone = cell("phone")
            # Skip header row + empty rows.
            if i == 0 and not _digits(phone):
                continue
            if not _digits(phone):
                continue
            first_name = cell("first_name")
            last_name = cell("last_name")
            if not (first_name or last_name or phone):
                continue
            out.append({
                "row": i + 1,  # sheet row number (1-based; row 1 is header)
                "name": f"{first_name} {last_name}".strip(),
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "email": cell("email"),
                "role": cell("role"),
                "location": cell("location"),
                "status": cell("status"),
            })
            if len(out) >= limit:
                break
        return {"candidates": out, "count": len(out), "source": "sheet"}

    # ── Writeback ─────────────────────────────────────────────────────────────

    def upsert_candidate(self, phone: str, fields: dict) -> dict:
        if not self.configured:
            return {"skipped": "composio not configured (need COMPOSIO_API_KEY, COMPOSIO_USER_ID, GOOGLE_SHEETS_ID)"}
        if not self.start_cell:
            return {"skipped": "COMPOSIO_WRITEBACK_START_CELL not set — confirm the sheet's writeback column first"}

        try:
            row = self.find_row(phone)
        except Exception as e:
            log.error("composio_lookup_failed", phone=phone, error=str(e))
            return {"error": f"lookup failed: {e}"}

        if row is None:
            log.info("composio_no_matching_row", phone=phone)
            return {"skipped": f"no row matching {phone}"}

        values = [[fields.get(col, "") for col in self.columns]]
        first_cell = f"{self.start_cell}{row}"
        try:
            # GOOGLESHEETS_BATCH_UPDATE writes starting at first_cell_location and
            # auto-extends right across `values`. (There is no GOOGLESHEETS_VALUES_UPDATE
            # on this connection — verified live; it 404s.)
            self._execute(
                "GOOGLESHEETS_BATCH_UPDATE",
                {"spreadsheet_id": self.spreadsheet_id, "sheet_name": self.sheet_name,
                 "first_cell_location": first_cell, "values": values,
                 "valueInputOption": "USER_ENTERED"},
            )
        except Exception as e:
            log.error("composio_update_failed", phone=phone, row=row, error=str(e))
            return {"error": f"update failed: {e}"}

        log.info("composio_writeback_ok", phone=phone, row=row, cell=first_cell)
        return {"ok": True, "row": row, "cell": first_cell}
