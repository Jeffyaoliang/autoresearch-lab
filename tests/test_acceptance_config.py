"""Acceptance configuration remains independent from execution permission."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_hub.acceptance.channel import observe, prepare, trigger
from research_hub.acceptance.policy import Settings, load_settings
from research_hub.acceptance.store import Store


class AcceptanceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {"RESEARCH_HUB_STATE_DIR": str(self.root)})
        self.env.start()
        self.settings = Settings(
            "fixture-session", self.root / "app.sock", self.root / "peer.json", os.getuid()
        )

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def config(self):
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(self.settings).items()
        }

    def test_missing_configuration_refuses_before_socket(self):
        with patch("research_hub.acceptance.wire.socket.socket") as socket_factory:
            with self.assertRaises(ValueError):
                observe()
            socket_factory.assert_not_called()

    def test_explicit_configuration_does_not_enable_trigger(self):
        (self.root / "acceptance.json").write_text(json.dumps(self.config()))
        settings = load_settings()
        store = Store(self.root / "store", settings.session_id)
        with patch("research_hub.acceptance.wire.socket.socket") as socket_factory:
            self.assertEqual(trigger(store)["decision"], "DISABLED_FAIL_CLOSED")
            socket_factory.assert_not_called()
        self.assertFalse((store.root / "execution.claim").exists())

    def test_executor_configuration_cannot_substitute_for_acceptance(self):
        (self.root / "executor.json").write_text(json.dumps(self.config()))
        with self.assertRaises(ValueError):
            load_settings()

    def test_incomplete_or_extra_configuration_rejected(self):
        for data in ({"session_id": "fixture-session"}, {**self.config(), "enabled": True}):
            (self.root / "acceptance.json").write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                load_settings()

    def test_preflight_from_another_session_is_not_saved(self):
        store = Store(self.root / "store", "fixture-session")
        evidence = {"thread_id": "other-fixture-session", "active_turn_absent_at_snapshot": True}
        with self.assertRaisesRegex(ValueError, "preflight_binding_or_status_invalid"):
            prepare(store, lambda: evidence)
        self.assertIsNone(store.read("preflight.json"))
        self.assertEqual(store.read_audit()[-1]["event"], "preflight_failed")

    def test_state_from_another_session_is_not_reused(self):
        store = Store(self.root / "store", "fixture-session")
        store.prepare()
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            Store(store.root, "different-session").prepare()


if __name__ == "__main__":
    unittest.main()
