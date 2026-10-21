"""Lever ATS adapter.

API: GET https://api.lever.co/v0/postings/{slug}?mode=json
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from job_watch.adapters.base import full_text, get_with_retry, snippet
from job_watch.models import CompanyConfig, Job

logger = logging.getLogger(__name__)

_BASE = "https://api.lever.co/v0/postings"


class LeverAdapter:
    async def fetch_jobs(
        self, company: CompanyConfig, client: httpx.AsyncClient
    ) -> list[Job]:
        url = f"{_BASE}/{company.slug}"
        resp = await get_with_retry(client, url, params={"mode": "json"})
        raw_jobs = resp.json()
        if not isinstance(raw_jobs, list):
            raw_jobs = raw_jobs.get("postings", [])
        logger.info("Lever %s: %d jobs fetched", company.name, len(raw_jobs))

        jobs: list[Job] = []
        for r in raw_jobs:
            categories = r.get("categories", {})
            location = categories.get("location", "") or ""
            remote = _is_remote(location)
            department = categories.get("department", "") or categories.get("team", "")

            posted_at: datetime | None = None
            raw_ts = r.get("createdAt")
            if raw_ts:
                try:
                    # Lever timestamps are milliseconds since epoch
                    posted_at = datetime.fromtimestamp(raw_ts / 1000, tz=timezone.utc)
                except Exception:
                    pass

            desc = r.get("descriptionPlain") or r.get("description", "") or ""
            jobs.append(Job(
                id=str(r["id"]),
                company=company.name,
                ats="lever",
                title=r.get("text", ""),
                location=location,
                remote=remote,
                department=department,
                url=r.get("hostedUrl", ""),
                posted_at=posted_at,
                description_snippet=snippet(desc),
                description=full_text(desc),
            ))
        return jobs


def _is_remote(location: str) -> bool:
    loc = location.lower()
    return "remote" in loc or "anywhere" in loc or "work from home" in loc
