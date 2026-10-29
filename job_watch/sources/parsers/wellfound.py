"""Parser for Wellfound (formerly AngelList Talent) job-alert emails (team@hi.wellfound.com).

Wellfound alerts list startup jobs.  The HTML typically includes company name,
job title, and location in a structured card layout.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from job_watch.models import ExtractedLead

_FOOTER_TEXTS = {
    "manage settings", "privacy policy", "terms of use", "terms & conditions",
    "unsubscribe", "view in browser", "help center", "contact us",
    "email preferences", "opt out", "do not sell", "cookie policy",
    "copyright", "all rights reserved", "download the app",
}

_JOB_PATH_PATTERNS = {"/jobs/", "/company/", "/role/"}


def parse(html_body: str, sender: str, message_id: str) -> list[ExtractedLead]:
    soup = BeautifulSoup(html_body, "html.parser")
    leads: list[ExtractedLead] = []

    # Wellfound job links point to wellfound.com/jobs or angel.co
    # Require job-specific URL patterns to avoid footer/nav links
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "wellfound.com" not in href and "angel.co" not in href:
            continue
        # Require job-specific URL patterns for wellfound.com links
        if "wellfound.com" in href and not any(pat in href for pat in _JOB_PATH_PATTERNS):
            continue

        title = anchor.get_text(strip=True)
        if not title or len(title) > 120:
            continue
        if title.lower() in {"view all", "unsubscribe", "wellfound", "view job", "apply"}:
            continue
        if title.lower().strip() in _FOOTER_TEXTS:
            continue
        # Max-word guard: job titles are rarely more than 10 words
        if len(title.split()) > 10:
            continue

        company = ""
        location = ""

        # Look for company name: often a heading above the job title link
        parent = anchor.find_parent(["td", "div", "li", "table"])
        if parent:
            # Previous sibling or heading elements often hold the company name
            prev = anchor.find_previous(["h1", "h2", "h3", "strong", "b"])
            if prev and prev.find_parent(["td", "div", "table"]) == parent.find_parent(["td", "div", "table"]):
                company = prev.get_text(strip=True)

            if not company:
                text = parent.get_text(" ", strip=True)
                # Pattern: "Company · Title · Location"
                parts = re.split(r"[·\n|]", text)
                parts = [p.strip() for p in parts if p.strip() and len(p.strip()) <= 100]
                # Title is somewhere in parts; company usually comes first
                for i, p in enumerate(parts):
                    if title.lower() in p.lower() and i > 0:
                        company = parts[i - 1]
                        location = parts[i + 1] if i + 1 < len(parts) else ""
                        break
                if not company and parts:
                    company = parts[0]

        if company and len(company) <= 100 and company.lower() != title.lower():
            leads.append(ExtractedLead(
                company_name_raw=company,
                job_title=title,
                location_raw=location,
                sender=sender,
                gmail_message_id=message_id,
            ))

    return leads
