"""job-watch CLI entrypoint.

Usage:
  python -m job_watch                     # normal run (fetch → filter → notify)
  python -m job_watch --seed              # seed state; no email sent
  python -m job_watch check-slug <ats> <slug>  # verify a company slug returns jobs
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import httpx

from job_watch import adapters as adapter_registry
from job_watch.config import load as load_config
from job_watch.db import JobStore
from job_watch.filter import ScoredJob, filter_and_score
from job_watch.models import AppConfig, CompanyConfig, Job
from job_watch.notify import send_digest
from job_watch.screener import ScreenedJob, screen_jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("job_watch")


async def _run_gmail_discovery(cfg: AppConfig, store: JobStore) -> None:
    """Fetch job-alert emails, store new companies, and probe their ATS boards."""
    from job_watch.discovery.resolver import ATSResolver, normalize_company
    from job_watch.sources.gmail import GmailSource

    dcfg = cfg.gmail_discovery
    source = GmailSource(dcfg, store.gmail_state)
    leads = await source.fetch_leads()

    if not leads:
        logger.info("gmail_discovery: no new leads this run")
        return

    # Build set of already-watched company keys to skip duplicates
    watched: set[str] = {normalize_company(c.name) for c in cfg.companies}

    resolver = ATSResolver(
        rate_limit_delay=dcfg.probe_rate_limit_delay,
        user_agent=cfg.user_agent,
    )
    now = datetime.now(tz=timezone.utc)

    # Deduplicate within this batch too
    batch_seen: set[str] = set()

    for lead in leads:
        key = normalize_company(lead.company_name_raw)
        if key in batch_seen or key in watched:
            continue
        batch_seen.add(key)

        # Skip if already in discovered_companies
        if await store.discovered.exists(key):
            continue

        inserted = await store.discovered.insert_candidate(
            name_normalized=key,
            name_raw=lead.company_name_raw,
            sender=lead.sender,
            gmail_message_id=lead.gmail_message_id,
            job_title_trigger=lead.job_title,
            now=now,
        )
        if not inserted:
            continue  # race condition, another run beat us

        logger.info(
            "gmail_discovery: new candidate %r (trigger: %r)", lead.company_name_raw, lead.job_title
        )

        # Check negative cache before probing
        cached_until = await store.discovered.get_negative_cache_until(key)
        if cached_until and cached_until > now:
            logger.info(
                "gmail_discovery: %r in negative cache until %s — skipping probe",
                key, cached_until.isoformat(),
            )
            continue

        result, slugs_tried = await resolver.probe(lead.company_name_raw)

        if result:
            neg_until = None
            new_status = "active" if dcfg.auto_activate else "candidate"
        else:
            neg_until = now + timedelta(days=dcfg.negative_cache_ttl_days)
            new_status = "candidate"

        await store.discovered.update_probe(
            key,
            ats=result.ats if result else None,
            slug=result.slug if result else None,
            slugs_tried=slugs_tried,
            negative_cache_until=neg_until,
            now=now,
        )
        if result:
            await store.discovered.set_status(key, new_status)  # type: ignore[arg-type]
            if dcfg.auto_activate:
                cfg.companies.append(
                    CompanyConfig(name=lead.company_name_raw, ats=result.ats, slug=result.slug)
                )
                logger.info(
                    "gmail_discovery: auto-activated %r (%s/%s)",
                    lead.company_name_raw, result.ats, result.slug,
                )


# ── Fetch one company, catching all errors so one failure doesn't abort the run ──

async def _fetch_company(
    company: CompanyConfig,
    client: httpx.AsyncClient,
    cfg: AppConfig,
) -> list[Job]:
    adapter_cls = adapter_registry.REGISTRY.get(company.ats)
    if adapter_cls is None:
        logger.error("Unknown ATS %r for %s — skipping", company.ats, company.name)
        return []
    try:
        adapter = adapter_cls()
        jobs = await adapter.fetch_jobs(company, client)
        return jobs
    except Exception as exc:
        logger.error("Failed to fetch %s (%s/%s): %s",
                     company.name, company.ats, company.slug, exc)
        return []


# ── Main scan ────────────────────────────────────────────────────────────────

async def run(*, seed: bool = False, config_path: str | None = None) -> None:
    cfg = load_config(config_path)
    mongo_uri = os.environ["MONGO_URI"]

    store = JobStore(mongo_uri)
    await store.ensure_index()

    # Gmail discovery — reads alert emails and logs new company leads
    logger.info("gmail_discovery: enabled=%s", cfg.gmail_discovery.enabled)
    if cfg.gmail_discovery.enabled:
        try:
            await _run_gmail_discovery(cfg, store)
        except Exception as exc:
            logger.error("gmail_discovery: failed: %s", exc, exc_info=True)

    now = datetime.now(tz=timezone.utc)

    limits = httpx.Limits(
        max_connections=cfg.concurrency_limit,
        max_keepalive_connections=cfg.concurrency_limit,
    )
    headers = {"User-Agent": cfg.user_agent}
    timeout = httpx.Timeout(cfg.request_timeout_sec)

    sem = asyncio.Semaphore(cfg.concurrency_limit)

    async def fetch_with_sem(company: CompanyConfig) -> tuple[CompanyConfig, list[Job]]:
        async with sem:
            jobs = await _fetch_company(company, client, cfg)
            return company, jobs

    new_matches: list[ScoredJob] = []

    async with httpx.AsyncClient(headers=headers, timeout=timeout, limits=limits) as client:
        tasks = [fetch_with_sem(c) for c in cfg.companies]
        results = await asyncio.gather(*tasks)

    for company, jobs in results:
        if not jobs:
            # Fetch failed or empty; skip closed-job detection to avoid false positives
            continue

        scored = filter_and_score(jobs, cfg.filter)
        active_ids = {j.id for j in jobs}

        for s in scored:
            is_new = await store.is_new(s.job)
            await store.upsert(s.job, now)
            if is_new:
                new_matches.append(s)

        # Refresh last_seen for all fetched jobs (not just filtered ones)
        for job in jobs:
            await store.upsert(job, now)

        await store.mark_closed(company.ats, company.name, active_ids, now)

    if not new_matches:
        logger.info("No new matches this run.")
        store.close()
        return

    logger.info("%d new match(es) found after keyword filter.", len(new_matches))

    # AI scoring (second pass) — runs when either llm_scoring or screening is enabled
    if cfg.llm_scoring.enabled:
        screened = await screen_jobs(
            new_matches, cfg.screening, store=store, scoring_cfg=cfg.llm_scoring
        )
    elif cfg.screening.enabled:
        screened = await screen_jobs(new_matches, cfg.screening)
    else:
        screened = [
            ScreenedJob(
                scored=s, llm_score=s.score * 10, ai_reason="",
                level_match=True, remote_us=None, salary_min=None, used_fallback=False,
            )
            for s in new_matches
        ]

    if not screened:
        logger.info("No matches survived AI screening this run.")
        store.close()
        return

    if seed:
        logger.info("--seed mode: state saved, no email sent.")
        for item in screened:
            j = item.scored.job
            logger.info("  [seeded] %s — %s (%s)", j.company, j.title, j.location)
        store.close()
        return

    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    gmail_to = os.environ.get("GMAIL_TO", "randykamradt@gmail.com")

    discovered_this_week: list[dict] = []
    if cfg.gmail_discovery.enabled:
        try:
            discovered_this_week = await store.discovered.get_discovered_since(days=7)
            # Only show candidate and active in the digest, not rejected
            discovered_this_week = [
                d for d in discovered_this_week if d.get("status") != "rejected"
            ]
        except Exception as exc:
            logger.warning("digest: failed to fetch discovered companies: %s", exc)

    send_digest(
        screened,
        gmail_user=gmail_user,
        gmail_app_password=gmail_app_password,
        to_addr=gmail_to,
        discovered=discovered_this_week,
    )
    store.close()


# ── check-slug subcommand ────────────────────────────────────────────────────

async def check_slug(ats: str, slug: str) -> None:
    from job_watch.models import CompanyConfig as CC
    adapter_cls = adapter_registry.REGISTRY.get(ats)
    if adapter_cls is None:
        print(f"ERROR: unknown ATS '{ats}'. Valid: {', '.join(adapter_registry.REGISTRY)}")
        sys.exit(1)

    company = CC(name=slug, ats=ats, slug=slug)
    headers = {"User-Agent": "job-watch/0.1 check-slug"}
    timeout = httpx.Timeout(30)

    print(f"Checking {ats}/{slug} …")
    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            adapter = adapter_cls()
            jobs = await adapter.fetch_jobs(company, client)
        if jobs:
            print(f"OK — {len(jobs)} open job(s) returned.")
            print("Sample titles:")
            for j in jobs[:5]:
                print(f"  {j.title!r}  [{j.location}]")
        else:
            print("WARNING — request succeeded but 0 jobs returned. Slug may be wrong.")
    except httpx.HTTPStatusError as exc:
        print(f"ERROR — HTTP {exc.response.status_code}: {exc.response.text[:200]}")
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR — {exc}")
        sys.exit(1)


# ── CLI entry ────────────────────────────────────────────────────────────────

def cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="job-watch",
        description="Monitor ATS job boards for new matching roles.",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Save current jobs to state WITHOUT sending an email (use on first run).",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to companies.yaml (default: auto-detected).",
    )

    sub = parser.add_subparsers(dest="command")
    cs = sub.add_parser("check-slug", help="Verify a company slug returns jobs.")
    cs.add_argument("ats", choices=list(adapter_registry.REGISTRY))
    cs.add_argument("slug")

    ms = sub.add_parser("mcp", help="Start the MCP server for Claude integration.")
    ms.add_argument(
        "--http",
        action="store_true",
        help="Use streamable HTTP transport instead of stdio (for cluster deployment).",
    )
    ms.add_argument("--host", default="0.0.0.0", help="HTTP bind address (default: 0.0.0.0).")
    ms.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000).")

    args = parser.parse_args()

    if args.command == "check-slug":
        asyncio.run(check_slug(args.ats, args.slug))
    elif args.command == "mcp":
        from job_watch.mcp.server import run_http, run_stdio
        if args.http:
            run_http(host=args.host, port=args.port)
        else:
            run_stdio()
    else:
        asyncio.run(run(seed=args.seed, config_path=args.config))


if __name__ == "__main__":
    cli()
