"""LLM fit scorer — replaces the old PASS/FAIL screener.

For each new job that passes the keyword filter, Claude returns a JSON
fit score (0–100) plus structured fields.  Only jobs at or above the
configured threshold reach the email digest.

Caching:
  A job is never scored twice.  The score is stored on the job document
  in MongoDB (llm_score subdoc) and retrieved from there on subsequent runs.

Daily cap:
  A configurable limit (default 200) prevents runaway API spend.  When the
  cap is reached, remaining jobs fall back to keyword score.

Fallback:
  On malformed JSON, one retry is attempted with an explicit JSON reminder.
  If the retry also fails (or any API error), the keyword score is mapped
  linearly to 0–80 so fallback items are distinguishable from LLM scores.
  The fallback is logged as:  llm_score_fallback: job_id=... reason=...

remote_us gate:
  When LlmScoringConfig.remote_us_required is True, jobs where remote_us=False
  are dropped regardless of score.  This fixes the known bug where "Remote"
  postings from non-US companies slipped through the location filter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import anthropic
from pydantic import BaseModel, Field, ValidationError

from job_watch.filter import ScoredJob
from job_watch.models import LlmScoringConfig, ScreeningConfig

if TYPE_CHECKING:
    from job_watch.db import JobStore

logger = logging.getLogger(__name__)

# ── Pydantic model for LLM response validation ────────────────────────────────

class LlmScoreResponse(BaseModel):
    score: int = Field(ge=0, le=100)
    level_match: bool
    remote_us: bool | None = None
    salary_min: int | None = None
    reason: str

# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are scoring job postings for a software engineer candidate.
You will be given the candidate's profile and a job posting.

Respond with ONLY a JSON object — no markdown, no preamble, no extra text:
{
  "score": <integer 0-100>,
  "level_match": <true if seniority matches candidate's target, false otherwise>,
  "remote_us": <true if US-remote confirmed, false if non-US or on-site only, null if unknown>,
  "salary_min": <integer in USD if a minimum salary range is stated, null otherwise>,
  "reason": "<one sentence explaining the score>"
}

Scoring guide:
  90-100: Exceptional fit — seniority, stack, and remote-US all confirmed
  70-89:  Good fit — most criteria met, minor gaps
  40-69:  Partial fit — seniority or stack mismatch, location unclear
  0-39:   Poor fit — multiple dealbreakers present"""

_RETRY_SUFFIX = "\n\nIMPORTANT: Respond with ONLY the JSON object. No other text."

# ── Dataclass returned to callers ─────────────────────────────────────────────

@dataclass
class ScreenedJob:
    scored: ScoredJob
    llm_score: int           # 0–100 (keyword fallback maps to 0–80)
    ai_reason: str
    level_match: bool
    remote_us: bool | None   # None = unknown
    salary_min: int | None
    used_fallback: bool      # True when keyword score was substituted

# ── Keyword score → fallback LLM score ───────────────────────────────────────

_MAX_KEYWORD_SCORE = 9   # len(boost_keywords) in default config

def _keyword_to_llm_score(keyword_score: int) -> int:
    """Map raw keyword count to 0–80 (always below 90 to distinguish from real LLM scores)."""
    if _MAX_KEYWORD_SCORE == 0:
        return 0
    return min(int(keyword_score / _MAX_KEYWORD_SCORE * 80), 80)

# ── Single-job scorer ─────────────────────────────────────────────────────────

def _build_user_msg(scored: ScoredJob, profile: str, scoring_cfg: LlmScoringConfig) -> str:
    job = scored.job
    dealbreakers = "\n".join(f"  - {d}" for d in scoring_cfg.dealbreakers)
    return f"""\
Candidate profile:
{profile.strip()}

Dealbreakers (auto-fail if present):
{dealbreakers}

Remote US required: {scoring_cfg.remote_us_required}

Job posting:
Company:    {job.company}
Title:      {job.title}
Location:   {job.location or "Not specified"}
Department: {job.department or "Not specified"}

Description:
{job.description_snippet}
"""


