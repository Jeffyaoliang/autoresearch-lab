"""Explicit supervisor for the local read-only Bridge, never research processes.

Importing this module opens no files, installs no signal handlers and starts no
processes. Only ``main`` or an explicit ``Supervisor.run`` starts a Bridge.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

from research_hub.config import state_dir


class AlreadyRunningError(RuntimeError):
    """Another supervisor owns the same local state directory."""


class SingleInstanceLock:
    """Nonblocking OS lock; a stale PID file never authorizes killing a process."""

    def __init__(self, path, *, platform=None):
        self.path = Path(path)
        self.platform = os.name if platform is None else platform
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            raise RuntimeError("lock_already_acquired")
        if self.path.is_symlink():
            raise ValueError("unsafe_supervisor_lock")
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        handle = os.fdopen(descriptor, "r+b")
        try:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("unsafe_supervisor_lock")
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if self.platform == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunningError("supervisor_already_running") from exc
        except BaseException:
            handle.close()
            raise
        self.handle = handle
        return self

    def release(self):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if self.platform == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        # Retain the inode: unlinking a lock file could let two owners coexist.

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def require_runtime(root):
    """Require operator-provided state and HTTP identity, without changing SCO."""
    if not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise ValueError("existing_supervisor_state_directory_required")
    config = root / "bridge.json"
    token = root / "http-token.txt"
    for path in (config, token):
        if path.is_symlink() or not path.is_file():
            raise ValueError("existing_bridge_configuration_and_token_required")
    if config.stat().st_size > 262144 or token.stat().st_size < 32:
        raise ValueError("invalid_bridge_configuration_or_token")
    try:
        with config.open("rb") as source:
            data = json.loads(source.read(262145))
        if not isinstance(data, dict):
            raise ValueError("configuration_must_be_object")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("invalid_bridge_configuration") from exc
    # Token contents remain unread here; only the Bridge consumes its own token.


def make_logger(root):
    logger = logging.Logger("research-hub-supervisor", level=logging.INFO)
    logger.propagate = False
    path = root / "supervisor.log"
    if path.is_symlink():
        raise ValueError("unsafe_supervisor_log")
    handler = RotatingFileHandler(path, maxBytes=65536, backupCount=2, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    logger.addHandler(handler)
    return logger


class Supervisor:
    """Own only Popen objects created here; dependencies are injectable offline."""

    def __init__(
        self,
        root,
        *,
        popen=None,
        monotonic=None,
        wall_time=None,
        stop_event=None,
        logger=None,
        executable=None,
    ):
        self.root = Path(root).absolute()
        self.popen = subprocess.Popen if popen is None else popen
        self.monotonic = time.monotonic if monotonic is None else monotonic
        self.wall_time = time.time if wall_time is None else wall_time
        self.stop_event = threading.Event() if stop_event is None else stop_event
        self.logger = logger
        self.executable = sys.executable if executable is None else executable
        self.child = None
        self.last_child_pid = None
        self.started_epoch = None

    def request_stop(self):
        """Request an orderly stop; callers must not provide a PID to terminate."""
        self.stop_event.set()

    def write_state(self, running, *, supervisor_running, exit_code=None):
        path = self.root / "supervisor-runtime.json"
        if path.is_symlink():
            raise ValueError("unsafe_supervisor_state")
        data = {
            "supervisor_pid": os.getpid(),
            "bridge_pid": self.last_child_pid,
            "running": bool(running),
            "supervisor_running": bool(supervisor_running),
            "started_epoch": self.started_epoch,
            "updated_epoch": self.wall_time(),
            "bridge_exit_code": exit_code,
            "managed_scope": "own_local_bridge_child_only",
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=".supervisor-", suffix=".tmp", dir=self.root
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def stop_child(self):
        """No PID lookup, state-file process attachment, taskkill or process tree kill."""
        child = self.child
        if child is None:
            return
        try:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        finally:
            self.child = None

    def run(self):
        require_runtime(self.root)
        with SingleInstanceLock(self.root / "supervisor.lock"):
            owned_logger = self.logger is None
            logger = make_logger(self.root) if owned_logger else self.logger
            delay = 1
            exit_code = None
            try:
                while not self.stop_event.is_set():
                    started = self.monotonic()
                    try:
                        env = dict(os.environ)
                        env["RESEARCH_HUB_STATE_DIR"] = str(self.root)
                        self.child = self.popen(
                            [self.executable, "-m", "research_hub", "http"],
                            cwd=str(self.root),
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        self.last_child_pid = self.child.pid
                        self.started_epoch = self.wall_time()
                        self.write_state(True, supervisor_running=True)
                        logger.info("bridge started pid=%s", self.child.pid)
                        exit_code = self.child.wait()
                        self.child = None
                        logger.warning("bridge exited code=%s; local restart scheduled", exit_code)
                    except OSError as exc:
                        # Exception text and the child's raw output are never logged.
                        logger.warning(
                            "bridge launch unavailable error_class=%s", type(exc).__name__
                        )
                        self.stop_child()
                        exit_code = None
                    self.write_state(False, supervisor_running=True, exit_code=exit_code)
                    if self.stop_event.is_set():
                        break
                    if self.monotonic() - started >= 60:
                        delay = 1
                    if self.stop_event.wait(delay):
                        break
                    delay = min(delay * 2, 30)
            finally:
                try:
                    self.stop_child()
                finally:
                    try:
                        self.write_state(False, supervisor_running=False, exit_code=exit_code)
                    finally:
                        if owned_logger:
                            for handler in list(logger.handlers):
                                handler.close()
                                logger.removeHandler(handler)


def main():
    """Explicit process entry; no platform collection is enabled by installation."""
    supervisor = Supervisor(state_dir())
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGTERM)

        def terminate(_signum, _frame):
            supervisor.request_stop()
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
    try:
        supervisor.run()
    except AlreadyRunningError:
        return 0
    except KeyboardInterrupt:
        supervisor.request_stop()
        return 0
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
