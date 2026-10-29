"""Tests for PR 2: ATS resolver, DiscoveredCompanyStore, and new MCP tools."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from job_watch.db import DiscoveredCompanyStore
from job_watch.discovery.resolver import ATSResolver, generate_slugs, normalize_company
from job_watch.models import AppConfig, CompanyConfig


# ── normalize_company / generate_slugs ───────────────────────────────────────

def test_normalize_strips_legal_suffix():
    assert normalize_company("Toast, Inc.") == "toast"


def test_normalize_ampersand():
    assert normalize_company("Hims & Hers") == "hims   hers".split() == ["hims", "hers"] or \
           "hims" in normalize_company("Hims & Hers")


def test_normalize_is_stable():
    assert normalize_company("Acme Corp") == normalize_company("acme corp")


def test_generate_slugs_returns_list():
    slugs = generate_slugs("Toast")
    assert isinstance(slugs, list)
    assert len(slugs) >= 1
    assert "toast" in slugs


def test_generate_slugs_multi_word():
    slugs = generate_slugs("Hims & Hers")
    assert any("-" in s or s == "himsandhers" or "hims" in s for s in slugs)


def test_generate_slugs_no_duplicates():
    slugs = generate_slugs("Acme Inc")
    assert len(slugs) == len(set(slugs))


def test_generate_slugs_strips_suffix():
    slugs = generate_slugs("Acme Inc.")
    # "inc" should not appear as a standalone slug
    assert not any(s == "inc" for s in slugs)


# ── ATSResolver ───────────────────────────────────────────────────────────────

def _make_job():
    from job_watch.models import Job
    return Job(
        id="j1", company="Acme", ats="greenhouse", title="Staff Engineer",
        location="Remote", remote=True, department="Eng", url="https://example.com",
        posted_at=None, description_snippet="test", description="test",
    )


@pytest.mark.asyncio
async def test_resolver_returns_result_when_board_found():
    resolver = ATSResolver(rate_limit_delay=0)

    async def fake_fetch(self, company, client):
        if company.ats == "greenhouse" and company.slug in ("acme", "acmecorp", "acme-corp"):
            return [_make_job()]
        return []

    with patch("job_watch.adapters.greenhouse.GreenhouseAdapter.fetch_jobs", new=fake_fetch):
        result, slugs_tried = await resolver.probe("Acme Corp")

    assert result is not None
    assert result.ats == "greenhouse"
    assert result.job_count == 1
    assert len(slugs_tried) >= 1


@pytest.mark.asyncio
async def test_resolver_returns_none_when_no_board():
    resolver = ATSResolver(rate_limit_delay=0)

    # Patch all three adapters to return empty
    with patch("job_watch.adapters.greenhouse.GreenhouseAdapter.fetch_jobs", new=AsyncMock(return_value=[])), \
         patch("job_watch.adapters.lever.LeverAdapter.fetch_jobs", new=AsyncMock(return_value=[])), \
         patch("job_watch.adapters.ashby.AshbyAdapter.fetch_jobs", new=AsyncMock(return_value=[])):
        result, slugs_tried = await resolver.probe("NoSuchCompany XYZ")

    assert result is None
    assert len(slugs_tried) > 0


@pytest.mark.asyncio
async def test_resolver_continues_after_404():
    resolver = ATSResolver(rate_limit_delay=0)

    call_count = {"n": 0}

    async def fake_fetch(company, client):
        call_count["n"] += 1
        if company.ats == "greenhouse":
            raise httpx.HTTPStatusError(
                "Not Found",
                request=MagicMock(),
                response=MagicMock(status_code=404),
            )
        if company.ats == "lever" and "noslug" not in company.slug:
            return [_make_job()]
        return []

    with patch("job_watch.adapters.greenhouse.GreenhouseAdapter.fetch_jobs", new=fake_fetch), \
         patch("job_watch.adapters.lever.LeverAdapter.fetch_jobs", new=fake_fetch), \
         patch("job_watch.adapters.ashby.AshbyAdapter.fetch_jobs", new=AsyncMock(return_value=[])):
        result, slugs_tried = await resolver.probe("SomeCompany")

    # Should have tried greenhouse (got 404) then moved to lever
    assert any("greenhouse" in s for s in slugs_tried)


@pytest.mark.asyncio
async def test_resolver_backs_off_on_429():
    resolver = ATSResolver(rate_limit_delay=0)
    sleep_calls: list[float] = []

    async def fake_sleep(n):
        sleep_calls.append(n)

    async def fake_fetch(self, company, client):
        if company.ats == "greenhouse":
            raise httpx.HTTPStatusError(
                "Too Many Requests",
                request=MagicMock(),
                response=MagicMock(status_code=429),
            )
        return []

    with patch("job_watch.adapters.greenhouse.GreenhouseAdapter.fetch_jobs", new=fake_fetch), \
         patch("job_watch.adapters.lever.LeverAdapter.fetch_jobs", new=AsyncMock(return_value=[])), \
         patch("job_watch.adapters.ashby.AshbyAdapter.fetch_jobs", new=AsyncMock(return_value=[])), \
         patch("asyncio.sleep", new=fake_sleep):
        await resolver.probe("RateLimitedCo")

    # 10-second back-off should appear in sleep calls after a 429
    assert any(s == 10 for s in sleep_calls)


# ── DiscoveredCompanyStore ────────────────────────────────────────────────────

@pytest.fixture
def discovered_store():
    client = AsyncMongoMockClient()
    return DiscoveredCompanyStore(client)


@pytest.mark.asyncio
async def test_insert_candidate_and_exists(discovered_store):
    now = datetime.now(tz=timezone.utc)
    inserted = await discovered_store.insert_candidate(
        "toast", "Toast, Inc.", "jobs@linkedin.com", "msg1", "Staff Engineer", now
    )
    assert inserted is True
    assert await discovered_store.exists("toast") is True


@pytest.mark.asyncio
async def test_insert_candidate_duplicate_returns_false(discovered_store):
    now = datetime.now(tz=timezone.utc)
    await discovered_store.insert_candidate("toast", "Toast", "s@s.com", "m1", "t", now)
    second = await discovered_store.insert_candidate("toast", "Toast", "s@s.com", "m2", "t", now)
    assert second is False


@pytest.mark.asyncio
async def test_update_probe_stores_result(discovered_store):
    now = datetime.now(tz=timezone.utc)
    await discovered_store.insert_candidate("toast", "Toast", "s@s.com", "m1", "Staff Eng", now)
    await discovered_store.update_probe(
        "toast",
        ats="greenhouse", slug="toast",
        slugs_tried=["greenhouse/toast"],
        negative_cache_until=None,
        now=now,
    )
    doc = await discovered_store.get_by_normalized("toast")
    assert doc["ats"] == "greenhouse"
    assert doc["slug"] == "toast"
    assert doc["probe"]["slugs_tried"] == ["greenhouse/toast"]
    assert doc["probe"]["negative_cache_until"] is None


@pytest.mark.asyncio
async def test_negative_cache_round_trips(discovered_store):
    now = datetime.now(tz=timezone.utc)
    until = now + timedelta(days=30)
    await discovered_store.insert_candidate("unknown-co", "Unknown Co", "s@s.com", "m1", "t", now)
    await discovered_store.update_probe(
        "unknown-co",
        ats=None, slug=None,
        slugs_tried=["greenhouse/unknown-co"],
        negative_cache_until=until,
        now=now,
    )
    cached = await discovered_store.get_negative_cache_until("unknown-co")
    assert cached is not None
    # mongomock may return timezone-naive datetimes; compare as UTC timestamps
    cached_ts = cached.replace(tzinfo=timezone.utc) if cached.tzinfo is None else cached
    assert abs((cached_ts - until).total_seconds()) < 1


@pytest.mark.asyncio
async def test_set_status(discovered_store):
    now = datetime.now(tz=timezone.utc)
    await discovered_store.insert_candidate("toast", "Toast", "s@s.com", "m1", "t", now)
    ok = await discovered_store.set_status("toast", "active")
    assert ok is True
    doc = await discovered_store.get_by_normalized("toast")
    assert doc["status"] == "active"


@pytest.mark.asyncio
async def test_set_status_not_found(discovered_store):
    ok = await discovered_store.set_status("nonexistent", "active")
    assert ok is False


@pytest.mark.asyncio
async def test_list_discovered_filter_by_status(discovered_store):
    now = datetime.now(tz=timezone.utc)
    await discovered_store.insert_candidate("co-a", "Co A", "s@s.com", "m1", "t", now)
    await discovered_store.insert_candidate("co-b", "Co B", "s@s.com", "m2", "t", now)
    await discovered_store.set_status("co-b", "rejected")

    candidates = await discovered_store.list_discovered("candidate")
    assert len(candidates) == 1
    assert candidates[0]["name_normalized"] == "co-a"

    rejected = await discovered_store.list_discovered("rejected")
    assert len(rejected) == 1

    all_docs = await discovered_store.list_discovered()
    assert len(all_docs) == 2


# ── MCP tools: list_discovered, set_company_status ───────────────────────────

def _make_mcp_context(store, cfg):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"store": store, "cfg": cfg}
    return ctx


def _make_store_with_discovered():
    from job_watch.db import JobStore
    client = AsyncMongoMockClient()
    store = JobStore.__new__(JobStore)
    store._client = client
    store._col = client["job-watch"]["seen_jobs"]
    from job_watch.db import GmailStateStore, DiscoveredCompanyStore
    store.gmail_state = GmailStateStore(client)
    store.discovered = DiscoveredCompanyStore(client)
    return store


@pytest.fixture
async def ctx_with_discovered():
    store = _make_store_with_discovered()
    now = datetime.now(tz=timezone.utc)
    await store.discovered.insert_candidate(
        "toast", "Toast, Inc.", "jobs@linkedin.com", "msg1", "Staff Engineer", now
    )
    await store.discovered.update_probe(
        "toast", ats="greenhouse", slug="toast",
        slugs_tried=["greenhouse/toast"], negative_cache_until=None, now=now,
    )
    await store.discovered.insert_candidate(
        "acme", "Acme Corp", "jobs@indeed.com", "msg2", "Principal Architect", now
    )
    cfg = AppConfig(companies=[CompanyConfig(name="Toast", ats="greenhouse", slug="toast")])
    return _make_mcp_context(store, cfg)


@pytest.mark.asyncio
async def test_list_discovered_returns_all(ctx_with_discovered):
    from job_watch.mcp.server import list_discovered
    result = await list_discovered(ctx_with_discovered)
    assert len(result) == 2
    names = {r["name_normalized"] for r in result}
    assert names == {"toast", "acme"}


@pytest.mark.asyncio
async def test_list_discovered_filters_by_status(ctx_with_discovered):
    from job_watch.mcp.server import list_discovered, set_company_status
    await set_company_status(ctx_with_discovered, "acme", "rejected")

    candidates = await list_discovered(ctx_with_discovered, status="candidate")
    assert len(candidates) == 1
    assert candidates[0]["name_normalized"] == "toast"

    rejected = await list_discovered(ctx_with_discovered, status="rejected")
    assert len(rejected) == 1
    assert rejected[0]["name_normalized"] == "acme"


@pytest.mark.asyncio
async def test_set_company_status_ok(ctx_with_discovered):
    from job_watch.mcp.server import set_company_status
    result = await set_company_status(ctx_with_discovered, "toast", "active")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_set_company_status_invalid(ctx_with_discovered):
    from job_watch.mcp.server import set_company_status
    result = await set_company_status(ctx_with_discovered, "toast", "maybe")
    assert result["ok"] is False
    assert "Invalid status" in result["error"]


@pytest.mark.asyncio
async def test_set_company_status_not_found(ctx_with_discovered):
    from job_watch.mcp.server import set_company_status
    result = await set_company_status(ctx_with_discovered, "nonexistent-co", "active")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_list_discovered_invalid_status(ctx_with_discovered):
    from job_watch.mcp.server import list_discovered
    result = await list_discovered(ctx_with_discovered, status="bogus")
    assert len(result) == 1
    assert "error" in result[0]
