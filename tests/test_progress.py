import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_hub.progress import projection, reader, runtime


def config():
    return {
        "project": "Example Research",
        "manifest_path": "/example/project/manifest.json",
        "evidence_ref": "example_manifest",
        "approved_stage": "dependency_check",
        "approved_status": "Blocked",
        "check_names": ["sandbox", "autonomous_episode"],
        "max_source_age_seconds": 3600,
    }


def probe(now=None):
    now = datetime.now(timezone.utc) if now is None else now
    return {
        "metadata": {
            "observed_at": now.isoformat(),
            "sources": [
                {
                    "path": config()["manifest_path"],
                    "sha256": "a" * 64,
                    "fields": [
                        {"field": key, "value": value}
                        for key, value in {
                            "/project": "Example Research",
                            "/current_stage": "dependency_check",
                            "/status": "Blocked",
                            "/updated_at_utc": (now - timedelta(minutes=5)).isoformat(),
                            "/checks/sandbox/status": "Unknown",
                            "/checks/autonomous_episode/status": "Blocked",
                        }.items()
                    ],
                    "execution_counts": {
                        "/execution/episodes_started": 0,
                        "/execution/simulators_started": 0,
                        "/execution/generated_programs_executed": 0,
                        "/execution/task_success": None,
                    },
                }
            ],
        }
    }


class ProjectionTests(unittest.TestCase):
    def test_reviewed_source_does_not_claim_live_scientific_results(self):
        result = projection.project_progress(probe(), config())
        self.assertEqual(result["recorded_status"], "Blocked")
        self.assertEqual(result["execution_record"]["episodes_started"], 0)
        self.assertFalse(result["live_research_progress_verified"])
        self.assertFalse(result["acp_task_mapping_verified"])
        self.assertIsNone(result["task_success"])

    def test_extra_source_text_is_not_exported(self):
        data = probe()
        data["metadata"]["sources"][0]["fields"].append(
            {"field": "/unrelated", "value": "SENTINEL_PRIVATE"}
        )
        self.assertNotIn("SENTINEL", json.dumps(projection.project_progress(data, config())))

    def test_substituted_and_duplicate_manifest_rejected(self):
        data = probe()
        data["metadata"]["sources"][0]["path"] = "/example/other.json"
        with self.assertRaises(ValueError):
            projection.project_progress(data, config())
        data = probe()
        data["metadata"]["sources"] *= 2
        with self.assertRaises(ValueError):
            projection.project_progress(data, config())

    def test_changed_stage_and_unreviewed_scientific_result_rejected(self):
        data = probe()
        data["metadata"]["sources"][0]["execution_counts"]["/execution/task_success"] = True
        with self.assertRaisesRegex(ValueError, "unreviewed_scientific_result"):
            projection.project_progress(data, config())
        data = probe()
        data["metadata"]["sources"][0]["fields"][1]["value"] = "training_done"
        with self.assertRaisesRegex(ValueError, "unreviewed_stage"):
            projection.project_progress(data, config())

    def test_stale_source_requires_explicit_historical_mode(self):
        data = probe()
        data["metadata"]["sources"][0]["fields"][3]["value"] = (
            datetime.now(timezone.utc) - timedelta(days=3)
        ).isoformat()
        with self.assertRaisesRegex(ValueError, "source_stale"):
            projection.project_progress(data, config())
        result = projection.project_progress(data, {**config(), "allow_historical_source": True})
        self.assertEqual(result["record_kind"], "historical_project_record")
        self.assertFalse(result["live_research_progress_verified"])
        self.assertGreater(result["source_age_seconds_at_read"], 86400)

    def test_future_source_duplicate_fields_and_boolean_counts_rejected(self):
        data = probe()
        data["metadata"]["sources"][0]["fields"][3]["value"] = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()
        with self.assertRaises(ValueError):
            projection.project_progress(data, config())
        data = probe()
        data["metadata"]["sources"][0]["fields"].append(
            copy.deepcopy(data["metadata"]["sources"][0]["fields"][0])
        )
        with self.assertRaises(ValueError):
            projection.project_progress(data, config())
        data = probe()
        data["metadata"]["sources"][0]["execution_counts"]["/execution/episodes_started"] = True
        with self.assertRaises(ValueError):
            projection.project_progress(data, config())

    def test_bounded_local_probe_excludes_unrelated_manifest_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            data = {
                "project": "Example Research",
                "current_stage": "dependency_check",
                "status": "Blocked",
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                "unrelated": "SENTINEL_PRIVATE",
                "checks": {
                    "sandbox": {"status": "Unknown"},
                    "autonomous_episode": {"status": "Blocked"},
                },
                "execution": {
                    "episodes_started": 0,
                    "simulators_started": 0,
                    "generated_programs_executed": 0,
                    "task_success": None,
                },
            }
            path.write_text(json.dumps(data))
            local_config = {**config(), "manifest_path": str(path)}
            output = reader.read_manifest_probe(local_config)
            self.assertNotIn("SENTINEL", json.dumps(output))
            self.assertEqual(
                projection.project_progress(output, local_config)["project"], "Example Research"
            )


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        runtime.CACHE = None
        self.addCleanup(setattr, runtime, "CACHE", None)

    def test_fresh_response_is_cached(self):
        with (
            patch.object(
                runtime, "projection_config", return_value=projection.projection_config(config())
            ),
            patch.object(runtime, "run_module", return_value=probe()) as run,
        ):
            self.assertTrue(runtime.query_progress()["available"])
            self.assertTrue(runtime.query_progress()["available"])
            self.assertEqual(run.call_count, 1)

    def test_stale_or_future_reads_fail_closed(self):
        for offset in [timedelta(minutes=-3), timedelta(minutes=3)]:
            with (
                self.subTest(offset=offset),
                patch.object(
                    runtime,
                    "projection_config",
                    return_value=projection.projection_config(config()),
                ),
                patch.object(
                    runtime, "run_module", return_value=probe(datetime.now(timezone.utc) + offset)
                ),
            ):
                self.assertFalse(runtime.query_progress()["available"])
                self.assertIsNone(runtime.CACHE)

    def test_expired_cache_is_not_returned_after_reader_failure(self):
        old = probe(datetime.now(timezone.utc) - timedelta(seconds=70))
        result = projection.project_progress(old, config())
        checked_config = projection.projection_config(config())
        runtime.CACHE = {"config": json.dumps(checked_config, sort_keys=True), "result": result}
        with (
            patch.object(runtime, "projection_config", return_value=checked_config),
            patch.object(runtime, "run_module", side_effect=ValueError("private failure detail")),
        ):
            response = runtime.query_progress()
        self.assertFalse(response["available"])
        self.assertNotIn("private", json.dumps(response))
        self.assertIsNone(runtime.CACHE)

    def test_configuration_change_invalidates_cache(self):
        checked_config = projection.projection_config(config())
        with (
            patch.object(runtime, "projection_config", return_value=checked_config),
            patch.object(runtime, "run_module", return_value=probe()),
        ):
            self.assertTrue(runtime.query_progress()["available"])
        with (
            patch.object(
                runtime,
                "projection_config",
                return_value={**checked_config, "approved_stage": "other_stage"},
            ),
            patch.object(runtime, "run_module", return_value=probe()) as run,
        ):
            self.assertFalse(runtime.query_progress()["available"])
            run.assert_called_once()
