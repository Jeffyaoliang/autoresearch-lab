import json
from pathlib import Path
import tempfile
import unittest

from research_hub.guidance.delivery import Delivery
from research_hub.guidance.endpoint import request
from research_hub.guidance.queue import GuidanceQueue


class RPC:
    def __init__(self, fail=False, result=None):
        self.calls = []
        self.fail = fail
        self.result = {} if result is None else result

    def call(self, method, params):
        self.calls.append((method, params))
        if self.fail:
            raise TimeoutError()
        return self.result


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for name in ["inbox", "claims", "receipts"]:
            (self.root / name).mkdir()
        self.bound = {
            "session_alias": "example_session",
            "session_id": "example-thread",
            "cwd": "/example/project",
            "codex_home": "/example/codex",
        }
        self.rpc = RPC()
        self.delivery = Delivery(self.root, self.rpc, self.bound)
        self.queue = GuidanceQueue(self.root / "inbox", self.bound["session_alias"])
        self.key = "test_msg_0001"
        self.queue.submit(self.key, "Discuss only. Do not execute.")

    def test_delivered_once_even_after_worker_restart(self):
        original = self.queue._path(self.key).read_bytes()
        self.assertTrue(self.delivery.deliver(self.key)["codex_received"])
        restarted = Delivery(self.root, self.rpc, self.bound)
        result = restarted.deliver(self.key)
        self.assertTrue(result["codex_received"])
        self.assertEqual(len(self.rpc.calls), 1)
        self.assertEqual(self.rpc.calls[0][0], "thread/inject_items")
        self.assertEqual(self.rpc.calls[0][1]["threadId"], "example-thread")
        self.assertFalse(result["model_processing_started"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(original, self.queue._path(self.key).read_bytes())

    def test_timeout_leaves_durable_claim_and_is_not_retried(self):
        self.rpc.fail = True
        with self.assertRaises(TimeoutError):
            self.delivery.deliver(self.key)
        restarted = Delivery(self.root, self.rpc, self.bound)
        result = restarted.deliver(self.key)
        self.assertEqual(result["state"], "delivery_pending_or_uncertain_do_not_resubmit")
        self.assertFalse(result["codex_received"])
        self.assertEqual(len(self.rpc.calls), 1)

    def test_ambiguous_ack_is_not_resent(self):
        self.rpc.result = None
        with self.assertRaisesRegex(ValueError, "ambiguous_delivery_ack"):
            self.delivery.deliver(self.key)
        self.assertFalse(self.delivery.deliver(self.key)["codex_received"])
        self.assertEqual(len(self.rpc.calls), 1)

    def test_alias_cannot_be_reassigned_with_existing_mailbox(self):
        with self.assertRaisesRegex(ValueError, "persisted_binding_mismatch"):
            Delivery(self.root, self.rpc, {**self.bound, "session_id": "another-thread"})
        self.assertEqual(self.rpc.calls, [])

    def test_symlink_receipt_rejected(self):
        (self.root / "receipts" / (self.key + ".json")).symlink_to(self.queue._path(self.key))
        with self.assertRaises(ValueError):
            self.delivery.status(self.key)

    def test_receipt_tampering_rejected(self):
        self.delivery.deliver(self.key)
        path = self.root / "receipts" / (self.key + ".json")
        valid = json.loads(path.read_text())
        for field, value in [
            ("message_sha256", "bad"),
            ("execution_authorized", True),
            ("binding", "other"),
            ("codex_received", False),
        ]:
            with self.subTest(field=field):
                path.write_text(json.dumps({**valid, field: value}))
                with self.assertRaises(ValueError):
                    self.delivery.status(self.key)

    def test_endpoint_submit_never_invokes_rpc(self):
        result = request(
            {"operation": "submit", "request_id": "endpoint0001", "message": "Review only."},
            self.root,
            self.bound,
        )
        self.assertEqual(result["state"], "queued_for_bound_codex")
        self.assertFalse(result["model_processing_started"])
        self.assertEqual(self.rpc.calls, [])
        duplicate = request(
            {"operation": "submit", "request_id": "endpoint0001", "message": "Review only."},
            self.root,
            self.bound,
        )
        self.assertTrue(duplicate["duplicate"])

    def test_endpoint_does_not_accept_execution_fields(self):
        with self.assertRaises(ValueError):
            request(
                {
                    "operation": "submit",
                    "request_id": "endpoint0001",
                    "message": "Review only.",
                    "start_turn": True,
                },
                self.root,
                self.bound,
            )
