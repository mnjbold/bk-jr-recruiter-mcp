"""
Recruitment SMS Flow Engine
Handles all the automation flows for BK's Mercury Z + New Era recruiting.

Flows:
  1. initial_outreach       — First contact for new Mercury Z candidate
  2. interview_confirm      — Confirm interview slot, add to calendar
  3. three_day_followup     — No-show reminder chain
  4. equipment_verify       — Mercury Z equipment check (ladder, fiber tools)
  5. international_outreach — Argentina / offshore candidates
  6. status_update          — BK → candidate via Telegram relay
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

COMPANY_NAME = os.environ.get("COMPANY_NAME", "Mercury Z")
BK_PHONE_NUMBER_ID = os.environ.get("QUO_BK_NUMBER_ID", "")  # Set after first run


# ── Message Templates ─────────────────────────────────────────────────────────

TEMPLATES = {

    # Mercury Z — initial outreach for field technicians
    "mercury_initial": (
        "Hi {name}! This is BK from {company}. "
        "We have a {role} opening in {location}. "
        "Do you have a ladder + fiber test kit available? "
        "Reply YES or NO — or call me directly."
    ),

    # Mercury Z — equipment follow-up if they said YES
    "mercury_equipment_confirm": (
        "Great {name}! Just to confirm — you have:\n"
        "• Extension ladder (20ft+)?\n"
        "• Fiber identifier / OTDR?\n"
        "• Available to start {start_date}?\n"
        "Reply YES to all three or let me know what's missing."
    ),

    # New Era / Bold Business — interview confirmation
    "interview_confirm": (
        "Hi {name}, confirming your interview for the {role} role "
        "on {date} at {time} {timezone}. "
        "Reply CONFIRM to lock it in, or RESCHEDULE if you need a different time."
    ),

    # Interview reminder (2hr before)
    "interview_reminder": (
        "Quick reminder {name} — your interview for {role} is in 2 hours "
        "({time} {timezone}). "
        "See you then! Reply RESCHEDULE if needed."
    ),

    # 3-day no-show follow-up (Day 1)
    "no_show_day1": (
        "Hi {name}, just following up on the {role} opportunity with {company}. "
        "Still interested? Reply YES to keep you in our pipeline."
    ),

    # 3-day no-show follow-up (Day 2)
    "no_show_day2": (
        "Hey {name} — one more check on the {role} role. "
        "We're moving forward with interviews this week. "
        "Reply YES to stay in or NO if you're not available."
    ),

    # 3-day no-show (Day 3 — final)
    "no_show_day3": (
        "Hi {name}, last follow-up from BK at {company}. "
        "We'll keep you in our network for future {role} openings. "
        "Reply INTERESTED anytime you're available."
    ),

    # Argentina / international candidates (bilingual)
    "international_initial": (
        "Hi {name} / Hola {name}! This is BK from Mercury Z. "
        "We have a {role} role in {location}. "
        "Reply YES if interested — English or Spanish is fine."
    ),

    # BK relay: human-typed message through Telegram
    "relay_passthrough": "{message}",

    # Reschedule request
    "reschedule_prompt": (
        "No problem {name}! What day and time works best for you? "
        "(We're flexible Mon–Fri, any timezone)"
    ),

    # Interview rescheduled confirmation
    "reschedule_confirm": (
        "Got it {name} — moving your interview to {new_date} at {new_time}. "
        "You'll get a calendar invite shortly. Any questions?"
    ),
}


def render_template(template_key: str, **kwargs) -> str:
    """Render a message template with variables."""
    template = TEMPLATES.get(template_key, "")
    try:
        return template.format(company=COMPANY_NAME, **kwargs)
    except KeyError as e:
        return template  # return as-is if missing vars


def render_job_template(job, template_key: str, candidate: dict, **extra) -> str:
    """
    Render a template defined on a JobPosting (flows/jobs/*.yaml) instead of
    the hardcoded Mercury Z TEMPLATES dict above. This is the generalized path
    — any job posting can define its own initial/follow-up copy without a
    code change.

    `extra` lets callers inject template vars that aren't in the candidate
    dict (e.g. `call_from_number` for the pre-notice SMS that names the
    number the Retell call is about to come from).
    """
    template = job.sms_templates.get(template_key, "")
    try:
        return template.format(
            name=candidate.get("name", "there"),
            locations_bullets=job.locations_bullets(),
            requirements_bullets=job.requirements_bullets(),
            company=job.company,
            title=job.title,
            **{k: v for k, v in candidate.items() if k not in ("name",)},
            **extra,
        )
    except KeyError:
        return template


# ── Flow State Machine ────────────────────────────────────────────────────────

class FlowState:
    """Tracks where a candidate is in the recruitment flow."""

    STATES = [
        "new",                  # just added, not yet contacted
        "initial_sent",         # first outreach sent
        "equipment_check",      # said YES, waiting on equipment confirm
        "interested",           # confirmed interest + equipment
        "interview_scheduled",  # interview booked
        "interview_confirmed",  # candidate confirmed
        "no_show_d1",           # no reply after day 1
        "no_show_d2",           # no reply after day 2
        "no_show_d3",           # no reply after day 3 → archive
        "declined",             # said NO
        "hired",                # placed
        "future_pool",          # keep for later
        # ── Screening-call states (Retell second-screen after SMS) ──────────
        "screening_call_queued",     # SMS interest confirmed, AI call being placed
        "screening_no_answer",       # call didn't connect / went to voicemail
        "screening_needs_follow_up", # call connected but needs BK's human judgment
        "screening_passed",          # call confirmed requirements + commitment
        "screening_failed",          # call confirmed candidate doesn't qualify
    ]

    @staticmethod
    def next_action(state: str, candidate_reply: str | None = None, job=None) -> dict:
        """
        Given current state + candidate reply, return next action.
        Returns: {action: str, template: str, new_state: str, notify_bk: bool}

        `job` (a flows.jobs.JobPosting) is optional for backward compatibility
        with the original hardcoded Mercury Z flow. When a job is provided and
        job.screening_call["enabled"] is true, a YES reply to the initial
        outreach routes straight to an AI screening call instead of the old
        two-step SMS equipment check — the call itself verifies equipment,
        schedule commitment, and location preference in one pass.
        """
        reply = (candidate_reply or "").upper().strip()
        # NOTE: NO must be evaluated first — "NOT INTERESTED" contains the
        # substring "INTERESTED", so a naive YES-first check misclassifies a
        # decline as an accept. This bit BOTH the pre-existing hardcoded flow
        # below and the job-aware branch until this fix.
        is_no = reply in ("NO", "N", "NOT INTERESTED", "NOT AVAILABLE") or reply.startswith("NO") or "NOT INTERESTED" in reply or "NOT AVAILABLE" in reply
        is_yes = (not is_no) and (
            reply in ("YES", "Y", "INTERESTED", "IM INTERESTED", "I'M INTERESTED")
            or reply.startswith("YES")
            or "INTERESTED" in reply
        )

        if state == "initial_sent" and job is not None and job.screening_call.get("enabled"):
            if is_yes:
                return {
                    "action": "trigger_screening_call",
                    "new_state": "screening_call_queued",
                    "notify_bk": True,
                    "bk_message": "\U0001F4DE {name} ({phone}) replied YES to {role} — placing AI screening call now.",
                }
            elif is_no:
                return {
                    "action": "send_template",
                    "template": "no_show_day3",
                    "new_state": "declined",
                    "notify_bk": True,
                    "bk_message": "\U0001F4F5 {name} ({phone}) declined the {role} role.",
                }
            else:
                return {
                    "action": "escalate_to_bk",
                    "new_state": "initial_sent",
                    "notify_bk": True,
                    "bk_message": "\U0001F4AC {name} ({phone}) replied to your outreach:\n\"{reply}\"\n\nTap to respond or I can handle it.",
                }

        if state == "initial_sent":
            if is_yes:
                return {
                    "action": "send_template",
                    "template": "mercury_equipment_confirm",
                    "new_state": "equipment_check",
                    "notify_bk": False,
                }
            elif reply in ("NO", "N", "NOT INTERESTED", "NOT AVAILABLE") or reply.startswith("NO") or "NOT INTERESTED" in reply or "NOT AVAILABLE" in reply:
                return {
                    "action": "send_template",
                    "template": "no_show_day3",  # graceful close
                    "new_state": "declined",
                    "notify_bk": True,
                    "bk_message": "📵 {name} ({phone}) declined the {role} role.",
                }
            else:
                # Unknown reply — escalate to BK
                return {
                    "action": "escalate_to_bk",
                    "new_state": "initial_sent",  # stay same
                    "notify_bk": True,
                    "bk_message": "💬 {name} ({phone}) replied to your outreach:\n\"{reply}\"\n\nTap to respond or I can handle it.",
                }

        elif state == "equipment_check":
            if reply in ("YES", "Y", "YES TO ALL", "ALL YES"):
                return {
                    "action": "notify_bk_ready",
                    "new_state": "interested",
                    "notify_bk": True,
                    "bk_message": "✅ {name} ({phone}) confirmed equipment for {role}. Ready for interview — want me to send a slot?",
                }
            else:
                return {
                    "action": "escalate_to_bk",
                    "new_state": "equipment_check",
                    "notify_bk": True,
                    "bk_message": "🔧 {name} ({phone}) equipment reply:\n\"{reply}\"\n\nNeeds your judgment.",
                }

        elif state == "interview_scheduled":
            if "CONFIRM" in reply:
                return {
                    "action": "confirm_calendar",
                    "template": None,
                    "new_state": "interview_confirmed",
                    "notify_bk": True,
                    "bk_message": "✅ {name} confirmed interview for {role} on {date}.",
                }
            elif "RESCHEDULE" in reply:
                return {
                    "action": "send_template",
                    "template": "reschedule_prompt",
                    "new_state": "interview_scheduled",
                    "notify_bk": True,
                    "bk_message": "🔄 {name} wants to reschedule {role} interview.",
                }

        # Default: unknown state/reply → escalate
        return {
            "action": "escalate_to_bk",
            "new_state": state,
            "notify_bk": True,
            "bk_message": "💬 {name} ({phone}): \"{reply}\"\n[State: {state}] — needs your input.",
        }

    @staticmethod
    def from_call_result(call_analysis: dict) -> dict:
        """
        Map a Retell `call_analysis` object (from the call_analyzed webhook)
        to a next action. Distinct from next_action() because this is driven
        by call outcome data, not an SMS reply.

        call_analysis fields used (see Retell "Get Call" API):
          - in_voicemail: bool
          - call_successful: bool
          - custom_analysis_data: dict — post_call_analysis_data configured
            on the agent; expected keys per flows/jobs/*.yaml
            `custom_analysis_fields` (e.g. screening_result, has_iphone_11_plus).
        """
        if call_analysis.get("in_voicemail"):
            return {
                "action": "notify_bk",
                "new_state": "screening_no_answer",
                "notify_bk": True,
                "bk_message": "\U0001F4EE {name} ({phone}) — screening call hit voicemail. Retry or hand off manually.",
            }

        custom = call_analysis.get("custom_analysis_data", {}) or {}
        result = (custom.get("screening_result") or "").lower()

        if result == "passed":
            return {
                "action": "notify_bk_ready",
                "new_state": "screening_passed",
                "notify_bk": True,
                "bk_message": "✅ {name} ({phone}) PASSED screening for {role}. Summary: " + call_analysis.get("call_summary", ""),
            }
        if result == "failed":
            return {
                "action": "notify_bk",
                "new_state": "screening_failed",
                "notify_bk": True,
                "bk_message": "❌ {name} ({phone}) did not qualify on the screening call for {role}. Summary: " + call_analysis.get("call_summary", ""),
            }
        if result == "needs_follow_up" or not call_analysis.get("call_successful", True):
            return {
                "action": "escalate_to_bk",
                "new_state": "screening_needs_follow_up",
                "notify_bk": True,
                "bk_message": "❓ {name} ({phone}) screening call needs your judgment. Summary: " + call_analysis.get("call_summary", ""),
            }

        # No structured result at all (post_call_analysis_data not configured
        # yet on the Retell agent) — surface the raw summary so BK can decide.
        return {
            "action": "escalate_to_bk",
            "new_state": "screening_needs_follow_up",
            "notify_bk": True,
            "bk_message": "\U0001F4DE {name} ({phone}) screening call finished — no structured result configured yet. Summary: " + call_analysis.get("call_summary", "(none)"),
        }
