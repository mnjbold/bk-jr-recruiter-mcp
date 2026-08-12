"""
SMS Recruitment Agent
Main agent brain — processes inbound SMS, decides response, executes actions.
Uses Gemini (via GEMINI_API_KEY) as LLM brain. No OpenRouter needed.

Integration points:
  - Quo API: send/receive SMS from BK's existing number
  - Telegram: notify BK of anything requiring human judgment
  - Google Calendar: auto-add confirmed interviews (via existing OAuth)
  - Hermes/OpenClaw: exposed as a tool via /api/tool endpoint
"""
from __future__ import annotations

import os
from datetime import datetime

import httpx
import structlog

from flows.jobs import list_jobs, load_job
from flows.recruitment_flows import FlowState, render_job_template, render_template
from src.notify import notify_bk as notify_channels
from src.quo_client import QuoClient
from src.retell_client import RetellClient

log = structlog.get_logger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
QUO_BK_NUMBER_ID = os.environ.get("QUO_BK_NUMBER_ID", "")  # Set after first setup

RETELL_SCREENING_FROM_NUMBER = os.environ.get("RETELL_SCREENING_FROM_NUMBER", "")  # dedicated number, NOT BK's Quo number
DEFAULT_JOB_ID = os.environ.get("DEFAULT_JOB_ID", "")


