"""Parser for LinkedIn job-alert emails.

Handles both jobalerts-noreply@linkedin.com and jobs-noreply@linkedin.com.

LinkedIn alert HTML contains a list of job cards.  Each card typically has:
  - An <a> tag whose text is the job title and whose href contains the company
    name as a query param, or
  - Sibling elements with company name and location.

We target the most stable selectors observed across alert formats.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from job_watch.models import ExtractedLead


def parse(html_body: str, sender: str, message_id: str) -> list[ExtractedLead]:
    soup = BeautifulSoup(html_body, "html.parser")
    leads: list[ExtractedLead] = []

    # Format 1: job cards with data attributes or structured table rows
    # Each job block contains title, company, and location in adjacent elements.
    # We look for patterns like "Title at Company" or separate <td>/<div> cells.

    # Try to find job title links — LinkedIn uses <a> tags with job titles
    # followed by company name in a nearby <span> or <td>.
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "linkedin.com/jobs" not in href and "linkedin.com/comm/jobs" not in href:
            continue

        title = anchor.get_text(strip=True)
        if not title or len(title) > 120:
            continue

        # Company name: look in the next sibling element or parent's next child
        company = ""
        location = ""

        parent = anchor.parent
        # Walk up one level if needed to find sibling text nodes
        for elem in (parent, anchor.find_next_sibling(), anchor.find_parent("td")):
            if elem is None:
                continue
            text = elem.get_text(" ", strip=True)
            # LinkedIn often has "Title · Company · Location" or "Title\nCompany\nLocation"
            parts = re.split(r"[·\n|]", text)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) >= 2 and parts[0].lower() in title.lower():
                company = parts[1] if len(parts) > 1 else ""
                location = parts[2] if len(parts) > 2 else ""
                break

        if not company:
            # Fallback: look for the next non-link text element
            nxt = anchor.find_next(string=True)
            if nxt:
                company = nxt.strip()

        if company and len(company) <= 100:
            leads.append(ExtractedLead(
                company_name_raw=company,
                job_title=title,
                location_raw=location,
                sender=sender,
                gmail_message_id=message_id,
            ))

    # Format 2: subject-line style "N new jobs for {title} at {company}"
    # Seen in digest-style alerts; extract from the email body text.
    if not leads:
        text = soup.get_text(" ", strip=True)
        for m in re.finditer(
            r"(?:new job|new jobs?)\s+(?:for|matching)[^:]*?:\s*(.+?)\s+at\s+(.+?)(?:\s*[-·|]|$)",
            text,
            re.IGNORECASE,
        ):
            title_raw, company_raw = m.group(1).strip(), m.group(2).strip()
            if title_raw and company_raw and len(company_raw) <= 100:
                leads.append(ExtractedLead(
                    company_name_raw=company_raw,
                    job_title=title_raw,
                    location_raw="",
                    sender=sender,
                    gmail_message_id=message_id,
                ))

    return leads
