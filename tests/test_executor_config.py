"""Configuration and failure-boundary checks using temporary data only."""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from research_hub.executor.core import Actor
from research_hub.executor.policy import Settings, data_dir, load_settings
from research_hub.executor.rpc import ExecutorRPC, expected_peer
from research_hub.executor.state import State


class ExecutorConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {"RESEARCH_HUB_STATE_DIR": str(self.root)})
        self.env.start()
        self.settings = Settings(
            "fixture-session",
            self.root / "project",
            self.root / "app.sock",
            self.root / "peer.json",
            self.root / "codex",
            self.root / "service",
            os.getuid(),
        )

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def config(self):
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(self.settings).items()
        }

    def test_missing_binding_refuses_before_socket(self):
        with patch("research_hub.executor.rpc.socket.socket") as socket_factory:
            with self.assertRaises(ValueError):
                ExecutorRPC()
            socket_factory.assert_not_called()

    def test_loads_only_explicit_binding(self):
        (self.root / "executor.json").write_text(json.dumps(self.config()))
        self.assertEqual(load_settings(), self.settings)

    def test_incomplete_and_unknown_fields_rejected(self):
        for data in (
            {"session_id": "fixture-session"},
            {**self.config(), "enable_execution": True},
        ):
            (self.root / "executor.json").write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                load_settings()

    def test_relative_binding_path_rejected(self):
        data = self.config()
        data["project_dir"] = "relative-project"
        (self.root / "executor.json").write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            load_settings()

    def test_data_root_override_is_separate_from_binding(self):
        custom = self.root / "custom-executor"
        with patch.dict(os.environ, {"RESEARCH_HUB_EXECUTOR_DIR": str(custom)}):
            self.assertEqual(data_dir(), custom)
            state = State()
            try:
                self.assertTrue((custom / "executor.sqlite3").is_file())
                with self.assertRaises(ValueError):
                    Actor(state)
            finally:
                state.db.close()

    def test_research_task_stays_disabled_even_after_gates(self):
        state = State(self.root / "queue")
        try:
            state.submit("fixture_text_001", "text_acceptance")
            state.update("fixture_text_001", state="completed", result={"acceptance_passed": True})
            state.meta("scoped_permissions_verified", True)
            job = state.submit("fixture_research_001", "rax_click_mouse")
            actor = Actor(state, self.settings)
            actor.connect = Mock()
            with self.assertRaisesRegex(ValueError, "scoped_runtime_not_yet_enabled"):
                actor.run(job)
            actor.connect.assert_not_called()
        finally:
            state.db.close()

    def test_queue_cannot_be_retargeted_by_configuration_change(self):
        state = State(self.root / "queue")
        try:
            Actor(state, self.settings)
            changed = replace(self.settings, session_id="another-fixture-session")
            with self.assertRaisesRegex(ValueError, "executor_state_binding_mismatch"):
                Actor(state, changed)
        finally:
            state.db.close()

    def test_missing_peer_has_no_pid_fallback(self):
        with self.assertRaises(FileNotFoundError):
            expected_peer(self.settings)

    def test_reused_peer_pid_rejected(self):
        self.settings.peer_manifest.write_text(json.dumps({"pid": 4242, "start_ticks": "old"}))
        self.settings.peer_manifest.chmod(0o600)
        fake_stat = "4242 (fixture process) " + " ".join(["0"] * 19 + ["new"])
        with patch.object(Path, "read_text", return_value=fake_stat):
            with self.assertRaisesRegex(ValueError, "app_server_pid_reused"):
                expected_peer(self.settings)

    def test_other_session_notifications_are_not_captured(self):
        client = object.__new__(ExecutorRPC)
        client.binding = {"session_id": "fixture-session"}
        client.events = []
        client.dispatch({"method": "turn/completed", "params": {"threadId": "other-session"}})
        self.assertEqual(client.events, [])


if __name__ == "__main__":
    unittest.main()
