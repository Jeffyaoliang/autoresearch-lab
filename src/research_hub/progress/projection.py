"""Project only an operator-approved manifest; raw text is never exported."""

from datetime import datetime, timezone
from pathlib import PurePosixPath
import re

from research_hub.config import read_config

DEFAULT_CHECKS = (
    "gpu_access",
    "sandbox",
    "simulator_dependencies",
    "single_task_simulator_doctor",
    "observation_action_feedback",
    "autonomous_episode",
    "nvidia_egl",
    "osmesa_cpu_render",
    "fixed_mujoco_cpu_smoke",
)
COUNTS = ("episodes_started", "simulators_started", "generated_programs_executed")


def projection_config(config=None):
    data = read_config("progress.json") if config is None else dict(config)
    for key in ("project", "manifest_path", "evidence_ref", "approved_stage", "approved_status"):
        if (
            not isinstance(data.get(key), str)
            or not data[key]
            or len(data[key]) > 512
            or "\x00" in data[key]
        ):
            raise ValueError("invalid_progress_config")
    if (
        not PurePosixPath(data["manifest_path"]).is_absolute()
        or ".." in PurePosixPath(data["manifest_path"]).parts
    ):
        raise ValueError("invalid_manifest_path")
    if data["approved_status"] not in ("Blocked", "Verified", "Unknown"):
        raise ValueError("invalid_progress_config")
    checks = data.get("check_names", DEFAULT_CHECKS)
    if (
        not isinstance(checks, (list, tuple))
        or not 1 <= len(checks) <= 50
        or any(
            not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key)
            for key in checks
        )
        or len(set(checks)) != len(checks)
    ):
        raise ValueError("invalid_check_names")
    data["check_names"] = tuple(checks)
    age = data.get("max_source_age_seconds", 86400)
    if type(age) is not int or not 1 <= age <= 365 * 86400:
        raise ValueError("invalid_source_freshness")
    data["max_source_age_seconds"] = age
    if type(data.get("allow_historical_source", False)) is not bool:
        raise ValueError("invalid_source_freshness")
    return data


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("invalid_source_time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("invalid_source_time")
    return parsed


def project_progress(probe, config=None):
    config = projection_config(config)
    meta = probe["metadata"]
    matches = [
        source for source in meta["sources"] if source.get("path") == config["manifest_path"]
    ]
    if len(matches) != 1:
        raise ValueError("missing_or_duplicate_manifest")
    source = matches[0]
    fields = {}
    for item in source["fields"]:
        if item["field"] in fields:
            raise ValueError("duplicate_source_field")
        fields[item["field"]] = item["value"]
    if fields.get("/project") != config["project"]:
        raise ValueError("unreviewed_project")
    if fields.get("/current_stage") != config["approved_stage"]:
        raise ValueError("unreviewed_stage")
    if fields.get("/status") != config["approved_status"]:
        raise ValueError("unreviewed_status")
    updated, observed = timestamp(fields["/updated_at_utc"]), timestamp(meta["observed_at"])
    age = (observed - updated).total_seconds()
    if age < 0:
        raise ValueError("invalid_source_time")
    if age > config["max_source_age_seconds"] and not config.get("allow_historical_source", False):
        raise ValueError("source_stale")
    digest = source["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid_source_hash")
    checks = {}
    for key in config["check_names"]:
        value = fields.get("/checks/" + key + "/status")
        if value not in ("Blocked", "Verified", "Unknown"):
            raise ValueError("unreviewed_check_state")
        checks[key] = value
    execution = {}
    for key in COUNTS:
        value = source["execution_counts"]["/execution/" + key]
        if type(value) is not int or value < 0:
            raise ValueError("invalid_execution_count")
        execution[key] = value
    if source["execution_counts"].get("/execution/task_success") is not None:
        raise ValueError("unreviewed_scientific_result")
    return {
        "project": config["project"],
        "recorded_status": config["approved_status"],
        "recorded_stage": config["approved_stage"],
        "read_at": observed.isoformat(),
        "source_updated_at": updated.isoformat(),
        "source_age_seconds_at_read": round(age),
        "live_research_progress_verified": False,
        "record_kind": "historical_project_record",
        "evidence_ref": config["evidence_ref"],
        "evidence_sha256": digest,
        "checks": checks,
        "execution_record": execution,
        "task_success": None,
        "acp_task_mapping_verified": False,
    }
