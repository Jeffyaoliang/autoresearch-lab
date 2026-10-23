import unittest, tempfile, hashlib, json, threading, os
from unittest.mock import patch, Mock
from pathlib import Path
from research_hub.executor.state import State, clean
from research_hub.executor.rpc import ExecutorRPC, ALLOWED
from research_hub.executor.policy import feature_config, MAX_GPU_HOURS, MAX_EPISODES
from research_hub.executor.policy import Settings, load_settings

SESSION_ID = "fixture-session"


def fixture_settings(root):
    root = Path(root)
    return Settings(
        SESSION_ID,
        root / "project",
        root / "app.sock",
        root / "peer.json",
        root / "codex",
        root / "auth",
        os.getuid(),
    )


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(self.tmp.name)
        self.settings = fixture_settings(self.tmp.name)

    def tearDown(self):
        self.state.db.close()
        self.tmp.cleanup()

    def test_persist_and_reopen(self):
        self.state.submit("persist_001", "text_acceptance")
        other = State(self.tmp.name)
        try:
            self.assertEqual(other.job("persist_001")["state"], "queued")
        finally:
            other.db.close()

    def test_idempotent(self):
        a = self.state.submit("idempotent_001", "text_acceptance")
        b = self.state.submit("idempotent_001", "text_acceptance")
        self.assertEqual(a, b)
        self.assertEqual(len(self.state.jobs()), 1)

    def test_task_conflict(self):
        self.state.submit("conflict_001", "text_acceptance")
        with self.assertRaises(ValueError):
            self.state.submit("conflict_001", "rax_click_mouse")

    def test_no_free_prompt(self):
        with self.assertRaises(ValueError):
            self.state.submit("scope_test_001", "run arbitrary command")

    def test_request_id_path_traversal(self):
        with self.assertRaises(ValueError):
            self.state.submit("../../credentials", "text_acceptance")

    def test_concurrent_dedup(self):
        ts = [
            threading.Thread(target=self.state.submit, args=("concurrent_001", "text_acceptance"))
            for _ in range(8)
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(self.state.jobs()), 1)

    def test_audit_chain(self):
        self.state.submit("chain_test_001", "text_acceptance")
        self.state.audit("chain_test_001", "turn/started", {"turn_id": "synthetic-test-only"})
        prev = "0" * 64
        for at, job, method, body, old, digest in self.state.db.execute(
            "select at,job,method,body,previous,digest from events order by seq"
        ):
            self.assertEqual(old, prev)
            self.assertEqual(
                digest, hashlib.sha256((prev + at + str(job) + method + body).encode()).hexdigest()
            )
            prev = digest

    def test_secret_redaction(self):
        v = clean(
            {
                "authorization": "secret_canary",
                "text": "Bearer synthetic-test-token",
                "developer_instructions": "private-canary",
            }
        )
        s = json.dumps(v)
        self.assertNotIn("synthetic-test-token", s)
        self.assertNotIn("private-canary", s)

    def test_event_reasoning_not_exported(self):
        self.assertNotIn("reasoning", clean({"reasoning": "private", "message": "ok"}))

    def test_safe_text_retry_preserves_job_id(self):
        self.state.submit("retry_test_001", "text_acceptance")
        self.state.update(
            "retry_test_001",
            state="failed",
            attempt=1,
            turn_id="synthetic-turn",
            result={
                "tool_items_observed": False,
                "final_thread_status": {"type": "idle"},
                "turn": {"status": "interrupted"},
            },
        )
        j = self.state.retry_text("retry_test_001")
        self.assertEqual(j["state"], "queued")
        self.assertEqual(j["id"], "retry_test_001")
        self.assertEqual(len(self.state.jobs()), 1)

    def test_uncertain_retry_denied(self):
        self.state.submit("retry_unknown_001", "text_acceptance")
        self.state.update(
            "retry_unknown_001",
            state="failed",
            attempt=1,
            result={
                "tool_items_observed": True,
                "final_thread_status": {"type": "idle"},
                "turn": {"status": "interrupted"},
            },
        )
        with self.assertRaisesRegex(ValueError, "uncertain_effects"):
            self.state.retry_text("retry_unknown_001")

    def test_retry_ceiling(self):
        from research_hub.executor.policy import MAX_RETRIES

        self.state.submit("retry_limit_001", "text_acceptance")
        self.state.update("retry_limit_001", state="failed", attempt=MAX_RETRIES + 1)
        with self.assertRaisesRegex(ValueError, "retry_not_allowed"):
            self.state.retry_text("retry_limit_001")

    def test_bound_rpc_no_network_for_wrong_session(self):
        x = object.__new__(ExecutorRPC)
        x.binding = {"session_id": SESSION_ID}
        with self.assertRaisesRegex(ValueError, "thread_binding"):
            x.call("turn/start", {"threadId": "unrelated-session"})

    def test_arbitrary_shell_rpc_denied(self):
        x = object.__new__(ExecutorRPC)
        x.binding = {"session_id": SESSION_ID}
        for method in [
            "command/exec",
            "process/spawn",
            "config/value/write",
            "thread/shellCommand",
        ]:
            with self.assertRaises(ValueError):
                x.call(method, {"threadId": SESSION_ID})

    def test_text_shell_config_disabled(self):
        self.assertIs(feature_config(self.settings, True)["features.shell_tool"], False)
        self.assertIs(feature_config(self.settings, True)["features.unified_exec"], False)

    def test_gpu_budget_limits(self):
        self.assertEqual(MAX_GPU_HOURS, 4)
        self.assertEqual(MAX_EPISODES, 1)

    def test_no_implicit_research_enable(self):
        import research_hub.executor.core as mod

        self.state.submit("accepted_text_001", "text_acceptance")
        self.state.update(
            "accepted_text_001", state="completed", result={"acceptance_passed": True}
        )
        self.state.submit("research_job_001", "rax_click_mouse")
        a = mod.Actor(self.state, self.settings)
        a.connect = Mock()
        a.thread = lambda: {"status": {"type": "idle"}}
        with (
            patch.object(mod, "holders", return_value=[123]),
            patch.object(mod, "expected_peer", return_value=123),
            patch.object(mod, "auth_mode", return_value=True),
        ):
            with self.assertRaisesRegex(ValueError, "credential_isolation_not_verified"):
                a.run(self.state.job("research_job_001"))

    def test_idle_connection_reused(self):
        from research_hub.executor.core import Actor

        a = Actor(self.state, self.settings)
        existing = Mock()
        a.rpc = existing
        a.connect = Mock(side_effect=AssertionError("must reuse idle subscription"))
        self.assertIs(a.ensure_connection(), existing)
        self.assertIs(a.ensure_connection(), existing)
        a.connect.assert_not_called()

    def test_idle_connection_created_once(self):
        from research_hub.executor.core import Actor

        a = Actor(self.state, self.settings)
        connection = Mock(peer_pid=123)

        def connect():
            a.rpc = connection
            return connection

        a.connect = Mock(side_effect=connect)
        self.assertIs(a.ensure_connection(), connection)
        self.assertIs(a.ensure_connection(), connection)
        self.assertEqual(a.connect.call_count, 1)

    def test_queued_stop_does_not_connect(self):
        from research_hub.executor.core import Actor

        self.state.submit("stopped_job_001", "text_acceptance")
        self.state.update("stopped_job_001", stop=1)
        a = Actor(self.state, self.settings)
        a.connect = lambda: self.fail("stopped job must not open RPC")
        a.run(self.state.job("stopped_job_001"))
        self.assertEqual(self.state.job("stopped_job_001")["state"], "cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
