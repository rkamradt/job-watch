"""MongoDB state layer for tracking seen/closed jobs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import motor.motor_asyncio
from bson import ObjectId

from job_watch.models import Job, ReviewStatus

logger = logging.getLogger(__name__)


def _doc_to_dict(doc: dict) -> dict:
    """Convert a MongoDB document to a JSON-serializable dict."""
    result = dict(doc)
    result["job_id"] = str(result.pop("_id"))
    for key in ("first_seen", "last_seen", "closed_at", "posted_at"):
        val = result.get(key)
        if isinstance(val, datetime):
            result[key] = val.isoformat()
    review = result.get("review")
    if isinstance(review, dict):
        updated = review.get("updated_at")
        if isinstance(updated, datetime):
            review["updated_at"] = updated.isoformat()
    return result


class GmailStateStore:
    """Tracks Gmail history ID so each message is read exactly once."""

    def __init__(self, client: motor.motor_asyncio.AsyncIOMotorClient) -> None:
        self._col = client["job-watch"]["gmail_state"]

    async def get_history_id(self) -> str | None:
        doc = await self._col.find_one({"_id": "singleton"})
        return doc["history_id"] if doc else None

    async def set_history_id(self, history_id: str, checked_at: datetime) -> None:
        await self._col.update_one(
            {"_id": "singleton"},
            {"$set": {"history_id": history_id, "last_checked": checked_at}},
            upsert=True,
        )


class JobStore:
    def __init__(self, mongo_uri: str) -> None:
        self._client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        self._col = self._client["job-watch"]["seen_jobs"]
        self.gmail_state = GmailStateStore(self._client)

    async def ensure_index(self) -> None:
        await self._col.create_index(
            [("ats", 1), ("company", 1), ("id", 1)],
            unique=True,
            name="ats_company_id",
        )
        await self._col.create_index(
            [("title", "text"), ("description", "text")],
            name="title_description_text",
        )
        await self._col.create_index(
            [("first_seen", -1)],
            name="first_seen_desc",
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
                    "description": job.description,
                    "posted_at": job.posted_at,
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

    # ── MCP query methods ─────────────────────────────────────────────────────

    async def search_jobs(
        self,
        *,
        query: str = "",
        company: str = "",
        since_days: int = 0,
        remote_only: bool = False,
        status: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """Full-text search over open jobs. status='' means all open jobs."""
        filt: dict[str, Any] = {"closed_at": None}

        if query:
            filt["$text"] = {"$search": query}

        if company:
            filt["company"] = {"$regex": company, "$options": "i"}

        if since_days > 0:
            cutoff = datetime.now(tz=timezone.utc) - timedelta(days=since_days)
            filt["first_seen"] = {"$gte": cutoff}

        if remote_only:
            filt["remote"] = True

        if status:
            if status == "new":
                filt["$or"] = [
                    {"review.status": "new"},
                    {"review": {"$exists": False}},
                ]
            else:
                filt["review.status"] = status

        if query:
            projection = {"score": {"$meta": "textScore"}}
            sort: list = [("score", {"$meta": "textScore"})]
            cursor = self._col.find(filt, projection).sort(sort).limit(limit)
        else:
            cursor = self._col.find(filt).sort([("first_seen", -1)]).limit(limit)

        docs = await cursor.to_list(length=limit)
        return [_doc_to_dict(doc) for doc in docs]

    async def get_job(self, job_id: str) -> dict | None:
        """Fetch a single job by its MongoDB _id (hex string)."""
        try:
            oid = ObjectId(job_id)
        except Exception:
            return None
        doc = await self._col.find_one({"_id": oid})
        return _doc_to_dict(doc) if doc else None

    async def new_since(self, days: int) -> list[dict]:
        """Return open jobs first seen within the last N days, newest first."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        cursor = self._col.find(
            {"first_seen": {"$gte": cutoff}, "closed_at": None}
        ).sort([("first_seen", -1)])
        docs = await cursor.to_list(length=None)
        return [_doc_to_dict(doc) for doc in docs]

    async def get_open_count_by_company(self) -> dict[str, int]:
        pipeline = [
            {"$match": {"closed_at": None}},
            {"$group": {"_id": "$company", "count": {"$sum": 1}}},
        ]
        result: dict[str, int] = {}
        async for doc in self._col.aggregate(pipeline):
            result[doc["_id"]] = doc["count"]
        return result

    async def get_stats(self) -> dict:
        now = datetime.now(tz=timezone.utc)
        week_ago = now - timedelta(days=7)

        total_open = await self._col.count_documents({"closed_at": None})
        new_this_week = await self._col.count_documents(
            {"first_seen": {"$gte": week_ago}, "closed_at": None}
        )

        by_company: dict[str, int] = {}
        async for doc in self._col.aggregate([
            {"$match": {"closed_at": None}},
            {"$group": {"_id": "$company", "count": {"$sum": 1}}},
        ]):
            by_company[doc["_id"]] = doc["count"]

        by_status: dict[str, int] = {}
        async for doc in self._col.aggregate([
            {"$match": {"closed_at": None}},
            {"$group": {
                "_id": {"$ifNull": ["$review.status", "new"]},
                "count": {"$sum": 1},
            }},
        ]):
            by_status[doc["_id"]] = doc["count"]

        return {
            "total_open": total_open,
            "new_this_week": new_this_week,
            "by_company": by_company,
            "by_review_status": by_status,
        }

    async def update_review(
        self,
        job_id: str,
        status: ReviewStatus,
        note: str | None = None,
    ) -> bool:
        """Set review status (and optional note) on a job. Returns True if found."""
        now = datetime.now(tz=timezone.utc)
        set_fields: dict[str, Any] = {
            "review.status": status,
            "review.updated_at": now,
        }
        if note is not None:
            set_fields["review.note"] = note
        try:
            result = await self._col.update_one(
                {"_id": ObjectId(job_id)},
                {"$set": set_fields},
            )
        except Exception:
            return False
        return result.matched_count > 0

    def close(self) -> None:
        self._client.close()
