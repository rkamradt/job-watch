"""Claude AI second-pass screener.

After keyword filtering, each matched job is sent to Claude with the
candidate's profile.  Claude returns PASS/FAIL + a one-sentence reason.
Only PASS jobs reach the email digest.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import anthropic

from job_watch.filter import ScoredJob
from job_watch.models import ScreeningConfig

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are screening job postings for a software engineer. You will be given
the candidate's profile and a job posting. Decide whether the role is a
good fit.

Respond with exactly two lines — no preamble, no extra text:
VERDICT: PASS
REASON: one sentence

Use PASS if the role matches the candidate's seniority, technical background,
and location preferences. Use FAIL otherwise."""


@dataclass
class ScreenedJob:
    scored: ScoredJob
    ai_passed: bool
    ai_reason: str


async def _screen_one(
    scored: ScoredJob,
    cfg: ScreeningConfig,
    client: anthropic.AsyncAnthropic,
    sem: asyncio.Semaphore,
) -> ScreenedJob:
    job = scored.job
    user_msg = f"""\
Candidate profile:
{cfg.candidate_profile.strip()}

Job posting:
Company: {job.company}
Title: {job.title}
Location: {job.location or "Not specified"}
Department: {job.department or "Not specified"}

Description:
{job.description_snippet}
"""
    async with sem:
        try:
            resp = await client.messages.create(
                model=cfg.model,
                max_tokens=128,
                system=_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = resp.content[0].text.strip()
            lines = {
                k.strip(): v.strip()
                for line in text.splitlines()
                if ": " in line
                for k, v in [line.split(": ", 1)]
            }
            passed = lines.get("VERDICT", "FAIL").upper() == "PASS"
            reason = lines.get("REASON", "")
            logger.info(
                "Claude screen %s — %s: %s (%s)",
                job.company, job.title,
                "PASS" if passed else "FAIL",
                reason,
            )
            return ScreenedJob(scored=scored, ai_passed=passed, ai_reason=reason)
        except Exception as exc:
            logger.warning(
                "Screening failed for %s — %s: %s; defaulting to PASS",
                job.company, job.title, exc,
            )
            return ScreenedJob(scored=scored, ai_passed=True, ai_reason="(screening unavailable)")


async def screen_jobs(
    jobs: list[ScoredJob],
    cfg: ScreeningConfig,
) -> list[ScreenedJob]:
    """Screen all jobs concurrently; return only PASS results, sorted by score."""
    client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
    sem = asyncio.Semaphore(3)
    tasks = [_screen_one(j, cfg, client, sem) for j in jobs]
    results = await asyncio.gather(*tasks)
    passed = [r for r in results if r.ai_passed]
    passed.sort(key=lambda r: r.scored.score, reverse=True)
    logger.info(
        "AI screening: %d/%d jobs passed", len(passed), len(results)
    )
    return passed
