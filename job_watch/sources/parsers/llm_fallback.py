"""LLM fallback parser — used when no HTML parser yields results.

Sends only the subject line and visible text (NOT the raw body) to Claude.
Logs which path was used.  Never logs email body content.
"""

from __future__ import annotations

import json
import logging

import anthropic
from bs4 import BeautifulSoup

from job_watch.models import ExtractedLead

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are extracting job postings from a job-alert email.
Return a JSON array.  Each element must have exactly these keys:
  "company": string, the employer name
  "title": string, the job title
  "location": string, city/state or "Remote" or ""

Return only the JSON array, no other text.
If no job postings are found, return [].
Limit to at most 20 results."""


async def parse_with_llm(
    html_body: str,
    sender: str,
    message_id: str,
    model: str,
) -> list[ExtractedLead]:
    """Extract leads via LLM.  Uses visible text only — never the raw HTML."""
    visible_text = BeautifulSoup(html_body, "html.parser").get_text(" ", strip=True)
    # Truncate to avoid large token counts; subject + job cards are typically < 3000 chars
    visible_text = visible_text[:3000]

    logger.info(
        "llm_fallback: used for sender=%s message_id=%s",
        sender,
        message_id,
    )

    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": visible_text}],
        )
        raw = resp.content[0].text.strip()
    except Exception as exc:
        logger.warning("llm_fallback: API error for message_id=%s: %s", message_id, exc)
        return []

    # Strip markdown fences if the model wrapped the JSON
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("expected list")
    except Exception:
        logger.warning("llm_fallback: malformed JSON for message_id=%s", message_id)
        return []

    leads: list[ExtractedLead] = []
    for item in items:
        company = str(item.get("company", "")).strip()
        title = str(item.get("title", "")).strip()
        location = str(item.get("location", "")).strip()
        if company and title and len(company) <= 100:
            leads.append(ExtractedLead(
                company_name_raw=company,
                job_title=title,
                location_raw=location,
                sender=sender,
                gmail_message_id=message_id,
            ))
    return leads
