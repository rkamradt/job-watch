"""MongoDB state layer for tracking seen/closed jobs."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import motor.motor_asyncio

from job_watch.models import Job

logger = logging.getLogger(__name__)


class JobStore:
    def __init__(self, mongo_uri: str) -> None:
        self._client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self._col = self._client["job-watch"]["seen_jobs"]

    async def ensure_index(self) -> None:
        await self._col.create_index(
            [("ats", 1), ("company", 1), ("id", 1)],
            unique=True,
            name="ats_company_id",
        )

    async def is_new(self, job: Job) -> bool:
        doc = await self._col.find_one(
            {"ats": job.ats, "company": job.company, "id": job.id},
            projection={"_id": 1},
        )
        return doc is None

    async def upsert(self, job: Job, now: datetime) -> None:
        """Insert new job or refresh last_seen on existing one."""
        await self._col.update_one(
            {"ats": job.ats, "company": job.company, "id": job.id},
            {
                "$set": {
                    "last_seen": now,
                    "title": job.title,
                    "url": job.url,
                    "location": job.location,
                    "department": job.department,
                    "remote": job.remote,
                },
                "$setOnInsert": {
                    "first_seen": now,
                    "closed_at": None,
                },
            },
            upsert=True,
        )

    async def mark_closed(
        self,
        ats: str,
        company: str,
        active_ids: set[str],
        now: datetime,
    ) -> int:
        """Mark any jobs for this company not in active_ids as closed."""
        result = await self._col.update_many(
            {
                "ats": ats,
                "company": company,
                "id": {"$nin": list(active_ids)},
                "closed_at": None,
            },
            {"$set": {"closed_at": now}},
        )
        if result.modified_count:
            logger.info("%s/%s: marked %d job(s) closed",
                        ats, company, result.modified_count)
        return result.modified_count

    def close(self) -> None:
        self._client.close()
