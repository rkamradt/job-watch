from job_watch.adapters.greenhouse import GreenhouseAdapter
from job_watch.adapters.lever import LeverAdapter
from job_watch.adapters.ashby import AshbyAdapter

REGISTRY: dict[str, type] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
}

__all__ = ["GreenhouseAdapter", "LeverAdapter", "AshbyAdapter", "REGISTRY"]
