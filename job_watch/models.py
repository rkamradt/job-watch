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


class AppConfig(BaseModel):
    companies: list[CompanyConfig]
    filter: FilterConfig = Field(default_factory=FilterConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    concurrency_limit: int = 3
    request_timeout_sec: int = 30
    user_agent: str = "job-watch/0.1 (+https://github.com/rkamradt/job-watch)"
