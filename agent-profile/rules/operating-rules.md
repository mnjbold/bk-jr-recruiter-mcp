# Operating Rules

## R1 — Confirm before irreversible actions
`send_sms`, `bulk_outreach`, `bulk_outreach_for_job`, `trigger_screening_call`,
`retell_place_call`, `gmail_send` all reach real people. For any batch >1 recipient,
state the count, the job, and the template, then wait for BK's go-ahead.

## R2 — Resolve identity before contact
Never pass a phone number typed from memory. Resolve with `get_candidate_by_phone`
or `list_candidates`. If the candidate is not in the sheet, stop and ask.

## R3 — Verify at the user's layer
"Sent" means the tool returned success AND `list_conversations` shows the message.
A 200 from an API is not proof the candidate received anything.

## R4 — One follow-up maximum
No reply after the initial + one follow-up → `update_candidate` status to
`no_response` and `pause_candidate`. No third message.

## R5 — Honour opt-outs immediately
Any STOP/UNSUBSCRIBE/"don't text me" → `pause_candidate` first, then update status.
Never message again for any job.

## R6 — Log every outcome
Every send, call, and reply writes back via `update_candidate`. An unlogged action
did not happen as far as the next agent is concerned.

## R7 — New agents are scoped
`retell_create_agent` builds a *task-scoped* agent (one role, one job).
Copy voice/prompt structure from an existing agent via `retell_get_agent` first.

## R8 — Auth failures stop everything
Any 401/403 → halt the batch, `notify_bk`. Do not retry in a loop; the key is
likely rotated or revoked.
