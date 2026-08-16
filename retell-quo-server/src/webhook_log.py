"""
Webhook event log — durable record of every inbound webhook receipt.

The Quo (/webhook/quo) and Retell (/webhook/retell) handlers call `record()`
after parsing each event. The recorder inserts to a SQLite table
(`webhook_events`) that survives backend restarts (the same SQLite file the
agent's `candidate_states` lives in). Exposes `recent()` for the
`list_webhook_events` MCP tool — so BK or the recruiting assistant can pull
the live event log without grepping structlog.

Why we added this:
  Before the log existed, /webhook/quo's only effect was to update
  candidate state in-memory and return 200. The actual message body was
  not persisted anywhere — so a question like "what did the candidate
  say at 14:23 today?" had no answer. This module fixes that.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_log_lock = threading.Lock()

# Default location matches the agent's existing SQLite file so a single
# `bkjr_state.db` covers both candidate states and event history. Fall
# back to /tmp when the writable disk is the only one available (tests).
_DB_PATH = Path(os.environ.get("BKJR_WEBHOOK_LOG_DB", "/tmp/bkjr_webhook_log.db"))


def _connect() -> sqlite3.Connection:
    DB = Path(os.environ.get("BKJR_WEBHOOK_LOG_DB", str(_DB_PATH)))
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_schema() -> None:
    with _log_lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at REAL NOT NULL,
                    source      TEXT NOT NULL,        -- 'quo' | 'retell'
                    event_type  TEXT NOT NULL,        -- e.g. 'message.received', 'call_analyzed'
                    phone       TEXT,
                    direction   TEXT,                 -- 'inbound' | 'outbound' | NULL
                    message_id  TEXT,
                    call_id     TEXT,
                    body        TEXT,                 -- SMS body or transcript excerpt
                    payload     TEXT NOT NULL         -- raw JSON for audit/debug
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_events_received_at ON webhook_events(received_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_events_source ON webhook_events(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_events_phone ON webhook_events(phone)")
            conn.commit()
        finally:
            conn.close()


_init_schema()


def record(
    source: str,
    event_type: str,
    *,
    phone: str | None = None,
    direction: str | None = None,
    message_id: str | None = None,
    call_id: str | None = None,
    body: str | None = None,
    payload: dict | None = None,
) -> int:
    """
    Insert one webhook event. Returns the row id.

    Args:
        source:      'quo' or 'retell'
        event_type:  e.g. 'message.received', 'call_analyzed'
        phone:       E.164 number, if discoverable
        direction:   'inbound' | 'outbound' (SMS only)
        message_id:  OpenPhone message id (SMS only)
        call_id:     Retell call id (call events)
        body:        SMS text body or transcript excerpt
        payload:     raw event payload (stored as JSON for audit)
    """
    received_at = time.time()
    payload_json = json.dumps(payload or {}, default=str, ensure_ascii=False)
    with _log_lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO webhook_events
                  (received_at, source, event_type, phone, direction,
                   message_id, call_id, body, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (received_at, source, event_type, phone, direction,
                 message_id, call_id, body, payload_json),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def recent(
    *,
    limit: int = 50,
    source: str | None = None,
    phone: str | None = None,
    since_seconds: int | None = None,
) -> list[dict]:
    """
    Return the most recent webhook events, newest first.

    Args:
        limit:           maximum rows to return (default 50, capped at 500)
        source:          filter by 'quo' | 'retell' | None (no filter)
        phone:           filter by exact E.164
        since_seconds:   only events received within this many seconds ago
    """
    limit = max(1, min(int(limit), 500))
    clauses: list[str] = []
    params: list = []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if phone:
        clauses.append("phone = ?")
        params.append(phone)
    if since_seconds is not None:
        clauses.append("received_at >= ?")
        params.append(time.time() - int(since_seconds))
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    with _log_lock:
        conn = _connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM webhook_events{where_sql} ORDER BY received_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def count(*, source: str | None = None) -> int:
    """Total rows in the log (optionally filtered by source)."""
    with _log_lock:
        conn = _connect()
        try:
            if source:
                (n,) = conn.execute(
                    "SELECT COUNT(*) FROM webhook_events WHERE source = ?", (source,),
                ).fetchone()
            else:
                (n,) = conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()
            return int(n)
        finally:
            conn.close()
