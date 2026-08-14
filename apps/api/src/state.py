"""
Durable local state for BK JR — the safety substrate.

Everything in here exists so that a mistake cannot reach a real candidate:
  opt_outs         who told us STOP (legally binding, never overridable)
  bk_state         practice_mode / bk_busy / busy_since / current_call_id
  processed_events webhook idempotency (see src/dedupe.py for that API)

Design notes
------------
* stdlib sqlite3 only — no ORM, no new dependency. One file, three tables.
* A fresh connection per call. sqlite3 connections are not safe to share
  across threads, and both FastAPI (threadpool) and FastMCP (async) will hit
  this concurrently. Opening a connection is microseconds; correctness is
  worth more than that.
* WAL + a 10s busy timeout so a concurrent writer blocks briefly instead of
  raising "database is locked".
* The guards live HERE and are invoked from the lowest-level send/dial
  functions in quo_client.py / retell_client.py — never from the MCP tool
  layer. A future feature that calls send_sms() inherits them for free.

Practice mode DEFAULTS ON. A fresh deploy with an empty database cannot
reach a real candidate until someone deliberately turns it off.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import UTC, datetime

import structlog

log = structlog.get_logger(__name__)

DEFAULT_DB_FILENAME = "bkjr_state.db"
DEFAULT_PRACTICE_NUMBER = "+18138223579"

# Whole-message keywords, matched case-insensitively after strip(). Carrier
# convention (and CTIA guidance) is that these are the mandatory set.
OPT_OUT_KEYWORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
OPT_IN_KEYWORDS = frozenset({"START", "UNSTOP"})

OPT_OUT_CONFIRMATION = (
    "You're unsubscribed from Bold Business recruiting messages "
    "and won't receive anything further. Reply START to opt back in."
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events (
    provider      TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (provider, event_id)
);
CREATE TABLE IF NOT EXISTS opt_outs (
    phone  TEXT PRIMARY KEY,
    reason TEXT,
    at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bk_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS candidate_states (
    phone         TEXT PRIMARY KEY,
    state_json    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""

_init_lock = threading.Lock()
_initialised: set[str] = set()


# ── Errors ────────────────────────────────────────────────────────────────────

class OptedOutError(RuntimeError):
    """Raised by the lowest-level send/dial path for an opted-out number."""

    def __init__(self, phone: str, reason: str = ""):
        self.phone = phone
        self.reason = reason
        super().__init__(
            f"{phone} has opted out of messages"
            + (f" ({reason})" if reason else "")
            + " — refusing to send or dial."
        )


class CallWindowError(RuntimeError):
    """Raised when a dial would fall outside the called party's legal window."""

    def __init__(self, phone: str, reason: str, next_ok_local_time: str | None = None):
        self.phone = phone
        self.reason = reason
        self.next_ok_local_time = next_ok_local_time
        super().__init__(
            f"refusing to dial {phone}: {reason}"
            + (f" — next allowed local time {next_ok_local_time}" if next_ok_local_time else "")
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now_iso() -> str:
    """ISO-8601 UTC with a Z suffix, second precision. One format everywhere."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_phone(raw: str | None) -> str:
    """
    Best-effort E.164. Bare 10-digit input is assumed NANP (+1) — the only
    assumption safe to make about this sheet, whose column H is US numbers.
    Anything already carrying a country code is preserved, so international
    candidates (+54 Argentina) still round-trip.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return ""
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def db_path() -> str:
    """
    BKJR_STATE_DB wins. Otherwise sit adjacent to BOLD_DB_PATH if that is set
    (so all of this deploy's local state lands in one directory), else cwd.
    """
    explicit = os.environ.get("BKJR_STATE_DB", "").strip()
    if explicit:
        return explicit
    bold = os.environ.get("BOLD_DB_PATH", "").strip()
    if bold:
        return os.path.join(os.path.dirname(os.path.abspath(bold)), DEFAULT_DB_FILENAME)
    return DEFAULT_DB_FILENAME


def connect() -> sqlite3.Connection:
    """Fresh connection with the schema guaranteed present."""
    path = db_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    if path not in _initialised:
        with _init_lock:
            if path not in _initialised:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:  # pragma: no cover
                    pass
                conn.executescript(_SCHEMA)
                conn.commit()
                _initialised.add(path)
    return conn


def reset_for_tests() -> None:
    """Forget which DB paths have been initialised. Tests repoint BKJR_STATE_DB."""
    with _init_lock:
        _initialised.clear()


# ── Opt-outs ──────────────────────────────────────────────────────────────────

def classify_inbound(text: str | None) -> str | None:
    """'stop' | 'start' | None. Whole-message match, case-insensitive, stripped."""
    word = str(text or "").strip().upper()
    # Strip trailing punctuation so "STOP." and "STOP!" still count.
    word = word.rstrip(".!?,;:")
    if word in OPT_OUT_KEYWORDS:
        return "stop"
    if word in OPT_IN_KEYWORDS:
        return "start"
    return None


def is_opted_out(phone: str) -> bool:
    norm = normalize_phone(phone)
    if not norm:
        return False
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM opt_outs WHERE phone = ?", (norm,)).fetchone()
    return row is not None


def record_opt_out(phone: str, reason: str = "inbound keyword") -> bool:
    """
    Record an opt-out. Returns True only if this is NEW — that return value is
    the guard that stops a second STOP from triggering a second confirmation
    SMS (which would itself be a compliance problem).
    """
    norm = normalize_phone(phone)
    if not norm:
        return False
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO opt_outs (phone, reason, at) VALUES (?, ?, ?)",
            (norm, reason, utc_now_iso()),
        )
        conn.commit()
    created = cur.rowcount == 1
    log.info("opt_out_recorded", phone=norm, reason=reason, newly_recorded=created)
    return created


