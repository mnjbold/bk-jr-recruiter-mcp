"""
Job posting config loader.

Generalizes the recruiting flow beyond a single hardcoded role (Mercury Z
fiber techs). Each job posting is a YAML file in this directory — BK (or any
future client) adds a new role by dropping in a new YAML file, no code change
required. See docs/plans/outbound_telephony_screening_agent_requirements.md
for the full schema rationale.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

JOBS_DIR = Path(__file__).resolve().parent


@dataclass
class JobPosting:
    job_id: str
    title: str
    company: str
    locations: list[str]
    contract_type: str = ""
    duration: str = ""
    schedule: str = ""
    per_diem: bool = False
    requirements: list[str] = field(default_factory=list)
    notes: str = ""
    sms_templates: dict = field(default_factory=dict)
    screening_call: dict = field(default_factory=dict)

    def locations_bullets(self) -> str:
        return "\n".join(f"* {loc}" for loc in self.locations)

    def requirements_bullets(self) -> str:
        return "\n".join(f"* {r}" for r in self.requirements)

    def screening_dynamic_variables(self, candidate: dict) -> dict:
        """
        Build the dynamic_variables payload injected into the Retell agent's
        prompt for this candidate's screening call. Keeping this on the
        JobPosting (not hardcoded in retell_client) is what lets ONE Retell
        agent handle every role/location/category.

        `context` is free-form text from the recruiter (BK) — surfaced to the
        voice agent as `{{context}}` so they can mention anything BK wanted
        passed along (e.g. "BK prefers WhatsApp after 6pm", "candidate is
        nervous — lead with empathy"). Empty string by default; the voice
        agent's prompt is responsible for NOT saying "Special context:" with
        nothing after when this is empty.
        """
        return {
            "candidate_name": candidate.get("name", "there"),
            "job_title": self.title,
            # NOTE: the voice agent's prompt references `{{company}}` (not
            # `{{company_name}}` — that was a latent bug; the LLM fell back to
            # its `default_dynamic_variables.company` value, which happened to
            # be "Bold Business", so BK's Mercury Z role was getting the wrong
            # company in the call). Fixed 2026-08-07.
            "company": self.company,
            "locations": ", ".join(self.locations),
            "candidate_location": candidate.get("location", ", ".join(self.locations)),
            "contract_type": self.contract_type,
            "duration": self.duration,
            "schedule": self.schedule,
            "per_diem": "yes" if self.per_diem else "no",
            "requirements": "; ".join(self.requirements),
            "screening_questions": " | ".join(self.screening_call.get("questions", [])),
            "context": candidate.get("context", "") or "",
        }


def load_job(job_id: str) -> JobPosting:
    path = JOBS_DIR / f"{job_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No job posting found for job_id={job_id!r} at {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return JobPosting(**data)


def list_jobs() -> list[str]:
    return sorted(p.stem for p in JOBS_DIR.glob("*.yaml"))
