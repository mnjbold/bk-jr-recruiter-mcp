"""
Tests for sync_sms_threads — SMS → candidate reconciliation.

Verifies:
  1. New threads (phone not in state) → created as sms_engaged
  2. Existing threads → updated with last_sms_activity
  3. Stale threads (beyond window) → skipped
  4. Threads where only BK's number is on the thread → skipped
  5. Phone number normalization (E.164)
  6. Quo API failure → ok=False with error message
  7. sheets sync best-effort (does not raise on failure)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from sms_sync import (
    _normalize_phone,
    _parse_iso,
    sync_sms_threads,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


class FakeQuo:
    """In-memory stand-in for QuoClient.list_conversations."""
    def __init__(self, conversations=None, fail=False):
        self.conversations = conversations or []
        self.fail = fail
        self.calls: list[dict] = []

    def list_conversations(self, phone_number_id):
        self.calls.append({"phone_number_id": phone_number_id})
        if self.fail:
            raise RuntimeError("simulated Quo failure")
        return self.conversations


class FakeAgent:
    def __init__(self, quo: FakeQuo):
        self.quo = quo
        self.candidate_states: dict = {}
        self.sheets_writes: list[dict] = []
        self.sheets_should_fail = False

    def _sync_to_sheets(self, cand: dict) -> None:
        if self.sheets_should_fail:
            raise RuntimeError("simulated sheets failure")
        self.sheets_writes.append(dict(cand))


def _iso(days_ago: int) -> str:
    """ISO 8601 timestamp N days ago."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# ── Helper unit tests ───────────────────────────────────────────────────────


class TestNormalizePhone:
    @pytest.mark.parametrize("raw,expected", [
        ("+18132952007", "+18132952007"),
        ("18132952007", "+18132952007"),
        ("8132952007", "+18132952007"),
        ("(813) 295-2007", "+18132952007"),
        ("+44 20 7946 0958", "+442079460958"),
        ("", None),
        (None, None),
    ])
    def test_normalization(self, raw, expected):
        assert _normalize_phone(raw) == expected


class TestParseIso:
    def test_z_suffix(self):
        dt = _parse_iso("2026-08-11T14:32:45Z")
        assert dt == datetime(2026, 8, 11, 14, 32, 45, tzinfo=timezone.utc)

    def test_offset_suffix(self):
        dt = _parse_iso("2026-08-11T14:32:45+00:00")
        assert dt == datetime(2026, 8, 11, 14, 32, 45, tzinfo=timezone.utc)

    def test_none(self):
        assert _parse_iso(None) is None

    def test_invalid(self):
        assert _parse_iso("not a date") is None


# ── Sync behavior ───────────────────────────────────────────────────────────


