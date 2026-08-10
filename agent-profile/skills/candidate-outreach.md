# Skill - Candidate Outreach

**Use when:** BK asks to contact candidates about a job, run a batch, or chase replies.

## Procedure
1. `list_jobs` / `get_job` - confirm the job exists and read its real details.
2. `list_candidates` - get the pool. Never type numbers by hand (R2).
3. Filter: skip anyone paused, already contacted for this job, or opted out.
4. **State the plan and wait** (R1): "N candidates, job X, template Y. Go?"
5. Send: `bulk_outreach_for_job` for a batch, `send_sms` for one.
6. Verify with `list_conversations` - a success return is not delivery (R3).
7. `update_candidate` for every recipient (R6).

## Follow-up
- No reply after ~24h -> one follow-up.
- Still nothing -> `update_candidate` status `no_response`, then `pause_candidate` (R4).

## Handling replies
| Reply | Action |
|---|---|
| Interested | `update_candidate` -> `interested`, then offer a screening call |
| Question about pay/hours | Answer **only** from `get_job`. Unknown -> `notify_bk` |
| Not interested | `update_candidate` -> `declined`, `pause_candidate` |
| STOP / unsubscribe | `pause_candidate` immediately (R5) |
| Angry / legal | `pause_candidate` + `notify_bk` at once |

## Message shape
Two sentences. Name, job, location, one question.
> "Hi {first} - BK team here. Got an IR technician run near {location} starting {start}. Interested?"
