import importlib
import json
import subprocess
import unittest
from unittest.mock import patch

from research_hub.guidance import runtime, transport


class RuntimeTests(unittest.TestCase):
    def result(self):
        return {
            "available": True,
            "request_id": "request01",
            "binding": "example_session",
            "policy": "analysis_advisory_only_v1",
            "state": "received_in_codex_context",
            "message_sha256": "a" * 64,
            "codex_received": True,
            "execution_authorized": False,
            "model_processing_started": False,
            "receipt_kind": "official_thread_inject_items_ack",
        }

    def test_no_arbitrary_operations_or_sensitive_messages(self):
        invalid = [
            {"operation": "shell", "request_id": "request01"},
            {"operation": "status", "request_id": "request01", "path": "/example/file"},
            {"operation": "status", "request_id": "../../x"},
        ]
        invalid += [
            {"operation": "submit", "request_id": "request01", "message": text}
            for text in ["Bearer TEST_SENTINEL", "api_key=examplecredential", "a" * 2001]
        ]
        for data in invalid:
            with self.assertRaises(ValueError):
                runtime.validate_request(data)

    def test_receipt_valid(self):
        self.assertTrue(
            runtime.project_result(self.result(), "request01", "example_session")["available"]
        )

    def test_raw_content_false_authority_and_wrong_binding_rejected(self):
        invalid = [
            ("message", "private body"),
            ("execution_authorized", True),
            ("model_processing_started", True),
            ("binding", "other"),
            ("state", "queued_for_bound_codex"),
            ("receipt_kind", None),
        ]
        for key, value in invalid:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    runtime.project_result(
                        {**self.result(), key: value}, "request01", "example_session"
                    )

    def test_fake_transport_is_projected_without_extra_data(self):
        with (
            patch.object(runtime, "read_config", return_value={"session_alias": "example_session"}),
            patch.object(runtime, "run_module", return_value={"metadata": self.result()}) as run,
        ):
            result = runtime.query_guidance({"operation": "status", "request_id": "request01"})
        self.assertTrue(result["available"])
        run.assert_called_once_with(
            "research_hub.guidance.transport", {"operation": "status", "request_id": "request01"}
        )

    def test_missing_configuration_never_launches_transport(self):
        with (
            patch.object(runtime, "read_config", side_effect=ValueError()),
            patch.object(runtime, "run_module") as run,
        ):
            self.assertFalse(
                runtime.query_guidance({"operation": "status", "request_id": "request01"})[
                    "available"
                ]
            )
            run.assert_not_called()

    def test_modules_import_without_platform_dependencies_or_io(self):
        with (
            patch("subprocess.run", side_effect=AssertionError("unexpected process")),
            patch("subprocess.Popen", side_effect=AssertionError("unexpected process")),
        ):
            for name in ["transport", "worker"]:
                importlib.reload(importlib.import_module("research_hub.guidance." + name))
            importlib.reload(importlib.import_module("research_hub.progress.reader"))
            for module in (
                "research_hub.transports.ssh",
                "research_hub.deployment.ssh",
                "research_hub.deployment.ssh_remote",
            ):
                importlib.reload(importlib.import_module(module))

    def test_remote_owner_and_state_are_verified(self):
        config = {
            "sco_executable": "/example/sco",
            "task_name": "example-task",
            "workspace_name": "example-space",
            "owner_user_id": "example-id",
            "owner_user_name": "example-user",
        }
        current = {
            "name": "example-task",
            "workspace_name": "example-space",
            "state": "RUNNING",
            "ownership": {"user_id": "example-id", "user_name": "another-user"},
        }
        response = subprocess.CompletedProcess([], 0, json.dumps(current))
        with patch.object(transport.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(ValueError, "remote_identity_or_state_mismatch"):
                transport.verify_remote(config)
        current["ownership"]["user_name"] = "example-user"
        current["state"] = "SUSPENDED"
        with patch.object(
            transport.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps(current)),
        ):
            with self.assertRaises(ValueError):
                transport.verify_remote(config)

    def test_remote_config_has_no_implicit_user_or_task(self):
        with patch.object(transport, "read_config", return_value={}):
            with self.assertRaises(ValueError):
                transport.load_remote_config()