def clear_opt_out(phone: str) -> bool:
    """START / UNSTOP. Returns True if a row was actually removed."""
    norm = normalize_phone(phone)
    if not norm:
        return False
    with connect() as conn:
        cur = conn.execute("DELETE FROM opt_outs WHERE phone = ?", (norm,))
        conn.commit()
    removed = cur.rowcount > 0
    log.info("opt_out_cleared", phone=norm, was_opted_out=removed)
    return removed


def list_opt_outs() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT phone, reason, at FROM opt_outs ORDER BY at DESC").fetchall()
    return [dict(r) for r in rows]


# ── Candidate state durability ───────────────────────────────────────────────

def checkpoint_candidate_state(phone: str, state_json: str) -> None:
    """
    Persist a candidate's state to SQLite before returning from handle_inbound.
    This means a backend restart cannot lose an inbound reply's state update.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO candidate_states (phone, state_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET state_json = excluded.state_json, "
            "updated_at = excluded.updated_at",
            (phone, state_json, utc_now_iso()),
        )
        conn.commit()


def load_candidate_states() -> dict[str, dict]:
    """
    Load all persisted candidate states from SQLite into memory on startup.
    This restores the in-memory candidate_states dict after a backend restart.
    """
    import json as _json
    out = {}
    with connect() as conn:
        rows = conn.execute("SELECT phone, state_json FROM candidate_states").fetchall()
    for row in rows:
        try:
            out[row["phone"]] = _json.loads(row["state_json"])
        except (ValueError, TypeError) as exc:
            log.warning("candidate_state_parse_failed", phone=row["phone"], error=str(exc))
    return out


# ── bk_state key/value ────────────────────────────────────────────────────────

def get_state(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM bk_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(key: str, value: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO bk_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


# ── Practice mode ─────────────────────────────────────────────────────────────

def practice_number() -> str:
    return normalize_phone(os.environ.get("PRACTICE_NUMBER", "") or DEFAULT_PRACTICE_NUMBER)


def practice_mode_on() -> bool:
    """
    DEFAULTS ON. An absent row means a fresh deploy, and a fresh deploy must
    not be able to text a real candidate.
    """
    raw = get_state("practice_mode")
    if raw is None:
        return True
    return str(raw).strip().lower() in ("1", "true", "on", "yes")


def set_practice_mode(on: bool) -> bool:
    set_state("practice_mode", "on" if on else "off")
    log.warning("practice_mode_changed", practice_mode="on" if on else "off",
                live_sends_possible=not on)
    return on


# ── The guards: enforced from quo_client / retell_client, never above them ────

def guard_sms(to: str, body: str, *, allow_opted_out: bool = False) -> tuple[str, str]:
    """
    Returns (actual_destination, actual_body).

    Raises OptedOutError unless allow_opted_out — which exists for exactly one
    caller: the single STOP confirmation message, which by law must go to a
    number that has, by definition, just opted out.
    """
    intended = normalize_phone(to)
    if not intended:
        raise ValueError("send_sms: 'to' did not contain a usable phone number")

    if not allow_opted_out and is_opted_out(intended):
        raise OptedOutError(intended, "STOP received previously")

    dest, actual_body = intended, body
    if practice_mode_on():
        dest = practice_number()
        actual_body = f"[PRACTICE] {body}"
        log.info("practice_mode_redirect", channel="sms",
                 intended_destination=intended, actual_destination=dest)

    # The practice number itself can opt out. Honour that too — otherwise the
    # STOP-handling path becomes untestable without spamming the operator.
    if not allow_opted_out and dest != intended and is_opted_out(dest):
        raise OptedOutError(dest, "practice number has opted out")

    return dest, actual_body


def guard_dial(to: str) -> tuple[str, str]:
    """
    Opt-out + practice-mode half of the dial guard. Returns
    (actual_destination, intended_destination). The TCPA calling-window half
    lives in src/calling_window.py and is applied by the caller against the
    destination this returns — i.e. against the party who will actually ring.
    """
    intended = normalize_phone(to)
    if not intended:
        raise ValueError("dial: 'to_number' did not contain a usable phone number")

    if is_opted_out(intended):
        raise OptedOutError(intended, "STOP received previously")

    dest = intended
    if practice_mode_on():
        dest = practice_number()
        log.info("practice_mode_redirect", channel="voice",
                 intended_destination=intended, actual_destination=dest)
        if is_opted_out(dest):
            raise OptedOutError(dest, "practice number has opted out")

    return dest, intended
