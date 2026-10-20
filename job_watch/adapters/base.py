"""Common adapter interface and shared HTTP helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import httpx

from job_watch.models import CompanyConfig, Job

logger = logging.getLogger(__name__)

# Retry settings
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class ATSAdapter(Protocol):
    async def fetch_jobs(
        self, company: CompanyConfig, client: httpx.AsyncClient
    ) -> list[Job]:
        """Return all current open jobs for *company*. Raise on unrecoverable error."""
        ...


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
) -> httpx.Response:
    """GET with exponential backoff on transient errors."""
    delay = 2.0
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                logger.warning("HTTP %s for %s — retry %d/%d in %.0fs",
                               resp.status_code, url, attempt, _MAX_RETRIES, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp
        except httpx.TimeoutException:
            if attempt == _MAX_RETRIES:
                raise
            logger.warning("Timeout on %s — retry %d/%d in %.0fs",
                           url, attempt, _MAX_RETRIES, delay)
            await asyncio.sleep(delay)
            delay *= 2

    raise RuntimeError(f"Unreachable: exhausted retries for {url}")


def _strip_html(html: str) -> str:
    """Very lightweight HTML tag stripper — no extra deps."""
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def snippet(raw: str, max_len: int = 2000) -> str:
    """Return a plain-text snippet from raw HTML or plain text."""
    text = _strip_html(raw) if "<" in raw else raw
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "…"
