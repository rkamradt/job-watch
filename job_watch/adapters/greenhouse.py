"""Greenhouse ATS adapter.

API: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from dateutil import parser as dtparser

from job_watch.adapters.base import full_text, get_with_retry, snippet
from job_watch.models import CompanyConfig, Job

logger = logging.getLogger(__name__)

_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter:
    async def fetch_jobs(
        self, company: CompanyConfig, client: httpx.AsyncClient
    ) -> list[Job]:
        url = f"{_BASE}/{company.slug}/jobs"
        resp = await get_with_retry(client, url, params={"content": "true"})
        data = resp.json()
        raw_jobs = data.get("jobs", [])
        logger.info("Greenhouse %s: %d jobs fetched", company.name, len(raw_jobs))

        jobs: list[Job] = []
        for r in raw_jobs:
            location = r.get("location", {}).get("name", "") or ""
            remote = _is_remote(location)
            department = ""
            depts = r.get("departments", [])
            if depts:
                department = depts[0].get("name", "")

            posted_at: datetime | None = None
            raw_date = r.get("updated_at") or r.get("created_at")
            if raw_date:
                try:
                    posted_at = dtparser.parse(raw_date).astimezone(timezone.utc)
                except Exception:
                    pass

            content = r.get("content", "") or ""
            jobs.append(Job(
                id=str(r["id"]),
                company=company.name,
                ats="greenhouse",
                title=r.get("title", ""),
                location=location,
                remote=remote,
                department=department,
                url=r.get("absolute_url", ""),
                posted_at=posted_at,
                description_snippet=snippet(content),
                description=full_text(content),
            ))
        return jobs


def _is_remote(location: str) -> bool:
    loc = location.lower()
    return "remote" in loc or "anywhere" in loc or "work from home" in loc