class SMSRecruitmentAgent:

    def __init__(self):
        self.quo = QuoClient()
        self.candidate_states: dict[str, dict] = {}  # In-memory; replace with DB

        # Optional integrations — degrade gracefully if not configured yet,
        # so the core SMS flow keeps working even before Retell/Sheets are set up.
        try:
            self.retell = RetellClient()
        except ValueError:
            self.retell = None
            log.warning("retell_not_configured", reason="RETELL_API_KEY not set — screening calls disabled")

        # Sheet writeback goes through Composio (BK's own Google login), not a
        # service account. The client no-ops cleanly until Composio is connected.
        from src.composio_sheets import ComposioSheetsClient
        self.sheets = ComposioSheetsClient()
        if not self.sheets.configured:
            log.warning("sheets_not_configured", reason="Composio not connected — writeback disabled")

    # ── Inbound SMS Handler ───────────────────────────────────────────────────

    def handle_inbound(self, webhook_payload: dict) -> dict:
        """
        Called when Quo fires a webhook for an inbound message.
        Determines candidate state, decides action, executes.
        """
        event = webhook_payload.get("event", "")
        if "message.received" not in event and "message" not in event:
            return {"handled": False, "reason": "not a message event"}

        data = webhook_payload.get("data", {})
        msg_body = data.get("body", data.get("content", ""))
        from_number = data.get("from", {}).get("phoneNumber", "") if isinstance(data.get("from"), dict) else data.get("from", "")
        to_number_id = data.get("phoneNumberId", QUO_BK_NUMBER_ID)
        message_id = data.get("id", "")

        log.info("inbound_sms", from_number=from_number, message=msg_body[:50])

        # Look up candidate state
        candidate = self.candidate_states.get(from_number, {
            "phone": from_number,
            "name": "Candidate",
            "state": "new",
            "role": "Unknown",
            "location": "Unknown",
        })

        current_state = candidate.get("state", "new")

        # Resolve the job posting this candidate belongs to (falls back to the
        # original hardcoded Mercury Z flow if no job_id is set).
        job = None
        job_id = candidate.get("job_id") or DEFAULT_JOB_ID
        if job_id:
            try:
                job = load_job(job_id)
            except FileNotFoundError:
                log.warning("job_not_found", job_id=job_id)

        # Determine next action
        next_action = FlowState.next_action(current_state, msg_body, job=job)
        log.info("flow_decision", state=current_state, action=next_action.get("action"))

        # Execute action
        result = self._execute_action(
            action=next_action,
            candidate=candidate,
            reply_text=msg_body,
            to_number_id=to_number_id,
            job=job,
        )

        # Update state
        candidate["state"] = next_action.get("new_state", current_state)
        self.candidate_states[from_number] = candidate
        self._sync_to_sheets(candidate)

        return {"handled": True, "action": next_action.get("action"), "result": result}

    def _execute_action(self, action: dict, candidate: dict, reply_text: str, to_number_id: str, job=None) -> dict:
        action_type = action.get("action", "")
        phone = candidate.get("phone", "")
        name = candidate.get("name", "Candidate")
        role = candidate.get("role", "role")

        if action_type == "send_template":
            text = render_template(
                action["template"],
                name=name,
                role=role,
                location=candidate.get("location", "your area"),
                date=candidate.get("interview_date", "TBD"),
                time=candidate.get("interview_time", "TBD"),
                timezone="EST",
                new_date=candidate.get("new_date", "TBD"),
                new_time=candidate.get("new_time", "TBD"),
                start_date=candidate.get("start_date", "next week"),
            )
            msg = self.quo.send_sms(to_number_id, phone, text)
            return {"sent": text, "message_id": msg.get("id")}

        elif action_type in ("escalate_to_bk", "notify_bk_ready", "notify_bk"):
            bk_msg = action.get("bk_message", "").format(
                name=name, phone=phone, role=role,
                reply=reply_text, state=candidate.get("state"),
                date=candidate.get("interview_date", "TBD"),
            )
            self._notify(bk_msg)
            return {"notified_bk": bk_msg}

        elif action_type == "confirm_calendar":
            # Would hook into Google Calendar API
            bk_msg = action.get("bk_message", "").format(
                name=name, phone=phone, role=role,
                date=candidate.get("interview_date", "TBD"),
            )
            self._notify(bk_msg)
            return {"calendar_action": "confirmed", "notified_bk": True}

        elif action_type == "trigger_screening_call":
            return self.trigger_screening_call(candidate, job)

        return {"unhandled": action_type}

    # ── Screening calls (Retell) ─────────────────────────────────────────────

    def trigger_screening_call(self, candidate: dict, job) -> dict:
        """Place the outbound AI screening call from the DEDICATED Retell number.

        Two-number strategy (spam-flag-safe):
          1. Send a pre-notice SMS from BK's Quo number, naming the number the
             call is about to come from (so the candidate picks up).
          2. Wait pre_notice_delay_seconds (default 10s) for the SMS to deliver.
          3. Place the Retell call from the dedicated Retell number.
          4. Optional post-call courtesy SMS (template: screening_call_followup).
        """
        if not self.retell:
            log.error("screening_call_skipped", reason="Retell not configured")
            return {"error": "Retell not configured (set RETELL_API_KEY)"}
        if not RETELL_SCREENING_FROM_NUMBER:
            log.error("screening_call_skipped", reason="RETELL_SCREENING_FROM_NUMBER not set")
            return {"error": "RETELL_SCREENING_FROM_NUMBER not set — never reuse BK's Quo number for this"}
        if job is None:
            return {"error": "No job posting resolved for this candidate — cannot build screening prompt"}

        # 1. Pre-notice SMS (tells the candidate what number is about to ring)
        pre_notice = job.sms_templates.get("screening_call_pre_notice")
        if pre_notice:
            try:
                number_id = QUO_BK_NUMBER_ID or self.quo.get_default_number_id()
                text = render_job_template(
                    job, "screening_call_pre_notice", candidate,
                    call_from_number=RETELL_SCREENING_FROM_NUMBER,
                )
                pre_msg = self.quo.send_sms(number_id, candidate["phone"], text)
                log.info("screening_pre_notice_sent",
                         to=candidate["phone"], message_id=pre_msg.get("id"),
                         from_quo_number=number_id, call_will_come_from=RETELL_SCREENING_FROM_NUMBER)
            except Exception as e:
                log.warning("screening_pre_notice_sms_failed",
                            to=candidate["phone"], error=str(e),
                            note="continuing to place the call anyway — better to call without notice than skip")

        # 2. Pause so the SMS has time to deliver before the phone rings.
        delay = int(job.screening_call.get("pre_notice_delay_seconds", 10) or 0)
        if delay > 0:
            import time as _time
            log.info("screening_pre_notice_delay", seconds=delay, to=candidate["phone"])
            _time.sleep(delay)

        # 3. Place the call
        dynamic_vars = job.screening_dynamic_variables(candidate)
        agent_id = job.screening_call.get("agent_id")
        voice_id = job.screening_call.get("voice_id")
        result = self.retell.create_phone_call(
            from_number=RETELL_SCREENING_FROM_NUMBER,
            to_number=candidate["phone"],
            agent_id=agent_id if agent_id and "REPLACE_WITH" not in agent_id else None,
            dynamic_variables=dynamic_vars,
            metadata={"job_id": job.job_id, "candidate_phone": candidate["phone"]},
            agent_override={"voice_id": voice_id} if voice_id else None,
        )

        # 4. Optional post-call courtesy SMS (threaded from the same Quo number
        #    so the candidate has a paper trail of the conversation).
        followup = job.sms_templates.get("screening_call_followup")
        if followup:
            try:
                number_id = QUO_BK_NUMBER_ID or self.quo.get_default_number_id()
                text = render_job_template(job, "screening_call_followup", candidate)
                self.quo.send_sms(number_id, candidate["phone"], text)
            except Exception as e:
                log.warning("screening_followup_sms_failed", error=str(e))

        return {"call_id": result.get("call_id"), "status": result.get("call_status"),
                "pre_notice_sent": bool(pre_notice)}

    def trigger_followup_call(self, args: dict) -> dict:
        """
        Entry point for the Retell CHAT agent's `trigger_followup_call` custom
        function (POSTed to /api/trigger-call). The candidate may not be in the
        in-memory state dict (wiped on restart), so build one from the args the
        chat agent extracted, resolve the job, then place the screening call.
        """
        # Accept every shape the chat agent (or any other caller) might send.
        # The dashboard tool spec uses `phone_number`; older tests / direct curl
        # sometimes send `candidate_phone` or `phone` or `to_number`.
        phone = (
            args.get("phone_number")
            or args.get("candidate_phone")
            or args.get("phone")
            or args.get("to_number")
        )
        if not phone:
            return {"error": "no candidate phone provided"}

        existing = self.candidate_states.get(phone, {})
        candidate = {
            **existing,
            "phone": phone,
            "name": args.get("candidate_name") or args.get("name") or existing.get("name", "there"),
            "location": args.get("location") or existing.get("location", ""),
            # Free-form context for the voice agent — BK's chat agent (or any
            # other caller) can pass talking points, special notes, empathy
            # flags, etc. Surfaces as `{{context}}` in the LLM prompt. Empty
            # string by default; the prompt handles "no context" gracefully.
            "context": args.get("context") or args.get("screening_context")
                       or args.get("notes") or existing.get("context", "") or "",
        }

        job_id = args.get("job_id") or existing.get("job_id") or DEFAULT_JOB_ID
        if not job_id:
            jobs = list_jobs()
            job_id = jobs[0] if len(jobs) == 1 else None
        if not job_id:
            return {"error": "no job_id resolved and multiple jobs exist — pass job_id"}
        try:
            job = load_job(job_id)
        except FileNotFoundError:
            return {"error": f"job {job_id!r} not found"}

        candidate["job_id"] = job_id
        candidate.setdefault("role", job.title)
        self.candidate_states[phone] = candidate
        return self.trigger_screening_call(candidate, job)

    def trigger_batch_screening_calls(self, candidates: list[dict], job, name: str | None = None,
                                      call_time_window: dict | None = None) -> dict:
        """Scheduled/batch screening campaign via Retell create-batch-call.

        call_time_window enforces quiet hours, e.g.
        {"start": "09:00", "end": "18:00", "timezone": "America/New_York"}.
        """
        if not self.retell:
            return {"error": "Retell not configured (set RETELL_API_KEY)"}
        if not RETELL_SCREENING_FROM_NUMBER:
            return {"error": "RETELL_SCREENING_FROM_NUMBER not set — never reuse BK's Quo number for this"}
        if job is None:
            return {"error": "No job posting resolved for these candidates — cannot build screening prompt"}

        tasks = [
            {
                "to_number": c["phone"],
                "retell_llm_dynamic_variables": {k: str(v) for k, v in job.screening_dynamic_variables(c).items()},
                "metadata": {"job_id": job.job_id, "candidate_phone": c["phone"]},
            }
            for c in candidates
        ]
        result = self.retell.create_batch_call(
            from_number=RETELL_SCREENING_FROM_NUMBER,
            tasks=tasks,
            name=name or f"screening-{job.job_id}",
            call_time_window=call_time_window,
        )
        return {"batch_call_id": result.get("batch_call_id"), "count": len(tasks)}

    def handle_retell_webhook(self, payload: dict) -> dict:
        """Handle call_started / call_ended / call_analyzed events from Retell."""
        event = payload.get("event", "")
        call = payload.get("call", payload)  # some payload shapes nest under "call"
        to_number = call.get("to_number", "")
        metadata = call.get("metadata", {}) or {}
        phone = metadata.get("candidate_phone") or to_number

        candidate = self.candidate_states.get(phone)
        if not candidate:
            log.warning("retell_webhook_unknown_candidate", phone=phone, evt=event)
            return {"handled": False, "reason": "unknown candidate"}

        if event != "call_analyzed":
            log.info("retell_webhook_ignored", evt=event, phone=phone)
            return {"handled": True, "event": event, "ignored": True}

        call_analysis = call.get("call_analysis", {}) or {}
        next_action = FlowState.from_call_result(call_analysis)

        bk_msg = next_action.get("bk_message", "").format(
            name=candidate.get("name", "Candidate"),
            phone=phone,
            role=candidate.get("role", "role"),
        )
        if next_action.get("notify_bk"):
            self._notify(bk_msg)

        cad = call_analysis.get("custom_analysis_data", {}) or {}
        candidate["state"] = next_action.get("new_state", candidate.get("state"))
        candidate["last_call_summary"] = call_analysis.get("call_summary", "")
        candidate["last_call_result"] = cad.get("screening_result", "")
        candidate["interested"] = cad.get("interested")
        candidate["callback_time"] = cad.get("callback_time", "")
        self.candidate_states[phone] = candidate
        self._sync_to_sheets(candidate)

        return {"handled": True, "new_state": candidate["state"]}

    # ── Sheets sync (best-effort — never breaks the SMS/call flow) ──────────

    def _sync_to_sheets(self, candidate: dict) -> None:
        if not self.sheets or not self.sheets.configured:
            return
        try:
            self.sheets.upsert_candidate(candidate["phone"], {
                "status": candidate.get("state", ""),
                "screening_result": candidate.get("last_call_result", ""),
                "interested": candidate.get("interested", ""),
                "callback_time": candidate.get("callback_time", ""),
                "summary": candidate.get("last_call_summary", ""),
            })
        except Exception as e:
            log.error("sheets_sync_failed", phone=candidate.get("phone"), error=str(e))

    def _notify(self, text: str) -> dict:
        return notify_channels(text)

    # ── Outbound: BK sends via Telegram relay ─────────────────────────────────

    def relay_from_bk(self, to_phone: str, message: str, from_number_id: str | None = None) -> dict:
        """
        BK types a message in Telegram → agent sends it from his Quo number.
        This is the 'invisible assistant' mode — candidates just see BK's number.
        """
        number_id = from_number_id or QUO_BK_NUMBER_ID or self.quo.get_default_number_id()
        if not number_id:
            return {"error": "No phone number ID available. Set QUO_BK_NUMBER_ID."}
        msg = self.quo.send_sms(number_id, to_phone, message)
        log.info("relay_sent", to=to_phone, via="quo", message_id=msg.get("id"))
        return {"sent": True, "message_id": msg.get("id"), "to": to_phone}

    # ── Bulk outreach ─────────────────────────────────────────────────────────

    def send_bulk_outreach(self, candidates: list[dict], template_key: str, **kwargs) -> list[dict]:
        """
        Legacy path — sends a hardcoded TEMPLATES entry (Mercury Z style).
        candidates: [{"name": "Juan", "phone": "+54911...", "role": "Fiber Tech", "location": "Argentina"}]
        Prefer send_bulk_outreach_for_job() for anything using flows/jobs/*.yaml.
        """
        results = []
        number_id = QUO_BK_NUMBER_ID or self.quo.get_default_number_id()

        for c in candidates:
            text = render_template(
                template_key,
                name=c.get("name", ""),
                role=c.get("role", "Technician"),
                location=c.get("location", ""),
                **kwargs,
            )
            try:
                msg = self.quo.send_sms(number_id, c["phone"], text)
                # Track state
                self.candidate_states[c["phone"]] = {
                    **c,
                    "state": "initial_sent",
                    "initial_sent_at": datetime.utcnow().isoformat(),
                }
                results.append({"phone": c["phone"], "status": "sent", "message_id": msg.get("id")})
                log.info("bulk_outreach_sent", to=c["phone"], name=c.get("name"))
            except Exception as e:
                results.append({"phone": c["phone"], "status": "error", "error": str(e)})
                log.error("bulk_outreach_failed", to=c["phone"], error=str(e))

        sent_count = sum(1 for r in results if r["status"] == "sent")
        self._notify(f"📤 Bulk outreach complete: {sent_count}/{len(candidates)} sent for {kwargs.get('role', 'role')} in {kwargs.get('location', '')}")
        return results

    def send_bulk_outreach_for_job(
        self,
        candidates: list[dict],
        job_id: str,
        skip_if_contacted: bool = True,
        dry_run: bool = False,
        max_per_run: int | None = None,
        delay_seconds: float = 0.2,
    ) -> list[dict]:
        """
        Generalized path — any job posting in flows/jobs/*.yaml, any role/
        location/category. This is what an NLP command like "text everyone
        for the Fiber I&R role" should call.

        candidates: [{"name": "...", "phone": "+1...", "location": "..." (optional)}]

        skip_if_contacted: dedup gate — skips anyone already tracked in
            candidate_states OR who has ANY prior message on Quo (existence
            check, not full history — cheap at 1000+-candidate scale). This is
            the "check messaging history before re-contacting" requirement.
        dry_run: renders the message and runs the dedup check but does NOT
            call Quo — use this to produce a preview for BK/Mel to approve
            before a real send (no calls placed, no texts sent, no state changes).
        max_per_run: hard cap for a controlled pilot batch (e.g. 50) instead of
            blasting the whole list — recommended for the first run of any new
            job/campaign before trusting it at full volume (1000+/session).
        delay_seconds: throttle between sends so we don't trip Quo's rate limit
            on a large batch.
        """
        import time as _time

        job = load_job(job_id)
        results = []
        number_id = QUO_BK_NUMBER_ID or self.quo.get_default_number_id()
        sent_count = 0

        for c in candidates:
            phone = c["phone"]

            if max_per_run is not None and sent_count >= max_per_run:
                results.append({"phone": phone, "status": "skipped", "reason": "max_per_run cap reached"})
                continue

            if skip_if_contacted:
                tracked = self.candidate_states.get(phone)
                if tracked and tracked.get("state", "new") != "new":
                    results.append({"phone": phone, "status": "skipped", "reason": f"already tracked (state={tracked['state']})"})
                    continue
                try:
                    if self.quo.has_any_message(number_id, phone):
                        results.append({"phone": phone, "status": "skipped", "reason": "prior message history on Quo"})
                        continue
                except Exception as e:
                    log.warning("dedup_check_failed", phone=phone, error=str(e))
                    # fail open — don't block the whole run over one lookup error,
                    # but log it so it's visible in the summary/notify.

            text = render_job_template(job, "initial", c)

            if dry_run:
                results.append({"phone": phone, "status": "preview", "text": text})
                continue

            try:
                msg = self.quo.send_sms(number_id, phone, text)
                self.candidate_states[phone] = {
                    **c,
                    "role": job.title,
                    "job_id": job.job_id,
                    "state": "initial_sent",
                    "initial_sent_at": datetime.utcnow().isoformat(),
                }
                self._sync_to_sheets(self.candidate_states[phone])
                results.append({"phone": phone, "status": "sent", "message_id": msg.get("id")})
                sent_count += 1
                log.info("bulk_outreach_sent", to=phone, name=c.get("name"), job_id=job_id)
            except Exception as e:
                results.append({"phone": phone, "status": "error", "error": str(e)})
                log.error("bulk_outreach_failed", to=phone, error=str(e))

            if delay_seconds:
                _time.sleep(delay_seconds)

        if not dry_run:
            skipped = sum(1 for r in results if r["status"] == "skipped")
            self._notify(
                f"📤 Bulk outreach complete: {sent_count}/{len(candidates)} sent "
                f"({skipped} skipped as already-contacted) for {job.title} ({', '.join(job.locations)})"
            )
        return results

    # ── AI Brain (Gemini) — for ambiguous replies ─────────────────────────────

    def classify_reply_with_ai(self, candidate_name: str, candidate_reply: str, context: str = "") -> str:
        """
        Use Gemini to classify ambiguous candidate replies.
        Returns: "interested" | "declined" | "reschedule" | "question" | "unknown"
        """
        if not GEMINI_API_KEY:
            return "unknown"

        prompt = f"""You are classifying a candidate's SMS reply in a recruitment context.
Candidate name: {candidate_name}
Context: {context}
Their reply: "{candidate_reply}"

Classify as ONE of: interested, declined, reschedule, question, unknown
Reply with just the single word."""

        try:
            resp = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=10,
            )
            result = resp.json()
            classification = result["candidates"][0]["content"]["parts"][0]["text"].strip().lower()
            return classification if classification in ("interested", "declined", "reschedule", "question", "unknown") else "unknown"
        except Exception as e:
            log.error("ai_classify_failed", error=str(e))
            return "unknown"
