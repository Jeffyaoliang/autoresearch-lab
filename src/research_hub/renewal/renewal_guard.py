"""Configured ACP task restart state machine. No create/copy/stop or arbitrary commands.

run() performs reads only; run(apply=True) requires an exact expiry attestation.
A caller must explicitly provide both a pinned Target and a transport runner.
There is no executable CLI or background deployment entry point.
An uncertain start is never resent; a later live observation must reconcile it.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from research_hub.config import state_dir
from .target import Target

FIELDS = (
    "zone",
    "framework",
    "roles",
    "mount",
    "scheduling",
    "resource_pool",
    "root_mapping",
    "fault_tolerance",
    "ssh",
)
ACTIVE = frozenset(
    (
        "QUEUING",
        "QUEUEING",
        "CREATING",
        "STARTING",
        "RUNNING",
        "RESTARTING",
        "RECOVERING",
        "INIT",
        "WAITING",
        "WAIT_QUOTA",
    )
)
PENDING = frozenset(("DISPATCHING", "UNCERTAIN"))


class Rejected(Exception):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise Rejected("invalid_lifecycle_time")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt
    except ValueError:
        raise Rejected("invalid_lifecycle_time") from None


def read_json(path):
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise Rejected("local_record_oversized")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise Rejected("local_record_unavailable") from None
    if not isinstance(value, dict):
        raise Rejected("invalid_local_record")
    return value


def job_record(value):
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        value = value["data"]
    if not isinstance(value, dict):
        raise Rejected("unverified_describe_schema")
    return value


def profile(job):
    if any(field not in job for field in FIELDS):
        raise Rejected("profile_fields_missing")
    return digest({field: job[field] for field in FIELDS})


def verify_identity(cached, fresh, policy, target):
    name, uid, owner = cached.get("name"), cached.get("uid"), cached.get("ownership")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", name)
        or "task_" + hashlib.sha256(name.encode()).hexdigest()[:12] != target.alias
        or not isinstance(uid, str)
        or not uid
        or not isinstance(owner, dict)
        or not isinstance(owner.get("user_id"), str)
        or not owner["user_id"]
        or not isinstance(owner.get("user_name"), str)
        or not owner["user_name"].startswith(target.owner_prefix)
    ):
        raise Rejected("cached_target_not_verified")
    if cached.get("workspace_name") != target.workspace:
        raise Rejected("workspace_mismatch")
    if (
        not isinstance(policy.get("uid_sha256"), str)
        or not isinstance(policy.get("owner_user_id_sha256"), str)
        or digest(uid) != target.uid_sha256
        or policy["uid_sha256"] != target.uid_sha256
        or digest(owner["user_id"]) != target.owner_user_id_sha256
        or policy["owner_user_id_sha256"] != target.owner_user_id_sha256
    ):
        raise Rejected("policy_identity_mismatch")
    if any(fresh.get(k) != cached.get(k) for k in ("name", "uid", "ownership", "workspace_name")):
        raise Rejected("live_identity_mismatch")
    if profile(cached) != target.profile_sha256 or profile(fresh) != target.profile_sha256:
        raise Rejected("profile_changed")


@contextmanager
def exclusive_lock(path):
    stream = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Rejected("another_guard_owns_lock") from None
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Rejected("another_guard_owns_lock") from None
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class Guard:
    def __init__(
        self, policy_path, cache_path, sco, runner, now=None, *, target: Target, state_root=None
    ):
        self.policy_path = Path(policy_path)
        self.root = Path(state_root) if state_root is not None else state_dir() / "renewal"
        self.cache_path, self.sco, self.runner = Path(cache_path), str(sco), runner
        self.target = target
        self.now = now or (lambda: datetime.now(timezone.utc))

    def stopped(self):
        if (self.root / "stop.flag").exists():
            raise Rejected("kill_switch_active")

    def save(self, ledger):
        self.stopped()
        target = self.root / "ledger.json"
        temp = self.root / "ledger.pending"
        # Only under the exclusive lock. A partial write never replaces the ledger.
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(ledger, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        self.stopped()
        os.replace(temp, target)
        if os.name != "nt":
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def describe(self, name):
        try:
            result = self.runner(
                [
                    self.sco,
                    "acp",
                    "jobs",
                    "describe",
                    name,
                    "--workspace-name=" + self.target.workspace,
                    "--format=json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
                check=False,
            )
            if result.returncode != 0:
                raise Rejected("live_describe_failed")
            return job_record(json.loads(result.stdout))
        except (subprocess.TimeoutExpired, OSError, ValueError):
            raise Rejected("live_describe_unavailable") from None

    def run(self, apply=False):
        result = {
            "alias": self.target.alias,
            "checked_at": self.now().isoformat(),
            "mode": "apply" if apply else "check",
            "start_dispatched": False,
        }
        try:
            self.stopped()
            if apply:
                with exclusive_lock(self.root / "renewal.lock"):
                    return self.inspect(result, True)
            return self.inspect(result, False)
        except Rejected as error:
            result.update(status="blocked", reason=str(error))
            return result
        except Exception:
            # Do not leak CLI text, raw requests, paths, env, or exception messages.
            result.update(status="blocked", reason="internal_error_fail_closed")
            return result

    def inspect(self, result, apply):
        policy, cached = read_json(self.policy_path), job_record(read_json(self.cache_path))
        if (
            policy.get("allowed_alias") != self.target.alias
            or policy.get("profile_sha256") != self.target.profile_sha256
        ):
            raise Rejected("policy_target_mismatch")
        # Validate the private cache before using its name in a fixed read command.
        verify_identity(cached, cached, policy, self.target)
        fresh = self.describe(cached["name"])
        verify_identity(cached, fresh, policy, self.target)
        result.update(state=fresh.get("state"), profile_sha256=self.target.profile_sha256)
        ledger_path = self.root / "ledger.json"
        if apply and not ledger_path.exists():
            raise Rejected("ledger_missing_requires_operator_reconciliation")
        ledger = read_json(ledger_path) if ledger_path.exists() else {"schema": 1, "records": {}}
        if ledger.get("schema") != 1 or not isinstance(ledger.get("records"), dict):
            raise Rejected("invalid_ledger")
        uid_hash = digest(cached["uid"])
        for cycle, record in ledger["records"].items():
            if (
                not isinstance(record, dict)
                or record.get("status") not in PENDING | {"ACCEPTED"}
                or record.get("uid_sha256") != uid_hash
                or record.get("profile_sha256") != self.target.profile_sha256
                or cycle
                != digest([cached["uid"], record.get("start_time"), record.get("suspend_time")])
            ):
                raise Rejected("invalid_ledger")
        pending = [
            r
            for r in ledger["records"].values()
            if isinstance(r, dict) and r.get("status") in PENDING
        ]
        if len(pending) > 1:
            raise Rejected("multiple_uncertain_dispatches")
        for record in pending:
            if record.get("uid_sha256") != uid_hash:
                raise Rejected("ledger_target_mismatch")
            changed = fresh.get("start_time") != record.get("start_time")
            if changed and fresh.get("state") in ACTIVE:
                if timestamp(fresh.get("start_time")) <= timestamp(record.get("start_time")):
                    raise Rejected("lifecycle_time_regressed")
                if apply:
                    record.update(
                        status="ACCEPTED",
                        reconciled_at=self.now().isoformat(),
                        accepted_start_time=fresh["start_time"],
                    )
                    self.save(ledger)
                return dict(result, status="accepted", reason="new_start_time_and_active_state")
            return dict(result, status="reconciliation_required", reason="dispatch_not_repeated")
        if fresh.get("state") in ACTIVE:
            return dict(result, status="observing", reason="target_already_active")
        if fresh.get("state") != "SUSPENDED":
            raise Rejected("target_not_suspended")
        start, suspend = fresh.get("start_time"), fresh.get("suspend_time")
        elapsed = (timestamp(suspend) - timestamp(start)).total_seconds()
        if timestamp(suspend) > self.now() or elapsed < 32 * 3600:
            raise Rejected("minimum_32h_lifecycle_not_verified")
        key = digest([cached["uid"], start, suspend])
        result.update(
            source_start_time=start,
            suspend_time=suspend,
            lifecycle_hours=round(elapsed / 3600, 6),
            cycle_key=key,
        )
        if key in ledger["records"]:
            return dict(result, status="reconciliation_required", reason="cycle_already_recorded")
        attestations = policy.get("expiry_attestations", [])
        if not isinstance(attestations, list) or not any(
            isinstance(a, dict)
            and a.get("start_time") == start
            and a.get("suspend_time") == suspend
            for a in attestations
        ):
            raise Rejected("expiration_unverified")
        if policy.get("enabled") is not True:
            raise Rejected("policy_disabled")
        if not apply:
            return dict(result, status="eligible", reason="exact_expiry_attestation_verified")
        latest = self.describe(cached["name"])
        verify_identity(cached, latest, policy, self.target)
        if any(latest.get(k) != fresh.get(k) for k in ("state", "start_time", "suspend_time")):
            raise Rejected("lifecycle_changed_before_dispatch")
        if read_json(self.policy_path) != policy:
            raise Rejected("policy_changed_during_check")
        self.stopped()
        record = {
            "status": "DISPATCHING",
            "uid_sha256": uid_hash,
            "start_time": start,
            "suspend_time": suspend,
            "profile_sha256": self.target.profile_sha256,
            "dispatch_at": self.now().isoformat(),
        }
        ledger["records"][key] = record
        self.save(ledger)  # Durable intent before a potentially irreversible request.
        self.stopped()
        result["start_dispatched"] = True
        try:
            reply = self.runner(
                [
                    self.sco,
                    "acp",
                    "jobs",
                    "start",
                    "--workspace-name=" + self.target.workspace,
                    cached["name"],
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=40,
                check=False,
            )
            record["cli_returncode"] = reply.returncode
        except (subprocess.TimeoutExpired, OSError):
            record["transport_uncertain"] = True
        record["status"] = "UNCERTAIN"
        self.save(ledger)
        # No retry after any submitted request, including nonzero exits/timeouts.
        observed = self.describe(cached["name"])
        verify_identity(cached, observed, policy, self.target)
        if observed.get("state") in ACTIVE and timestamp(observed.get("start_time")) > timestamp(
            start
        ):
            record.update(
                status="ACCEPTED",
                accepted_start_time=observed["start_time"],
                reconciled_at=self.now().isoformat(),
            )
            self.save(ledger)
            return dict(
                result,
                status="accepted",
                reason="new_start_time_and_active_state",
                state=observed["state"],
            )
        return dict(result, status="reconciliation_required", reason="dispatch_not_repeated")
