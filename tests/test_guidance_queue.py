import json
from pathlib import Path
import tempfile
import unittest

from research_hub.guidance.queue import GuidanceQueue


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.queue = GuidanceQueue(self.root, "example_session")

    def test_receipt_does_not_claim_model_delivery(self):
        result = self.queue.submit("verify_0001", "仅分析现有进度，暂不执行。")
        self.assertFalse(result["codex_received"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["state"], "queued_waiting_for_codex_binding")

    def test_same_request_not_duplicated(self):
        first = self.queue.submit("verify_0001", "仅确认编号")
        second = self.queue.submit("verify_0001", "仅确认编号")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_conflict_preserves_original(self):
        self.queue.submit("verify_0001", "A")
        path = next(self.root.iterdir())
        raw = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.queue.submit("verify_0001", "B")
        self.assertEqual(path.read_bytes(), raw)

    def test_invalid_inputs_do_not_create_records(self):
        for key, message in [
            ("../escape", "x"),
            ("verify_0001", "Bearer TEST_SENTINEL_NOT_REAL"),
            ("verify_0001", "x\x00"),
            ("verify_0001", "a" * 2001),
        ]:
            with self.subTest(key=key, message=message[:12]):
                with self.assertRaises(ValueError):
                    self.queue.submit(key, message)
        self.assertFalse(list(self.root.iterdir()))

    def test_symlink_record_rejected(self):
        outside = self.root / "outside"
        outside.write_text("{}")
        (self.root / "request_verify_0001.json").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.queue.submit("verify_0001", "x")

    def test_tampering_and_cross_binding_rejected(self):
        self.queue.submit("verify_0001", "A")
        with self.assertRaises(ValueError):
            GuidanceQueue(self.root, "another_session").status("verify_0001")
        path = next(self.root.iterdir())
        data = json.loads(path.read_text())
        data["message"] = "B"
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.queue.status("verify_0001")

    def test_record_cannot_be_renamed_to_another_request(self):
        self.queue.submit("verify_0001", "A")
        (self.root / "request_verify_0001.json").rename(self.root / "request_verify_0002.json")
        with self.assertRaises(ValueError):
            self.queue.status("verify_0002")
