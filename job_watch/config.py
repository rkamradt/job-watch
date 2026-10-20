"""Load AppConfig from a YAML file.

Resolution order:
  1. Path passed explicitly to load()
  2. JOB_WATCH_CONFIG env var
  3. /config/companies.yaml  (Kubernetes ConfigMap mount)
  4. ./companies.yaml        (local dev / Docker image default)
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from job_watch.models import AppConfig

_SEARCH_PATHS = [
    "/config/companies.yaml",
    "./companies.yaml",
]


def load(path: str | None = None) -> AppConfig:
    if path is None:
        path = os.environ.get("JOB_WATCH_CONFIG")
    if path is None:
        for candidate in _SEARCH_PATHS:
            if Path(candidate).exists():
                path = candidate
                break
    if path is None:
        raise FileNotFoundError(
            "No config file found. Set JOB_WATCH_CONFIG or place companies.yaml "
            "in /config/ or the current directory."
        )
    with open(path) as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)
