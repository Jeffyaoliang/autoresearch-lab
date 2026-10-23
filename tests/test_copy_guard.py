"""Offline fixtures only: no SSH, SCO, model, credential, or GPU access."""

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from research_hub.renewal import copy_guard as m
from research_hub.renewal.target import Target


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)
        self.base = {
            "name": "fixture-root",
            "uid": "fixture-root-uid",
            "ownership": {"user_name": "fixture-owner-name", "user_id": "fixture-owner"},
            "workspace_name": "fixture-workspace",
            "state": "SUSPENDED",
            "start_time": (self.now - timedelta(hours=33)).isoformat(),
            "suspend_time": (self.now - timedelta(minutes=30)).isoformat(),
            **{k: {} for k in m.PROFILE_FIELDS},
        }
        self.base["roles"] = [{"startup_script": "SECRET_DO_NOT_EXPORT"}]
        self.base["fault_tolerance"] = {
            "backoff_limit": 3,
            "rules": [
                {"action": "RESTART_JOB", "exit_codes": "1-255", "mark_node_unschedulable": False}
            ],
        }
        self.base["mount"] = [
            {
                "id": "/fixed/resource/afs-volume",
                "type": "PV_AFS",
                "mount_path": "/mnt/afs",
                "subdir": "/",
                "access_mode": "UNSPECIFIED",
            },
            {
                "id": "/fixed/resource/aoss-volume",
                "type": "PV_AOSS",
                "mount_path": "/mnt/aoss",
                "subdir": "",
            },
        ]
        self.target = Target(
            alias=m.alias(self.base["name"]),
            workspace="fixture-workspace",
            owner_prefix="fixture-owner-name",
            uid_sha256=m.digest(self.base["uid"]),
            owner_user_id_sha256=m.digest("fixture-owner"),
            profile_sha256=m.profile(self.base),
            name_prefix="fixture-renew",
            display_name_prefix="fixture-renew",
        )
        self.cache, self.policy_path = self.root / "cache.json", self.root / "policy.json"
        self.ledger = self.root / "copy-ledger.json"
        self.policy = {
            "enabled": True,
            "allowed_alias": self.target.alias,
            "uid_sha256": self.target.uid_sha256,
            "owner_user_id_sha256": self.target.owner_user_id_sha256,
            "profile_sha256": self.target.profile_sha256,
            "replacement_policy": "scheduled_32h_replacement_policy",
            "window_start_hours": 32,
            "window_end_hours": 34,
            "copy_mount_policy": "afs_only",
            "deterministic_name_prefix": self.target.name_prefix,
            "max_generations": 3,
            "initial_expiry_attestations": [
                {"start_time": self.base["start_time"], "suspend_time": self.base["suspend_time"]}
            ],
        }
        self.write(self.cache, self.base)
        self.write(self.policy_path, self.policy)
        self.write(
            self.ledger,
            {
                "schema": 1,
                "template_name": self.base["name"],
                "template_uid_sha256": self.target.uid_sha256,
                "records": [],
            },
        )
        self.jobs, self.calls, self.behavior = [deepcopy(self.base)], [], "ok"
        self.guard = m.Guard(
            self.policy_path,
            self.cache,
            "sco.exe",
            self.runner,
            lambda: self.now,
            target=self.target,
            state_root=self.root,
        )

    def write(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[3] == "list":
            page = int(next(x.split("=")[1] for x in args if x.startswith("--page-token=")))
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(self.jobs[(page - 1) * 100 : page * 100]), stderr=""
            )
        if args[3] == "describe":
            job = next((j for j in self.jobs if j["name"] == args[4]), None)
            return SimpleNamespace(
                returncode=0 if job else 1, stdout=json.dumps(job), stderr="SECRET"
            )
        self.assertEqual(args[3], "copy")
        record = json.loads(self.ledger.read_text())["records"][-1]
        self.assertEqual(record["status"], "CLAIMED")
        self.assertEqual(
            args,
            [
                "sco.exe",
                "acp",
                "jobs",
                "copy",
                "--workspace-name=fixture-workspace",
                "--copy-job-name=fixture-root",
                "--job-name=" + record["display_name"],
                "--name=" + record["name"],
                "--storage-mount=/fixed/resource/afs-volume:/mnt/afs",
            ],
        )
        if self.behavior == "timeout":
            raise subprocess.TimeoutExpired(args, 45, output="SECRET")
        if self.behavior == "nonzero":
            return SimpleNamespace(returncode=70, stdout="SECRET", stderr="SECRET")
        child = deepcopy(self.base)
        child["mount"] = [v for v in child["mount"] if v["type"] == "PV_AFS"]
        child["mount"][0]["subdir"] = ""
        child["fault_tolerance"] = dict(child["fault_tolerance"], rules=[])
        child.update(
            name=record["name"],
            uid="child-uid",
            state="RUNNING",
            start_time=self.now.isoformat(),
            suspend_time=None,
        )
        self.jobs.append(child)
        return SimpleNamespace(returncode=0, stdout="SECRET", stderr="SECRET")

    def copies(self):
        return sum(c[3] == "copy" for c in self.calls)

    def test_readonly_eligible_and_single_copy_bound_observing(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.guard.run()["status"], "eligible")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertTrue(self.guard.run(True)["copy_dispatched"])
        self.assertEqual(self.guard.run()["status"], "bound")
        self.assertEqual(self.guard.run(True)["status"], "bound")
        self.assertEqual(self.guard.run(True)["status"], "observing")
        self.assertEqual(self.copies(), 1)
        self.assertNotIn("SECRET", self.ledger.read_text())

    def test_timeout_never_resends(self):
        self.behavior = "timeout"
        self.assertTrue(self.guard.run(True)["copy_dispatched"])
        self.assertEqual(self.guard.run(True)["status"], "reconciliation_required")
        self.assertEqual(self.copies(), 1)

    def test_nonzero_70_unknown_and_never_resends(self):
        self.behavior = "nonzero"
        result = self.guard.run(True)
        self.assertEqual(
            result["safe_error"], {"category": "backend_error_unclassified", "exit_code": 70}
        )
        self.guard.run(True)
        self.assertEqual(self.copies(), 1)
        self.assertNotIn("SECRET", json.dumps(result) + self.ledger.read_text())

    def test_initial_attestation_required(self):
        self.policy["initial_expiry_attestations"] = []
        self.write(self.policy_path, self.policy)
        self.assertEqual(self.guard.run(True)["reason"], "initial_expiration_unverified")
        self.assertEqual(self.copies(), 0)

    def test_queue_overlap_prevents_copy(self):
        other = deepcopy(self.base)
        other.update(name="another", uid="other", state="WAIT_QUOTA")
        self.jobs.append(other)
        self.assertEqual(
            self.guard.run(True)["reason"], "owned_active_or_unknown_task_prevents_copy"
        )

    def test_early_manual_suspension_blocks(self):
        self.jobs[0]["start_time"] = (self.now - timedelta(hours=2)).isoformat()
        self.assertEqual(self.guard.run(True)["reason"], "outside_scheduled_32h_replacement_window")

    def test_offline_beyond_window_blocks(self):
        self.jobs[0]["start_time"] = (self.now - timedelta(hours=35)).isoformat()
        self.assertEqual(self.guard.run(True)["reason"], "outside_scheduled_32h_replacement_window")

    def test_profile_changed_blocks(self):
        self.jobs[0]["roles"] = []
        self.assertEqual(self.guard.run(True)["reason"], "profile_or_afs_changed")

    def test_stop_flag(self):
        (self.root / "stop.flag").touch()
        self.assertEqual(self.guard.run(True)["reason"], "kill_switch_active")
        self.assertEqual(self.calls, [])

    def test_missing_ledger_no_reset(self):
        self.ledger.unlink()
        self.assertEqual(self.guard.run(True)["reason"], "local_record_unavailable_or_invalid")
        self.assertFalse(self.ledger.exists())

    def test_complete_pagination(self):
        for i in range(100):
            job = deepcopy(self.base)
            job.update(name=f"old-{i}", uid=f"old-{i}")
            self.jobs.append(job)
        self.assertEqual(self.guard.run()["status"], "eligible")
        self.assertEqual(sum(c[3] == "list" for c in self.calls), 2)

    def test_unlimited_explicit_policy(self):
        self.policy["max_generations"] = None
        self.write(self.policy_path, self.policy)
        self.assertEqual(self.guard.run()["status"], "eligible")

    def test_generation_budget_blocks_second_replacement(self):
        self.policy["max_generations"] = 1
        self.write(self.policy_path, self.policy)
        self.guard.run(True)
        self.guard.run(True)  # Bind the named child after the independent read.
        self.now += timedelta(hours=33)
        self.jobs[-1].update(
            state="SUSPENDED", suspend_time=(self.now - timedelta(minutes=30)).isoformat()
        )
        result = self.guard.run(True)
        self.assertEqual(result["reason"], "generation_limit_reached")
        self.assertEqual(self.copies(), 1)
        self.assertEqual(len(json.loads(self.ledger.read_text())["records"]), 1)

    def test_disabled_approval_prevents_transport(self):
        self.policy["enabled"] = False
        self.write(self.policy_path, self.policy)
        self.assertEqual(self.guard.run(True)["reason"], "policy_disabled")
        self.assertEqual(self.calls, [])

    def test_error_contains_only_enum_number_and_uuid(self):
        result = m.safe_error(
            SimpleNamespace(
                returncode=1,
                stdout=json.dumps(
                    {
                        "error": {
                            "http_status": 403,
                            "request_id": "12345678-1234-1234-1234-123456789abc",
                            "message": "SECRET",
                        }
                    }
                ),
                stderr="SECRET",
            )
        )
        self.assertEqual(
            result,
            {
                "category": "permission_denied",
                "exit_code": 1,
                "backend_numeric_code": 403,
                "request_uuid": "12345678-1234-1234-1234-123456789abc",
            },
        )

    def test_late_original_start_detected_before_active_return(self):
        self.guard.run(True)
        self.guard.run(True)
        self.jobs[0]["state"] = "RUNNING"
        result = self.guard.run(True)
        self.assertEqual(result["status"], "overlap_detected")
        self.assertEqual(result["active_or_inflight_count"], 2)
        self.assertEqual(self.copies(), 1)

    def test_afs_only_expected_hash_is_derived_not_policy_override(self):
        self.guard.run(True)
        record = json.loads(self.ledger.read_text())["records"][-1]
        self.assertEqual(record["profile_sha256"], self.target.profile_sha256)
        self.assertEqual(record["child_profile_sha256"], m.profile(self.jobs[-1]))
        self.assertNotEqual(record["child_profile_sha256"], self.target.profile_sha256)
        self.jobs[-1]["mount"].append(deepcopy(self.base["mount"][1]))
        self.assertEqual(self.guard.run(True)["reason"], "profile_or_afs_changed")

    def test_legacy_uncertain_record_hash_backfill_keeps_failure_evidence(self):
        self.guard.run(True)
        data = json.loads(self.ledger.read_text())
        record = data["records"][-1]
        record.pop("child_profile_sha256")
        record["safe_error"] = {"category": "backend_error_unclassified", "exit_code": 70}
        self.write(self.ledger, data)
        self.assertEqual(self.guard.run(True)["status"], "bound")
        updated = json.loads(self.ledger.read_text())["records"][-1]
        self.assertEqual(updated["safe_error"], record["safe_error"])
        self.assertEqual(updated["child_profile_sha256"], m.profile(self.jobs[-1]))
        self.assertEqual(len(json.loads(self.ledger.read_text())["records"]), 1)
        self.assertEqual(self.copies(), 1)

    def test_child_afs_path_change_rejected(self):
        self.guard.run(True)
        self.jobs[-1]["mount"][0]["mount_path"] = "/other/path"
        self.assertEqual(self.guard.run(True)["reason"], "profile_or_afs_changed")

    def test_afs_only_policy_must_be_explicit(self):
        self.policy.pop("copy_mount_policy")
        self.write(self.policy_path, self.policy)
        self.assertEqual(self.guard.run(True)["reason"], "policy_scope_mismatch")
        self.assertEqual(self.copies(), 0)

    def test_error_fixed_flags_never_export_identifiers_or_secrets(self):
        result = m.safe_error(
            SimpleNamespace(
                returncode=70,
                stdout="",
                stderr="invalid mount type PV_AOSS; volume SECRET_PRIVATE_ID not found",
            )
        )
        self.assertEqual(result["category"], "backend_error_unclassified")
        self.assertEqual(
            result["diagnostic_flags"],
            {"mentions_pv_aoss": True, "invalid_mount_type": True, "volume_not_found": True},
        )
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["diagnostic_terms"], ["found", "invalid", "mount", "not"])

    def test_root_mount_and_exact_default_restart_normalization(self):
        before = deepcopy(self.base)
        expected = deepcopy(self.base)
        expected["mount"] = [dict(self.base["mount"][0], subdir="")]
        expected["fault_tolerance"]["rules"] = []
        result_hash, spec = m.afs_copy_profile(self.base)
        self.assertEqual(result_hash, m.profile(expected))
        self.assertEqual(spec, "/fixed/resource/afs-volume:/mnt/afs")
        self.assertEqual(before, self.base)

    def test_nonroot_subdir_fails_closed(self):
        changed = deepcopy(self.base)
        changed["mount"][0]["subdir"] = "/nested"
        with self.assertRaises(m.Rejected):
            m.afs_copy_profile(changed)

    def test_nondefault_restart_rule_is_not_normalized(self):
        changed = deepcopy(self.base)
        changed["fault_tolerance"]["rules"][0]["exit_codes"] = "2-255"
        expected = deepcopy(changed)
        expected["mount"] = [dict(changed["mount"][0], subdir="")]
        self.assertEqual(m.afs_copy_profile(changed)[0], m.profile(expected))

    def test_retired_setup_failure_is_history_only(self):
        self.guard.run(True)
        data = json.loads(self.ledger.read_text())
        failed = data["records"][0]
        failed.update(
            status="SETUP_FAILED_RETIRED",
            uid_sha256=m.digest("bad-setup-child"),
            child_profile_sha256=m.digest("bad-mount-profile"),
            retirement_reason="setup_mount_path_misparsed",
        )
        self.write(self.ledger, data)
        self.assertEqual(self.guard.run()["reason"], "copy_ledger_record_unverified")
        second = deepcopy(failed)
        second.update(
            generation=2,
            name=f"{self.target.name_prefix}-g0002",
            display_name="fixture-renew-g0002",
            status="UNCERTAIN",
            source_uid_sha256=failed["uid_sha256"],
            child_profile_sha256=m.afs_copy_profile(self.base)[0],
        )
        second.pop("uid_sha256")
        data["records"].append(second)
        self.write(self.ledger, data)
        self.assertEqual(
            len(self.guard.ledger(self.base, m.afs_copy_profile(self.base)[0])["records"]), 2
        )
        # It cannot autonomously treat the retired failed setup as an expiry.
        self.assertEqual(self.guard.run(True)["status"], "reconciliation_required")
        self.assertEqual(self.copies(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