async def _call_llm(
    client: anthropic.AsyncAnthropic,
    model: str,
    user_msg: str,
    retry: bool = False,
) -> tuple[str, int, int]:
    """Call Claude, return (raw_text, input_tokens, output_tokens)."""
    content = user_msg + (_RETRY_SUFFIX if retry else "")
    resp = await client.messages.create(
        model=model,
        max_tokens=256,
        system=_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = resp.content[0].text.strip()
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def _parse_response(text: str) -> LlmScoreResponse | None:
    # Strip possible markdown fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(
            l for l in lines if not l.startswith("```")
        ).strip()
    try:
        data = json.loads(cleaned)
        return LlmScoreResponse.model_validate(data)
    except (json.JSONDecodeError, ValidationError):
        return None


async def _score_one(
    scored: ScoredJob,
    screening_cfg: ScreeningConfig,
    scoring_cfg: LlmScoringConfig,
    client: anthropic.AsyncAnthropic,
    sem: asyncio.Semaphore,
    store: "JobStore",
) -> ScreenedJob:
    job = scored.job
    now = datetime.now(tz=timezone.utc)

    # ── Check cache ──────────────────────────────────────────────────────────
    cached = await store.get_llm_score(job.ats, job.company, job.id)
    if cached:
        logger.info(
            "llm_score: cache hit %s — %s (score=%d)",
            job.company, job.title, cached["score"],
        )
        return ScreenedJob(
            scored=scored,
            llm_score=cached["score"],
            ai_reason=cached.get("reason", ""),
            level_match=cached.get("level_match", True),
            remote_us=cached.get("remote_us"),
            salary_min=cached.get("salary_min"),
            used_fallback=cached.get("used_fallback", False),
        )

    # ── Check daily cap ──────────────────────────────────────────────────────
    today_calls = await store.scoring_usage.get_today_calls()
    if today_calls >= scoring_cfg.daily_cap:
        logger.warning(
            "llm_score_fallback: job=%s/%s reason=daily_cap_reached",
            job.company, job.title,
        )
        return _fallback(scored)

    # ── Call LLM ─────────────────────────────────────────────────────────────
    user_msg = _build_user_msg(scored, screening_cfg.candidate_profile, scoring_cfg)
    parsed: LlmScoreResponse | None = None
    total_input = total_output = 0

    async with sem:
        try:
            text, inp, out = await _call_llm(client, scoring_cfg.model, user_msg)
            total_input, total_output = inp, out
            parsed = _parse_response(text)

            if parsed is None:
                # Retry once with explicit JSON reminder
                logger.warning(
                    "llm_score: malformed response for %s — %s; retrying",
                    job.company, job.title,
                )
                text2, inp2, out2 = await _call_llm(
                    client, scoring_cfg.model, user_msg, retry=True
                )
                total_input += inp2
                total_output += out2
                parsed = _parse_response(text2)

        except Exception as exc:
            logger.warning(
                "llm_score_fallback: job=%s/%s reason=api_error error=%s",
                job.company, job.title, exc,
            )
            return _fallback(scored)

    if parsed is None:
        logger.warning(
            "llm_score_fallback: job=%s/%s reason=malformed_json",
            job.company, job.title,
        )
        return _fallback(scored)

    # ── Record usage and cache result ────────────────────────────────────────
    await store.scoring_usage.record(total_input, total_output)

    score_data = {
        "score": parsed.score,
        "level_match": parsed.level_match,
        "remote_us": parsed.remote_us,
        "salary_min": parsed.salary_min,
        "reason": parsed.reason,
        "scored_at": now,
        "model": scoring_cfg.model,
        "used_fallback": False,
    }
    await store.save_llm_score(job.ats, job.company, job.id, score_data)

    logger.info(
        "llm_score: %s — %s → %d (%s)",
        job.company, job.title, parsed.score, parsed.reason,
    )
    return ScreenedJob(
        scored=scored,
        llm_score=parsed.score,
        ai_reason=parsed.reason,
        level_match=parsed.level_match,
        remote_us=parsed.remote_us,
        salary_min=parsed.salary_min,
        used_fallback=False,
    )