class TestSyncSmoke:

    def test_new_thread_creates_candidate(self):
        convo = {
            "id": "CN1", "phoneNumberId": "PN1",
            "participants": ["+18132952008"],  # not BK's own number
            "lastActivityAt": _iso(1),
        }
        quo = FakeQuo([convo])
        agent = FakeAgent(quo)
        out = sync_sms_threads(agent, window_days=7, primary_number="+18132952007")
        assert out["ok"] is True
        assert out["created_count"] == 1
        assert out["updated_count"] == 0
        new = agent.candidate_states["+18132952008"]
        assert new["state"] == "sms_engaged"
        assert new["source"] == "sms_sync"

    def test_existing_thread_updates_state(self):
        convo = {
            "id": "CN1", "phoneNumberId": "PN1",
            "participants": ["+18132952008"],
            "lastActivityAt": _iso(1),
        }
        quo = FakeQuo([convo])
        agent = FakeAgent(quo)
        # Seed existing candidate with stale state
        agent.candidate_states["+18132952008"] = {
            "phone": "+18132952008", "name": "John", "state": "new"
        }
        out = sync_sms_threads(agent, window_days=7, primary_number="+18132952007")
        assert out["ok"] is True
        assert out["created_count"] == 0
        assert out["updated_count"] == 1
        # State bumped from "new" to "sms_engaged"
        assert agent.candidate_states["+18132952008"]["state"] == "sms_engaged"

    def test_existing_thread_with_active_state_unchanged(self):
        """If a candidate is already in an active state (e.g. interview_scheduled),
        we do NOT downgrade to sms_engaged — that would lose information."""
        convo = {
            "id": "CN1", "phoneNumberId": "PN1",
            "participants": ["+18132952008"],
            "lastActivityAt": _iso(1),
        }
        quo = FakeQuo([convo])
        agent = FakeAgent(quo)
        agent.candidate_states["+18132952008"] = {
            "phone": "+18132952008", "name": "John", "state": "interview_scheduled"
        }
        out = sync_sms_threads(agent, window_days=7, primary_number="+18132952007")
        assert out["updated_count"] == 1
        # State preserved
        assert agent.candidate_states["+18132952008"]["state"] == "interview_scheduled"
        # But last_sms_activity is bumped
        assert "last_sms_activity" in agent.candidate_states["+18132952008"]

    def test_stale_thread_skipped(self):
        convo = {
            "id": "CN1", "phoneNumberId": "PN1",
            "participants": ["+18132952008"],
            "lastActivityAt": _iso(30),  # 30 days ago
        }
        quo = FakeQuo([convo])
        agent = FakeAgent(quo)
        out = sync_sms_threads(agent, window_days=7, primary_number="+18132952007")
        assert out["in_window"] == 0
        assert out["created_count"] == 0
        # The thread is reported in skipped_sample
        assert any(s["conversation_id"] == "CN1" for s in out["skipped_sample"])

    def test_own_number_only_skipped(self):
        """Thread where only BK's number is on the thread → skipped (no inbound)."""
        convo = {
            "id": "CN1", "phoneNumberId": "PN1",
            "participants": ["+18132952007"],  # BK's own number only
            "lastActivityAt": _iso(1),
        }
        quo = FakeQuo([convo])
        agent = FakeAgent(quo)
        out = sync_sms_threads(agent, window_days=7, primary_number="+18132952007")
        assert out["in_window"] == 1
        assert out["created_count"] == 0
        assert out["skipped_count"] == 1

    def test_phone_normalization(self):
        """Quo returns '+1813...' but candidates may be stored as '1813...'."""
        convo = {
            "id": "CN1", "phoneNumberId": "PN1",
            "participants": ["18132952008"],  # missing + and 1 prefix
            "lastActivityAt": _iso(1),
        }
        quo = FakeQuo([convo])
        agent = FakeAgent(quo)
        # Seed existing candidate with normalized form
        agent.candidate_states["+18132952008"] = {
            "phone": "+18132952008", "name": "John", "state": "interview_scheduled"
        }
        out = sync_sms_threads(agent, window_days=7, primary_number="+18132952007")
        assert out["updated_count"] == 1
        # No new candidate created — normalization matched existing
        assert out["created_count"] == 0

    def test_quo_failure_returns_error(self):
        quo = FakeQuo(fail=True)
        agent = FakeAgent(quo)
        out = sync_sms_threads(agent, window_days=7)
        assert out["ok"] is False
        assert "Quo" in out["error"]

    def test_sheets_sync_failure_does_not_break_sync(self):
        convo = {
            "id": "CN1", "phoneNumberId": "PN1",
            "participants": ["+18132952008"],
            "lastActivityAt": _iso(1),
        }
        quo = FakeQuo([convo])
        agent = FakeAgent(quo)
        agent.sheets_should_fail = True
        out = sync_sms_threads(agent, window_days=7, primary_number="+18132952007")
        # Sync still succeeded for the candidate_state write
        assert out["ok"] is True
        assert out["created_count"] == 1
        # But sheets was never written
        assert agent.sheets_writes == []

    def test_window_days_minimum_is_one(self):
        """If caller passes 0 or negative, clamp to 1."""
        quo = FakeQuo([])
        agent = FakeAgent(quo)
        out = sync_sms_threads(agent, window_days=0)
        assert out["window_days"] == 1
        out = sync_sms_threads(agent, window_days=-5)
        assert out["window_days"] == 1

    def test_passes_phone_number_id_to_quo(self):
        quo = FakeQuo([])
        agent = FakeAgent(quo)
        sync_sms_threads(agent, window_days=7, phone_number_id="PNPGjMWOw8")
        assert quo.calls[0]["phone_number_id"] == "PNPGjMWOw8"

    def test_passes_none_when_no_phone_number_id(self):
        quo = FakeQuo([])
        agent = FakeAgent(quo)
        sync_sms_threads(agent, window_days=7, phone_number_id="")
        # Should pass None or "" — not a missing kwarg
        assert quo.calls  # the call happened

    def test_skipped_truncated_to_20(self):
        """Bad input (1000 stale threads) doesn't blow up the response."""
        convos = [
            {
                "id": f"CN{i}", "phoneNumberId": "PN1",
                "participants": [f"+1813295{i:04d}"],
                "lastActivityAt": _iso(30),
            }
            for i in range(50)
        ]
        quo = FakeQuo(convos)
        agent = FakeAgent(quo)
        out = sync_sms_threads(agent, window_days=7)
        assert out["skipped_count"] == 50
        # Truncated to 20 in the response payload
        assert len(out["skipped_sample"]) == 20

    def test_no_timestamp_skipped(self):
        convo = {
            "id": "CN1", "phoneNumberId": "PN1",
            "participants": ["+18132952008"],
            "lastActivityAt": None,
        }
        quo = FakeQuo([convo])
        agent = FakeAgent(quo)
        out = sync_sms_threads(agent, window_days=7, primary_number="+18132952007")
        assert out["in_window"] == 0
        assert any(s.get("reason") == "no last_activity timestamp" for s in out["skipped_sample"])

    def test_mixed_threads_summary(self):
        """Realistic mix: 5 in window, 3 stale, 1 own-number."""
        convos = [
            {"id": f"C{i}", "phoneNumberId": "PN1", "participants": [f"+1813295{i:04d}"], "lastActivityAt": _iso(2)}
            for i in range(5)
        ] + [
            {"id": f"S{i}", "phoneNumberId": "PN1", "participants": [f"+1813296{i:04d}"], "lastActivityAt": _iso(30)}
            for i in range(3)
        ] + [
            {"id": "OWN", "phoneNumberId": "PN1", "participants": ["+18132952007"], "lastActivityAt": _iso(1)},
        ]
        quo = FakeQuo(convos)
        agent = FakeAgent(quo)
        out = sync_sms_threads(agent, window_days=7, primary_number="+18132952007")
        assert out["total_threads"] == 9
        assert out["in_window"] == 6  # 5 new + 1 own
        assert out["created_count"] == 5  # 5 new
        assert out["skipped_count"] == 4  # 3 stale + 1 own