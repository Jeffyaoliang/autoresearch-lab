"""Alibaba ECS same-instance start adapter, via the official CLI and existing profile."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil

from .docker import bounded_runner
from .renewal_guard import Rejected, digest, exclusive_lock, timestamp
from .restart import read_private, write_private


class Aliyun:
    def __init__(self, config, runner=None):
        executable = config.get("aliyun_executable") or shutil.which("aliyun")
        if (
            not isinstance(executable, str)
            or not Path(executable).is_absolute()
            or not Path(executable).is_file()
        ):
            raise Rejected("aliyun_cli_unavailable")
        patterns = {
            "region": r"[a-z][a-z0-9-]{1,63}",
            "profile_name": r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}",
            "account_id": r"[0-9]{8,32}",
            "instance_id": r"i-[A-Za-z0-9]{1,64}",
        }
        if any(
            not isinstance(config.get(k), str) or not re.fullmatch(p, config[k])
            for k, p in patterns.items()
        ):
            raise Rejected("invalid_aliyun_target")
        self.config = {key: config[key] for key in patterns}
        self.config.update(backend="aliyun-ecs", aliyun_executable=executable)
        self.target, self.runner = config, runner or bounded_runner

    def api(self, service, action, args=()):
        if (service, action) not in {
            ("sts", "GetCallerIdentity"),
            ("ecs", "DescribeInstances"),
            ("ecs", "StartInstance"),
        }:
            raise Rejected("aliyun_operation_not_allowed")
        config = self.config
        env = dict(os.environ)
        env.pop("ALIBABA_CLOUD_IGNORE_PROFILE", None)
        reply = self.runner(
            [
                config["aliyun_executable"],
                service,
                action,
                "--profile",
                config["profile_name"],
                "--region",
                config["region"],
                "--retry-count",
                "0",
                "--connect-timeout",
                "5",
                "--read-timeout",
                "10",
                *args,
            ],
            env=env,
            timeout=20,
        )
        if reply.returncode:
            raise Rejected("aliyun_api_unavailable")
        try:
            result = json.loads(reply.stdout)
        except ValueError:
            raise Rejected("aliyun_response_invalid") from None
        if not isinstance(result, dict) or "Code" in result:
            raise Rejected("aliyun_response_invalid")
        return result

    def inspect(self):
        config = self.config
        caller = self.api("sts", "GetCallerIdentity")
        if caller.get("AccountId") != config["account_id"]:
            raise Rejected("aliyun_account_mismatch")
        response = self.api(
            "ecs",
            "DescribeInstances",
            ["--RegionId", config["region"], "--InstanceIds", json.dumps([config["instance_id"]])],
        )
        if response.get("NextToken") or response.get("TotalCount", 1) != 1:
            raise Rejected("aliyun_instance_missing_or_ambiguous")
        instances = response.get("Instances", {}).get("Instance")
        if not isinstance(instances, list) or len(instances) != 1:
            raise Rejected("aliyun_instance_missing_or_ambiguous")
        item = instances[0]
        if (
            item.get("InstanceId") != config["instance_id"]
            or item.get("RegionId") != config["region"]
        ):
            raise Rejected("aliyun_instance_identity_mismatch")
        if item.get("Status") not in {"Running", "Stopped", "Starting", "Stopping", "Pending"}:
            raise Rejected("aliyun_status_unverified")
        if (
            not isinstance(item.get("OperationLocks"), dict)
            or item["OperationLocks"].get("LockReason") != []
        ):
            raise Rejected("aliyun_instance_locked_or_locks_unverified")
        if item.get("SpotStrategy") != "NoSpot":
            raise Rejected("aliyun_spot_replacement_not_supported")
        charge = item.get("InstanceChargeType")
        if charge == "PrePaid":
            if timestamp(item.get("ExpiredTime")) <= datetime.now(timezone.utc):
                raise Rejected("aliyun_subscription_expired")
        elif charge != "PostPaid":
            raise Rejected("aliyun_charge_type_unverified")
        keys = (
            "CreationTime",
            "InstanceType",
            "ImageId",
            "ZoneId",
            "InstanceChargeType",
            "SpotStrategy",
            "VpcAttributes",
            "SecurityGroupIds",
        )
        if any(key not in item for key in keys):
            raise Rejected("aliyun_profile_incomplete")
        if any(not isinstance(item[key], str) or not item[key] for key in keys[:6]) or any(
            not isinstance(item[key], dict) for key in keys[6:]
        ):
            raise Rejected("aliyun_profile_incomplete")
        timestamp(item["CreationTime"])
        identity = digest(
            [config["account_id"], config["region"], item["InstanceId"], item["CreationTime"]]
        )
        return {
            "identity": identity,
            "profile": digest({k: item[k] for k in keys}),
            "state": item["Status"],
        }

    def pin(self):
        current = self.inspect()
        return {
            **self.config,
            "identity": current["identity"],
            "profile": current["profile"],
        }, current

    def start(self, expected):
        if self.inspect() != expected or expected["state"] != "Stopped":
            raise Rejected("aliyun_instance_changed_before_start")
        self.api(
            "ecs",
            "StartInstance",
            [
                "--RegionId",
                self.config["region"],
                "--InstanceId",
                self.config["instance_id"],
                "--InitLocalDisk",
                "false",
                "--DryRun",
                "false",
            ],
        )


DEFAULT_POLICY = {"enabled": False, "max_starts": 3, "cooldown_seconds": 60}


class CloudGuard:
    """Track observed stopped episodes, not invented Docker exit codes or boot timestamps."""

    def __init__(self, root, backend, target, now=None):
        self.root, self.backend, self.target = Path(root), backend, target
        self.now = now or (lambda: datetime.now(timezone.utc))

    def stopped(self):
        flag = self.root / "stop.flag"
        if flag.exists() or flag.is_symlink():
            raise Rejected("stop_flag_active")

    def verify(self, current):
        if any(current.get(k) != self.target.get(k) for k in ("identity", "profile")):
            raise Rejected("aliyun_pinned_profile_changed")

    def save(self, ledger):
        temporary = self.root / "cloud-ledger.pending"
        write_private(temporary, ledger)
        path = self.root / "ledger.json"
        if path.is_symlink():
            raise Rejected("unsafe_state_path")
        os.replace(temporary, path)
        from research_hub.guidance.queue import sync_directory

        sync_directory(self.root)

    def run(self, apply=False):
        result = {"mode": "apply" if apply else "check", "start_dispatched": False}
        try:
            if self.root.resolve() != self.root or not self.root.is_dir():
                raise Rejected("unsafe_state_root")
            self.stopped()
            if apply:
                lock = self.root / "restart.lock"
                if lock.is_symlink():
                    raise Rejected("unsafe_state_path")
                with exclusive_lock(lock):
                    return self.inspect(result, True)
            return self.inspect(result, False)
        except Rejected as error:
            return {**result, "status": "blocked", "reason": str(error)}
        except Exception:
            return {**result, "status": "blocked", "reason": "cloud_check_failed"}

    def inspect(self, result, apply):
        self.stopped()
        if read_private(self.root / "target.private.json") != self.target:
            raise Rejected("target_config_changed")
        policy = read_private(self.root / "policy.private.json")
        if (
            set(policy) != {"enabled", "max_starts", "cooldown_seconds"}
            or type(policy["enabled"]) is not bool
            or type(policy["max_starts"]) is not int
            or not 1 <= policy["max_starts"] <= 100
            or type(policy["cooldown_seconds"]) is not int
            or not 1 <= policy["cooldown_seconds"] <= 86400
        ):
            raise Rejected("invalid_cloud_policy")
        ledger = read_private(self.root / "ledger.json")
        if (
            set(ledger) != {"schema", "target_sha256", "episode", "stopped_since", "records"}
            or ledger["schema"] != 1
            or ledger["target_sha256"] != digest(self.target)
            or type(ledger["episode"]) is not int
            or ledger["episode"] < 0
            or not isinstance(ledger["records"], dict)
            or len(ledger["records"]) > 100
        ):
            raise Rejected("invalid_cloud_ledger")
        records = ledger["records"]
        for key, record in records.items():
            if (
                not isinstance(record, dict)
                or record.get("status") not in {"DISPATCHING", "UNCERTAIN", "ACCEPTED"}
                or not key.isdigit()
                or not 1 <= int(key) <= ledger["episode"]
            ):
                raise Rejected("invalid_cloud_ledger")
        pending = [r for r in records.values() if r["status"] != "ACCEPTED"]
        if len(pending) > 1:
            raise Rejected("multiple_uncertain_starts")
        current = self.backend.inspect()
        self.verify(current)
        result.update(state=current["state"], start_attempts=len(records))
        if pending:
            if current["state"] == "Running":
                if apply:
                    pending[0]["status"] = "ACCEPTED"
                    ledger["stopped_since"] = None
                    self.save(ledger)
                return {
                    **result,
                    "status": "accepted",
                    "reason": "pinned_instance_observed_running",
                }
            return {
                **result,
                "status": "reconciliation_required",
                "reason": "cloud_start_never_resent",
            }
        if current["state"] != "Stopped":
            if ledger["stopped_since"] is not None and apply:
                ledger["stopped_since"] = None
                self.save(ledger)
            return {**result, "status": "observing", "reason": "instance_not_stopped"}
        if ledger["stopped_since"] is None:
            ledger["episode"] += 1
            ledger["stopped_since"] = self.now().isoformat()
            if apply:
                self.save(ledger)
        age = (self.now() - timestamp(ledger["stopped_since"])).total_seconds()
        if age < 0:
            raise Rejected("future_stop_observation")
        if str(ledger["episode"]) in records:
            raise Rejected("cloud_episode_already_recorded")
        if len(records) >= policy["max_starts"]:
            raise Rejected("cloud_start_budget_exhausted")
        if not policy["enabled"]:
            raise Rejected("policy_disabled")
        if age < policy["cooldown_seconds"]:
            return {**result, "status": "observing", "reason": "stopped_observation_cooldown"}
        if not apply:
            return {
                **result,
                "status": "eligible",
                "reason": "pinned_stopped_instance_and_budget_verified",
            }
        if (
            self.backend.inspect() != current
            or read_private(self.root / "policy.private.json") != policy
            or read_private(self.root / "target.private.json") != self.target
        ):
            raise Rejected("cloud_target_changed_before_start")
        self.stopped()
        record = {"status": "DISPATCHING", "dispatched_at": self.now().isoformat()}
        records[str(ledger["episode"])] = record
        self.save(ledger)
        self.stopped()
        result.update(start_dispatched=True, start_attempts=len(records))
        try:
            self.backend.start(current)
        except Exception:
            pass
        record["status"] = "UNCERTAIN"
        self.save(ledger)
        try:
            observed = self.backend.inspect()
            self.verify(observed)
            if observed["state"] == "Running":
                record["status"] = "ACCEPTED"
                ledger["stopped_since"] = None
                self.save(ledger)
                return {
                    **result,
                    "state": "Running",
                    "status": "accepted",
                    "reason": "pinned_instance_observed_running",
                }
        except Exception:
            pass
        return {**result, "status": "reconciliation_required", "reason": "cloud_start_never_resent"}
