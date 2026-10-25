"""Backend-neutral, bounded same-container restart guard. No provisioning or model turns."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Protocol

from .renewal_guard import Rejected, digest, exclusive_lock, timestamp


@dataclass(frozen=True)
class Container:
    identity: str
    profile: str
    state: str
    started_at: str
    finished_at: str
    exit_code: int

    def cycle(self):
        return digest([self.identity, self.started_at, self.finished_at])


class Backend(Protocol):
    """Implementations must independently pin and verify their host/container identity."""

    def inspect(self) -> Container: ...
    def start(self, expected: Container) -> None: ...


def read_private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise Rejected("private_record_unavailable")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise Rejected("private_record_unavailable") from None
    if not isinstance(value, dict):
        raise Rejected("invalid_private_record")
    return value


def write_private(path, data, *, exclusive=False):
    path = Path(path)
    if path.is_symlink():
        raise Rejected("unsafe_state_path")
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def policy_config(value):
    keys = {"enabled", "allowed_exit_codes", "max_restarts", "cooldown_seconds"}
    if set(value) != keys or type(value.get("enabled")) is not bool:
        raise Rejected("invalid_restart_policy")
    codes = value["allowed_exit_codes"]
    if (
        not isinstance(codes, list)
        or not 1 <= len(codes) <= 255
        or any(type(code) is not int or not 1 <= code <= 255 for code in codes)
        or len(set(codes)) != len(codes)
    ):
        raise Rejected("invalid_exit_code_policy")
    for key, minimum, maximum in (("max_restarts", 1, 100), ("cooldown_seconds", 1, 86400)):
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise Rejected("invalid_restart_policy")
    return value


class RestartGuard:
    def __init__(self, root, backend: Backend, target, now=None):
        self.root, self.backend, self.target = Path(root), backend, target
        self.now = now or (lambda: datetime.now(timezone.utc))

    def stopped(self):
        path = self.root / "stop.flag"
        if path.exists() or path.is_symlink():
            raise Rejected("stop_flag_active")

    def verify(self, current):
        if current.identity != self.target["identity"] or current.profile != self.target["profile"]:
            raise Rejected("container_identity_or_profile_changed")
        if current.state not in {
            "running",
            "restarting",
            "paused",
            "exited",
            "created",
            "dead",
            "removing",
        }:
            raise Rejected("unknown_container_state")

    def save(self, ledger):
        path = self.root / "ledger.json"
        temporary = self.root / "ledger.pending"
        write_private(temporary, ledger)
        if path.is_symlink():
            raise Rejected("unsafe_state_path")
        os.replace(temporary, path)
        from research_hub.guidance.queue import sync_directory

        sync_directory(self.root)

    def run(self, apply=False):
        result = {
            "mode": "apply" if apply else "check",
            "start_dispatched": False,
        }
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
            return {**result, "status": "blocked", "reason": "restart_check_failed"}

    def inspect(self, result, apply):
        self.stopped()
        if read_private(self.root / "target.private.json") != self.target:
            raise Rejected("target_config_changed")
        policy = policy_config(read_private(self.root / "policy.private.json"))
        ledger = read_private(self.root / "ledger.json")
        if (
            set(ledger) != {"schema", "target_sha256", "records"}
            or ledger["schema"] != 1
            or ledger["target_sha256"] != digest(self.target)
            or not isinstance(ledger["records"], dict)
            or len(ledger["records"]) > 100
        ):
            raise Rejected("ledger_target_or_schema_mismatch")
        records = ledger["records"]
        for key, record in records.items():
            if (
                not isinstance(record, dict)
                or record.get("status") not in {"DISPATCHING", "UNCERTAIN", "ACCEPTED"}
                or key
                != digest(
                    [self.target["identity"], record.get("started_at"), record.get("finished_at")]
                )
            ):
                raise Rejected("invalid_restart_ledger")
            timestamp(record["started_at"])
            timestamp(record["finished_at"])
        pending = [record for record in records.values() if record["status"] != "ACCEPTED"]
        if len(pending) > 1:
            raise Rejected("multiple_uncertain_starts")
        current = self.backend.inspect()
        self.verify(current)
        result.update(state=current.state, restart_attempts=len(records))
        if pending:
            record = pending[0]
            if timestamp(current.started_at) > timestamp(record["started_at"]) and timestamp(
                current.started_at
            ) >= timestamp(record["finished_at"]):
                if apply:
                    record.update(status="ACCEPTED", observed_started_at=current.started_at)
                    self.save(ledger)
                return {
                    **result,
                    "status": "accepted" if current.state == "running" else "restart_observed",
                    "reason": "new_container_lifecycle_observed",
                    "container_running": current.state == "running",
                }
            return {
                **result,
                "status": "reconciliation_required",
                "reason": "uncertain_start_never_resent",
            }
        if current.state in {"running", "restarting"}:
            return {**result, "status": "observing", "reason": "container_already_active"}
        if current.state != "exited":
            raise Rejected("container_not_exited")
        if (
            type(current.exit_code) is not int
            or current.exit_code not in policy["allowed_exit_codes"]
        ):
            raise Rejected("exit_code_not_allowed")
        started, finished = timestamp(current.started_at), timestamp(current.finished_at)
        if finished < started or finished > self.now():
            raise Rejected("invalid_exit_lifecycle")
        if current.cycle() in records:
            raise Rejected("exit_cycle_already_recorded")
        if len(records) >= policy["max_restarts"]:
            raise Rejected("restart_budget_exhausted")
        if (self.now() - finished).total_seconds() < policy["cooldown_seconds"]:
            return {**result, "status": "observing", "reason": "exit_cooldown_active"}
        if not policy["enabled"]:
            raise Rejected("policy_disabled")
        if not apply:
            return {
                **result,
                "status": "eligible",
                "reason": "pinned_failed_container_and_budget_verified",
            }
        latest = self.backend.inspect()
        self.verify(latest)
        if latest != current:
            raise Rejected("container_changed_before_start")
        if (
            read_private(self.root / "policy.private.json") != policy
            or read_private(self.root / "target.private.json") != self.target
        ):
            raise Rejected("configuration_changed_before_start")
        self.stopped()
        record = {
            "status": "DISPATCHING",
            "started_at": current.started_at,
            "finished_at": current.finished_at,
            "dispatched_at": self.now().isoformat(),
        }
        records[current.cycle()] = record
        self.save(ledger)  # Intent survives a timeout, process crash, or ambiguous CLI result.
        self.stopped()
        result["start_dispatched"] = True
        result["restart_attempts"] = len(records)
        try:
            self.backend.start(current)
        except Exception:
            pass  # Never retry a write after uncertain transport outcomes.
        record["status"] = "UNCERTAIN"
        self.save(ledger)
        try:
            observed = self.backend.inspect()
            self.verify(observed)
            if (
                timestamp(observed.started_at) > started
                and timestamp(observed.started_at) >= finished
            ):
                record.update(status="ACCEPTED", observed_started_at=observed.started_at)
                self.save(ledger)
                return {
                    **result,
                    "status": "accepted" if observed.state == "running" else "restart_observed",
                    "reason": "new_container_lifecycle_observed",
                    "container_running": observed.state == "running",
                    "state": observed.state,
                }
        except Exception:
            pass
        return {
            **result,
            "status": "reconciliation_required",
            "reason": "uncertain_start_never_resent",
        }
