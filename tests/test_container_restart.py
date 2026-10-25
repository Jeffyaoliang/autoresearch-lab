"""Backend-neutral crash/reconciliation tests and Docker identity/command boundaries."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from research_hub.renewal.cli import initialize
from research_hub.renewal.docker import Docker
from research_hub.renewal.restart import Container, RestartGuard, policy_config
from research_hub.renewal.renewal_guard import Rejected, digest

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
OLD = Container(
    "identity",
    "profile",
    "exited",
    (NOW - timedelta(hours=1)).isoformat(),
    (NOW - timedelta(minutes=2)).isoformat(),
    1,
)


class Backend:
    def __init__(self):
        self.current, self.starts = OLD, 0
        self.uncertain, self.restart_exits = False, False

    def inspect(self):
        return self.current

    def start(self, expected):
        assert expected == self.current
        self.starts += 1
        if self.uncertain:
            raise TimeoutError("private daemon details")
        self.current = replace(
            OLD,
            state="exited" if self.restart_exits else "running",
            started_at=NOW.isoformat(),
            finished_at=(NOW + timedelta(seconds=1)).isoformat(),
        )


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path.resolve()
    target = {"identity": "identity", "profile": "profile"}
    policy = {
        "enabled": True,
        "allowed_exit_codes": [1, 137],
        "max_restarts": 2,
        "cooldown_seconds": 60,
    }
    (root / "target.private.json").write_text(json.dumps(target))
    (root / "policy.private.json").write_text(json.dumps(policy))
    (root / "ledger.json").write_text(
        json.dumps({"schema": 1, "target_sha256": digest(target), "records": {}})
    )
    backend = Backend()
    guard = RestartGuard(root, backend, target, now=lambda: NOW)
    return root, backend, guard


def test_check_is_read_only_and_success_is_independently_observed(fixture):
    root, backend, guard = fixture
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert guard.run()["status"] == "eligible"
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert backend.starts == 0
    assert guard.run(apply=True)["status"] == "accepted"
    assert backend.starts == 1
    assert guard.run(apply=True)["status"] == "observing"
    assert backend.starts == 1


def test_uncertain_start_is_never_resent_and_later_reconciles(fixture):
    root, backend, guard = fixture
    backend.uncertain = True
    assert guard.run(apply=True)["status"] == "reconciliation_required"
    assert guard.run(apply=True)["status"] == "reconciliation_required"
    assert backend.starts == 1
    assert len(json.loads((root / "ledger.json").read_text())["records"]) == 1
    backend.current = replace(OLD, state="running", started_at=NOW.isoformat())
    assert guard.run(apply=True)["status"] == "accepted"
    assert backend.starts == 1


def test_crash_after_durable_intent_never_resent(fixture):
    root, backend, guard = fixture

    def crash(expected):
        ledger = json.loads((root / "ledger.json").read_text())
        assert ledger["records"][OLD.cycle()]["status"] == "DISPATCHING"
        raise KeyboardInterrupt

    backend.start = crash
    with pytest.raises(KeyboardInterrupt):
        guard.run(apply=True)
    assert guard.run(apply=True)["reason"] == "uncertain_start_never_resent"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"identity": "other"}, "container_identity_or_profile_changed"),
        ({"profile": "other"}, "container_identity_or_profile_changed"),
        ({"state": "paused"}, "container_not_exited"),
        ({"state": "created"}, "container_not_exited"),
        ({"exit_code": 0}, "exit_code_not_allowed"),
        ({"exit_code": True}, "exit_code_not_allowed"),
        ({"finished_at": (NOW + timedelta(seconds=1)).isoformat()}, "invalid_exit_lifecycle"),
    ],
)
def test_ineligible_container_is_not_started(fixture, change, reason):
    _, backend, guard = fixture
    backend.current = replace(OLD, **change)
    assert guard.run(apply=True)["reason"] == reason
    assert not backend.starts


def test_budget_survives_process_restart(fixture):
    root, backend, guard = fixture
    policy = json.loads((root / "policy.private.json").read_text())
    policy["max_restarts"] = 1
    (root / "policy.private.json").write_text(json.dumps(policy))
    assert guard.run(apply=True)["status"] == "accepted"
    backend.current = replace(
        OLD,
        started_at=(NOW - timedelta(seconds=100)).isoformat(),
        finished_at=(NOW - timedelta(seconds=70)).isoformat(),
    )
    replacement = RestartGuard(root, backend, guard.target, now=lambda: NOW)
    assert replacement.run(apply=True)["reason"] == "restart_budget_exhausted"
    assert backend.starts == 1


def test_quick_exit_is_not_claimed_as_running(fixture):
    _, backend, guard = fixture
    backend.restart_exits = True
    result = guard.run(apply=True)
    assert result["status"] == "restart_observed" and not result["container_running"]


def test_stop_disabled_cooldown_and_missing_ledger_block(fixture):
    root, backend, guard = fixture
    (root / "stop.flag").touch()
    assert guard.run(apply=True)["reason"] == "stop_flag_active"
    (root / "stop.flag").unlink()
    policy = json.loads((root / "policy.private.json").read_text())
    policy["enabled"] = False
    (root / "policy.private.json").write_text(json.dumps(policy))
    assert guard.run(apply=True)["reason"] == "policy_disabled"
    backend.current = replace(OLD, finished_at=(NOW - timedelta(seconds=2)).isoformat())
    assert guard.run(apply=True)["reason"] == "exit_cooldown_active"
    (root / "ledger.json").unlink()
    assert guard.run(apply=True)["reason"] == "private_record_unavailable"
    assert backend.starts == 0


def test_changes_before_dispatch_are_rejected(fixture):
    _, backend, guard = fixture
    reads = iter([OLD, replace(OLD, state="running")])
    backend.inspect = lambda: next(reads)
    assert guard.run(apply=True)["reason"] == "container_changed_before_start"
    assert backend.starts == 0


def test_backend_return_code_is_not_success_evidence(fixture):
    _, backend, guard = fixture
    backend.start = lambda expected: None
    assert guard.run(apply=True)["status"] == "reconciliation_required"


@pytest.mark.parametrize(
    "value",
    [
        {"enabled": True},
        {"enabled": True, "allowed_exit_codes": [0], "max_restarts": 3, "cooldown_seconds": 60},
        {"enabled": True, "allowed_exit_codes": [1], "max_restarts": 0, "cooldown_seconds": 60},
    ],
)
def test_invalid_policy(value):
    with pytest.raises(Rejected):
        policy_config(value)


def docker_data():
    return {
        "Id": "a" * 64,
        "Name": "/research-test",
        "Image": "sha256:image",
        "Config": {"Env": ["PRIVATE_PASSWORD=do-not-export"]},
        "HostConfig": {"AutoRemove": False, "RestartPolicy": {"Name": "no"}},
        "Mounts": [],
        "State": {
            "Status": "exited",
            "ExitCode": 1,
            "StartedAt": OLD.started_at,
            "FinishedAt": OLD.finished_at,
        },
    }


def docker_runner(data=None, daemon="daemon-A"):
    data = data or docker_data()
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "info" in args:
            value = daemon
        else:
            assert args[-3:-1] == ["container", "inspect"]
            value = [data]
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

    return run, calls


def docker_config():
    return {
        "backend": "docker",
        "docker_executable": str(Path(__import__("sys").executable).resolve()),
        "docker_host": "unix:///var/run/docker.sock",
    }


def test_docker_pins_full_identity_and_hashes_private_configuration(monkeypatch):
    run, calls = docker_runner()
    monkeypatch.setenv("DOCKER_HOST", "tcp://wrong-host:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "wrong-context")
    target = Docker(docker_config(), runner=run).pin("research-test")
    assert "do-not-export" not in json.dumps(target)
    backend = Docker(target, runner=run)
    current = backend.inspect()
    assert current.identity == target["identity"]
    assert target["container_id"] == "a" * 64
    assert all(args[1:3] == ["--host", "unix:///var/run/docker.sock"] for args, _ in calls)
    assert all(not any(key.startswith("DOCKER_") for key in kwargs["env"]) for _, kwargs in calls)
    wrong, _ = docker_runner(daemon="daemon-B")
    with pytest.raises(Rejected, match="docker_daemon_changed"):
        Docker(target, runner=wrong).inspect()


def test_docker_blocks_native_restart_and_autoremove():
    for updates in ({"AutoRemove": True}, {"RestartPolicy": {"Name": "always"}}):
        data = docker_data()
        data["HostConfig"].update(updates)
        run, _ = docker_runner(data)
        with pytest.raises(Rejected, match="docker_native_restart_or_autoremove_conflict"):
            Docker(docker_config(), runner=run).pin("research-test")


def test_docker_start_uses_exact_id_and_checks_expected_cycle():
    run, _ = docker_runner()
    target = Docker(docker_config(), runner=run).pin("research-test")
    backend = Docker(target, runner=run)
    expected = backend.inspect()
    with (
        patch.object(backend, "command", return_value="") as command,
        patch.object(backend, "inspect", return_value=expected),
    ):
        backend.start(expected)
        command.assert_called_once_with(["container", "start", "a" * 64])
    with patch.object(backend, "inspect", return_value=replace(expected, state="running")):
        with pytest.raises(Rejected):
            backend.start(expected)


def test_symlink_ledger_is_rejected(fixture, tmp_path):
    root, backend, guard = fixture
    (root / "ledger.json").rename(root / "original.json")
    (root / "ledger.json").symlink_to(root / "original.json")
    assert guard.run(apply=True)["reason"] == "private_record_unavailable"
    assert not backend.starts


def test_init_is_disabled_and_does_not_overwrite(tmp_path):
    target = {"identity": "pinned", "profile": "hash"}
    root = tmp_path.resolve() / "new"
    with patch.object(Docker, "pin", return_value=target):
        assert initialize(root, docker_config(), "research-test")["policy_enabled"] is False
        before = (root / "ledger.json").read_bytes()
        with pytest.raises(FileExistsError):
            initialize(root, docker_config(), "research-test")
        assert (root / "ledger.json").read_bytes() == before


def test_apply_lock_prevents_second_controller(fixture):
    from research_hub.renewal.renewal_guard import exclusive_lock

    root, backend, guard = fixture
    with exclusive_lock(root / "restart.lock"):
        assert guard.run(apply=True)["reason"] == "another_guard_owns_lock"
    assert backend.starts == 0


@pytest.mark.parametrize(
    "program, timeout",
    [("import time;time.sleep(5)", 0.1), ("import sys;sys.stdout.write('x'*1200000)", 3)],
)
def test_docker_process_limits(program, timeout):
    import os, sys
    from research_hub.renewal.docker import bounded_runner

    with pytest.raises(Rejected, match="docker_command_limit_exceeded"):
        bounded_runner([sys.executable, "-c", program], env=dict(os.environ), timeout=timeout)


def test_watch_exits_on_stop_flag(tmp_path, capsys):
    from research_hub.renewal import cli

    guard = SimpleNamespace(
        run=lambda **kwargs: {"status": "blocked", "reason": "stop_flag_active"}
    )
    with (
        patch.object(cli, "build_guard", return_value=guard),
        patch.object(cli.time, "sleep") as sleep,
    ):
        assert cli.main(["docker", "watch", "--state-dir", str(tmp_path), "--apply"]) == 0
    sleep.assert_not_called()
    assert json.loads(capsys.readouterr().out)["reason"] == "stop_flag_active"


def test_top_level_renewal_dispatch():
    from research_hub.cli import main

    with patch("research_hub.renewal.cli.main", return_value=7) as call:
        assert main(["renewal", "docker", "check", "--state-dir", "/example/state"]) == 7
    call.assert_called_once_with(["docker", "check", "--state-dir", "/example/state"])
