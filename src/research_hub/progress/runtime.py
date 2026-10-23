"""On-demand reviewed reader with bounded caching; no background collector."""

from datetime import datetime, timezone
import json
import subprocess
import threading

from research_hub.guidance.runtime import run_module
from .projection import project_progress, projection_config, timestamp

LOCK = threading.Lock()
CACHE = None
MAX_READ_AGE_SECONDS = 120
CACHE_SECONDS = 60


def _fresh(result, config, now):
    read_age = (now - timestamp(result["read_at"])).total_seconds()
    if not 0 <= read_age <= MAX_READ_AGE_SECONDS:
        raise ValueError("read_stale_or_future")
    source_age = (now - timestamp(result["source_updated_at"])).total_seconds()
    if source_age < 0 or (
        source_age > config["max_source_age_seconds"]
        and not config.get("allow_historical_source", False)
    ):
        raise ValueError("source_stale_or_future")
    return read_age


def query_progress():
    global CACHE
    now = datetime.now(timezone.utc)
    with LOCK:
        try:
            config = projection_config()
            fingerprint = json.dumps(config, sort_keys=True)
            if CACHE and CACHE["config"] == fingerprint:
                age = _fresh(CACHE["result"], config, now)
                if age < CACHE_SECONDS:
                    return {
                        "available": True,
                        "queried_at": now.isoformat(timespec="seconds"),
                        **CACHE["result"],
                    }
            probe = run_module("research_hub.progress.reader")
            result = project_progress(probe, config)
            now = datetime.now(timezone.utc)
            _fresh(result, config, now)
            CACHE = {"config": fingerprint, "result": result}
            return {"available": True, "queried_at": now.isoformat(timespec="seconds"), **result}
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            StopIteration,
            subprocess.SubprocessError,
        ):
            # Never silently serve a stale cache after source failure.
            CACHE = None
            return {
                "available": False,
                "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reason": "source_unavailable_or_requires_review",
            }
