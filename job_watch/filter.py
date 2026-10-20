"""Title and location filtering, plus keyword scoring."""

from __future__ import annotations

from dataclasses import dataclass

from job_watch.models import FilterConfig, Job


@dataclass
class ScoredJob:
    job: Job
    score: int
    matched_keywords: list[str]


def passes_title(title: str, cfg: FilterConfig) -> bool:
    t = title.lower()
    if not any(kw in t for kw in cfg.title_include):
        return False
    if any(kw in t for kw in cfg.title_exclude):
        return False
    return True


def passes_location(location: str, cfg: FilterConfig) -> bool:
    """Return True if the location is acceptable (US / remote / unknown)."""
    loc = location.lower()

    # Explicit non-US mention → drop
    if any(pat in loc for pat in cfg.location_drop_patterns):
        return False

    # Explicit US/remote mention → keep
    if not loc or any(pat in loc for pat in cfg.location_keep_patterns):
        return True

    # Ambiguous (city name only, etc.) → keep; better to show than miss
    return True


def score(job: Job, cfg: FilterConfig) -> ScoredJob:
    haystack = (job.title + " " + job.description_snippet).lower()
    matched = [kw for kw in cfg.boost_keywords if kw in haystack]
    return ScoredJob(job=job, score=len(matched), matched_keywords=matched)


def filter_and_score(jobs: list[Job], cfg: FilterConfig) -> list[ScoredJob]:
    results: list[ScoredJob] = []
    for job in jobs:
        if not passes_title(job.title, cfg):
            continue
        if not passes_location(job.location, cfg):
            continue
        results.append(score(job, cfg))
    # Sort by score descending
    results.sort(key=lambda s: s.score, reverse=True)
    return results
