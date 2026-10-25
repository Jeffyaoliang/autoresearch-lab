"""Official CLI/API-shaped fixtures; no cloud credentials, network calls or real instance starts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from research_hub.renewal.aliyun import Aliyun, CloudGuard
from research_hub.renewal.restart import write_private
from research_hub.renewal.renewal_guard import Rejected, digest

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def config():
    return dict(
        region="cn-hangzhou",
        account_id="1234567890123456",
        instance_id="i-test123",
        profile_name="research",
        aliyun_executable=str(Path(sys.executable).resolve()),
    )


def instance():
    return dict(
        InstanceId="i-test123",
        RegionId="cn-hangzhou",
        Status="Stopped",
        CreationTime="2026-01-01T00:00Z",
        InstanceType="ecs.test",
        ImageId="m-test",
        ZoneId="cn-hangzhou-a",
        InstanceChargeType="PostPaid",
        SpotStrategy="NoSpot",
        OperationLocks={"LockReason": []},
        VpcAttributes={"VpcId": "vpc-test"},
        SecurityGroupIds={"SecurityGroupId": ["sg-test"]},
    )


class CLI:
    def __init__(self):
        self.item, self.calls = instance(), []
        self.account = "1234567890123456"
        self.start_state = "Running"
        self.fail = False

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        assert args[args.index("--retry-count") + 1] == "0"
        action = args[2]
        if action == "GetCallerIdentity":
            value = {"AccountId": self.account}
        elif action == "DescribeInstances":
            assert json.loads(args[args.index("--InstanceIds") + 1]) == ["i-test123"]
            value = {"Instances": {"Instance": [deepcopy(self.item)]}}
        elif action == "StartInstance":
            assert args[args.index("--InstanceId") + 1] == "i-test123"
            assert args[args.index("--InitLocalDisk") + 1] == "false"
            if self.fail:
                raise TimeoutError("private credential-bearing server text")
            self.item["Status"] = self.start_state
            value = {"RequestId": "request-fixture"}
        else:
            raise AssertionError("unexpected API")
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")


@pytest.fixture
def fixture(tmp_path):
    cli = CLI()
    backend = Aliyun(config(), runner=cli)
    target, _ = backend.pin()
    backend = Aliyun(target, runner=cli)
    root = tmp_path.resolve()
    write_private(root / "target.private.json", target, exclusive=True)
    write_private(
        root / "policy.private.json",
        {"enabled": True, "max_starts": 2, "cooldown_seconds": 1},
        exclusive=True,
    )
    write_private(
        root / "ledger.json",
        {
            "schema": 1,
            "target_sha256": digest(target),
            "episode": 1,
            "stopped_since": (NOW - timedelta(minutes=1)).isoformat(),
            "records": {},
        },
        exclusive=True,
    )
    return root, cli, CloudGuard(root, backend, target, now=lambda: NOW)


def test_read_only_check_and_observed_running(fixture):
    root, cli, guard = fixture
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert guard.run()["status"] == "eligible"
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert not any(call[2] == "StartInstance" for call in cli.calls)
    result = guard.run(apply=True)
    assert result["status"] == "accepted" and result["start_dispatched"]
    assert guard.run(apply=True)["status"] == "observing"
    assert sum(call[2] == "StartInstance" for call in cli.calls) == 1


def test_starting_is_not_running_and_never_resends(fixture):
    _, cli, guard = fixture
    cli.start_state = "Starting"
    assert guard.run(apply=True)["status"] == "reconciliation_required"
    assert guard.run(apply=True)["status"] == "reconciliation_required"
    assert sum(call[2] == "StartInstance" for call in cli.calls) == 1
    cli.item["Status"] = "Running"
    assert guard.run(apply=True)["status"] == "accepted"


def test_timeout_durable_intent_prevents_repeat(fixture):
    root, cli, guard = fixture
    cli.fail = True
    assert guard.run(apply=True)["status"] == "reconciliation_required"
    assert guard.run(apply=True)["status"] == "reconciliation_required"
    assert sum(call[2] == "StartInstance" for call in cli.calls) == 1
    assert (
        next(iter(json.loads((root / "ledger.json").read_text())["records"].values()))["status"]
        == "UNCERTAIN"
    )


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("InstanceId", "i-other", "aliyun_instance_identity_mismatch"),
        ("RegionId", "cn-other", "aliyun_instance_identity_mismatch"),
        ("SpotStrategy", "SpotAsPriceGo", "aliyun_spot_replacement_not_supported"),
        (
            "OperationLocks",
            {"LockReason": [{"LockReason": "financial"}]},
            "aliyun_instance_locked_or_locks_unverified",
        ),
        ("InstanceType", "ecs.changed", "aliyun_pinned_profile_changed"),
    ],
)
def test_wrong_target_profile_or_locked_resource_blocks(fixture, key, value, reason):
    _, cli, guard = fixture
    cli.item[key] = value
    assert guard.run(apply=True)["reason"] == reason
    assert not any(call[2] == "StartInstance" for call in cli.calls)


def test_wrong_cloud_account_does_not_describe_or_start():
    cli = CLI()
    cli.account = "9999999999999999"
    with pytest.raises(Rejected, match="aliyun_account_mismatch"):
        Aliyun(config(), runner=cli).pin()
    assert len(cli.calls) == 1


def test_budget_survives_new_guard(fixture):
    root, cli, guard = fixture
    policy = root / "policy.private.json"
    policy.write_text(json.dumps({"enabled": True, "max_starts": 1, "cooldown_seconds": 1}))
    assert guard.run(apply=True)["status"] == "accepted"
    cli.item["Status"] = "Stopped"
    later = CloudGuard(root, guard.backend, guard.target, now=lambda: NOW + timedelta(minutes=2))
    assert later.run(apply=True)["reason"] == "cloud_start_budget_exhausted"
    assert sum(call[2] == "StartInstance" for call in cli.calls) == 1


def test_new_observation_cooldown_and_disabled_policy(fixture):
    root, _, guard = fixture
    ledger = json.loads((root / "ledger.json").read_text())
    ledger["stopped_since"] = None
    ledger["episode"] = 0
    (root / "ledger.json").write_text(json.dumps(ledger))
    assert guard.run(apply=True)["reason"] == "stopped_observation_cooldown"
    assert json.loads((root / "ledger.json").read_text())["episode"] == 1
    policy = root / "policy.private.json"
    data = json.loads(policy.read_text())
    data["enabled"] = False
    policy.write_text(json.dumps(data))
    assert guard.run(apply=True)["reason"] == "policy_disabled"


def test_stop_flag_and_cli_init_backend_dispatch(fixture):
    from research_hub.renewal.cli import build_guard

    root, _, guard = fixture
    (root / "stop.flag").touch()
    assert guard.run(apply=True)["reason"] == "stop_flag_active"
    with pytest.raises(Rejected, match="state_backend_mismatch"):
        build_guard(root, "docker")


def test_cloud_init_is_disabled_and_records_observation(tmp_path):
    from research_hub.renewal.cli import initialize_aliyun

    root = tmp_path.resolve() / "new"
    cli = CLI()
    with patch("research_hub.renewal.aliyun.bounded_runner", cli):
        assert initialize_aliyun(root, config())["policy_enabled"] is False
    assert json.loads((root / "ledger.json").read_text())["stopped_since"]
    assert not any(call[2] == "StartInstance" for call in cli.calls)


def test_cloud_crash_after_intent_requires_reconciliation(fixture):
    root, cli, guard = fixture

    def crash(expected):
        ledger = json.loads((root / "ledger.json").read_text())
        assert next(iter(ledger["records"].values()))["status"] == "DISPATCHING"
        raise KeyboardInterrupt

    guard.backend.start = crash
    with pytest.raises(KeyboardInterrupt):
        guard.run(apply=True)
    assert guard.run(apply=True)["reason"] == "cloud_start_never_resent"
    assert not any(call[2] == "StartInstance" for call in cli.calls)


def test_expired_subscription_is_not_automatically_renewed(fixture):
    _, cli, guard = fixture
    cli.item.update(InstanceChargeType="PrePaid", ExpiredTime="2020-01-01T00:00Z")
    assert guard.run(apply=True)["reason"] == "aliyun_subscription_expired"
    assert not any(call[2] == "StartInstance" for call in cli.calls)
