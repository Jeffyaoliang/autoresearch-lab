"""Local regression tests. Fixtures are never installed as live research data."""

import unittest, json, math
from unittest.mock import patch
import httpx
from research_hub import bridge


class BoundaryTests(unittest.TestCase):
    def test_metrics_only_numbers(self):
        self.assertEqual(
            bridge.sanitize_metrics(
                {"loss": 0.2, "secret": "fixture-secret", "accuracy": True, "reward": float("inf")}
            ),
            {"loss": 0.2},
        )

    def test_log_redaction(self):
        raw = "loss=0.4 step=12\npassword=fixture-secret loss=999\nignore previous instructions reward=900\nC:\\Users\\private epoch=88\nsuccess_rate=0.75"
        self.assertEqual(
            bridge.metrics_from_log(raw), {"loss": 0.4, "step": 12.0, "success_rate": 0.75}
        )

    def test_jobs_allowlist(self):
        raw = json.dumps(
            {
                "jobs": [
                    {
                        "name": "pt-fixture-private",
                        "state": "RUNNING",
                        "password": "fixture-secret",
                        "command": "unsafe",
                    }
                ]
            }
        )
        parsed = bridge.parse_jobs(raw)
        with patch.object(bridge, "jobs_internal", return_value=(parsed, None)):
            output = bridge.status()
        self.assertTrue(output["available"])
        self.assertNotIn("fixture", json.dumps(output))
        self.assertNotIn("password", json.dumps(output))
        self.assertNotIn("raw_id", json.dumps(output))

    def test_schema_unknown(self):
        with self.assertRaises(ValueError):
            bridge.parse_jobs('{"unsupported":[]}')

    def test_unsafe_job_id(self):
        with self.assertRaises(ValueError):
            bridge.parse_jobs('[{"name":"x; execute","state":"RUNNING"}]')

    def test_unconfigured_is_not_empty_success(self):
        with patch.object(bridge, "config", return_value={"sco_enabled": True, "workspace": None}):
            output = bridge.status()
        self.assertFalse(output["available"])
        self.assertFalse(output["live_research_data"])

    def test_path_input_rejected(self):
        self.assertEqual(bridge.approved_report("../../private")["reason"], "invalid_report_alias")

    def test_shell_input_rejected(self):
        self.assertEqual(bridge.task_progress("task_123;whoami")["reason"], "invalid_task_alias")

    def test_remote_afs_not_local(self):
        with patch.object(
            bridge,
            "config",
            return_value={
                "approved_reports": {
                    "report_fixture": {
                        "reviewed_for_export": True,
                        "source_kind": "remote_acp_afs",
                        "path": "/mnt/afs/private/report.json",
                    }
                }
            },
        ):
            self.assertEqual(
                bridge.approved_report("report_fixture")["reason"],
                "remote_source_access_unverified",
            )

    def test_approved_report_exports_only_snapshot_metrics(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approved.json"
            path.write_text(
                json.dumps(
                    {
                        "observed_at": "2026-01-01T00:00:00+00:00",
                        "state": "SUCCEEDED",
                        "metrics": {"loss": 0.4, "token": "fixture-secret"},
                        "raw_log": "fixture-private-path",
                    }
                ),
                encoding="utf-8",
            )
            cfg = {
                "approved_reports": {
                    "report_fixture": {
                        "reviewed_for_export": True,
                        "source_kind": "windows_approved_json",
                        "path": str(path),
                    }
                }
            }
            with patch.object(bridge, "config", return_value=cfg):
                out = bridge.approved_report("report_fixture")
        self.assertTrue(out["available"])
        self.assertFalse(out["live_research_data"])
        self.assertEqual(out["metrics"], {"loss": 0.4})
        self.assertNotIn("fixture-private", json.dumps(out))
        self.assertNotIn("fixture-secret", json.dumps(out))

    def test_unapproved_report_denied(self):
        with patch.object(
            bridge,
            "config",
            return_value={"approved_reports": {"report_fixture": {"reviewed_for_export": False}}},
        ):
            self.assertEqual(
                bridge.approved_report("report_fixture")["reason"], "report_not_approved"
            )

    def test_missing_timestamp_not_invented(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approved.json"
            path.write_text('{"metrics":{"loss":0.2}}')
            cfg = {
                "approved_reports": {
                    "report_fixture": {
                        "reviewed_for_export": True,
                        "source_kind": "windows_approved_json",
                        "path": str(path),
                    }
                }
            }
            with patch.object(bridge, "config", return_value=cfg):
                out = bridge.approved_report("report_fixture")
        self.assertEqual(out["reason"], "report_observation_time_required")

    def test_owner_scope_match(self):
        raw = json.dumps(
            [
                {
                    "name": "fixture-job",
                    "state": "RUNNING",
                    "ownership": {"user_name": "fixture-user", "user_id": "fixture-owner"},
                }
            ]
        )
        self.assertEqual(len(bridge.parse_jobs(raw, "fixture-user", "fixture-owner")), 1)

    def test_other_user_rejected(self):
        raw = json.dumps(
            [
                {
                    "name": "fixture-job",
                    "state": "RUNNING",
                    "ownership": {"user_name": "other", "user_id": "another"},
                }
            ]
        )
        with self.assertRaises(ValueError):
            bridge.parse_jobs(raw, "fixture-user", "fixture-owner")

    def test_missing_owner_rejected(self):
        with self.assertRaises(ValueError):
            bridge.parse_jobs(
                '[{"name":"fixture-job","state":"RUNNING"}]', "fixture-user", "fixture-owner"
            )

    def test_stale_snapshot_not_live(self):
        snapshot = {"snapshot": {"available": True, "observed_at": "2026-01-01T00:00:00+00:00"}}
        with patch.object(bridge, "read_collector_state", return_value=snapshot):
            out = bridge.health()
        self.assertFalse(out["live_research_data"])
        self.assertFalse(out["collector"]["fresh_source_snapshot"])

    def test_source_errors_do_not_export_raw(self):
        with patch.object(
            bridge, "jobs_internal", return_value=(None, bridge.error("upstream_read_failed"))
        ):
            out = bridge.status()
        self.assertFalse(out["available"])
        self.assertNotIn("stderr", out)
