"""job-watch MCP server.

Transports:
  stdio  — default, for Claude Code / Claude Desktop local use
  http   — streamable HTTP on 0.0.0.0:8000, for cluster deployment

Entry points:
  job-watch mcp              # stdio (from CLI)
  job-watch mcp --http       # streamable HTTP
  job-watch-mcp              # stdio shortcut (direct script)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer, Context

from job_watch import adapters as adapter_registry
from job_watch.config import load as load_config
from job_watch.db import JobStore
from job_watch.models import AppConfig, CompanyConfig

logger = logging.getLogger(__name__)


# ── Lifespan: open/close MongoDB connection ───────────────────────────────────

@asynccontextmanager
async def _lifespan(server: FastMCP):
    mongo_uri = os.environ["MONGO_URI"]
    store = JobStore(mongo_uri)
    await store.ensure_index()
    cfg = load_config()
    logger.info("job-watch MCP server started (%d companies configured)",
                len(cfg.companies))
    yield {"store": store, "cfg": cfg}
    store.close()
    logger.info("job-watch MCP server stopped")


mcp = MCPServer("job-watch", lifespan=_lifespan)


# ── Read tools ────────────────────────────────────────────────────────────────

@mcp.tool()
async def search_jobs(
    ctx: Context,
    query: str = "",
    company: str = "",
    since_days: int = 0,
    remote_only: bool = False,
    status: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search open job postings by keyword, company, recency, remote flag, or review status.

    Args:
        query: Keywords to search in title and description (empty = all).
        company: Filter by company name (case-insensitive substring match).
        since_days: Only jobs first seen in the last N days (0 = no limit).
        remote_only: If true, only return jobs flagged as remote.
        status: Filter by review status: new, interested, applied, skipped.
                Empty string returns all open jobs regardless of status.
                Jobs with no review status are treated as 'new'.
        limit: Maximum number of results to return (default 20, max 100).

    Note: Whether a job is a strong match for your background is a suggestion —
    you make the final call on what to pursue.
    """
    lc = ctx.request_context.lifespan_context
    store: JobStore = lc["store"]
    limit = min(limit, 100)
    return await store.search_jobs(
        query=query,
        company=company,
        since_days=since_days,
        remote_only=remote_only,
        status=status,
        limit=limit,
    )


@mcp.tool()
async def get_job(ctx: Context, job_id: str) -> dict | None:
    """Fetch the full record for a single job, including description and review status.

    Args:
        job_id: The job_id string returned by search_jobs or new_since.

    Returns the complete job document, or null if not found.
    """
    lc = ctx.request_context.lifespan_context
    store: JobStore = lc["store"]
    return await store.get_job(job_id)


@mcp.tool()
async def new_since(ctx: Context, days: int = 7) -> list[dict]:
    """Return open jobs first seen in the last N days, newest first.

    Args:
        days: Look-back window in days (default 7).

    Useful for weekly review sessions. Jobs include their full description
    so you can compare them against your background and each other.
    """
    lc = ctx.request_context.lifespan_context
    store: JobStore = lc["store"]
    return await store.new_since(days)


@mcp.tool()
async def list_companies(ctx: Context) -> list[dict]:
    """List all configured companies with their ATS type and current open job count.

    Returns each company's name, ATS system, slug, and how many open jobs
    are currently in the database for that company.
    """
    lc = ctx.request_context.lifespan_context
    store: JobStore = lc["store"]
    cfg: AppConfig = lc["cfg"]

    counts = await store.get_open_count_by_company()
    return [
        {
            "name": c.name,
            "ats": c.ats,
            "slug": c.slug,
            "open_jobs": counts.get(c.name, 0),
        }
        for c in cfg.companies
    ]


@mcp.tool()
async def stats(ctx: Context) -> dict:
    """Return aggregate counts: total open, new this week, by company, by review status.

    Review status counts treat jobs with no status as 'new'.
    """
    lc = ctx.request_context.lifespan_context
    store: JobStore = lc["store"]
    return await store.get_stats()


@mcp.tool()
async def mark_job(
    ctx: Context,
    job_id: str,
    status: str,
    note: str = "",
) -> dict:
    """Set the review status on a job posting.

    Args:
        job_id: The job_id string returned by search_jobs or new_since.
        status: One of: interested, applied, skipped, new.
        note: Optional free-text note (e.g. "applied via LinkedIn").

    Returns {"ok": true} on success, {"ok": false, "error": "..."} on failure.
    """
    valid = {"new", "interested", "applied", "skipped"}
    if status not in valid:
        return {"ok": False, "error": f"Invalid status {status!r}. Valid: {', '.join(sorted(valid))}"}

    lc = ctx.request_context.lifespan_context
    store: JobStore = lc["store"]
    from job_watch.models import ReviewStatus
    updated = await store.update_review(job_id, status, note or None)  # type: ignore[arg-type]
    if not updated:
        return {"ok": False, "error": f"Job {job_id!r} not found"}
    return {"ok": True}


@mcp.tool()
async def list_discovered(ctx: Context, status: str = "") -> list[dict]:
    """List companies discovered from job-alert emails, optionally filtered by status.

    Args:
        status: Filter by status: candidate, active, rejected, or "" for all.
                - candidate: found in an alert email; ATS board not yet confirmed or awaiting approval
                - active: board confirmed; will be polled on the next run
                - rejected: manually excluded via set_company_status

    Returns each company's normalized name, raw name, source info, ATS/slug if found,
    status, and probe result.
    """
    valid = {"", "candidate", "active", "rejected"}
    if status not in valid:
        return [{"error": f"Invalid status {status!r}. Valid: candidate, active, rejected, or empty string for all."}]

    lc = ctx.request_context.lifespan_context
    store: JobStore = lc["store"]
    return await store.discovered.list_discovered(status)


