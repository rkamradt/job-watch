"""Per-sender HTML parsers for job-alert emails.

Each parser receives a decoded HTML body string and returns a list of
ExtractedLead objects.  An empty list means "nothing useful found."

The registry maps a normalized sender address to its parser function.
"""

from __future__ import annotations

import logging

from job_watch.models import ExtractedLead
from job_watch.sources.parsers import glassdoor, indeed, linkedin, wellfound

logger = logging.getLogger(__name__)

# Maps lowercase sender address → parser callable
REGISTRY: dict[str, object] = {
    "jobalerts-noreply@linkedin.com": linkedin.parse,
    "jobs-noreply@linkedin.com": linkedin.parse,
    "donotreply@match.indeed.com": indeed.parse,
    "team@hi.wellfound.com": wellfound.parse,
    "noreply@glassdoor.com": glassdoor.parse,
}


def run_parsers(
    html_body: str,
    sender: str,
    message_id: str,
) -> list[ExtractedLead]:
    """Try the sender-specific parser; return its leads (may be empty)."""
    parser = REGISTRY.get(sender.lower())
    if parser is None:
        return []
    leads: list[ExtractedLead] = parser(html_body, sender, message_id)  # type: ignore[call-arg]
    logger.info(
        "parsers: sender=%s html_leads=%d message_id=%s",
        sender,
        len(leads),
        message_id,
    )
    return leads
