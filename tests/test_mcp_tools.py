"""Unit tests for MCP tool logic via JobStore query methods.

Uses mongomock-motor for an in-process async MongoDB mock — no real DB needed.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from mongomock_motor import AsyncMongoMockClient

from job_watch.db import JobStore
from job_watch.models import Job


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_job(
    job_id: str = "1",
    company: str = "Toast",
    ats: str = "greenhouse",
    title: str = "Staff Software Engineer",
    location: str = "Remote",
    remote: bool = True,
    description: str = "We need a staff engineer with Kafka and Kubernetes experience.",
    department: str = "Engineering",
) -> Job:
    return Job(
        id=job_id,
        company=company,
        ats=ats,
        title=title,
        location=location,
        remote=remote,
        department=department,
        url=f"https://example.com/jobs/{job_id}",
        posted_at=datetime.now(tz=timezone.utc) - timedelta(days=2),
        description_snippet=description[:200],
        description=description,
    )


@pytest.fixture
def store():
    """JobStore backed by an in-memory mongomock-motor client."""
    client = AsyncMongoMockClient()
    s = JobStore.__new__(JobStore)
    s._client = client
    s._col = client["job-watch"]["seen_jobs"]
    return s


@pytest.fixture
async def populated_store(store):
    """Store with a handful of fixture jobs already inserted."""
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=10)

    jobs = [
        _make_job("1", company="Toast",      title="Staff Software Engineer",           location="Remote"),
        _make_job("2", company="Toast",      title="Principal Engineer, Platform",      location="Remote",   description="Platform engineering with Kafka event streaming"),
        _make_job("3", company="Arcadia",    title="Staff Engineer, Backend",           location="New York", remote=False),
        _make_job("4", company="Smartsheet", title="Lead Software Engineer",            location="Remote",   description="LLM and AI platform work"),
        _make_job("5", company="Arcadia",    title="Principal Software Architect",      location="Remote"),
    ]

    for i, job in enumerate(jobs):
        first_seen = old if i >= 3 else now - timedelta(days=i)
        await store.upsert(job, first_seen)

    # Mark job 3 as closed
    await store._col.update_one(
        {"ats": "greenhouse", "company": "Arcadia", "id": "3"},
        {"$set": {"closed_at": now}},
    )

    # Mark job 1 as 'interested'
    await store._col.update_one(
        {"ats": "greenhouse", "company": "Toast", "id": "1"},
        {"$set": {"review": {"status": "interested", "note": "Strong fit", "updated_at": now}}},
    )

    return store


# ── search_jobs ───────────────────────────────────────────────────────────────

async def test_search_jobs_no_filter(populated_store):
    results = await populated_store.search_jobs()
    # Job 3 is closed, so expect 4 open jobs
    assert len(results) == 4
    # All results have job_id
    assert all("job_id" in r for r in results)


async def test_search_jobs_company_filter(populated_store):
    results = await populated_store.search_jobs(company="arcadia")
    # Job 3 is closed; only job 5 (Principal Architect) should remain
    assert len(results) == 1
    assert results[0]["company"] == "Arcadia"


async def test_search_jobs_remote_only(populated_store):
    results = await populated_store.search_jobs(remote_only=True)
    assert all(r["remote"] for r in results)


async def test_search_jobs_since_days(populated_store):
    # Jobs 1 and 2 (Toast) were inserted within the last 5 days; jobs 4,5 are older
    results = await populated_store.search_jobs(since_days=5)
    titles = {r["title"] for r in results}
    assert "Staff Software Engineer" in titles
    assert "Principal Engineer, Platform" in titles


async def test_search_jobs_status_new(populated_store):
    # Job 1 is 'interested'; the rest are unset (= new)
    results = await populated_store.search_jobs(status="new")
    companies_titles = {r["title"] for r in results}
    assert "Staff Software Engineer" not in companies_titles  # job 1 is interested


async def test_search_jobs_status_interested(populated_store):
    results = await populated_store.search_jobs(status="interested")
    assert len(results) == 1
    assert results[0]["title"] == "Staff Software Engineer"


async def test_search_jobs_limit(populated_store):
    results = await populated_store.search_jobs(limit=2)
    assert len(results) == 2


# ── get_job ───────────────────────────────────────────────────────────────────

async def test_get_job_found(populated_store):
    all_jobs = await populated_store.search_jobs()
    job_id = all_jobs[0]["job_id"]
    result = await populated_store.get_job(job_id)
    assert result is not None
    assert result["job_id"] == job_id
    assert "description" in result


async def test_get_job_not_found(populated_store):
    result = await populated_store.get_job("000000000000000000000000")
    assert result is None


async def test_get_job_invalid_id(populated_store):
    result = await populated_store.get_job("not-an-object-id")
    assert result is None


# ── new_since ─────────────────────────────────────────────────────────────────

async def test_new_since_7_days(populated_store):
    results = await populated_store.new_since(7)
    # Jobs 1 and 2 are within 7 days; jobs 4 and 5 are 10 days old
    assert len(results) == 2


async def test_new_since_excludes_closed(populated_store):
    results = await populated_store.new_since(30)
    ids = {r["id"] for r in results}
    assert "3" not in ids  # job 3 is closed


# ── stats ─────────────────────────────────────────────────────────────────────

async def test_stats_shape(populated_store):
    s = await populated_store.get_stats()
    assert s["total_open"] == 4
    assert "new_this_week" in s
    assert isinstance(s["by_company"], dict)
    assert isinstance(s["by_review_status"], dict)


async def test_stats_review_counts(populated_store):
    s = await populated_store.get_stats()
    # 1 interested, 3 with no review (= new)
    assert s["by_review_status"].get("interested", 0) == 1
    assert s["by_review_status"].get("new", 0) == 3


# ── update_review ─────────────────────────────────────────────────────────────

async def test_mark_job_updates_status(populated_store):
    all_jobs = await populated_store.search_jobs(company="arcadia")
    job_id = all_jobs[0]["job_id"]

    ok = await populated_store.update_review(job_id, "skipped", note="Out of my range")
    assert ok is True

    updated = await populated_store.get_job(job_id)
    assert updated["review"]["status"] == "skipped"
    assert updated["review"]["note"] == "Out of my range"


async def test_mark_job_no_note(populated_store):
    all_jobs = await populated_store.search_jobs(status="new", limit=1)
    job_id = all_jobs[0]["job_id"]

    ok = await populated_store.update_review(job_id, "interested")
    assert ok is True

    updated = await populated_store.get_job(job_id)
    assert updated["review"]["status"] == "interested"


async def test_mark_job_not_found(populated_store):
    ok = await populated_store.update_review("000000000000000000000000", "skipped")
    assert ok is False


# ── upsert stores description ────────────────────────────────────────────────

async def test_upsert_stores_description(store):
    now = datetime.now(tz=timezone.utc)
    job = _make_job(description="Detailed description with Kafka and Kubernetes skills.")
    await store.upsert(job, now)

    doc = await store._col.find_one({"id": "1"})
    assert doc["description"] == "Detailed description with Kafka and Kubernetes skills."


async def test_upsert_description_not_overwritten(store):
    now = datetime.now(tz=timezone.utc)
    job = _make_job(description="Original description.")
    await store.upsert(job, now)

    # Second upsert with different description should not overwrite
    job2 = _make_job(description="Updated description.")
    await store.upsert(job2, now + timedelta(hours=1))

    doc = await store._col.find_one({"id": "1"})
    assert doc["description"] == "Original description."
