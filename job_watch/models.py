from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


ReviewStatus = Literal["new", "interested", "applied", "skipped"]


@dataclass
class Job:
    id: str
    company: str
    ats: str
    title: str
    location: str
    remote: bool
    department: str
    url: str
    posted_at: Optional[datetime]
    description_snippet: str  # plain text, max ~2000 chars
    description: str = ""    # full plain text, stored in MongoDB on first insert


# ── Config models (Pydantic, loaded from YAML) ──────────────────────────────

class CompanyConfig(BaseModel):
    name: str
    ats: str
    slug: str


class FilterConfig(BaseModel):
    title_include: list[str] = Field(default_factory=lambda: [
        "principal", "staff", "lead", "senior staff",
    ])
    title_engineering: list[str] = Field(default_factory=lambda: [
        "engineer", "developer", "architect", "swe", "software",
    ])
    title_exclude: list[str] = Field(default_factory=lambda: [
        "manager", "director", "intern", "data scientist", "sales",
        "recruiter", "business development", "emea", "apac", "latam",
        "designer", "product manager", "program manager", "analyst",
    ])
    location_keep_patterns: list[str] = Field(default_factory=lambda: [
        "remote", "united states", " us ", "u.s.", "maine", "boston",
        "anywhere", "nationwide", "new england",
    ])
    location_drop_patterns: list[str] = Field(default_factory=lambda: [
        "united kingdom", " uk", "england", "london",
        "ireland", "dublin",
        "canada", "toronto", "vancouver",
        "europe", "germany", "france", "netherlands", "spain",
        "sweden", "denmark", "norway", "finland", "poland",
        "bulgaria", "romania", "czech",
        "australia", "sydney", "melbourne",
        "india", "bangalore", "pune", "hyderabad",
        "singapore", "hong kong", "japan", "china", "brazil",
        "mexico", "israel", "tel aviv", "new zealand", "south africa",
        "chennai", "taiwan", "taipei",
    ])
    boost_keywords: list[str] = Field(default_factory=lambda: [
        "kafka", "kubernetes", "event-driven", "platform",
        "java", "kotlin", " ai", "llm", "machine learning",
    ])


class ScreeningConfig(BaseModel):
    enabled: bool = False
    model: str = "claude-haiku-4-5-20251001"
    candidate_profile: str = ""


DiscoveredStatus = Literal["candidate", "active", "rejected"]


class GmailDiscoveryConfig(BaseModel):
    enabled: bool = False
    alert_senders: list[str] = Field(default_factory=lambda: [
        "jobalerts-noreply@linkedin.com",
        "jobs-noreply@linkedin.com",
        "donotreply@match.indeed.com",
        "team@hi.wellfound.com",
        "noreply@glassdoor.com",
    ])
    llm_fallback_enabled: bool = True
    llm_fallback_model: str = "claude-haiku-4-5-20251001"
    auto_activate: bool = False
    probe_rate_limit_delay: float = 2.0
    negative_cache_ttl_days: int = 30


class AppConfig(BaseModel):
    companies: list[CompanyConfig]
    filter: FilterConfig = Field(default_factory=FilterConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    gmail_discovery: GmailDiscoveryConfig = Field(default_factory=GmailDiscoveryConfig)
    concurrency_limit: int = 3
    request_timeout_sec: int = 30
    user_agent: str = "job-watch/0.1 (+https://github.com/rkamradt/job-watch)"


@dataclass
class ExtractedLead:
    """A company/job lead parsed from a single job-alert email."""
    company_name_raw: str
    job_title: str
    location_raw: str
    sender: str
    gmail_message_id: str
