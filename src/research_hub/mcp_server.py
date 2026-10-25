"""Narrow stdio view of the EXISTING collector; ACP snapshot plus a fixed approved research-record reader.
Approved reads plus fixed-session advisory delivery only. No model turns or execution. Never import bridge.py or read credentials.
"""

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import read_config, state_dir

ROOT = state_dir()
SNAPSHOT = ROOT / "collector-state.json"
STATES = {
    "WAITING",
    "INIT",
    "QUEUEING",
    "CREATING",
    "STARTING",
    "RUNNING",
    "RESTARTING",
    "SUSPENDING",
    "SUSPENDED",
    "SUCCEEDED",
    "FAILED",
    "DELETING",
    "DELETED",
    "RECOVERING",
    "UNKNOWN",
}


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    state: Literal[
        "WAITING",
        "INIT",
        "QUEUEING",
        "CREATING",
        "STARTING",
        "RUNNING",
        "RESTARTING",
        "SUSPENDING",
        "SUSPENDED",
        "SUCCEEDED",
        "FAILED",
        "DELETING",
        "DELETED",
        "RECOVERING",
        "UNKNOWN",
    ]


class ResourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available: Literal[False] = False
    reason: Literal["not_in_existing_verified_snapshot"] = "not_in_existing_verified_snapshot"


class TaskStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available: bool
    queried_at: str
    observed_at: str | None = None
    source: Literal["existing_SCO_ACP_collector_snapshot", "unavailable_for_ssh_transport"] = (
        "existing_SCO_ACP_collector_snapshot"
    )
    coverage: Literal["first_page_only_max_20", "not_applicable"] = "first_page_only_max_20"
    reason: Literal[
        "fresh_snapshot",
        "snapshot_unavailable",
        "snapshot_unverified",
        "snapshot_stale",
        "platform_task_listing_not_supported",
    ]
    tasks: list[Task]
    resource_summary: ResourceSummary = ResourceSummary()


