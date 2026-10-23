import json
from datetime import datetime, timedelta, timezone

import pytest

from research_hub import mcp_server
from research_hub.cli import demo

CLOCK = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot(**overrides):
    result = {
        "available": True,
        "source": "SCO_ACP_READ_ONLY",
        "task_scope": "verified_user_name_prefix_and_user_id",
        "coverage": "first_page_only_max_20",
        "observed_at": CLOCK.isoformat(),
        "tasks": [{"task_id": "task_0123456789ab", "state": "RUNNING"}],
    }
    result.update(overrides)
    return json.dumps(
        {"schema_version": 1, "collector": "acp_read_only_status", "snapshot": result}
    ).encode()


def test_current_snapshot_projects_aliases_only():
    result = mcp_server.project_snapshot(snapshot(), CLOCK)
    assert result.available
    assert result.tasks[0].task_id == "task_0123456789ab"
    assert result.reason == "fresh_snapshot"
    assert not result.resource_summary.available


@pytest.mark.parametrize("seconds", [-6, 181])
def test_stale_and_future_snapshots_are_not_live(seconds):
    observed = CLOCK - timedelta(seconds=seconds)
    result = mcp_server.project_snapshot(snapshot(observed_at=observed.isoformat()), CLOCK)
    assert not result.available
    assert result.reason == "snapshot_stale"
    assert not result.tasks


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "unreviewed"},
        {"task_scope": "all_users"},
        {"coverage": "unknown"},
        {"observed_at": "2026-01-01T00:00:00"},
        {"tasks": [{"task_id": "private-job-name", "state": "RUNNING"}]},
        {"tasks": [{"task_id": "task_0123456789ab", "state": "RUNNING"}] * 2},
        {"tasks": [{"task_id": "task_0123456789ab", "state": "UNSUPPORTED"}]},
    ],
)
def test_unverified_snapshots_are_not_success(overrides):
    result = mcp_server.project_snapshot(snapshot(**overrides), CLOCK)
    assert not result.available
    assert result.reason == "snapshot_unverified"


def test_read_status_rejects_symlinks(tmp_path, monkeypatch):
    target = tmp_path / "target.json"
    target.write_bytes(snapshot())
    link = tmp_path / "collector-state.json"
    link.symlink_to(target)
    monkeypatch.setattr(mcp_server, "SNAPSHOT", link)
    assert mcp_server.read_status().reason == "snapshot_unavailable"


def test_demo_is_explicitly_synthetic():
    result = demo()
    assert result["mode"] == "synthetic_demo"
    assert result["projection"]["available"]
    assert not result["real_platform_queried"]
    assert not result["model_turn_started"]
    assert not result["experiment_executed"]


def test_guidance_receipt_cannot_claim_execution():
    with pytest.raises(ValueError):
        mcp_server.GuidanceReceipt(available=True, execution_authorized=True)
    with pytest.raises(ValueError):
        mcp_server.GuidanceReceipt(available=True, model_processing_started=True)
