"""Ashby ATS adapter.

API: GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from dateutil import parser as dtparser

from job_watch.adapters.base import full_text, get_with_retry, snippet
from job_watch.models import CompanyConfig, Job

logger = logging.getLogger(__name__)

_BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyAdapter:
    async def fetch_jobs(
        self, company: CompanyConfig, client: httpx.AsyncClient
    ) -> list[Job]:
        url = f"{_BASE}/{company.slug}"
        resp = await get_with_retry(
            client, url, params={"includeCompensation": "true"}
        )
        data = resp.json()
        raw_jobs = data.get("jobPostings", [])
        logger.info("Ashby %s: %d jobs fetched", company.name, len(raw_jobs))

        jobs: list[Job] = []
        for r in raw_jobs:
            location = r.get("location", "") or ""
            # Ashby also has isRemote flag
            remote = r.get("isRemote", False) or _is_remote(location)
            department = r.get("department", "") or ""

            posted_at: datetime | None = None
            raw_date = r.get("publishedDate") or r.get("updatedAt")
            if raw_date:
                try:
                    posted_at = dtparser.parse(raw_date).astimezone(timezone.utc)
                except Exception:
                    pass

            desc = r.get("descriptionHtml") or r.get("descriptionPlain", "") or ""
            jobs.append(Job(
                id=str(r["id"]),
                company=company.name,
                ats="ashby",
                title=r.get("title", ""),
                location=location,
                remote=remote,
                department=department,
                url=r.get("jobUrl", ""),
                posted_at=posted_at,
                description_snippet=snippet(desc),
                description=full_text(desc),
            ))
        return jobs


def _is_remote(location: str) -> bool:
    loc = location.lower()
    return "remote" in loc or "anywhere" in loc or "work from home" in loc
