"""Synthetic offline tests; fixtures are never live evidence."""

import unittest, tempfile, json, ast, concurrent.futures, time
from pathlib import Path
from unittest.mock import Mock
from research_hub.acceptance.policy import PROMPT, EXPECTED_REPLY, spec, execution_gate, READ_RPC
from research_hub.acceptance.channel import prepare, trigger, status
from research_hub.acceptance.store import Store, OnceClaim
from research_hub.acceptance.events import EventCapture, collect_notifications
from research_hub.acceptance.wire import ObservationClient

SESSION_ID = "fixture-session"


class Boundaries(unittest.TestCase):
    def test_fixed_scope(self):
        self.assertEqual(SESSION_ID, "fixture-session")
        self.assertEqual(PROMPT, "只回复：Research Hub 连接测试成功。不要调用工具或执行命令。")
        self.assertEqual(spec(SESSION_ID)["execution_limit"], 1)

    def test_disabled(self):
        self.assertFalse(execution_gate()["enabled"])

    def test_write_rpc_denied_before_send(self):
        c = object.__new__(ObservationClient)
        c._send = Mock()
        c.binding = {"session_id": SESSION_ID}
        for m in [
            "turn/start",
            "thread/resume",
            "thread/start",
            "thread/inject_items",
            "turn/steer",
            "command/exec",
            "thread/shellCommand",
            "process/spawn",
            "config/value/write",
        ]:
            with self.assertRaises(ValueError):
                c.call(m, {"threadId": SESSION_ID})
        c._send.assert_not_called()

    def test_wrong_session(self):
        c = object.__new__(ObservationClient)
        c._send = Mock()
        c.binding = {"session_id": SESSION_ID}
        with self.assertRaises(ValueError):
            c.call("thread/read", {"threadId": "other"})
        c._send.assert_not_called()

    def test_only_read_rpc(self):
        self.assertEqual(READ_RPC, {"initialize", "thread/read", "thread/loaded/list"})

    def test_original_policy_not_imported(self):
        tree = ast.parse(
            (Path(__file__).parents[1] / "src/research_hub/acceptance/wire.py").read_text()
        )
        self.assertNotIn(
            "guidance_rpc", [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        )


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.s = Store(self.root, SESSION_ID)

    def tearDown(self):
        self.tmp.cleanup()

    def observer(self):
        return {
            "thread_id": SESSION_ID,
            "active_turn_absent_at_snapshot": True,
            "fixture_only": True,
        }

    def test_prepare_idempotence(self):
        prepare(self.s, self.observer)
        old = (self.root / "request.json").read_bytes()
        prepare(self.s, self.observer)
        self.assertEqual(old, (self.root / "request.json").read_bytes())
        self.assertEqual(status(self.s)["audit_records"], 4)

    def test_preflight_failure_audited(self):
        def failing():
            raise OSError("PRIVATE_SENTINEL")

        with self.assertRaises(OSError):
            prepare(self.s, failing)
        rows = self.s.read_audit()
        self.assertEqual(rows[-1]["event"], "preflight_failed")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(rows))

    def test_changed_request(self):
        self.s.prepare()
        p = self.root / "request.json"
        d = json.loads(p.read_text())
        d["prompt"] = "changed"
        p.write_text(json.dumps(d))
        with self.assertRaises(ValueError):
            status(self.s)

    def test_trigger_denial_idempotence(self):
        self.assertFalse(trigger(self.s)["turn_start_sent"])
        self.assertTrue(trigger(self.s)["duplicate_denial"])
        self.assertEqual(len(self.s.read_audit()), 1)
        self.assertFalse((self.root / "execution.claim").exists())

    def test_forged_approval_cannot_enable(self):
        (self.root / "authorization.json").write_text('{"enabled":true}')
        self.assertEqual(trigger(self.s)["decision"], "DISABLED_FAIL_CLOSED")

    def test_single_claim_under_race(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            r = list(ex.map(lambda _: OnceClaim(self.root, SESSION_ID).reserve(), range(16)))
        self.assertEqual(sum(r), 1)

    def test_uncertain_claim_not_retried(self):
        self.assertTrue(OnceClaim(self.root, SESSION_ID).reserve())
        self.assertFalse(OnceClaim(self.root, SESSION_ID).reserve())

    def test_audit_tamper(self):
        self.s.audit("prepared", {})
        p = self.root / "audit.jsonl"
        p.write_text(p.read_text().replace("prepared", "changed"))
        with self.assertRaises(ValueError):
            self.s.read_audit()

    def test_symlink(self):
        target = self.root / "target"
        target.write_text("{}")
        (self.root / "request.json").symlink_to(target)
        with self.assertRaises(OSError):
            self.s.prepare()

    def test_path_injection(self):
        with self.assertRaises(ValueError):
            self.s.read("../../private")

    def test_no_fake_live_turn(self):
        self.assertIsNone(status(self.s)["turn_id"])
        self.assertFalse(status(self.s)["model_turn_started"])


class Events(unittest.TestCase):
    def setUp(self):
        self.c = EventCapture("fixture-turn", SESSION_ID)

    def e(self, m, **p):
        return {"method": m, "params": {"threadId": SESSION_ID, "turnId": "fixture-turn", **p}}

    def start(self):
        self.c.feed(self.e("turn/started", turn={"id": "fixture-turn", "status": "inProgress"}))

    def end(self, **extra):
        return self.c.feed(
            self.e("turn/completed", turn={"id": "fixture-turn", "status": "completed", **extra})
        )

    def test_success(self):
        self.start()
        self.c.feed(self.e("item/agentMessage/delta", delta=EXPECTED_REPLY))
        self.c.feed(self.e("item/completed", item={"type": "agentMessage", "text": EXPECTED_REPLY}))
        r = self.end()
        self.assertEqual(r["model_reply"], EXPECTED_REPLY)
        self.assertTrue(r["terminal_event_seen"])
        self.assertIsNotNone(r["started_observed_at"])
        self.assertIsNotNone(r["ended_observed_at"])

    def test_wrong_thread(self):
        e = self.e("turn/started")
        e["params"]["threadId"] = "other"
        with self.assertRaises(ValueError):
            self.c.feed(e)

    def test_wrong_turn(self):
        e = self.e("turn/started")
        e["params"]["turnId"] = "other"
        with self.assertRaises(ValueError):
            self.c.feed(e)

    def test_unscoped(self):
        with self.assertRaises(ValueError):
            self.c.feed({"method": "turn/started", "params": {}})

    def test_delta_before_start(self):
        with self.assertRaises(ValueError):
            self.c.feed(self.e("item/agentMessage/delta", delta="x"))

    def test_duplicate_start(self):
        self.start()
        with self.assertRaises(ValueError):
            self.start()

    def test_error_redaction(self):
        self.start()
        r = self.end(status="failed", error={"message": "PRIVATE_SENTINEL"})
        self.assertEqual(r["state"], "failed")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(self.c.summary()))

    def test_tool_detection_is_not_prevention(self):
        self.start()
        r = self.c.feed(
            self.e("item/started", item={"type": "commandExecution", "command": "PRIVATE_COMMAND"})
        )
        self.assertTrue(r["forbidden_item_observed"])
        self.assertFalse(self.c.summary()["tool_prevention_proven_by_events"])
        self.assertNotIn("PRIVATE_COMMAND", json.dumps(self.c.summary()))

    def test_unexpected_reply_redacted(self):
        self.start()
        self.c.feed(self.e("item/agentMessage/delta", delta="PRIVATE_SENTINEL"))
        r = self.end()
        self.assertIsNone(r["model_reply"])
        self.assertTrue(r["reply_redacted"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(self.c.summary()))

    def test_reply_bound(self):
        self.start()
        with self.assertRaises(ValueError):
            self.c.feed(self.e("item/agentMessage/delta", delta="x" * 8193))

    def test_observation_timeout(self):
        r = collect_notifications(Mock(), "fixture-turn", time.monotonic() - 1, SESSION_ID)
        self.assertFalse(r["terminal_event_seen"])
        self.assertTrue(r["error"]["execution_status_unknown"])

    def test_transport_timeout_retained(self):
        c = Mock()
        c.receive_notification.side_effect = TimeoutError()
        r = collect_notifications(c, "fixture-turn", time.monotonic() + 1, SESSION_ID)
        self.assertEqual(r["error"]["kind"], "event_transport_error")
        self.assertFalse(r["terminal_event_seen"])

    def test_transport_eof_retained(self):
        c = Mock()
        c.receive_notification.side_effect = EOFError()
        r = collect_notifications(c, "fixture-turn", time.monotonic() + 1, SESSION_ID)
        self.assertEqual(r["error"]["error_class"], "EOFError")

    def test_bad_event_stream_retained(self):
        c = Mock()
        c.receive_notification.return_value = {"method": "turn/started", "params": {}}
        r = collect_notifications(c, "fixture-turn", time.monotonic() + 1, SESSION_ID)
        self.assertEqual(r["error"]["kind"], "invalid_event_stream")

    def test_delta_final_mismatch(self):
        self.start()
        self.c.feed(self.e("item/agentMessage/delta", delta="a"))
        with self.assertRaises(ValueError):
            self.c.feed(self.e("item/completed", item={"type": "agentMessage", "text": "b"}))


if __name__ == "__main__":
    unittest.main()