def project_snapshot(raw: bytes, clock: datetime) -> TaskStatus:
    base = dict(available=False, queried_at=clock.isoformat(timespec="seconds"), tasks=[])
    try:
        if len(raw) > 65536:
            raise ValueError()
        record = json.loads(raw)
        s = record["snapshot"]
        if record["schema_version"] != 1 or record["collector"] != "acp_read_only_status":
            raise ValueError()
        if s.get("available") is not True:
            return TaskStatus(**base, reason="snapshot_unavailable")
        if (
            s.get("source") != "SCO_ACP_READ_ONLY"
            or s.get("task_scope") != "verified_user_name_prefix_and_user_id"
            or s.get("coverage") != "first_page_only_max_20"
        ):
            raise ValueError()
        timestamp = s["observed_at"]
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError()
        age = (clock - observed).total_seconds()
        if age < -5 or age > 180:
            return TaskStatus(
                **base, reason="snapshot_stale", observed_at=observed.isoformat(timespec="seconds")
            )
        tasks = s["tasks"]
        if not isinstance(tasks, list) or len(tasks) > 20:
            raise ValueError()
        output = []
        seen = set()
        for t in tasks:
            task_id = t["task_id"]
            state = t["state"]
            if (
                not isinstance(task_id, str)
                or not re.fullmatch(r"task_[a-f0-9]{12}", task_id)
                or task_id in seen
                or state not in STATES
            ):
                raise ValueError()
            seen.add(task_id)
            output.append(Task(task_id=task_id, state=state))
        return TaskStatus(
            available=True,
            queried_at=base["queried_at"],
            observed_at=observed.isoformat(timespec="seconds"),
            reason="fresh_snapshot",
            tasks=output,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return TaskStatus(**base, reason="snapshot_unverified")


def read_status() -> TaskStatus:
    clock = datetime.now(timezone.utc)
    try:
        remote = read_config("remote.json")
    except ValueError:
        remote = {}
    if remote.get("transport") == "ssh":
        return TaskStatus(
            available=False,
            queried_at=clock.isoformat(timespec="seconds"),
            tasks=[],
            reason="platform_task_listing_not_supported",
            source="unavailable_for_ssh_transport",
            coverage="not_applicable",
        )
    try:
        # Fixed local file only, no argument-selected paths. Reject symlinks/reparse points.
        st = SNAPSHOT.lstat()
        if SNAPSHOT.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
            raise OSError()
        with SNAPSHOT.open("rb") as f:
            raw = f.read(65537)
        return project_snapshot(raw, clock)
    except OSError:
        return TaskStatus(
            available=False,
            queried_at=clock.isoformat(timespec="seconds"),
            tasks=[],
            reason="snapshot_unavailable",
        )


class ResearchProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available: bool
    queried_at: str
    reason: Literal["source_unavailable_or_requires_review"] | None = None
    project: str | None = None
    recorded_status: str | None = None
    recorded_stage: str | None = None
    read_at: str | None = None
    source_updated_at: str | None = None
    source_age_seconds_at_read: int | None = None
    live_research_progress_verified: Literal[False] = False
    record_kind: Literal["historical_project_record"] = "historical_project_record"
    evidence_ref: str | None = None
    evidence_sha256: str | None = None
    checks: dict[str, Literal["Blocked", "Verified", "Unknown"]] | None = None
    execution_record: dict[str, int] | None = None
    task_success: None = None
    acp_task_mapping_verified: Literal[False] = False


class GuidanceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available: bool
    reason: (
        Literal[
            "invalid_request",
            "invalid_request_id",
            "invalid_message",
            "possible_secret_rejected",
            "idempotency_conflict",
            "pilot_queue_quota_reached",
            "operation_not_allowed",
            "guidance_unavailable_or_requires_review",
        ]
        | None
    ) = None
    request_id: str | None = None
    binding: str | None = None
    policy: Literal["analysis_advisory_only_v1"] | None = None
    state: (
        Literal[
            "queued_for_bound_codex",
            "delivery_pending_or_uncertain_do_not_resubmit",
            "received_in_codex_context",
        ]
        | None
    ) = None
    created_at: str | None = None
    message_sha256: str | None = None
    duplicate: bool | None = None
    codex_received: bool = False
    execution_authorized: Literal[False] = False
    model_processing_started: Literal[False] = False
    received_at: str | None = None
    receipt_kind: Literal["official_thread_inject_items_ack"] | None = None


mcp = FastMCP("Research Hub")


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def list_research_tasks() -> TaskStatus:
    """Read existing SCO ACP task aliases and platform states. SSH deployments explicitly report platform_task_listing_not_supported; a reachable SSH host is not a running research task. Includes observation/query times. Freshness limit 180 seconds; first 20 owned tasks only. Resource details unavailable. No scientific results or logs."""
    return read_status()


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def get_research_progress() -> ResearchProgress:
    """Read the approved configured research project historical project record via a fixed read-only source. No arguments. Distinguish queried_at/read_at from source_updated_at: a recent read is NOT a live experiment result. Emits only reviewed phase, check states, counts, evidence alias/hash and times. Task mapping unverified. Fails closed on source/schema/stage changes. No logs, shell, arbitrary paths, writes, credentials or GPU actions."""
    import asyncio
    from .progress.runtime import query_progress

    return ResearchProgress(**(await asyncio.to_thread(query_progress)))


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def submit_research_guidance(request_id: str, message: str) -> GuidanceReceipt:
    """Deliver advisory text to the ONE bound configured research project Codex thread context. A WRITE to its private mailbox/history, NOT execution permission. No model turn is started; reception does not mean the model has read or acted on it. Stable request_id (8-64 ASCII letters/digits/_/-) deduplicates retries; changing text for the same ID fails. Max 2000 characters, no credentials. No Shell, arbitrary paths/threads, logs, project writes, or GPU actions. Use get_research_guidance_status to confirm reception."""
    import asyncio
    from .guidance.runtime import query_guidance

    return GuidanceReceipt(
        **(
            await asyncio.to_thread(
                query_guidance,
                {"operation": "submit", "request_id": request_id, "message": message},
            )
        )
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
async def get_research_guidance_status(request_id: str) -> GuidanceReceipt:
    """Read a known advisory request's receipt from the fixed configured research project mailbox. Distinguish queued, uncertain delivery, and official Codex context reception. Never returns message body or model output. model_processing_started=false and execution_authorized=false. Unknown IDs fail closed."""
    import asyncio
    from .guidance.runtime import query_guidance

    return GuidanceReceipt(
        **(
            await asyncio.to_thread(
                query_guidance, {"operation": "status", "request_id": request_id}
            )
        )
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
