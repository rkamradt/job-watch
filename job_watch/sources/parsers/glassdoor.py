"""Parser for Glassdoor job-alert emails (noreply@glassdoor.com).

Glassdoor alerts list jobs with title, company name, location, and salary range.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from job_watch.models import ExtractedLead


def parse(html_body: str, sender: str, message_id: str) -> list[ExtractedLead]:
    soup = BeautifulSoup(html_body, "html.parser")
    leads: list[ExtractedLead] = []

    # Glassdoor job links point to glassdoor.com/job-listing or /partner/jobListing
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "glassdoor.com" not in href:
            continue

        title = anchor.get_text(strip=True)
        if not title or len(title) > 120:
            continue
        if title.lower() in {"view all jobs", "unsubscribe", "glassdoor", "view job", "apply now"}:
            continue

        company = ""
        location = ""

        parent = anchor.find_parent(["td", "div", "li"])
        if parent:
            text = parent.get_text(" ", strip=True)
            # Glassdoor cards: "Title · Company · Location · Salary"
            parts = re.split(r"[·\n|—]", text)
            parts = [p.strip() for p in parts if p.strip() and len(p.strip()) <= 120]
            for i, p in enumerate(parts):
                if title.lower() in p.lower():
                    company = parts[i + 1] if i + 1 < len(parts) else ""
                    location = parts[i + 2] if i + 2 < len(parts) else ""
                    break

        if company and len(company) <= 100:
            leads.append(ExtractedLead(
                company_name_raw=company,
                job_title=title,
                location_raw=location,
                sender=sender,
                gmail_message_id=message_id,
            ))

    return leads
