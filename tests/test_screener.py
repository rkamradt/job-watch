"""Tests for PR 3: LLM fit scorer, caching, daily cap, remote_us gate, fallback."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from job_watch.db import JobStore, ScoringUsageStore
from job_watch.filter import ScoredJob
from job_watch.models import (
    AppConfig, CompanyConfig, Job, LlmScoringConfig, ScreeningConfig,
)
from job_watch.screener import (
    LlmScoreResponse, ScreenedJob, _keyword_to_llm_score, _parse_response, score_jobs,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_job(
    job_id: str = "j1",
    company: str = "Toast",
    title: str = "Staff Software Engineer",
    location: str = "Remote, US",
    remote: bool = True,
) -> Job:
    return Job(
        id=job_id, company=company, ats="greenhouse",
        title=title, location=location, remote=remote,
        department="Engineering", url=f"https://example.com/{job_id}",
        posted_at=None,
        description_snippet="Kafka Kubernetes event-driven Java platform",
        description="Full description here.",
    )


def _make_scored(job: Job | None = None, keyword_score: int = 3) -> ScoredJob:
    j = job or _make_job()
    return ScoredJob(job=j, score=keyword_score, matched_keywords=["kafka", "java", "kubernetes"])


def _make_store() -> JobStore:
    client = AsyncMongoMockClient()
    store = JobStore.__new__(JobStore)
    store._client = client
    store._col = client["job-watch"]["seen_jobs"]
    from job_watch.db import GmailStateStore, DiscoveredCompanyStore
    store.gmail_state = GmailStateStore(client)
    store.discovered = DiscoveredCompanyStore(client)
    store.scoring_usage = ScoringUsageStore(client)
    return store


def _make_cfgs(threshold: int = 70, daily_cap: int = 200, remote_us_required: bool = True):
    screening = ScreeningConfig(
        enabled=True,
        model="claude-haiku-4-5-20251001",
        candidate_profile="20+ years Java/Kotlin, remote US preferred.",
    )
    scoring = LlmScoringConfig(
        enabled=True,
        threshold=threshold,
        daily_cap=daily_cap,
        model="claude-haiku-4-5-20251001",
        remote_us_required=remote_us_required,
    )
    return screening, scoring


def _mock_llm_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.content = [MagicMock(text=json.dumps(data))]
    resp.usage.input_tokens = 1000
    resp.usage.output_tokens = 100
    return resp


# ── _parse_response ───────────────────────────────────────────────────────────

def test_parse_response_valid():
    data = {"score": 85, "level_match": True, "remote_us": True, "salary_min": 200000, "reason": "great fit"}
    result = _parse_response(json.dumps(data))
    assert result is not None
    assert result.score == 85
    assert result.remote_us is True
    assert result.salary_min == 200000


def test_parse_response_minimal():
    data = {"score": 50, "level_match": False, "reason": "partial fit"}
    result = _parse_response(json.dumps(data))
    assert result is not None
    assert result.remote_us is None
    assert result.salary_min is None


def test_parse_response_markdown_fence():
    data = {"score": 75, "level_match": True, "remote_us": None, "salary_min": None, "reason": "ok"}
    text = f"```json\n{json.dumps(data)}\n```"
    result = _parse_response(text)
    assert result is not None
    assert result.score == 75


def test_parse_response_malformed_returns_none():
    assert _parse_response("not json at all") is None
    assert _parse_response('{"score": "oops"}') is None  # score must be int
    assert _parse_response("") is None


def test_parse_response_score_out_of_range_returns_none():
    data = {"score": 150, "level_match": True, "reason": "too high"}
    assert _parse_response(json.dumps(data)) is None


# ── _keyword_to_llm_score ─────────────────────────────────────────────────────

def test_keyword_fallback_zero():
    assert _keyword_to_llm_score(0) == 0


def test_keyword_fallback_max_caps_at_80():
    assert _keyword_to_llm_score(100) == 80


def test_keyword_fallback_below_threshold():
    # A single keyword match should score below the default threshold of 70
    score = _keyword_to_llm_score(1)
    assert score < 70


# ── score_jobs: happy path ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_jobs_returns_scored_results():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=50)
    scored = [_make_scored()]

    good_resp = _mock_llm_response(
        {"score": 82, "level_match": True, "remote_us": True, "salary_min": None, "reason": "good match"}
    )

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=good_resp)
        mock_cls.return_value = client

        results = await score_jobs(scored, screening, scoring, store)

    assert len(results) == 1
    assert results[0].llm_score == 82
    assert results[0].ai_reason == "good match"
    assert results[0].used_fallback is False


@pytest.mark.asyncio
async def test_score_jobs_sorted_by_score():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=0)
    scored = [
        _make_scored(_make_job("j1"), keyword_score=1),
        _make_scored(_make_job("j2"), keyword_score=2),
    ]

    responses = [
        _mock_llm_response({"score": 60, "level_match": True, "remote_us": True, "salary_min": None, "reason": "ok"}),
        _mock_llm_response({"score": 90, "level_match": True, "remote_us": True, "salary_min": None, "reason": "great"}),
    ]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock(side_effect=responses)
        mock_cls.return_value = client

        results = await score_jobs(scored, screening, scoring, store)

    assert results[0].llm_score >= results[1].llm_score


# ── score_jobs: threshold filter ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_jobs_drops_below_threshold():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=70)
    scored = [_make_scored()]

    low_resp = _mock_llm_response(
        {"score": 45, "level_match": False, "remote_us": True, "salary_min": None, "reason": "poor fit"}
    )

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=low_resp)
        mock_cls.return_value = client

        results = await score_jobs(scored, screening, scoring, store)

    assert results == []


# ── score_jobs: remote_us gate ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_jobs_drops_non_us_remote():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=0, remote_us_required=True)
    scored = [_make_scored()]

    non_us_resp = _mock_llm_response(
        {"score": 80, "level_match": True, "remote_us": False, "salary_min": None, "reason": "non-US remote"}
    )

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=non_us_resp)
        mock_cls.return_value = client

        results = await score_jobs(scored, screening, scoring, store)

    assert results == []


@pytest.mark.asyncio
async def test_score_jobs_keeps_unknown_remote_us():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=0, remote_us_required=True)
    scored = [_make_scored()]

    unknown_resp = _mock_llm_response(
        {"score": 75, "level_match": True, "remote_us": None, "salary_min": None, "reason": "unclear remote"}
    )

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=unknown_resp)
        mock_cls.return_value = client

        results = await score_jobs(scored, screening, scoring, store)

    assert len(results) == 1


# ── score_jobs: retry on malformed JSON ──────────────────────────────────────

@pytest.mark.asyncio
async def test_score_jobs_retries_on_malformed_then_succeeds():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=0)
    scored = [_make_scored()]

    bad_resp = MagicMock()
    bad_resp.content = [MagicMock(text="sorry I cannot score this")]
    bad_resp.usage.input_tokens = 500
    bad_resp.usage.output_tokens = 20

    good_resp = _mock_llm_response(
        {"score": 78, "level_match": True, "remote_us": True, "salary_min": None, "reason": "good on retry"}
    )

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock(side_effect=[bad_resp, good_resp])
        mock_cls.return_value = client

        results = await score_jobs(scored, screening, scoring, store)

    assert len(results) == 1
    assert results[0].llm_score == 78
    assert results[0].used_fallback is False


@pytest.mark.asyncio
async def test_score_jobs_falls_back_when_both_attempts_malformed():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=0)
    scored = [_make_scored(keyword_score=5)]

    bad_resp = MagicMock()
    bad_resp.content = [MagicMock(text="not json")]
    bad_resp.usage.input_tokens = 500
    bad_resp.usage.output_tokens = 10

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=bad_resp)
        mock_cls.return_value = client

        results = await score_jobs(scored, screening, scoring, store)

    assert len(results) == 1
    assert results[0].used_fallback is True
    assert results[0].llm_score == _keyword_to_llm_score(5)


@pytest.mark.asyncio
async def test_score_jobs_falls_back_on_api_error():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=0)
    scored = [_make_scored(keyword_score=4)]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock(side_effect=Exception("API down"))
        mock_cls.return_value = client

        results = await score_jobs(scored, screening, scoring, store)

    assert len(results) == 1
    assert results[0].used_fallback is True


# ── score_jobs: daily cap ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_jobs_respects_daily_cap():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=0, daily_cap=0)  # cap already exceeded
    scored = [_make_scored(keyword_score=6)]

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock()
        mock_cls.return_value = client

        results = await score_jobs(scored, screening, scoring, store)

    # LLM should never have been called
    client.messages.create.assert_not_called()
    assert len(results) == 1
    assert results[0].used_fallback is True


# ── score_jobs: caching ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_jobs_uses_cache_on_second_call():
    store = _make_store()
    screening, scoring = _make_cfgs(threshold=0)
    job = _make_job("cached-job")
    now = datetime.now(tz=timezone.utc)
    await store.upsert(job, now)

    scored = [_make_scored(job)]

    good_resp = _mock_llm_response(
        {"score": 88, "level_match": True, "remote_us": True, "salary_min": None, "reason": "cached result"}
    )

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=good_resp)
        mock_cls.return_value = client

        # First call — hits LLM
        await score_jobs(scored, screening, scoring, store)
        first_call_count = client.messages.create.call_count

        # Second call — should use cache
        await score_jobs(scored, screening, scoring, store)
        second_call_count = client.messages.create.call_count

    assert first_call_count == 1
    assert second_call_count == 1  # no new LLM call


# ── ScoringUsageStore ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scoring_usage_increments():
    client = AsyncMongoMockClient()
    store = ScoringUsageStore(client)

    assert await store.get_today_calls() == 0
    count = await store.record(1000, 100)
    assert count == 1
    count = await store.record(1000, 100)
    assert count == 2
    assert await store.get_today_calls() == 2


# ── llm_score cache in JobStore ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_job_store_llm_score_cache():
    store = _make_store()
    job = _make_job()
    now = datetime.now(tz=timezone.utc)
    await store.upsert(job, now)

    assert await store.get_llm_score(job.ats, job.company, job.id) is None

    score_data = {
        "score": 82, "level_match": True, "remote_us": True,
        "salary_min": None, "reason": "good", "scored_at": now,
        "model": "claude-haiku-4-5-20251001", "used_fallback": False,
    }
    await store.save_llm_score(job.ats, job.company, job.id, score_data)

    cached = await store.get_llm_score(job.ats, job.company, job.id)
    assert cached is not None
    assert cached["score"] == 82
    assert cached["used_fallback"] is False
