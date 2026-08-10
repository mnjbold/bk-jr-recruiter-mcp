# Skill - Screening Calls

**Use when:** a candidate is interested and needs qualifying, or BK asks to call someone.

## Procedure
1. Confirm consent - the candidate agreed to a call by SMS, or BK explicitly asked.
2. `get_candidate_by_phone` - confirm identity and current status.
3. `trigger_screening_call` (standard screening) or `retell_place_call` (custom goal).
4. Confirm queued via `list_pending_screenings`.
5. After: `list_recent_screenings` for the outcome, then `update_candidate` (R6).

## Rules
- Never cold-call someone who has not replied to SMS first.
- One call attempt. No answer -> SMS follow-up, not a redial.
- Call outcomes are written back by the Retell webhook (`POST /webhook/retell`);
  if the record is still blank after ~10 min, check `list_recent_screenings`
  before assuming failure.

## Escalate
Candidate asks something the agent cannot answer, or disputes what was said ->
`notify_bk` with the call id.
