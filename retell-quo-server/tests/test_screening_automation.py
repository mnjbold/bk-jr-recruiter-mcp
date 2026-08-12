"""
Tests for process_screening_result — the post-screening automation.

Verifies:
  1. Validation: bad screening_result → error
  2. Validation: missing phone AND no call_id → error
  3. PASSED branch: state updated, SMS sent, BK notified, GCal created,
     Drive folder created, Gmail sent
  4. NEEDS_FOLLOW_UP branch: state updated, personalized SMS based on
     which field failed, candidate paused, BK notified
  5. FAILED branch: state updated, candidate paused permanently
  6. Idempotency: same call_id twice → second time actions_skipped
  7. Provider failures: SMS fails → other actions still execute

These tests use a FakeAgent (no real I/O). They do NOT cover the
Retell get_call path (network) — that path is exercised by the live
call_analyzed webhook in production.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from screening_automation import (
    VALID_RESULTS,
    _followup_message,
    process_screening_result,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


class FakeAgent:
    """In-memory stand-in for the real SMSRecruitmentAgent."""
    def __init__(self):
        self.candidate_states: dict = {}
        self.notifications: list[str] = []
        self.sheets_writes: list[dict] = []

    def _notify(self, msg: str) -> None:
        self.notifications.append(msg)

    def _sync_to_sheets(self, cand: dict) -> None:
        self.sheets_writes.append(dict(cand))

    def pause_candidate(self, phone: str, reason: str = "") -> dict:
        cand = self.candidate_states.get(phone, {"phone": phone})
        cand["paused"] = True
        cand["pause_reason"] = reason
        self.candidate_states[phone] = cand
        return cand


class FakeG:
    """Stand-in for ComposioGoogleClient. Records every call."""
    def __init__(self):
        self.sms: list[dict] = []
        self.gcal: list[dict] = []
        self.drive: list[dict] = []
        self.gmail: list[dict] = []
        self.fail_sms = False
        self.fail_gcal = False

    def send_sms(self, to: str, message: str, from_number_id: str | None = None) -> dict:
        if self.fail_sms:
            return {"ok": False, "error": "simulated SMS failure"}
        self.sms.append({"to": to, "message": message, "from_number_id": from_number_id})
        return {"ok": True, "message_id": f"msg_{len(self.sms)}"}

    def gcal_create_event(self, summary: str, start: str, end: str, attendees=None, description=""):
        if self.fail_gcal:
            raise RuntimeError("simulated GCal failure")
        self.gcal.append({"summary": summary, "start": start, "end": end, "description": description})
        return {"ok": True, "event_id": f"evt_{len(self.gcal)}"}

    def gdrive_create_folder(self, name: str, parent_id: str = "") -> dict:
        self.drive.append({"name": name, "parent_id": parent_id})
        return {"ok": True, "folder_id": f"folder_{len(self.drive)}"}

    def gmail_send(self, to: str, subject: str, body: str) -> dict:
        self.gmail.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "message_id": f"mail_{len(self.gmail)}"}


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def g():
    return FakeG()


@pytest.fixture
def candidate():
    return {
        "phone": "+18132952007",
        "name": "John Doe",
        "state": "screening_call_queued",
    }


# ── Validation ──────────────────────────────────────────────────────────────


class TestValidation:
    def test_invalid_screening_result(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        out = process_screening_result(agent, {"phone": candidate["phone"], "screening_result": "maybe"}, g)
        assert out["ok"] is False
        assert "invalid" in out["error"]
        assert "maybe" in out["error"]

    def test_missing_phone_no_call_id(self, agent, g):
        out = process_screening_result(
            agent, {"screening_result": "passed"}, g
        )
        assert out["ok"] is False
        assert "phone" in out["error"].lower() or "call_id" in out["error"].lower()

    def test_valid_results_constant(self):
        assert VALID_RESULTS == ("passed", "needs_follow_up", "failed")


# ── PASSED branch ───────────────────────────────────────────────────────────


class TestPassedBranch:

    def test_state_updated(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "passed",
            "custom_analysis_fields": {"preferred_location": "Alachua, FL"},
        }, g)
        c = agent.candidate_states[candidate["phone"]]
        assert c["state"] == "screening_passed"
        assert c["screening_result"] == "passed"
        assert c["preferred_location"] == "Alachua, FL"

    def test_sms_sent(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "passed",
        }, g)
        assert len(g.sms) == 1
        assert g.sms[0]["to"] == candidate["phone"]
        assert "passed" in g.sms[0]["message"].lower()

    def test_bk_notified(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "passed",
            "custom_analysis_fields": {"preferred_location": "Granite Quarry, NC"},
        }, g)
        assert len(agent.notifications) == 1
        assert "John Doe" in agent.notifications[0]
        assert "Granite Quarry" in agent.notifications[0]

    def test_gcal_event_created(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "passed",
        }, g)
        assert len(g.gcal) == 1
        assert "John Doe" in g.gcal[0]["summary"]
        assert "Fiber I&R" in g.gcal[0]["summary"]

    def test_drive_folder_created(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "passed",
        }, g)
        assert len(g.drive) == 1
        assert "John Doe" in g.drive[0]["name"]

    def test_gmail_packet_sent(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "passed",
        }, g)
        assert len(g.gmail) == 1
        assert "John Doe" in g.gmail[0]["subject"]

    def test_sheets_synced(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "passed",
        }, g)
        assert len(agent.sheets_writes) >= 1
        last = agent.sheets_writes[-1]
        assert last["state"] == "screening_passed"


# ── NEEDS_FOLLOW_UP branch ──────────────────────────────────────────────────


class TestNeedsFollowUpBranch:

    def test_state_updated(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "needs_follow_up",
            "custom_analysis_fields": {"has_iphone_11_plus": False},
        }, g)
        c = agent.candidate_states[candidate["phone"]]
        assert c["state"] == "screening_needs_follow_up"
        assert c["failed_field"] == "has_iphone_11_plus"

    @pytest.mark.parametrize("failed_field,expected_substr", [
        ("has_iphone_11_plus", "iPhone"),
        ("has_vehicle_with_ladder_rack", "ladder rack"),
        ("can_commit_schedule", "6 days/week"),
    ])
    def test_sms_personalized_by_failed_field(
        self, agent, g, candidate, failed_field, expected_substr
    ):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "needs_follow_up",
            "custom_analysis_fields": {failed_field: False},
        }, g)
        assert len(g.sms) == 1
        assert expected_substr in g.sms[0]["message"]

    def test_candidate_paused(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "needs_follow_up",
            "custom_analysis_fields": {"has_iphone_11_plus": False},
        }, g)
        assert agent.candidate_states[candidate["phone"]]["paused"] is True
        assert "needs_follow_up" in agent.candidate_states[candidate["phone"]]["pause_reason"]

    def test_bk_notified_with_failed_field(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "needs_follow_up",
            "custom_analysis_fields": {"has_iphone_11_plus": False},
        }, g)
        assert "has_iphone_11_plus" in agent.notifications[0]


# ── FAILED branch ───────────────────────────────────────────────────────────


class TestFailedBranch:

    def test_state_updated(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "failed",
            "custom_analysis_fields": {"has_vehicle_with_ladder_rack": False},
        }, g)
        c = agent.candidate_states[candidate["phone"]]
        assert c["state"] == "screening_failed"
        assert c["screening_result"] == "failed"
        assert c["failed_field"] == "has_vehicle_with_ladder_rack"

    def test_candidate_paused_permanently(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "failed",
            "custom_analysis_fields": {"has_iphone_11_plus": False},
        }, g)
        c = agent.candidate_states[candidate["phone"]]
        assert c["paused"] is True
        assert c["pause_reason"] == "screening_failed"

    def test_no_sms_on_failed(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "failed",
        }, g)
        assert len(g.sms) == 0

    def test_no_gcal_on_failed(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "failed",
        }, g)
        assert len(g.gcal) == 0


# ── Idempotency ─────────────────────────────────────────────────────────────


class TestIdempotency:

    def test_same_call_id_runs_once(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        params = {
            "phone": candidate["phone"],
            "call_id": "call_abc123",
            "screening_result": "passed",
        }
        # First run: all actions taken
        r1 = process_screening_result(agent, params, g)
        assert len(r1["actions_taken"]) >= 3
        assert len(r1["actions_skipped"]) == 0
        first_sms_count = len(g.sms)
        first_gcal_count = len(g.gcal)
        assert first_sms_count >= 1
        assert first_gcal_count >= 1

        # Second run: same call_id → actions skipped
        r2 = process_screening_result(agent, params, g)
        assert len(r2["actions_taken"]) == 0
        assert len(r2["actions_skipped"]) >= 3
        # No duplicate side effects
        assert len(g.sms) == first_sms_count
        assert len(g.gcal) == first_gcal_count

    def test_different_call_ids_run_independently(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        r1 = process_screening_result(agent, {
            "phone": candidate["phone"],
            "call_id": "call_001",
            "screening_result": "passed",
        }, g)
        r2 = process_screening_result(agent, {
            "phone": candidate["phone"],
            "call_id": "call_002",
            "screening_result": "passed",
        }, g)
        # Both took actions because different call_ids
        assert len(r1["actions_taken"]) >= 3
        assert len(r2["actions_taken"]) >= 3

    def test_response_includes_idempotency_key(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        out = process_screening_result(agent, {
            "phone": candidate["phone"],
            "call_id": "call_xyz",
            "screening_result": "passed",
        }, g)
        assert out["idempotency_key"] == "call_xyz"


# ── Provider failure isolation ──────────────────────────────────────────────


class TestProviderFailureIsolation:

    def test_sms_failure_does_not_break_others(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        g.fail_sms = True
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "passed",
        }, g)
        # State still updated, BK still notified, GCal still created
        c = agent.candidate_states[candidate["phone"]]
        assert c["state"] == "screening_passed"
        assert len(agent.notifications) == 1
        assert len(g.gcal) == 1

    def test_gcal_failure_does_not_break_others(self, agent, g, candidate):
        agent.candidate_states[candidate["phone"]] = candidate
        g.fail_gcal = True
        process_screening_result(agent, {
            "phone": candidate["phone"],
            "screening_result": "passed",
        }, g)
        # State, SMS, BK notification, Gmail all still ran
        c = agent.candidate_states[candidate["phone"]]
        assert c["state"] == "screening_passed"
        assert len(g.sms) == 1
        assert len(agent.notifications) == 1
        assert len(g.gmail) == 1


# ── Helpers ─────────────────────────────────────────────────────────────────


class TestFollowupMessage:

    def test_iphone_failure(self):
        msg = _followup_message("Jane", "has_iphone_11_plus")
        assert "Jane" in msg
        assert "iPhone" in msg

    def test_vehicle_failure(self):
        msg = _followup_message("Bob", "has_vehicle_with_ladder_rack")
        assert "Bob" in msg
        assert "ladder" in msg.lower()

    def test_schedule_failure(self):
        msg = _followup_message("Alice", "can_commit_schedule")
        assert "Alice" in msg
        assert "schedule" in msg.lower() or "6 days" in msg

    def test_unknown_field_falls_back_to_generic(self):
        msg = _followup_message("Pat", "some_unknown_field")
        assert "Pat" in msg
        # Generic message still mentions the role
        assert "Mercury Z" in msg

    def test_blank_name_uses_there(self):
        msg = _followup_message("", "has_iphone_11_plus")
        assert "there" in msg.lower()