@mcp.tool()
async def set_company_status(ctx: Context, name_normalized: str, status: str) -> dict:
    """Approve or reject a discovered company.

    Use this after list_discovered to act on candidates:
      - "active"   → approve; the company will be polled on the next run
      - "rejected" → exclude; it will not appear in future list_discovered results unless filtered
      - "candidate" → reset to unreviewed

    Args:
        name_normalized: The name_normalized value from list_discovered.
        status: One of: active, rejected, candidate.

    Returns {"ok": true} on success, {"ok": false, "error": "..."} on failure.
    """
    valid = {"candidate", "active", "rejected"}
    if status not in valid:
        return {"ok": False, "error": f"Invalid status {status!r}. Valid: {', '.join(sorted(valid))}"}

    lc = ctx.request_context.lifespan_context
    store: JobStore = lc["store"]
    updated = await store.discovered.set_status(name_normalized, status)  # type: ignore[arg-type]
    if not updated:
        return {"ok": False, "error": f"Company {name_normalized!r} not found in discovered_companies"}
    return {"ok": True}


@mcp.tool()
async def suggest_company(
    ctx: Context,
    name: str,
    ats: str,
    slug: str,
) -> dict:
    """Validate an ATS company slug and return the YAML snippet to add to companies.yaml.

    Fetches the company's job board to confirm the slug is valid and returns
    a sample of current job titles so you can verify it's the right company.

    This tool does NOT edit companies.yaml — config changes go through git.

    Args:
        name: Display name for the company (e.g. "Acme Corp").
        ats: ATS system: greenhouse, lever, or ashby.
        slug: Company slug on that ATS (e.g. "acme").
    """
    adapter_cls = adapter_registry.REGISTRY.get(ats)
    if adapter_cls is None:
        valid = ", ".join(sorted(adapter_registry.REGISTRY))
        return {"valid": False, "error": f"Unknown ATS '{ats}'. Valid options: {valid}"}

    company = CompanyConfig(name=name, ats=ats, slug=slug)
    headers = {"User-Agent": "job-watch/0.1 mcp/suggest-company"}

    logger.info("suggest_company: validating %s/%s for '%s'", ats, slug, name)

    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(30)
        ) as client:
            adapter = adapter_cls()
            jobs = await adapter.fetch_jobs(company, client)
    except httpx.HTTPStatusError as exc:
        return {
            "valid": False,
            "error": f"HTTP {exc.response.status_code} from ATS API — slug may be wrong",
        }
    except Exception as exc:
        return {"valid": False, "error": str(exc)}

    logger.info("suggest_company: %s/%s → %d jobs found", ats, slug, len(jobs))

    yaml_snippet = f"  - {{ name: {name}, ats: {ats}, slug: {slug} }}"
    return {
        "valid": True,
        "job_count": len(jobs),
        "sample_titles": [j.title for j in jobs[:5]],
        "yaml_snippet": yaml_snippet,
        "instructions": (
            "Add the yaml_snippet to the 'companies' list in companies.yaml "
            "and commit to git. Then run 'job-watch --seed' to populate the database."
        ),
    }


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("resource://job-watch/filter-criteria")
async def filter_criteria() -> str:
    """Your configured target-role criteria and candidate profile."""
    cfg = load_config()
    f = cfg.filter
    lines = [
        "# job-watch — Target Role Criteria",
        "",
        "## Title must include one of (seniority):",
        *[f"  - {kw}" for kw in f.title_include],
        "",
        "## Title must also include one of (engineering discipline):",
        *[f"  - {kw}" for kw in f.title_engineering],
        "",
        "## Title must NOT include (exclusions):",
        *[f"  - {kw}" for kw in f.title_exclude],
        "",
        "## Boost keywords (raise ranking score):",
        *[f"  - {kw}" for kw in f.boost_keywords],
        "",
        "## Location: keep patterns:",
        *[f"  - {pat}" for pat in f.location_keep_patterns],
        "",
        "## Location: drop patterns:",
        *[f"  - {pat}" for pat in f.location_drop_patterns],
    ]
    if cfg.screening.candidate_profile:
        lines += [
            "",
            "## Candidate profile (used for AI screening):",
            cfg.screening.candidate_profile,
        ]
    return "\n".join(lines)


# ── Prompts ───────────────────────────────────────────────────────────────────

@mcp.prompt()
def weekly_review() -> str:
    """Walk through new job postings from the past week and mark each one."""
    return """\
You are helping with a weekly job-lead review session using job-watch data.

Steps:
1. Call stats() to show a summary (total open, new this week, breakdown by company).
2. Call new_since(days=7) to fetch jobs posted in the last 7 days.
3. For each job, display one line:
     [company] [title] — [location] — keywords: [matched boost keywords if any]
   Then show the first 2-3 sentences of the description.
4. For each job, suggest a status based on the role's fit with my background
   (read the filter-criteria resource for context), but frame it as a suggestion.
5. Ask me for feedback: interested / applied / skipped / leave as new.
6. After all jobs, call stats() again to show the updated counts.

Keep the session conversational. One job at a time unless I ask for a batch.
Note: matching judgments are suggestions — I make the final call.
"""


# ── Health endpoint (HTTP transport only) ────────────────────────────────────

@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


# ── Entry points ──────────────────────────────────────────────────────────────

def run_stdio() -> None:
    """Start the MCP server over stdio (for Claude Code / Claude Desktop)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    mcp.run(transport="stdio")


def run_http(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Start the MCP server over streamable HTTP (for cluster deployment)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    mcp.run(transport="streamable-http", host=host, port=port)