def _fallback(scored: ScoredJob) -> ScreenedJob:
    fb_score = _keyword_to_llm_score(scored.score)
    return ScreenedJob(
        scored=scored,
        llm_score=fb_score,
        ai_reason="(scoring unavailable)",
        level_match=True,
        remote_us=None,
        salary_min=None,
        used_fallback=True,
    )

# ── Public API ────────────────────────────────────────────────────────────────

async def score_jobs(
    jobs: list[ScoredJob],
    screening_cfg: ScreeningConfig,
    scoring_cfg: LlmScoringConfig,
    store: "JobStore",
) -> list[ScreenedJob]:
    """Score all jobs concurrently; apply threshold and remote_us gate; sort by score."""
    client = anthropic.AsyncAnthropic()
    sem = asyncio.Semaphore(3)
    tasks = [
        _score_one(s, screening_cfg, scoring_cfg, client, sem, store)
        for s in jobs
    ]
    results: list[ScreenedJob] = list(await asyncio.gather(*tasks))

    # remote_us gate
    if scoring_cfg.remote_us_required:
        before = len(results)
        results = [r for r in results if r.remote_us is not False]
        dropped = before - len(results)
        if dropped:
            logger.info("llm_score: dropped %d job(s) where remote_us=False", dropped)

    # threshold filter
    before = len(results)
    results = [r for r in results if r.llm_score >= scoring_cfg.threshold]
    dropped = before - len(results)
    if dropped:
        logger.info(
            "llm_score: dropped %d job(s) below threshold %d",
            dropped, scoring_cfg.threshold,
        )

    results.sort(key=lambda r: r.llm_score, reverse=True)
    logger.info("llm_score: %d job(s) above threshold %d", len(results), scoring_cfg.threshold)
    return results


# Keep old name as alias so existing callers (main.py) still work during transition
async def screen_jobs(
    jobs: list[ScoredJob],
    cfg: ScreeningConfig,
    store: "JobStore | None" = None,
    scoring_cfg: "LlmScoringConfig | None" = None,
) -> list[ScreenedJob]:
    """Legacy entry point — delegates to score_jobs when llm_scoring is enabled,
    otherwise uses keyword score with a simple PASS gate (score >= 1)."""
    if store is not None and scoring_cfg is not None and scoring_cfg.enabled:
        return await score_jobs(jobs, cfg, scoring_cfg, store)

    # Old PASS/FAIL path kept for backwards compatibility when llm_scoring.enabled=False
    client = anthropic.AsyncAnthropic()
    sem = asyncio.Semaphore(3)

    async def _old_screen(scored: ScoredJob) -> ScreenedJob | None:
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
        old_system = (
            "You are screening job postings for a software engineer. "
            "Respond with exactly two lines:\nVERDICT: PASS\nREASON: one sentence\n"
            "Use PASS if the role matches. Use FAIL otherwise."
        )
        async with sem:
            try:
                resp = await client.messages.create(
                    model=cfg.model, max_tokens=128, system=old_system,
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
                if not passed:
                    return None
                return ScreenedJob(
                    scored=scored, llm_score=scored.score * 10,
                    ai_reason=reason, level_match=True,
                    remote_us=None, salary_min=None, used_fallback=False,
                )
            except Exception as exc:
                logger.warning("Screening failed for %s — %s: %s", job.company, job.title, exc)
                return ScreenedJob(
                    scored=scored, llm_score=scored.score * 10,
                    ai_reason="(screening unavailable)", level_match=True,
                    remote_us=None, salary_min=None, used_fallback=True,
                )

    tasks = [_old_screen(j) for j in jobs]
    raw = list(await asyncio.gather(*tasks))
    passed = [r for r in raw if r is not None]
    passed.sort(key=lambda r: r.llm_score, reverse=True)
    logger.info("AI screening: %d/%d jobs passed", len(passed), len(raw))
    return passed
