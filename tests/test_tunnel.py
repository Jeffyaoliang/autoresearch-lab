"""Operator deployment tests: fake client and loopback responses only."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from research_hub.deployment import tunnel

KEY = "sk-TEST_SENTINEL_NOT_A_REAL_RUNTIME_KEY"


class FakeClient:
    def __init__(self, polls=None):
        self.pid = 43210
        self.returncode = None
        self.polls = list(polls or [])
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.polls:
            self.returncode = self.polls.pop(0)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class TunnelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.client = self.root / "fake-client"
        self.client.write_text("not a real executable")
        self.client.chmod(0o700)
        self.config = {
            "tunnel_client": str(self.client),
            "tunnel_id": "tunnel_example_test_id",
            "http_proxy": None,
        }
        (self.root / "tunnel.json").write_text(json.dumps(self.config))
        self.snapshot()
        self.env = patch.dict(os.environ, {"RESEARCH_HUB_STATE_DIR": str(self.root)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def snapshot(self, seconds_old=0):
        record = {
            "schema_version": 1,
            "collector": "acp_read_only_status",
            "snapshot": {
                "available": True,
                "source": "SCO_ACP_READ_ONLY",
                "coverage": "first_page_only_max_20",
                "task_scope": "verified_user_name_prefix_and_user_id",
                "observed_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=seconds_old)
                ).isoformat(),
                "tasks": [{"task_id": "task_123456abcdef", "state": "RUNNING"}],
            },
        }
        (self.root / "collector-state.json").write_text(json.dumps(record))

    def invoke(self, argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = tunnel.main(argv)
        return code, output.getvalue()

    def assert_no_secret_on_disk(self):
        for path in self.root.iterdir():
            if path.is_file():
                self.assertNotIn(KEY, path.read_text(), path.name)

    def test_import_has_no_process_network_or_key_side_effects(self):
        with (
            patch.object(subprocess, "Popen", side_effect=AssertionError("process")),
            patch.object(subprocess, "run", side_effect=AssertionError("process")),
            patch("getpass.getpass", side_effect=AssertionError("key")),
            patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network")),
        ):
            importlib.reload(tunnel)

    def test_default_and_explicit_preflight_are_read_only(self):
        original = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with (
            patch.object(tunnel, "_key", side_effect=AssertionError("key")),
            patch.object(tunnel.subprocess, "run", side_effect=AssertionError("process")),
            patch.object(tunnel.subprocess, "Popen", side_effect=AssertionError("process")),
            patch.object(tunnel, "local_read", side_effect=AssertionError("network")),
        ):
            for args in ([], ["--preflight"]):
                code, output = self.invoke(args)
                self.assertEqual(code, 0, output)
                self.assertFalse(json.loads(output)["cloud_call_verified"])
        self.assertEqual(original, {path.name: path.read_bytes() for path in self.root.iterdir()})

    def test_prepare_has_fixed_key_free_command_and_no_network(self):
        with (
            patch.object(tunnel, "_key", side_effect=AssertionError("key")),
            patch.object(tunnel.subprocess, "run", side_effect=AssertionError("process")),
            patch.object(tunnel.subprocess, "Popen", side_effect=AssertionError("process")),
            patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": KEY}),
        ):
            code, output = self.invoke(["--prepare"])
        self.assertEqual(code, 0, output)
        profile = (self.root / "tunnel-profile.yaml").read_text()
        self.assertIn("env:CONTROL_PLANE_API_KEY", profile)
        self.assertIn("https://api.openai.com", profile)
        self.assertIn('listen_addr: "127.0.0.1:0"', profile)
        self.assertIn("-m research_hub stdio", profile)
        self.assertNotIn("http_proxy:", profile)
        self.assertFalse((self.root / "tunnel-status.json").exists())
        self.assertNotIn(KEY, output)
        self.assert_no_secret_on_disk()

    def test_prepare_refuses_to_overwrite_another_profile(self):
        path = self.root / "tunnel-profile.yaml"
        path.write_text("operator reviewed different configuration")
        with patch.object(tunnel, "_key", side_effect=AssertionError("key")):
            code, output = self.invoke(["--connect"])
        self.assertEqual(code, 1)
        self.assertIn("existing_profile_differs_requires_review", output)
        self.assertEqual(path.read_text(), "operator reviewed different configuration")

    def test_prepare_writes_lf_and_is_idempotent(self):
        path = tunnel.prepare(self.config, self.root)
        original = path.read_bytes()
        self.assertEqual(original, tunnel.profile_text(self.config, self.root).encode("utf-8"))
        self.assertNotIn(b"\r\n", original)
        modified_at = path.stat().st_mtime_ns
        self.assertEqual(tunnel.prepare(self.config, self.root), path)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, modified_at)

    def test_existing_crlf_profile_is_accepted_without_rewriting(self):
        path = self.root / "tunnel-profile.yaml"
        original = tunnel.profile_text(self.config, self.root).replace("\n", "\r\n").encode("utf-8")
        path.write_bytes(original)
        modified_at = path.stat().st_mtime_ns
        tunnel.prepare(self.config, self.root)
        tunnel.prepare(self.config, self.root)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, modified_at)

    def test_changed_crlf_profile_is_rejected_without_rewriting(self):
        path = self.root / "tunnel-profile.yaml"
        changed = tunnel.profile_text(self.config, self.root).replace("level: warn", "level: debug")
        original = changed.replace("\n", "\r\n").encode("utf-8")
        path.write_bytes(original)
        modified_at = path.stat().st_mtime_ns
        with self.assertRaisesRegex(tunnel.TunnelError, "existing_profile_differs_requires_review"):
            tunnel.prepare(self.config, self.root)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, modified_at)

    def test_connect_with_existing_crlf_profile_reaches_doctor(self):
        path = self.root / "tunnel-profile.yaml"
        original = tunnel.profile_text(self.config, self.root).replace("\n", "\r\n").encode("utf-8")
        path.write_bytes(original)
        with (
            patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": KEY}),
            patch.object(
                tunnel.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)
            ) as doctor,
            patch.object(
                tunnel.subprocess, "Popen", side_effect=AssertionError("run must not start")
            ),
        ):
            code, output = self.invoke(["--connect"])
        self.assertEqual(code, 1)
        self.assertIn("doctor_failed", output)
        doctor.assert_called_once()
        self.assertEqual(doctor.call_args.args[0][1], "doctor")
        self.assertEqual(path.read_bytes(), original)

    def test_missing_installed_distribution_fails_before_key(self):
        with (
            patch.object(tunnel, "distribution", side_effect=tunnel.PackageNotFoundError()),
            patch.object(tunnel, "_key", side_effect=AssertionError("key")),
        ):
            code, output = self.invoke(["--connect"])
        self.assertEqual(code, 1)
        self.assertIn("package_not_installed", output)

    def test_stale_future_and_wrong_source_fail_before_key_or_process(self):
        with (
            patch.object(tunnel, "_key", side_effect=AssertionError("key")),
            patch.object(tunnel.subprocess, "run", side_effect=AssertionError("process")),
        ):
            for age in (181, -30):
                self.snapshot(age)
                code, output = self.invoke(["--connect"])
                self.assertEqual(code, 1)
                self.assertIn("collector_snapshot_not_fresh_or_verified", output)
        self.assertFalse((self.root / "tunnel-profile.yaml").exists())

    def test_configuration_never_accepts_literal_keys_or_proxy_credentials(self):
        variants = [
            {**self.config, "api_key": KEY},
            {**self.config, "http_proxy": "http://user:secret@example.invalid:8000"},
            {**self.config, "tunnel_client": "relative-client"},
            {**self.config, "tunnel_id": KEY},
        ]
        for variant in variants:
            with self.subTest(variant_keys=list(variant)):
                (self.root / "tunnel.json").write_text(json.dumps(variant))
                code, output = self.invoke([])
                self.assertEqual(code, 1)
                self.assertNotIn(KEY, output)
                self.assertNotIn("user:secret", output)

    def test_proxy_value_is_only_in_child_environment(self):
        config = {**self.config, "http_proxy": "http://127.0.0.1:9888"}
        text = tunnel.profile_text(config, self.root)
        flags = tunnel._flags(self.root / "tunnel-profile.yaml", config)
        env = tunnel._environment(KEY, config, self.root)
        self.assertIn("env:HTTPS_PROXY", text)
        self.assertNotIn(config["http_proxy"], text)
        self.assertNotIn(config["http_proxy"], str(flags))
        self.assertEqual(env["HTTPS_PROXY"], config["http_proxy"])
        self.assertEqual(env["CONTROL_PLANE_API_KEY"], KEY)

    def test_single_instance_lock_and_existing_process_block_start(self):
        with tunnel.instance_lock(self.root):
            with self.assertRaisesRegex(tunnel.TunnelError, "instance_locked"):
                with tunnel.instance_lock(self.root):
                    self.fail("second launcher acquired lock")
            code, output = self.invoke(["--prepare"])
            self.assertEqual(code, 1)
            self.assertIn("instance_locked", output)
        (self.root / "tunnel-status.json").write_text(json.dumps({"running": True, "pid": 12345}))
        with (
            patch.object(tunnel, "_pid_running", return_value=True),
            patch.object(tunnel, "_key", side_effect=AssertionError("key")),
        ):
            code, output = self.invoke(["--connect"])
        self.assertEqual(code, 1)
        self.assertIn("existing_instance_requires_review", output)

    def test_symlink_snapshot_profile_and_lock_fail_closed(self):
        for filename in ("collector-state.json", "tunnel-profile.yaml", "tunnel.lock"):
            with self.subTest(filename=filename):
                path = self.root / filename
                original = path.read_bytes() if path.exists() else None
                if path.exists():
                    path.unlink()
                outside = self.root / "fixture-outside"
                outside.write_text("{}")
                path.symlink_to(outside)
                code, _ = self.invoke(["--prepare"])
                self.assertEqual(code, 1)
                path.unlink()
                if original is not None:
                    path.write_bytes(original)
                outside.unlink()

    def test_non_loopback_and_redirect_health_never_followed(self):
        with patch.object(tunnel, "build_opener", side_effect=AssertionError("network")):
            for url in (
                "https://example.invalid/healthz",
                "http://127.0.0.1:1234/private",
                "http://user@127.0.0.1:1234/healthz",
                "http://127.0.0.1:1234/healthz?key=x",
            ):
                with self.assertRaises(tunnel.TunnelError):
                    tunnel.local_read(url)
        with self.assertRaisesRegex(tunnel.TunnelError, "health_redirect_rejected"):
            tunnel._NoRedirect().redirect_request(
                None, None, 302, None, None, "https://example.invalid"
            )

    def test_doctor_failure_discards_raw_diagnostics_and_does_not_run(self):
        captured = {}

        def doctor(args, **kwargs):
            captured.update(args=list(args), env=dict(kwargs["env"]))
            return subprocess.CompletedProcess(
                args, 2, (KEY + " arbitrary private stdout").encode(), b"private stderr"
            )

        with (
            patch.dict(
                os.environ, {"CONTROL_PLANE_API_KEY": KEY, "UNREVIEWED_SECRET": "not-forwarded"}
            ),
            patch.object(tunnel.subprocess, "run", side_effect=doctor),
            patch.object(
                tunnel.subprocess, "Popen", side_effect=AssertionError("run must not start")
            ),
        ):
            code, output = self.invoke(["--connect"])
        self.assertEqual(code, 1)
        self.assertIn("doctor_failed", output)
        self.assertNotIn(KEY, str(captured["args"]))
        self.assertEqual(captured["env"]["CONTROL_PLANE_API_KEY"], KEY)
        self.assertNotIn("UNREVIEWED_SECRET", captured["env"])
        self.assertNotIn("private", output)
        state = json.loads((self.root / "tunnel-status.json").read_text())
        self.assertEqual(state["doctor_exit_code"], 2)
        self.assertNotIn("diagnostics", state)
        self.assert_no_secret_on_disk()

    def test_doctor_timeout_never_outputs_exception_payload(self):
        error = subprocess.TimeoutExpired(
            ["client", KEY], 60, output=KEY.encode(), stderr=KEY.encode()
        )
        with (
            patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": KEY}),
            patch.object(tunnel.subprocess, "run", side_effect=error),
            patch.object(
                tunnel.subprocess, "Popen", side_effect=AssertionError("run must not start")
            ),
        ):
            code, output = self.invoke(["--connect"])
        self.assertEqual(code, 1)
        self.assertIn("doctor_timeout", output)
        self.assertNotIn(KEY, output)
        self.assert_no_secret_on_disk()

    def test_connect_only_reads_key_after_explicit_mode_and_stops_cleanly(self):
        with (
            patch.object(tunnel, "_key", return_value=KEY) as get_key,
            patch.object(
                tunnel.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, KEY.encode(), KEY.encode()),
            ),
            patch.object(tunnel, "_runtime_session", side_effect=KeyboardInterrupt),
        ):
            code, output = self.invoke(["--connect"])
        self.assertEqual(code, 130)
        get_key.assert_called_once()
        self.assertIn("operator_stopped", output)
        self.assertNotIn(KEY, output)
        self.assert_no_secret_on_disk()

    def test_retries_are_bounded_and_only_use_environment_key(self):
        environments = []

        def session(config, root, env, flags, attempt):
            environments.append((dict(env), list(flags), attempt))
            return 1

        with (
            patch.object(tunnel.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)),
            patch.object(tunnel, "_runtime_session", side_effect=session) as run,
            patch.object(tunnel.time, "sleep"),
        ):
            with self.assertRaisesRegex(tunnel.TunnelError, "client_retry_limit_reached"):
                tunnel.connect(self.config, self.root, KEY)
        self.assertEqual(run.call_count, tunnel.MAX_ATTEMPTS)
        self.assertEqual(environments[-1][2], tunnel.MAX_ATTEMPTS)
        for env, flags, _ in environments:
            self.assertEqual(env["CONTROL_PLANE_API_KEY"], KEY)
            self.assertNotIn(KEY, str(flags))
        self.assertNotIn("CONTROL_PLANE_API_KEY", run.call_args.args[2])
        self.assert_no_secret_on_disk()

    def test_health_ready_and_increasing_poll_metrics_are_not_cloud_verification(self):
        process = FakeClient([None, None, None, 0])
        polls = iter([1, 2, 3])

        def spawn(*args, **kwargs):
            self.assertNotIn(KEY, str(args))
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            (self.root / "tunnel-health.url").write_text("http://127.0.0.1:54321")
            return process

        def http(url):
            if url.endswith("/metrics"):
                count = next(polls)
                return (
                    200,
                    f'client_poll_commands_total{{private_label="not-for-export"}} {count}\n'.encode(),
                )
            return 200, b"ignored private health response"

        output = io.StringIO()
        with (
            patch.object(tunnel.subprocess, "Popen", side_effect=spawn),
            patch.object(tunnel, "local_read", side_effect=http),
            patch.object(tunnel.time, "sleep"),
            redirect_stdout(output),
        ):
            tunnel._runtime_session(self.config, self.root, {"CONTROL_PLANE_API_KEY": KEY}, [], 1)
        samples = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(
            any(sample["ready_verified"] and sample["polling_verified"] for sample in samples)
        )
        self.assertTrue(all(sample["cloud_call_verified"] is False for sample in samples))
        self.assertNotIn("private", output.getvalue())
        state = json.loads((self.root / "tunnel-status.json").read_text())
        self.assertFalse(state["running"])
        self.assertFalse(state["ready_verified"])
        self.assertNotIn("private", json.dumps(state))
        self.assertFalse(process.terminated)
        self.assert_no_secret_on_disk()

    def test_interrupt_cleans_only_the_client_spawned_by_this_session(self):
        process, unrelated = FakeClient(), FakeClient()
        with (
            patch.object(tunnel.subprocess, "Popen", return_value=process),
            patch.object(tunnel.time, "sleep", side_effect=KeyboardInterrupt),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                tunnel._runtime_session(self.config, self.root, {}, [], 1)
        self.assertTrue(process.terminated)
        self.assertFalse(unrelated.terminated)
        self.assertFalse(unrelated.killed)
        self.assertFalse(json.loads((self.root / "tunnel-status.json").read_text())["running"])

    def test_disappearing_health_pointer_clears_previous_readiness(self):
        process = FakeClient([None, None, 0])

        def spawn(*args, **kwargs):
            (self.root / "tunnel-health.url").write_text("http://127.0.0.1:54321")
            return process

        def sleep(_):
            (self.root / "tunnel-health.url").unlink(missing_ok=True)

        output = io.StringIO()
        with (
            patch.object(tunnel.subprocess, "Popen", side_effect=spawn),
            patch.object(tunnel, "local_read", return_value=(200, b"")),
            patch.object(tunnel.time, "sleep", side_effect=sleep),
            redirect_stdout(output),
        ):
            tunnel._runtime_session(self.config, self.root, {}, [], 1)
        samples = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(samples[0]["ready_verified"])
        self.assertFalse(samples[1]["ready_verified"])
        self.assertIsNone(samples[1]["health_http"])

    def test_unsafe_health_pointer_stops_own_client_before_network(self):
        process = FakeClient()

        def spawn(*args, **kwargs):
            (self.root / "tunnel-health.url").write_text("https://example.invalid/healthz")
            return process

        with (
            patch.object(tunnel.subprocess, "Popen", side_effect=spawn),
            patch.object(tunnel, "local_read", side_effect=AssertionError("network")),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(tunnel.TunnelError, "invalid_loopback_url"):
                tunnel._runtime_session(self.config, self.root, {}, [], 1)
        self.assertTrue(process.terminated)

    def test_hidden_input_refuses_echo_fallback(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys.stdin, "isatty", return_value=False),
            patch.object(tunnel.getpass, "getpass", side_effect=AssertionError("must not prompt")),
        ):
            with self.assertRaisesRegex(
                tunnel.TunnelError, "interactive_key_or_environment_required"
            ):
                tunnel._key()

    def test_owned_cleanup_escalates_only_after_wait_timeout(self):
        process = FakeClient()
        process.wait = Mock(side_effect=[subprocess.TimeoutExpired("fake", 10), -9])
        tunnel._stop_owned(process)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
