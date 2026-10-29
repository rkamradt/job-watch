"""Tests for PR 4: digest discovered-companies section and score rendering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

from job_watch.db import DiscoveredCompanyStore
from job_watch.filter import ScoredJob
from job_watch.models import Job
from job_watch.notify import _build_discovered_html, _build_discovered_plain, _build_plain, _build_html
from job_watch.screener import ScreenedJob


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_screened(score: int = 82, used_fallback: bool = False) -> ScreenedJob:
    job = Job(
        id="j1", company="Toast", ats="greenhouse",
        title="Staff Software Engineer", location="Remote, US", remote=True,
        department="Engineering", url="https://example.com/j1",
        posted_at=None, description_snippet="Kafka Kubernetes Java",
        description="Full description.",
    )
    scored = ScoredJob(job=job, score=3, matched_keywords=["kafka", "java", "kubernetes"])
    return ScreenedJob(
        scored=scored,
        llm_score=score,
        ai_reason="Strong Java/Kafka match for principal-level role.",
        level_match=True,
        remote_us=True,
        salary_min=200_000,
        used_fallback=used_fallback,
    )


def _make_discovered(name: str = "Acme Corp", status: str = "candidate", has_board: bool = False) -> dict:
    return {
        "name_normalized": "acme",
        "name_raw": name,
        "status": status,
        "ats": "greenhouse" if has_board else None,
        "slug": "acme" if has_board else None,
        "source": {
            "sender": "jobalerts-noreply@linkedin.com",
            "first_seen": datetime.now(tz=timezone.utc).isoformat(),
            "job_title_trigger": "Principal Engineer",
        },
    }


now = datetime.now(tz=timezone.utc)


# ── Plain text digest ─────────────────────────────────────────────────────────

def test_plain_includes_score():
    text = _build_plain([_make_screened(score=82)], now)
    assert "82" in text


def test_plain_marks_fallback_with_asterisk():
    text = _build_plain([_make_screened(score=44, used_fallback=True)], now)
    assert "44*" in text or ("44" in text and "*" in text)


def test_plain_shows_salary():
    text = _build_plain([_make_screened()], now)
    assert "200,000" in text


def test_plain_shows_fit_note_for_llm_scored():
    text = _build_plain([_make_screened(used_fallback=False)], now)
    assert "Strong Java/Kafka match" in text


def test_plain_hides_fit_note_for_fallback():
    text = _build_plain([_make_screened(used_fallback=True)], now)
    assert "Strong Java/Kafka match" not in text


# ── Discovered section ────────────────────────────────────────────────────────

def test_discovered_plain_empty_returns_empty_string():
    assert _build_discovered_plain([]) == ""


def test_discovered_plain_shows_company_and_status():
    disc = [_make_discovered("Acme Corp", status="candidate")]
    text = _build_discovered_plain(disc)
    assert "Acme Corp" in text
    assert "candidate" in text


def test_discovered_plain_shows_board_when_found():
    disc = [_make_discovered("Acme Corp", has_board=True)]
    text = _build_discovered_plain(disc)
    assert "greenhouse/acme" in text


def test_discovered_plain_shows_not_found_when_no_board():
    disc = [_make_discovered("Acme Corp", has_board=False)]
    text = _build_discovered_plain(disc)
    assert "board not found" in text


def test_discovered_html_empty_returns_empty_string():
    assert _build_discovered_html([]) == ""


def test_discovered_html_contains_company_name():
    disc = [_make_discovered("Beta Inc", status="active", has_board=True)]
    html = _build_discovered_html(disc)
    assert "Beta Inc" in html
    assert "active" in html


def test_discovered_html_shows_count_in_heading():
    disc = [_make_discovered(), _make_discovered("Beta")]
    html = _build_discovered_html(disc)
    assert "(2)" in html


# ── _build_html score badge ───────────────────────────────────────────────────

def test_html_includes_score_badge():
    html = _build_html([_make_screened(score=88)], now)
    assert "88" in html
    # High score → green colour
    assert "#2d7a2d" in html


def test_html_grey_badge_for_fallback():
    html = _build_html([_make_screened(score=44, used_fallback=True)], now)
    assert "44*" in html
    # Fallback → grey
    assert "#888" in html


def test_html_extra_html_injected():
    html = _build_html([_make_screened()], now, extra_html="<p>DISCOVERY_SECTION</p>")
    assert "DISCOVERY_SECTION" in html


# ── DiscoveredCompanyStore.get_discovered_since ───────────────────────────────

@pytest.mark.asyncio
async def test_get_discovered_since_returns_recent():
    client = AsyncMongoMockClient()
    store = DiscoveredCompanyStore(client)
    now_dt = datetime.now(tz=timezone.utc)

    await store.insert_candidate("toast", "Toast", "s@s.com", "m1", "Staff Eng", now_dt)
    await store.insert_candidate(
        "old-co", "OldCo", "s@s.com", "m2", "Lead Eng",
        now_dt - timedelta(days=30),
    )

    recent = await store.get_discovered_since(days=7)
    names = [d["name_normalized"] for d in recent]
    assert "toast" in names
    assert "old-co" not in names


@pytest.mark.asyncio
async def test_get_discovered_since_returns_empty_when_none():
    client = AsyncMongoMockClient()
    store = DiscoveredCompanyStore(client)
    result = await store.get_discovered_since(days=7)
    assert result == []


# ── stats now includes scored_jobs and avg_llm_score ─────────────────────────

@pytest.mark.asyncio
async def test_stats_includes_scoring_fields():
    from job_watch.db import JobStore
    client = AsyncMongoMockClient()
    store = JobStore.__new__(JobStore)
    store._client = client
    store._col = client["job-watch"]["seen_jobs"]
    from job_watch.db import GmailStateStore, DiscoveredCompanyStore, ScoringUsageStore
    store.gmail_state = GmailStateStore(client)
    store.discovered = DiscoveredCompanyStore(client)
    store.scoring_usage = ScoringUsageStore(client)

    s = await store.get_stats()
    assert "scored_jobs" in s
    assert "avg_llm_score" in s
    assert s["scored_jobs"] == 0
    assert s["avg_llm_score"] is None
