"""Offline boundary tests. No SCO, SSH, credentials, or real resources are used."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from research_hub.renewal import renewal_guard as guard
from research_hub.renewal.target import Target


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = {
            "name": "owned-fixture",
            "uid": "fixture-uid",
            "ownership": {
                "user_name": "fixture-owner-name",
                "user_id": "owner-id",
                "tenant_id": "tenant-id",
                "requestor_id": "requestor-id",
            },
            "workspace_name": "fixture-workspace",
            "state": "SUSPENDED",
            "start_time": "2026-10-21T08:08:51Z",
            "suspend_time": "2026-10-22T16:32:31Z",
            **{field: {} for field in guard.FIELDS},
        }
        self.job["roles"] = [{"startup_script": "PRIVATE_STARTUP_SCRIPT_DO_NOT_EXPORT"}]
        self.alias = "task_" + hashlib.sha256(self.job["name"].encode()).hexdigest()[:12]
        self.profile = guard.profile(self.job)
        self.target = Target(
            alias=self.alias,
            workspace="fixture-workspace",
            owner_prefix="fixture-owner-name",
            uid_sha256=guard.digest(self.job["uid"]),
            owner_user_id_sha256=guard.digest(self.job["ownership"]["user_id"]),
            profile_sha256=self.profile,
        )
        self.policy = {
            "enabled": True,
            "allowed_alias": self.alias,
            "profile_sha256": self.profile,
            "expiry_attestations": [],
            "uid_sha256": self.target.uid_sha256,
            "owner_user_id_sha256": self.target.owner_user_id_sha256,
        }
        self.cache = self.root / "cache.json"
        self.policy_path = self.root / "policy.json"
        self.write(self.cache, self.job)
        self.write(self.root / "ledger.json", {"schema": 1, "records": {}})
        self.persist_policy()
        self.fresh = deepcopy(self.job)
        self.calls = []
        self.start_behavior = "accepted"

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def persist_policy(self):
        self.write(self.policy_path, self.policy)

    def attest(self):
        self.policy["expiry_attestations"] = [
            {"start_time": self.job["start_time"], "suspend_time": self.job["suspend_time"]}
        ]
        self.persist_policy()

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[3] == "describe":
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.fresh), stderr="")
        self.assertEqual(
            args,
            [
                "sco.exe",
                "acp",
                "jobs",
                "start",
                "--workspace-name=fixture-workspace",
                self.job["name"],
            ],
        )
        if self.start_behavior == "timeout":
            raise subprocess.TimeoutExpired(args, 40, output="DO_NOT_EXPORT", stderr="SECRET")
        if self.start_behavior == "disconnect":
            raise OSError("SECRET TRANSPORT MESSAGE")
        if self.start_behavior == "nonzero":
            return SimpleNamespace(returncode=3, stdout="SECRET", stderr="SECRET")
        self.fresh.update(state="STARTING", start_time="2026-10-23T00:00:00Z")
        return SimpleNamespace(returncode=0, stdout="SECRET RESPONSE", stderr="")

    def run_guard(self, apply=False):
        instance = guard.Guard(
            self.policy_path,
            self.cache,
            "sco.exe",
            self.runner,
            now=lambda: datetime(2026, 10, 23, 1, tzinfo=timezone.utc),
            target=self.target,
            state_root=self.root,
        )
        return instance.run(apply)

    def starts(self):
        return sum(args[3] == "start" for args in self.calls)

    def test_unexplained_32h_suspension_is_not_expiry(self):
        result = self.run_guard(True)
        self.assertEqual(result["reason"], "expiration_unverified")
        self.assertEqual(self.starts(), 0)
        self.assertEqual(json.loads((self.root / "ledger.json").read_text())["records"], {})

    def test_manual_cause_does_not_authorize(self):
        self.fresh["reason"] = "manual_stop"
        self.assertEqual(self.run_guard(True)["reason"], "expiration_unverified")
        self.assertEqual(self.starts(), 0)

    def test_similar_timestamp_attestation_rejected(self):
        self.attest()
        self.policy["expiry_attestations"][0]["suspend_time"] = "2026-10-22T16:32:32Z"
        self.persist_policy()
        self.assertEqual(self.run_guard(True)["reason"], "expiration_unverified")

    def test_check_is_read_only_even_when_eligible(self):
        self.attest()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.run_guard()["status"], "eligible")
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        self.assertEqual(self.starts(), 0)

    def test_policy_and_cache_cannot_jointly_rebind_configured_target(self):
        self.attest()
        changed = deepcopy(self.job)
        changed["uid"] = "different-instance"
        self.write(self.cache, changed)
        self.fresh = deepcopy(changed)
        self.policy["uid_sha256"] = guard.digest(changed["uid"])
        self.persist_policy()
        self.assertEqual(self.run_guard(True)["reason"], "policy_identity_mismatch")
        self.assertEqual(self.starts(), 0)

    def test_exact_attestation_single_start_and_durable_intent(self):
        self.attest()
        original = self.runner

        def inspect_intent(args, **kwargs):
            if args[3] == "start":
                ledger = json.loads((self.root / "ledger.json").read_text())
                self.assertEqual(next(iter(ledger["records"].values()))["status"], "DISPATCHING")
            return original(args, **kwargs)

        self.runner = inspect_intent
        result = self.run_guard(True)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["start_dispatched"])
        self.assertEqual(self.starts(), 1)
        serialized = json.dumps(result) + (self.root / "ledger.json").read_text()
        for forbidden in [
            "PRIVATE_STARTUP",
            "SECRET",
            self.job["name"],
            self.job["uid"],
            "owner-id",
        ]:
            self.assertNotIn(forbidden, serialized)
        self.run_guard(True)
        self.assertEqual(self.starts(), 1)

    def test_timeout_is_never_retried(self):
        self.attest()
        self.start_behavior = "timeout"
        self.assertEqual(self.run_guard(True)["status"], "reconciliation_required")
        self.assertEqual(self.run_guard(True)["reason"], "dispatch_not_repeated")
        self.assertEqual(self.starts(), 1)

    def test_disconnect_and_nonzero_are_not_retried(self):
        self.attest()
        self.start_behavior = "disconnect"
        self.run_guard(True)
        self.start_behavior = "nonzero"
        self.run_guard(True)
        self.assertEqual(self.starts(), 1)

    def test_nonzero_exit_is_uncertain_not_retriable(self):
        self.attest()
        self.start_behavior = "nonzero"
        self.run_guard(True)
        self.run_guard(True)
        self.assertEqual(self.starts(), 1)

    def test_later_read_only_reconciliation(self):
        self.attest()
        self.start_behavior = "timeout"
        self.run_guard(True)
        before = (self.root / "ledger.json").read_bytes()
        self.fresh.update(state="RUNNING", start_time="2026-10-23T00:01:00Z")
        result = self.run_guard()
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["start_dispatched"])
        self.assertEqual(before, (self.root / "ledger.json").read_bytes())
        self.assertEqual(self.starts(), 1)

    def test_profile_change_blocks(self):
        self.attest()
        self.fresh["roles"][0]["startup_script"] = "changed"
        self.assertEqual(self.run_guard(True)["reason"], "profile_changed")
        self.assertEqual(self.starts(), 0)

    def test_owner_uid_and_workspace_must_match(self):
        self.attest()
        for key, value in [
            ("uid", "another"),
            ("workspace_name", "other"),
            ("ownership", {"user_id": "other", "user_name": "fixture-owner-name"}),
        ]:
            with self.subTest(key=key):
                self.fresh = deepcopy(self.job)
                self.fresh[key] = value
                self.assertEqual(self.run_guard(True)["reason"], "live_identity_mismatch")
        self.assertEqual(self.starts(), 0)

    def test_cache_and_live_identity_changes_cannot_rebind(self):
        self.attest()
        for key in ("uid", "owner"):
            with self.subTest(key=key):
                cached = deepcopy(self.job)
                if key == "uid":
                    cached["uid"] = "new-instance-same-name"
                else:
                    cached["ownership"]["user_id"] = "new-owner"
                self.write(self.cache, cached)
                self.fresh = deepcopy(cached)
                self.assertEqual(self.run_guard(True)["reason"], "policy_identity_mismatch")
        self.assertEqual(self.starts(), 0)

    def test_early_suspension_and_running_state_block(self):
        self.attest()
        self.fresh["suspend_time"] = "2026-10-21T10:08:51Z"
        self.assertEqual(self.run_guard(True)["reason"], "minimum_32h_lifecycle_not_verified")
        self.fresh = deepcopy(self.job)
        self.fresh["state"] = "RUNNING"
        self.assertEqual(self.run_guard(True)["status"], "observing")
        self.assertEqual(self.starts(), 0)

    def test_suspending_cannot_start(self):
        self.attest()
        self.fresh["state"] = "SUSPENDING"
        self.assertEqual(self.run_guard(True)["reason"], "target_not_suspended")
        self.assertEqual(self.starts(), 0)

    def test_corrupt_ledger_fails_closed(self):
        self.attest()
        self.write(
            self.root / "ledger.json", {"schema": 1, "records": {"fake": {"status": "ACCEPTED"}}}
        )
        self.assertEqual(self.run_guard(True)["reason"], "invalid_ledger")
        self.assertEqual(self.starts(), 0)

    def test_lost_ledger_cannot_reinitialize_and_dispatch(self):
        self.attest()
        (self.root / "ledger.json").unlink()
        self.assertEqual(
            self.run_guard(True)["reason"], "ledger_missing_requires_operator_reconciliation"
        )
        self.assertFalse((self.root / "ledger.json").exists())
        self.assertEqual(self.starts(), 0)

    def test_lifecycle_change_between_preflight_and_dispatch(self):
        self.attest()
        original = self.runner

        def concurrent_change(args, **kwargs):
            if len(self.calls) == 1:
                self.fresh["state"] = "STARTING"
            return original(args, **kwargs)

        self.runner = concurrent_change
        self.assertEqual(self.run_guard(True)["reason"], "lifecycle_changed_before_dispatch")
        self.assertEqual(self.starts(), 0)

    def test_policy_disabled_between_preflight_and_dispatch(self):
        self.attest()
        original = self.runner

        def concurrent_change(args, **kwargs):
            if len(self.calls) == 1:
                self.policy["enabled"] = False
                self.persist_policy()
            return original(args, **kwargs)

        self.runner = concurrent_change
        self.assertEqual(self.run_guard(True)["reason"], "policy_changed_during_check")
        self.assertEqual(self.starts(), 0)

    def test_disabled_policy_blocks(self):
        self.attest()
        self.policy["enabled"] = False
        self.persist_policy()
        self.assertEqual(self.run_guard(True)["reason"], "policy_disabled")
        self.assertEqual(self.starts(), 0)

    def test_kill_switch_blocks_and_no_ledger(self):
        self.attest()
        (self.root / "stop.flag").touch()
        self.assertEqual(self.run_guard(True)["reason"], "kill_switch_active")
        self.assertEqual(json.loads((self.root / "ledger.json").read_text())["records"], {})
        self.assertEqual(self.starts(), 0)

    def test_kill_switch_after_intent_prevents_dispatch(self):
        self.attest()
        original = guard.Guard.save

        def save_and_stop(instance, value):
            original(instance, value)
            (self.root / "stop.flag").touch()

        with patch.object(guard.Guard, "save", save_and_stop):
            self.assertEqual(self.run_guard(True)["reason"], "kill_switch_active")
        self.assertEqual(self.starts(), 0)
        (self.root / "stop.flag").unlink()
        self.assertEqual(self.run_guard(True)["reason"], "dispatch_not_repeated")
        self.assertEqual(self.starts(), 0)

    def test_nonblocking_lock(self):
        self.attest()
        with guard.exclusive_lock(self.root / "renewal.lock"):
            self.assertEqual(self.run_guard(True)["reason"], "another_guard_owns_lock")
        self.assertEqual(self.starts(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
