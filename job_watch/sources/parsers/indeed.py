"""Parser for Indeed job-alert emails (donotreply@match.indeed.com).

Indeed alerts contain a list of job cards.  Each card has the job title
as a link, with the company name and location in adjacent text.
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

_JOB_PATH_PATTERNS = {"/rc/clk", "/viewjob", "/pagead", "/m/"}


def parse(html_body: str, sender: str, message_id: str) -> list[ExtractedLead]:
    soup = BeautifulSoup(html_body, "html.parser")
    leads: list[ExtractedLead] = []

    # Indeed job cards: <a> with indeed.com/rc/clk or /viewjob links
    # Require job-specific URL patterns to avoid footer/nav links
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "indeed.com" not in href:
            continue
        # Require job-specific URL patterns
        if not any(pat in href for pat in _JOB_PATH_PATTERNS):
            continue

        title = anchor.get_text(strip=True)
        if not title or len(title) > 120:
            continue
        # Skip navigation links
        if title.lower() in {"view all jobs", "unsubscribe", "indeed", "view job"}:
            continue
        if title.lower().strip() in _FOOTER_TEXTS:
            continue
        # Max-word guard: job titles are rarely more than 10 words
        if len(title.split()) > 10:
            continue

        company = ""
        location = ""

        # Company and location often follow the title link as plain text siblings
        for sibling in anchor.find_next_siblings(string=True):
            s = sibling.strip()
            if s and not company:
                company = s
            elif s and not location:
                location = s
                break

        # Also try structured parent container
        if not company:
            parent = anchor.find_parent(["td", "div", "li"])
            if parent:
                text = parent.get_text(" ", strip=True)
                # Remove the title from the text and take what remains
                remainder = text.replace(title, "").strip()
                parts = re.split(r"[·\-\n|]", remainder)
                parts = [p.strip() for p in parts if p.strip()]
                if parts:
                    company = parts[0]
                if len(parts) > 1:
                    location = parts[1]

        if company and len(company) <= 100:
            leads.append(ExtractedLead(
                company_name_raw=company,
                job_title=title,
                location_raw=location,
                sender=sender,
                gmail_message_id=message_id,
            ))

    return leads
