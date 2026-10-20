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

    store.close()

    if not new_matches:
        logger.info("No new matches this run.")
        return

    logger.info("%d new match(es) found after keyword filter.", len(new_matches))

    # AI screening (second pass) — only runs when enabled in config
    if cfg.screening.enabled:
        screened = await screen_jobs(new_matches, cfg.screening)
    else:
        screened = [ScreenedJob(scored=s, ai_passed=True, ai_reason="") for s in new_matches]

    if not screened:
        logger.info("No matches survived AI screening this run.")
        return

    if seed:
        logger.info("--seed mode: state saved, no email sent.")
        for item in screened:
            j = item.scored.job
            logger.info("  [seeded] %s — %s (%s)", j.company, j.title, j.location)
        return

    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    gmail_to = os.environ.get("GMAIL_TO", "randykamradt@gmail.com")

    send_digest(
        screened,
        gmail_user=gmail_user,
        gmail_app_password=gmail_app_password,
        to_addr=gmail_to,
    )


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

    args = parser.parse_args()

    if args.command == "check-slug":
        asyncio.run(check_slug(args.ats, args.slug))
    else:
        asyncio.run(run(seed=args.seed, config_path=args.config))


if __name__ == "__main__":
    cli()
