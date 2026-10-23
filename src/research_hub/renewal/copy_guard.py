"""Configured-template ACP replacement state machine. Default check never writes.
A caller must explicitly provide both a pinned Target and a transport runner.
There is no executable CLI or background deployment entry point.

Only SCO describe/list/copy are implemented. A durable claim is never resent,
including after a timeout or nonzero exit. An explicit afs_only policy preserves
the original AFS mount and excludes other mounts; all other profile fields stay
unchanged. The old start ledger, MCP tools and credentials are not modified.
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

PROFILE_FIELDS = (
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
IN_FLIGHT = ACTIVE | {"SUSPENDING", "DELETING"}
TERMINAL = frozenset(("SUSPENDED", "SUCCEEDED", "FAILED", "DELETED"))


class Rejected(Exception):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def alias(name):
    return "task_" + hashlib.sha256(name.encode()).hexdigest()[:12]


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt
    except (AttributeError, TypeError, ValueError):
        raise Rejected("invalid_lifecycle_time") from None


def read_json(path):
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, ValueError):
        raise Rejected("local_record_unavailable_or_invalid") from None


def unwrap(data):
    return data["data"] if isinstance(data, dict) and isinstance(data.get("data"), dict) else data


def safe_error(result=None, exception=None):
    """Only enum labels/numbers and a structured UUID survive. No raw text."""
    if isinstance(exception, subprocess.TimeoutExpired):
        return {"category": "transport_timeout"}
    if exception is not None:
        return {"category": "transport_disconnected"}
    safe = {"category": "backend_error_unclassified", "exit_code": result.returncode}
    # Keep only fixed, generic diagnostic words. Never retain arbitrary response text.
    words = r"quota|permission|denied|unauthorized|forbidden|invalid|required|unsupported|unknown|flag|parameter|argument|name|job|workspace|region|zone|image|mount|resource|state|conflict|exists|timeout|network|connection|subscription|not|found|copy|error|failed|disabled"
    diagnostic_terms = sorted(
        set(re.findall(r"\b(?:" + words + r")\b", (result.stdout + "\n" + result.stderr).lower()))
    )
    text = (result.stdout + "\n" + result.stderr).lower()
    flags = {
        "mentions_pv_aoss": bool(re.search(r"\bpv_aoss\b", text)),
        "invalid_mount_type": bool(re.search(r"\binvalid[\s_-]+mount[\s_-]+type\b", text)),
        "volume_not_found": bool(re.search(r"\bvolume\b[^\n]{0,120}\bnot[\s_-]+found\b", text)),
    }
    if any(flags.values()):
        safe["diagnostic_flags"] = flags  # Hints only; never asserted as the root cause.
    for raw in (result.stdout, result.stderr):
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        for item in (value, value.get("error")):
            if not isinstance(item, dict):
                continue
            for name in ("status", "status_code", "http_status", "code"):
                code = item.get(name)
                if isinstance(code, int) and not isinstance(code, bool):
                    safe["backend_numeric_code"] = code
                    safe["category"] = {
                        401: "authentication_required",
                        403: "permission_denied",
                        404: "resource_not_found",
                        409: "name_conflict",
                        429: "rate_limited",
                        503: "backend_unavailable",
                    }.get(code, "backend_error_unclassified")
            request = item.get("request_id", item.get("requestId"))
            if isinstance(request, str) and re.fullmatch(
                r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", request
            ):
                safe["request_uuid"] = request.lower()
    if safe["category"] == "backend_error_unclassified" and diagnostic_terms:
        safe["diagnostic_terms"] = diagnostic_terms
    return safe


def profile(job):
    if not isinstance(job, dict) or any(k not in job for k in PROFILE_FIELDS):
        raise Rejected("profile_fields_missing")
    return digest({k: job[k] for k in PROFILE_FIELDS})


def afs_copy_profile(template):
    """Derive the sole permitted mount override from the verified fixed template."""
    mounts = template.get("mount")
    if not isinstance(mounts, list) or not all(isinstance(v, dict) for v in mounts):
        raise Rejected("afs_mount_schema_unverified")
    selected = [v for v in mounts if v.get("type") == "PV_AFS"]
    if len(selected) != 1:
        raise Rejected("exactly_one_fixed_afs_mount_required")
    mount = selected[0]
    for key in ("id", "mount_path", "subdir"):
        value = mount.get(key)
        if (
            not isinstance(value, str)
            or not value
            or any(c in value for c in (":", ",", "\n", "\r", "\x00"))
        ):
            raise Rejected("afs_mount_value_unverified")
    if not mount["mount_path"].startswith("/"):
        raise Rejected("afs_mount_path_unverified")
    if mount["subdir"] != "/":
        raise Rejected("only_fixed_afs_root_subdir_supported")
    derived = {k: template[k] for k in PROFILE_FIELDS}
    # This installed CLI treats a third colon component as part of mount_path.
    # A two-component root mount is observed back as subdir='', equivalent to '/'.
    derived["mount"] = [dict(mount, subdir="")]
    fault = template.get("fault_tolerance")
    if not isinstance(fault, dict):
        raise Rejected("fault_tolerance_schema_unverified")
    default_rule = {
        "action": "RESTART_JOB",
        "exit_codes": "1-255",
        "mark_node_unschedulable": False,
    }
    if fault.get("rules") == [default_rule]:
        derived["fault_tolerance"] = dict(fault, rules=[])
    specification = ":".join(mount[k] for k in ("id", "mount_path"))
    return profile(derived), specification


@contextmanager
def exclusive_lock(path):
    stream = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError:
        raise Rejected("copy_lock_unavailable") from None
    finally:
        if locked:
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
        self.policy_path, self.cache_path = Path(policy_path), Path(cache_path)
        self.root = Path(state_root) if state_root is not None else state_dir() / "renewal"
        self.sco, self.runner = str(sco), runner
        self.target = target
        self.now = now or (lambda: datetime.now(timezone.utc))

    def stopped(self):
        if (self.root / "stop.flag").exists():
            raise Rejected("kill_switch_active")

    def save(self, ledger):
        self.stopped()
        temp = self.root / "copy-ledger.pending"
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(ledger, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        self.stopped()
        os.replace(temp, self.root / "copy-ledger.json")
        if os.name != "nt":
            fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def call_read(self, args):
        self.stopped()
        try:
            reply = self.runner(
                [self.sco, "acp", "jobs", *args, "--format=json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise Rejected("sco_read_unavailable") from None
        if reply.returncode:
            raise Rejected("sco_read_failed")
        try:
            return json.loads(reply.stdout)
        except ValueError:
            raise Rejected("sco_read_schema_unverified") from None

    def describe(self, name):
        value = unwrap(
            self.call_read(["describe", name, "--workspace-name=" + self.target.workspace])
        )
        if not isinstance(value, dict):
            raise Rejected("describe_schema_unverified")
        return value

    def verify(self, job, name=None, uid_hash=None, expected_profile=None):
        ownership = job.get("ownership", {})
        if (
            not isinstance(ownership, dict)
            or not isinstance(job.get("uid"), str)
            or not job["uid"]
            or not isinstance(ownership.get("user_name"), str)
            or not ownership["user_name"].startswith(self.target.owner_prefix)
            or digest(ownership.get("user_id")) != self.target.owner_user_id_sha256
            or job.get("workspace_name") != self.target.workspace
            or (name is not None and job.get("name") != name)
            or (uid_hash is not None and digest(job["uid"]) != uid_hash)
        ):
            raise Rejected("identity_or_workspace_mismatch")
        if profile(job) != (
            self.target.profile_sha256 if expected_profile is None else expected_profile
        ):
            raise Rejected("profile_or_afs_changed")

    def list_owned(self, owner_id):
        jobs, seen, page = [], set(), 1
        while page <= 100:
            data = self.call_read(
                [
                    "list",
                    "--workspace-name=" + self.target.workspace,
                    "--user-id=" + owner_id,
                    "--page-size=100",
                    "--page-token=" + str(page),
                ]
            )
            if isinstance(data, dict):
                data = data.get("data", data)
                if isinstance(data, dict):
                    data = data.get("jobs", data.get("items"))
            if not isinstance(data, list) or len(data) > 100:
                raise Rejected("list_pagination_schema_unverified")
            for job in data:
                if not isinstance(job, dict) or job.get("ownership", {}).get("user_id") != owner_id:
                    raise Rejected("list_owner_scope_unverified")
                key = (job.get("name"), job.get("uid"))
                if not all(isinstance(v, str) and v for v in key) or key in seen:
                    raise Rejected("list_pagination_not_complete")
                seen.add(key)
                jobs.append(job)
            if len(data) < 100:
                return jobs
            page += 1
        raise Rejected("list_page_limit_reached")

    def verify_policy(self, policy):
        required = {
            "allowed_alias": self.target.alias,
            "uid_sha256": self.target.uid_sha256,
            "owner_user_id_sha256": self.target.owner_user_id_sha256,
            "profile_sha256": self.target.profile_sha256,
            "replacement_policy": "scheduled_32h_replacement_policy",
            "window_start_hours": 32,
            "window_end_hours": 34,
            "deterministic_name_prefix": self.target.name_prefix,
            "copy_mount_policy": "afs_only",
        }
        if any(policy.get(k) != v for k, v in required.items()):
            raise Rejected("policy_scope_mismatch")
        limit = policy.get("max_generations")
        if "max_generations" not in policy or (
            limit is not None
            and (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000)
        ):
            raise Rejected("generation_limit_required")
        if policy.get("enabled") is not True:
            raise Rejected("policy_disabled")

    def ledger(self, template, child_profile):
        ledger = read_json(self.root / "copy-ledger.json")
        if (
            ledger.get("schema") != 1
            or ledger.get("template_name") != template["name"]
            or ledger.get("template_uid_sha256") != self.target.uid_sha256
            or not isinstance(ledger.get("records"), list)
        ):
            raise Rejected("copy_ledger_unverified")
        for generation, record in enumerate(ledger["records"], 1):
            expected_source = (
                self.target.uid_sha256
                if generation == 1
                else ledger["records"][generation - 2].get("uid_sha256")
            )
            retired = isinstance(record, dict) and record.get("status") == "SETUP_FAILED_RETIRED"
            if (
                not isinstance(record, dict)
                or record.get("generation") != generation
                or record.get("name") != f"{self.target.name_prefix}-g{generation:04d}"
                or record.get("display_name")
                != f"{self.target.display_name_prefix}-g{generation:04d}"
                or record.get("profile_sha256") != self.target.profile_sha256
                or (
                    not retired
                    and "child_profile_sha256" in record
                    and record["child_profile_sha256"] != child_profile
                )
                or record.get("source_uid_sha256") != expected_source
                or record.get("status")
                not in ("CLAIMED", "UNCERTAIN", "BOUND", "SETUP_FAILED_RETIRED")
                or (
                    generation < len(ledger["records"])
                    and record.get("status") not in ("BOUND", "SETUP_FAILED_RETIRED")
                )
                or (
                    record.get("status") == "BOUND"
                    and record.get("child_profile_sha256") != child_profile
                )
                or (
                    record.get("status") in ("BOUND", "SETUP_FAILED_RETIRED")
                    and not re.fullmatch("[a-f0-9]{64}", record.get("uid_sha256", ""))
                )
                or (
                    retired
                    and (
                        generation == len(ledger["records"])
                        or record.get("retirement_reason") != "setup_mount_path_misparsed"
                        or not re.fullmatch("[a-f0-9]{64}", record.get("child_profile_sha256", ""))
                    )
                )
            ):
                raise Rejected("copy_ledger_record_unverified")
        return ledger

    def eligible(self, source, initial, policy):
        if source.get("state") != "SUSPENDED":
            raise Rejected("source_not_suspended")
        start, suspend = source.get("start_time"), source.get("suspend_time")
        start_at, suspended_at = timestamp(start), timestamp(suspend)
        age = (self.now() - start_at).total_seconds() / 3600
        lifetime = (suspended_at - start_at).total_seconds() / 3600
        if not 32 <= age <= 34 or not 32 <= lifetime <= 34 or suspended_at > self.now():
            raise Rejected("outside_scheduled_32h_replacement_window")
        if initial and not any(
            isinstance(a, dict)
            and a.get("start_time") == start
            and a.get("suspend_time") == suspend
            for a in policy.get("initial_expiry_attestations", [])
        ):
            raise Rejected("initial_expiration_unverified")

    def run(self, apply=False):
        result = {
            "root_alias": self.target.alias,
            "checked_at": self.now().isoformat(),
            "mode": "apply" if apply else "check",
            "copy_dispatched": False,
        }
        try:
            self.stopped()
            if apply:
                with exclusive_lock(self.root / "copy-renewal.lock"):
                    return self.inspect(result, True)
            return self.inspect(result, False)
        except Rejected as error:
            return dict(result, status="blocked", reason=str(error))
        except Exception:
            return dict(result, status="blocked", reason="internal_error_fail_closed")

    def inspect(self, result, apply):
        policy = read_json(self.policy_path)
        self.verify_policy(policy)
        cached = unwrap(read_json(self.cache_path))
        if not isinstance(cached, dict) or alias(cached.get("name", "")) != self.target.alias:
            raise Rejected("template_cache_unverified")
        self.verify(cached, uid_hash=self.target.uid_sha256)
        template = self.describe(cached["name"])
        self.verify(template, cached["name"], self.target.uid_sha256)
        child_profile, afs_spec = afs_copy_profile(template)
        ledger = self.ledger(template, child_profile)
        records = ledger["records"]
        owner_id = cached["ownership"]["user_id"]
        jobs = self.list_owned(owner_id)
        root_rows = [j for j in jobs if j.get("name") == template["name"]]
        if len(root_rows) != 1 or root_rows[0].get("uid") != template["uid"]:
            raise Rejected("complete_owned_list_missing_verified_template")
        if any(j.get("state") not in IN_FLIGHT | TERMINAL for j in jobs):
            raise Rejected("owned_unknown_state_prevents_copy")
        active = [j for j in jobs if j.get("state") in IN_FLIGHT]
        if len(active) > 1:
            return dict(
                result,
                status="overlap_detected",
                reason="multiple_owned_active_or_inflight_tasks",
                active_or_inflight_count=len(active),
            )
        result.update(
            listed_owned_jobs=len(jobs),
            profile_sha256=self.target.profile_sha256,
            child_profile_sha256=child_profile,
            copy_mount_policy="afs_only",
            generation=len(records),
        )
        current = template
        if records:
            latest = records[-1]
            matched = [job for job in jobs if job.get("name") == latest["name"]]
            if len(matched) != 1:
                return dict(
                    result,
                    status="reconciliation_required",
                    reason="claimed_name_not_observed_no_resend",
                )
            child = self.describe(latest["name"])
            self.verify(child, latest["name"], latest.get("uid_sha256"), child_profile)
            if latest["status"] != "BOUND" or "child_profile_sha256" not in latest:
                if read_json(self.policy_path) != policy:
                    raise Rejected("policy_changed_during_check")
                if apply:
                    latest.update(
                        status="BOUND",
                        uid_sha256=digest(child["uid"]),
                        child_profile_sha256=child_profile,
                        bound_at=self.now().isoformat(),
                    )
                    self.save(ledger)
                return dict(
                    result,
                    status="bound",
                    reason="copy_identity_and_exact_profile_verified",
                    child_alias=alias(child["name"]),
                    state=child.get("state"),
                )
            current = child
        if current.get("state") in ACTIVE:
            if active and active[0].get("name") != current["name"]:
                return dict(
                    result, status="overlap_detected", reason="active_owner_differs_from_bound_task"
                )
            return dict(
                result, status="observing", reason="bound_task_active", state=current["state"]
            )
        if template.get("state") in IN_FLIGHT or template.get("state") not in TERMINAL:
            raise Rejected("root_template_not_safely_inactive")
        for job in jobs:
            state = job.get("state")
            # Conservatively refuse any owned active/unknown task; a missing resource
            # shape must not silently let an overlapping replacement start.
            if state in IN_FLIGHT or state not in TERMINAL:
                raise Rejected("owned_active_or_unknown_task_prevents_copy")
        self.eligible(current, not records, policy)
        generation = len(records) + 1
        if policy["max_generations"] is not None and generation > policy["max_generations"]:
            raise Rejected("generation_limit_reached")
        result.update(
            generation=generation,
            source_start_time=current["start_time"],
            source_suspend_time=current["suspend_time"],
            replacement_policy="scheduled_32h_replacement_policy",
        )
        if not apply:
            return dict(
                result, status="eligible", reason="fixed_profile_replacement_window_verified"
            )
        # Recheck both lifecycle and complete owned-task list immediately before claim.
        fresh = self.describe(current["name"])
        self.verify(
            fresh,
            current["name"],
            digest(current["uid"]),
            self.target.profile_sha256 if not records else child_profile,
        )
        if any(fresh.get(k) != current.get(k) for k in ("state", "start_time", "suspend_time")):
            raise Rejected("source_changed_before_claim")
        jobs = self.list_owned(owner_id)
        if any(j.get("state") in IN_FLIGHT or j.get("state") not in TERMINAL for j in jobs):
            raise Rejected("owned_task_became_active_before_claim")
        if read_json(self.policy_path) != policy:
            raise Rejected("policy_changed_during_check")
        self.eligible(fresh, not records, policy)
        self.stopped()
        name = f"{self.target.name_prefix}-g{generation:04d}"
        if any(j.get("name") == name for j in jobs):
            raise Rejected("deterministic_name_exists_without_claim")
        record = {
            "generation": generation,
            "name": name,
            "display_name": f"{self.target.display_name_prefix}-g{generation:04d}",
            "profile_sha256": self.target.profile_sha256,
            "child_profile_sha256": child_profile,
            "copy_mount_policy": "afs_only",
            "status": "CLAIMED",
            "source_uid_sha256": digest(current["uid"]),
            "source_start_time": current["start_time"],
            "source_suspend_time": current["suspend_time"],
            "claimed_at": self.now().isoformat(),
        }
        records.append(record)
        self.save(ledger)
        self.stopped()
        result["copy_dispatched"] = True
        try:
            reply = self.runner(
                [
                    self.sco,
                    "acp",
                    "jobs",
                    "copy",
                    "--workspace-name=" + self.target.workspace,
                    "--copy-job-name=" + cached["name"],
                    "--job-name=" + record["display_name"],
                    "--name=" + name,
                    "--storage-mount=" + afs_spec,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                check=False,
            )
            record["exit_code"] = reply.returncode
            if reply.returncode:
                record["safe_error"] = safe_error(reply)
                result["safe_error"] = record["safe_error"]
        except (subprocess.TimeoutExpired, OSError) as error:
            record["safe_error"] = safe_error(exception=error)
            result["safe_error"] = record["safe_error"]
        record["status"] = "UNCERTAIN"
        self.save(ledger)
        # No response text is treated as acceptance. Next check independently lists
        # and describes this claimed name, and never reissues the same generation.
        return dict(
            result,
            status="reconciliation_required",
            reason="copy_submitted_verify_named_job_no_resend",
        )
