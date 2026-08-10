# Skill - Build an Agent on Demand

**Use when:** BK wants a new voice agent for a specific campaign, role, or task.

## Procedure
1. `retell_list_agents` - check whether one already fits. Reuse beats create.
2. `retell_get_agent` on the closest match - copy its voice_id and prompt structure (R7).
3. Draft the new agent goal in one sentence. Confirm with BK.
4. `retell_create_agent` - scope it to ONE role and ONE job.
5. `retell_get_agent` on the new id to verify it exists and is configured.
6. Test on a known-safe number before any candidate hears it.

## Scoping a good agent
- **Goal:** one sentence. "Confirm availability and truck capacity for {job}."
- **Voice:** match an existing production agent unless BK asks otherwise.
- **Boundaries:** state what it must not promise (pay, start date, hours not in the job record).
- **Exit:** what ends the call - answer captured, voicemail, or refusal.

## Anti-patterns
- A general-purpose "does everything" agent - vague agents produce vague calls.
- Creating a new agent per candidate. Scope per *campaign*, not per person.
