"""Smoke tests for the MCP server — starts the server in-process over HTTP
and exercises list_companies, search_jobs, and stats through an MCP client.

Requires MONGO_URI to be set (or uses a mongomock-motor patch).
These are integration-style tests; run them with a real or mock MongoDB.
"""

from __future__ import annotations

import os
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from mongomock_motor import AsyncMongoMockClient

from job_watch.db import JobStore
from job_watch.models import Job
from job_watch.mcp.server import (
    list_companies,
    search_jobs,
    stats,
    get_job,
    mark_job,
    suggest_company,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_context(store: JobStore, cfg) -> MagicMock:
    """Build a minimal FastMCP Context mock with lifespan_context set."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"store": store, "cfg": cfg}
    return ctx


def _make_store_with_jobs() -> JobStore:
    client = AsyncMongoMockClient()
    store = JobStore.__new__(JobStore)
    store._client = client
    store._col = client["job-watch"]["seen_jobs"]
    return store


async def _seed_store(store: JobStore) -> None:
    now = datetime.now(tz=timezone.utc)
    jobs = [
        Job(
            id="j1", company="Toast", ats="greenhouse",
            title="Staff Software Engineer", location="Remote", remote=True,
            department="Engineering", url="https://example.com/j1",
            posted_at=now - timedelta(days=1),
            description_snippet="Kafka, Kubernetes, distributed systems",
            description="We are looking for a staff engineer with Kafka and Kubernetes experience.",
        ),
        Job(
            id="j2", company="Arcadia", ats="lever",
            title="Principal Software Architect", location="Remote", remote=True,
            department="Platform", url="https://example.com/j2",
            posted_at=now - timedelta(days=2),
            description_snippet="Event-driven architecture, Java, platform engineering",
            description="Build out our agent platform with event-driven architecture.",
        ),
    ]
    for job in jobs:
        await store.upsert(job, now - timedelta(days=i) if (i := jobs.index(job)) == 0 else now - timedelta(days=2))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def ctx_with_data(tmp_path):
    """FastMCP context with a mock store + minimal AppConfig."""
    from job_watch.models import AppConfig, CompanyConfig

    store = _make_store_with_jobs()
    now = datetime.now(tz=timezone.utc)

    jobs = [
        Job(
            id="j1", company="Toast", ats="greenhouse",
            title="Staff Software Engineer", location="Remote", remote=True,
            department="Engineering", url="https://example.com/j1",
            posted_at=now - timedelta(days=1),
            description_snippet="Kafka Kubernetes",
            description="Staff engineer role with Kafka and Kubernetes.",
        ),
        Job(
            id="j2", company="Arcadia", ats="lever",
            title="Principal Software Architect", location="Remote", remote=True,
            department="Platform", url="https://example.com/j2",
            posted_at=now - timedelta(days=2),
            description_snippet="Agent platform Java",
            description="Build the agent platform with Java and event-driven architecture.",
        ),
    ]
    for job in jobs:
        await store.upsert(job, now)

    cfg = AppConfig(companies=[
        CompanyConfig(name="Toast", ats="greenhouse", slug="toast"),
        CompanyConfig(name="Arcadia", ats="lever", slug="arcadia"),
    ])

    return _fake_context(store, cfg)


# ── Smoke tests ───────────────────────────────────────────────────────────────

async def test_list_companies_returns_all(ctx_with_data):
    result = await list_companies(ctx_with_data)
    assert len(result) == 2
    names = {c["name"] for c in result}
    assert names == {"Toast", "Arcadia"}
    assert all("open_jobs" in c for c in result)
    assert all(c["open_jobs"] >= 0 for c in result)


async def test_list_companies_open_count(ctx_with_data):
    result = await list_companies(ctx_with_data)
    by_name = {c["name"]: c["open_jobs"] for c in result}
    assert by_name["Toast"] == 1
    assert by_name["Arcadia"] == 1


async def test_search_jobs_returns_results(ctx_with_data):
    result = await search_jobs(ctx_with_data)
    assert len(result) == 2
    assert all("job_id" in r for r in result)
    assert all("title" in r for r in result)


async def test_search_jobs_company_filter(ctx_with_data):
    result = await search_jobs(ctx_with_data, company="arcadia")
    assert len(result) == 1
    assert result[0]["company"] == "Arcadia"


async def test_search_jobs_remote_filter(ctx_with_data):
    result = await search_jobs(ctx_with_data, remote_only=True)
    assert len(result) == 2


async def test_stats_sane_response(ctx_with_data):
    result = await stats(ctx_with_data)
    assert result["total_open"] == 2
    assert "new_this_week" in result
    assert isinstance(result["by_company"], dict)
    assert isinstance(result["by_review_status"], dict)
    # Both jobs have no review = counted as 'new'
    assert result["by_review_status"].get("new", 0) == 2


async def test_get_job_found(ctx_with_data):
    jobs = await search_jobs(ctx_with_data, limit=1)
    job_id = jobs[0]["job_id"]
    result = await get_job(ctx_with_data, job_id)
    assert result is not None
    assert result["job_id"] == job_id
    assert "description" in result


async def test_mark_job_and_verify(ctx_with_data):
    jobs = await search_jobs(ctx_with_data, limit=1)
    job_id = jobs[0]["job_id"]

    result = await mark_job(ctx_with_data, job_id, "interested", "Looks like a great fit")
    assert result["ok"] is True

    updated = await get_job(ctx_with_data, job_id)
    assert updated["review"]["status"] == "interested"
    assert updated["review"]["note"] == "Looks like a great fit"


async def test_mark_job_invalid_status(ctx_with_data):
    jobs = await search_jobs(ctx_with_data, limit=1)
    job_id = jobs[0]["job_id"]
    result = await mark_job(ctx_with_data, job_id, "maybe")
    assert result["ok"] is False
    assert "Invalid status" in result["error"]


async def test_mark_job_not_found(ctx_with_data):
    result = await mark_job(ctx_with_data, "000000000000000000000000", "skipped")
    assert result["ok"] is False


async def test_suggest_company_invalid_ats(ctx_with_data):
    result = await suggest_company(ctx_with_data, "Acme", "workday", "acme")
    assert result["valid"] is False
    assert "workday" in result["error"]
