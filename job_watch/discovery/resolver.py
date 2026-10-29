"""ATS resolver — given a company name, find its job-board slug.

For each new discovered company we generate candidate slugs and probe the
three supported ATS APIs (Greenhouse, Lever, Ashby) in sequence.  A board
counts as found only if it returns at least one posting.

Rate-limiting:
  A configurable delay (default 2 s) is inserted between probe attempts.

Negative caching:
  When no board is found, a TTL timestamp is stored in the discovered_companies
  document so we skip re-probing until the TTL expires.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from job_watch import adapters as adapter_registry
from job_watch.models import CompanyConfig

logger = logging.getLogger(__name__)

_ATS_ORDER = ["greenhouse", "lever", "ashby"]


@dataclass
class ProbeResult:
    ats: str
    slug: str
    job_count: int


def normalize_company(name: str) -> str:
    """Return a stable lowercase key for deduplication.

    "Toast, Inc."  → "toast"
    "Hims & Hers"  → "hims hers"
    "23andMe"      → "23andme"
    """
    n = name.lower().strip()
    n = re.sub(r"[,\.'\"\(\)&]", " ", n)
    n = re.sub(r"\b(inc|llc|ltd|corp|co|plc|gmbh|ag|the)\b", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def generate_slugs(name: str) -> list[str]:
    """Generate candidate ATS board slugs from a company name.

    Returns a short list ordered from most-likely to least-likely.
    """
    base = normalize_company(name)

    # hyphenated version: "stripe inc" → "stripe"
    hyphenated = re.sub(r"\s+", "-", base).strip("-")
    # no-separator version: "stripe inc" → "stripe"
    no_sep = re.sub(r"\s+", "", base)
    # original words joined by hyphen before suffix stripping
    raw_hyph = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    seen: set[str] = set()
    candidates: list[str] = []
    for s in [hyphenated, no_sep, raw_hyph]:
        s = s.strip("-")
        if s and s not in seen:
            seen.add(s)
            candidates.append(s)
    return candidates


class ATSResolver:
    """Probe ATS APIs to find a company's job board."""

    def __init__(
        self,
        rate_limit_delay: float = 2.0,
        request_timeout: float = 15.0,
        user_agent: str = "job-watch/0.1 discovery/probe",
    ) -> None:
        self._delay = rate_limit_delay
        self._timeout = httpx.Timeout(request_timeout)
        self._headers = {"User-Agent": user_agent}

    async def probe(self, company_name: str) -> tuple[ProbeResult | None, list[str]]:
        """Try all ATS × slug combinations.

        Returns (ProbeResult | None, slugs_tried).
        Slugs_tried lists every (ats/slug) pair attempted for logging/caching.
        """
        slugs = generate_slugs(company_name)
        slugs_tried: list[str] = []

        async with httpx.AsyncClient(
            headers=self._headers, timeout=self._timeout
        ) as client:
            for ats_name in _ATS_ORDER:
                adapter_cls = adapter_registry.REGISTRY.get(ats_name)
                if adapter_cls is None:
                    continue
                adapter = adapter_cls()

                for slug in slugs:
                    slugs_tried.append(f"{ats_name}/{slug}")
                    await asyncio.sleep(self._delay)

                    company = CompanyConfig(name=company_name, ats=ats_name, slug=slug)
                    try:
                        jobs = await adapter.fetch_jobs(company, client)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 429:
                            logger.warning(
                                "resolver: rate-limited on %s/%s — backing off 10 s",
                                ats_name, slug,
                            )
                            await asyncio.sleep(10)
                        # 404 is expected for wrong slugs; any non-2xx → next candidate
                        continue
                    except Exception as exc:
                        logger.debug("resolver: %s/%s error: %s", ats_name, slug, exc)
                        continue

                    if jobs:
                        logger.info(
                            "resolver: found %s on %s/%s (%d jobs)",
                            company_name, ats_name, slug, len(jobs),
                        )
                        return ProbeResult(ats=ats_name, slug=slug, job_count=len(jobs)), slugs_tried

        logger.info("resolver: no board found for %r (tried %s)", company_name, ", ".join(slugs_tried))
        return None, slugs_tried
