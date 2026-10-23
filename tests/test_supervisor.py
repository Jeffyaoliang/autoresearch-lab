"""Supervisor tests never start the collector, a platform client, or a model."""

import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from research_hub.deployment import supervisor as module


class Clock:
    def __init__(self):
        self.value = 0

    def monotonic(self):
        return self.value

    def wall_time(self):
        return 1000 + self.value

    def advance(self, seconds):
        self.value += seconds


class StopEvent:
    def __init__(self, clock):
        self.clock = clock
        self.stopped = False
        self.delays = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.delays.append(seconds)
        self.clock.advance(seconds)
        return self.stopped


class Process:
    def __init__(
        self, clock, *, pid=321, duration=0, on_wait=None, interrupt=False, terminate_timeout=False
    ):
        self.clock = clock
        self.pid = pid
        self.duration = duration
        self.on_wait = on_wait
        self.interrupt = interrupt
        self.terminate_timeout = terminate_timeout
        self.returncode = None
        self.terminated = 0
        self.killed = 0
        self.wait_timeouts = []

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if timeout is not None:
            if self.terminate_timeout and not self.killed:
                raise subprocess.TimeoutExpired("fixture-bridge", timeout)
            self.returncode = -9 if self.killed else -15
            return self.returncode
        if self.on_wait:
            self.on_wait()
        if self.interrupt:
            raise KeyboardInterrupt
        self.clock.advance(self.duration)
        self.returncode = 1
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        # A synthetic local config and identity placeholder, never a real secret.
        (self.root / "bridge.json").write_text('{"sco_enabled":false}')
        (self.root / "http-token.txt").write_text("fixture-placeholder-" * 3)
        self.clock = Clock()
        self.event = StopEvent(self.clock)
        self.logger = Mock()

    def supervisor(self, popen, **kwargs):
        return module.Supervisor(
            self.root,
            popen=popen,
            monotonic=self.clock.monotonic,
            wall_time=self.clock.wall_time,
            stop_event=self.event,
            logger=kwargs.pop("logger", self.logger),
            **kwargs,
        )

    def state(self):
        return json.loads((self.root / "supervisor-runtime.json").read_text())

    def test_native_single_instance_lock_and_release(self):
        path = self.root / "supervisor.lock"
        with module.SingleInstanceLock(path):
            with self.assertRaises(module.AlreadyRunningError):
                module.SingleInstanceLock(path).acquire()
        with module.SingleInstanceLock(path):
            self.assertTrue(path.is_file())
        self.assertTrue(path.is_file())

    def test_windows_lock_uses_one_byte_and_lazy_platform_import(self):
        locking = Mock()
        fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        with patch.dict(sys.modules, {"msvcrt": fake}):
            with module.SingleInstanceLock(self.root / "windows.lock", platform="nt"):
                self.assertEqual(locking.call_args.args[1:], (2, 1))
            self.assertEqual(locking.call_args.args[1:], (0, 1))
        self.assertEqual(locking.call_count, 2)

    def test_duplicate_supervisor_never_starts_a_child(self):
        popen = Mock()
        with module.SingleInstanceLock(self.root / "supervisor.lock"):
            with self.assertRaises(module.AlreadyRunningError):
                self.supervisor(popen).run()
        popen.assert_not_called()

    def test_missing_runtime_configuration_never_starts_a_child(self):
        (self.root / "http-token.txt").unlink()
        popen = Mock()
        with self.assertRaisesRegex(ValueError, "configuration_and_token_required"):
            self.supervisor(popen).run()
        popen.assert_not_called()
        self.assertFalse((self.root / "supervisor.lock").exists())
        self.assertEqual(
            json.loads((self.root / "bridge.json").read_text()), {"sco_enabled": False}
        )

    def test_command_environment_stdio_and_state_scope(self):
        def during_wait():
            record = self.state()
            self.assertTrue(record["running"])
            self.assertTrue(record["supervisor_running"])
            self.assertEqual(record["bridge_pid"], 321)
            self.event.set()

        child = Process(self.clock, on_wait=during_wait)
        popen = Mock(return_value=child)
        self.supervisor(popen).run()
        args, options = popen.call_args
        self.assertEqual(args, ([sys.executable, "-m", "research_hub", "http"],))
        self.assertEqual(options["cwd"], str(self.root))
        self.assertEqual(options["env"]["RESEARCH_HUB_STATE_DIR"], str(self.root))
        for field in ("stdin", "stdout", "stderr"):
            self.assertEqual(options[field], subprocess.DEVNULL)
        final = self.state()
        self.assertFalse(final["running"])
        self.assertFalse(final["supervisor_running"])
        self.assertEqual(final["managed_scope"], "own_local_bridge_child_only")
        self.assertEqual(child.terminated, 0)
        self.assertEqual(child.killed, 0)

    def run_durations(self, durations):
        children = []

        def popen(*_args, **_kwargs):
            index = len(children)
            child = Process(
                self.clock,
                pid=400 + index,
                duration=durations[index],
                on_wait=self.event.set if index == len(durations) - 1 else None,
            )
            children.append(child)
            return child

        self.supervisor(popen).run()
        return children

    def test_restart_backoff_is_bounded(self):
        children = self.run_durations([0] * 8)
        self.assertEqual(len(children), 8)
        self.assertEqual(self.event.delays, [1, 2, 4, 8, 16, 30, 30])
        self.assertTrue(all(child.terminated == 0 for child in children))

    def test_stable_run_resets_restart_delay(self):
        self.run_durations([0, 0, 60, 0])
        self.assertEqual(self.event.delays, [1, 2, 1])

    def test_interrupt_cleans_only_created_child_and_marks_stopped(self):
        # Stale state is never a process lookup or kill authorization.
        (self.root / "supervisor-runtime.json").write_text('{"bridge_pid":999999,"running":true}')
        child = Process(self.clock, interrupt=True)
        with patch.object(os, "kill", side_effect=AssertionError("must not signal a PID")):
            with self.assertRaises(KeyboardInterrupt):
                self.supervisor(Mock(return_value=child)).run()
        self.assertEqual(child.terminated, 1)
        self.assertEqual(child.killed, 0)
        self.assertEqual(child.wait_timeouts, [None, 10])
        self.assertEqual(self.state()["bridge_pid"], child.pid)
        self.assertFalse(self.state()["running"])
        # The lock was released even though the wait was interrupted.
        with module.SingleInstanceLock(self.root / "supervisor.lock"):
            pass

    def test_unresponsive_child_is_killed_only_after_its_own_timeout(self):
        child = Process(self.clock, interrupt=True, terminate_timeout=True)
        with self.assertRaises(KeyboardInterrupt):
            self.supervisor(Mock(return_value=child)).run()
        self.assertEqual(child.terminated, 1)
        self.assertEqual(child.killed, 1)
        self.assertEqual(child.wait_timeouts, [None, 10, 5])
        self.assertFalse(self.state()["running"])

    def test_launch_failure_logs_class_without_private_error_or_output(self):
        child = Process(self.clock, on_wait=self.event.set)
        popen = Mock(side_effect=[OSError("PRIVATE_SENTINEL"), child])
        self.supervisor(popen, logger=None).run()
        text = (self.root / "supervisor.log").read_text()
        self.assertNotIn("PRIVATE_SENTINEL", text)
        self.assertIn("error_class=OSError", text)
        self.assertEqual(self.event.delays, [1])
        self.assertLess((self.root / "supervisor.log").stat().st_size, 65536)
        self.assertFalse(self.state()["supervisor_running"])

    def test_log_rotation_is_bounded(self):
        logger = module.make_logger(self.root)
        try:
            for _ in range(600):
                logger.info("fixture record %s", "x" * 500)
        finally:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
        files = list(self.root.glob("supervisor.log*"))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size <= 65536 for path in files))

    def test_import_does_not_touch_files_or_launch_processes(self):
        with (
            patch("os.open", side_effect=AssertionError("import opened a file")),
            patch("subprocess.Popen", side_effect=AssertionError("import started a process")),
            patch.object(Path, "mkdir", side_effect=AssertionError("import created a directory")),
        ):
            importlib.reload(module)

    def test_main_sigterm_cleans_via_run_and_restores_handler(self):
        instance = Mock()
        installed = {}

        def install(number, handler):
            installed[number] = handler

        def run():
            installed[signal.SIGTERM](signal.SIGTERM, None)

        instance.run.side_effect = run
        prior = object()
        with (
            patch.object(module, "Supervisor", return_value=instance),
            patch.object(module, "state_dir", return_value=self.root),
            patch.object(module.signal, "getsignal", return_value=prior),
            patch.object(module.signal, "signal", side_effect=install) as signal_call,
        ):
            self.assertEqual(module.main(), 0)
        instance.request_stop.assert_called()
        self.assertEqual(signal_call.call_args.args, (signal.SIGTERM, prior))


if __name__ == "__main__":
    unittest.main()
