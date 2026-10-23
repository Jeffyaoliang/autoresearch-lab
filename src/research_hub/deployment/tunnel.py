"""Operator-only launcher for an official Tunnel forwarding to the stdio adapter.

Import and preflight never contact the control plane or read a runtime key.
The launcher does not install a client, collect research data or start model turns.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
import importlib.util
from importlib.metadata import PackageNotFoundError, distribution
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import warnings

from research_hub.config import read_config, state_dir

MAX_ATTEMPTS = 6
RETRY_SECONDS = 5
MAX_LOCAL_BYTES = 65536
TOOLS = (
    "list_research_tasks",
    "get_research_progress",
    "submit_research_guidance",
    "get_research_guidance_status",
)
ENV_ALLOW = {
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "TEMP",
    "TMP",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "USERDOMAIN",
    "USERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "LANG",
    "XDG_STATE_HOME",
    "RESEARCH_HUB_STATE_DIR",
    "RESEARCH_HUB_GUIDANCE_DIR",
}


class TunnelError(ValueError):
    """A safe, fixed diagnostic code; never put private input in this exception."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TunnelError("health_redirect_rejected")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_file(path, limit=MAX_LOCAL_BYTES):
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_size > limit
        ):
            raise TunnelError("unsafe_local_file")
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise TunnelError("unsafe_local_file")
        return raw
    except OSError:
        raise TunnelError("local_file_unavailable") from None


def _root():
    root = state_dir()
    if not root.is_absolute() or not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise TunnelError("state_directory_unavailable_or_unsafe")
    return root


def load_config():
    config = read_config("tunnel.json")
    if not {"tunnel_client", "tunnel_id"} <= config.keys() or not config.keys() <= {
        "tunnel_client",
        "tunnel_id",
        "http_proxy",
    }:
        raise TunnelError("invalid_tunnel_config")
    client = config["tunnel_client"]
    if (
        not isinstance(client, str)
        or not client
        or "\x00" in client
        or not Path(client).is_absolute()
        or not Path(client).is_file()
    ):
        raise TunnelError("tunnel_client_unavailable")
    if os.name != "nt" and not os.access(client, os.X_OK):
        raise TunnelError("tunnel_client_not_executable")
    tunnel_id = config["tunnel_id"]
    if not isinstance(tunnel_id, str) or not re.fullmatch(
        r"tunnel_[A-Za-z0-9_-]{8,128}", tunnel_id
    ):
        raise TunnelError("invalid_tunnel_identity")
    proxy = config.get("http_proxy")
    if proxy is not None:
        if not isinstance(proxy, str) or any(ord(char) < 32 for char in proxy):
            raise TunnelError("invalid_proxy")
        parsed = urlparse(proxy)
        try:
            port = parsed.port
        except ValueError:
            raise TunnelError("invalid_proxy") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or port == 0
        ):
            raise TunnelError("invalid_proxy")
    return {"tunnel_client": client, "tunnel_id": tunnel_id, "http_proxy": proxy}


def _loopback(url, base_only=False):
    if not isinstance(url, str):
        raise TunnelError("invalid_loopback_url")
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        raise TunnelError("invalid_loopback_url") from None
    allowed_paths = {"", "/"} if base_only else {"/healthz", "/readyz", "/metrics"}
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not port
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in allowed_paths
    ):
        raise TunnelError("invalid_loopback_url")
    return url.rstrip("/") if base_only else url


def local_read(url):
    """Only the client's loopback health API; bypass inherited proxies."""
    _loopback(url)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(Request(url), timeout=5) as response:
            body = response.read(MAX_LOCAL_BYTES + 1)
            status_code = response.status
    except HTTPError as error:
        status_code, body = error.code, error.read(MAX_LOCAL_BYTES + 1)
    if len(body) > MAX_LOCAL_BYTES:
        raise TunnelError("health_response_too_large")
    return status_code, body


@contextmanager
def instance_lock(root, create=True):
    """Keep one launcher per state directory; do not unlink a held lock file."""
    path = root / "tunnel.lock"
    if path.is_symlink():
        raise TunnelError("unsafe_lock")
    if not create and not path.exists():
        yield
        return
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise TunnelError("lock_unavailable") from None
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TunnelError("unsafe_lock")
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                if not create:
                    raise TunnelError("lock_requires_review")
                os.write(descriptor, b"0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise TunnelError("instance_locked") from None
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise TunnelError("instance_locked") from None
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _pid_running(pid):
    if sys.platform == "win32":
        # os.kill(pid, 0) is NOT a portable existence check on Windows.
        # Open only a query handle; never request terminate access.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # Invalid parameter: no such PID.
                return False
            raise TunnelError("previous_instance_requires_review")
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise TunnelError("previous_instance_requires_review")
            return code.value == 259  # STILL_ACTIVE.
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        raise TunnelError("previous_instance_requires_review") from None
    return True


def _previous_instance(root):
    path = root / "tunnel-status.json"
    if not path.exists() and not path.is_symlink():
        return
    previous = json.loads(_safe_file(path))
    if not isinstance(previous, dict):
        raise TunnelError("previous_status_requires_review")
    if type(previous.get("running")) is not bool:
        raise TunnelError("previous_status_requires_review")
    if previous.get("running") is not True:
        return
    pid = previous.get("pid")
    if type(pid) is not int or pid <= 0:
        raise TunnelError("previous_instance_requires_review")
    if _pid_running(pid):
        raise TunnelError("existing_instance_requires_review")


def preflight(config=None, root=None, check_instance=True):
    """Read-only module, executable and collector checks; no key or control-plane I/O."""
    root = _root() if root is None else root
    config = load_config() if config is None else config
    try:
        distribution("autoresearch-hub")
    except PackageNotFoundError:
        raise TunnelError("package_not_installed") from None
    if importlib.util.find_spec("research_hub.__main__") is None:
        raise TunnelError("package_entry_unavailable")
    from research_hub.mcp_server import project_snapshot

    snapshot = project_snapshot(
        _safe_file(root / "collector-state.json"), datetime.now(timezone.utc)
    )
    if not snapshot.available or snapshot.reason != "fresh_snapshot":
        raise TunnelError("collector_snapshot_not_fresh_or_verified")
    if check_instance:
        with instance_lock(root, create=False):
            _previous_instance(root)
    return {
        "mode": "preflight",
        "local_preflight": "PASS",
        "collector_fresh": True,
        "package_entry_available": True,
        "expected_tools": list(TOOLS),
        "tunnel_started": False,
        "cloud_call_verified": False,
    }


def profile_text(config, root):
    executable = str(Path(sys.executable))
    # The official command parser needs forward slashes for Windows paths.
    if os.name == "nt":
        executable = Path(sys.executable).as_posix()
        command = subprocess.list2cmdline([executable, "-m", "research_hub", "stdio"])
    else:
        command = shlex.join([executable, "-m", "research_hub", "stdio"])
    proxy_line = 'http_proxy: "env:HTTPS_PROXY"\n' if config["http_proxy"] is not None else ""
    return (
        "config_version: 1\n"
        + proxy_line
        + 'control_plane:\n  base_url: "https://api.openai.com"\n  tunnel_id: '
        + json.dumps(config["tunnel_id"])
        + '\n  api_key: "env:CONTROL_PLANE_API_KEY"\n'
        + 'health:\n  listen_addr: "127.0.0.1:0"\n  url_file: '
        + json.dumps(str(root / "tunnel-health.url"))
        + "\nadmin_ui:\n  open_browser: false\nlog:\n  level: warn\n  format: json\n"
        + "mcp:\n  commands:\n    - channel: main\n      command: "
        + json.dumps(command)
        + "\n"
    )


def prepare(config, root):
    path = root / "tunnel-profile.yaml"
    profile = profile_text(config, root)
    if path.exists() or path.is_symlink():
        if _safe_file(path).decode("utf-8").replace("\r\n", "\n") != profile:
            raise TunnelError("existing_profile_differs_requires_review")
    else:
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
        except OSError:
            raise TunnelError("profile_unavailable") from None
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(profile)
            stream.flush()
            os.fsync(stream.fileno())
    return path


def _save_state(root, data):
    path = root / "tunnel-status.json"
    if path.is_symlink():
        raise TunnelError("unsafe_status")
    descriptor, temporary = tempfile.mkstemp(prefix=".tunnel-status-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _key():
    key = os.environ.get("CONTROL_PLANE_API_KEY")
    if key is None:
        if not sys.stdin.isatty():
            raise TunnelError("interactive_key_or_environment_required")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("OpenAI Tunnel runtime key (hidden): ")
    if (
        not isinstance(key, str)
        or not key.startswith("sk-")
        or not 20 <= len(key) <= 8192
        or not key.isascii()
        or any(char.isspace() or ord(char) < 32 for char in key)
    ):
        raise TunnelError("invalid_runtime_key")
    return key


def _environment(key, config, root):
    env = {name: value for name, value in os.environ.items() if name.upper() in ENV_ALLOW}
    env.update(
        CONTROL_PLANE_API_KEY=key,
        RESEARCH_HUB_STATE_DIR=str(root),
        NO_PROXY="127.0.0.1,localhost",
        PYTHONDONTWRITEBYTECODE="1",
    )
    if config["http_proxy"] is not None:
        env["HTTPS_PROXY"] = config["http_proxy"]
    return env


def _flags(profile, config):
    flags = [
        "--config",
        str(profile),
        "--control-plane.base-url",
        "https://api.openai.com",
        "--control-plane.api-key",
        "env:CONTROL_PLANE_API_KEY",
        "--mcp.stdio-send-initialized-notification",
        "--mcp.max-concurrent-requests",
        "1",
        "--log.http-raw-unsafe=false",
        "--harpoon.capture-payloads=false",
    ]
    if config["http_proxy"] is not None:
        flags += ["--control-plane.http-proxy", "env:HTTPS_PROXY"]
    return flags


def _stop_owned(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _poll_sample(base):
    sample = {"health_http": None, "ready_http": None}
    totals = {}
    for name, endpoint in [("health_http", "healthz"), ("ready_http", "readyz")]:
        try:
            sample[name] = local_read(base + "/" + endpoint)[0]
        except (OSError, ValueError):
            pass
    try:
        status_code, raw = local_read(base + "/metrics")
        if status_code == 200:
            for line in raw.decode("utf-8", errors="replace").splitlines():
                match = re.fullmatch(
                    r"([A-Za-z_:][A-Za-z0-9_:]*poll[A-Za-z0-9_:]*_total)(?:\{[^}]*\})?\s+([0-9.eE+\-]+)(?:\s+\d+)?",
                    line,
                )
                if match:
                    value = float(match[2])
                    if math.isfinite(value) and value >= 0:
                        totals[match[1]] = totals.get(match[1], 0) + value
    except (OSError, ValueError):
        pass
    return sample, totals


def _runtime_session(config, root, env, flags, attempt):
    url_file = root / "tunnel-health.url"
    if url_file.is_symlink():
        raise TunnelError("unsafe_health_file")
    if url_file.exists():
        # This is only our launcher's old pointer, under the held instance lock.
        _safe_file(url_file, 4096)
        url_file.unlink()
    process = subprocess.Popen(
        [config["tunnel_client"], "run", *flags],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    state = {
        "checked_at": now(),
        "running": True,
        "pid": process.pid,
        "attempt": attempt,
        "doctor_passed": True,
        "ready_verified": False,
        "polling_verified": False,
        "cloud_call_verified": False,
        "health_http": None,
        "ready_http": None,
    }
    baseline = None
    try:
        _save_state(root, state)
        while process.poll() is None:
            if url_file.exists() or url_file.is_symlink():
                base = _loopback(_safe_file(url_file, 4096).decode("utf-8").strip(), base_only=True)
                sample, counters = _poll_sample(base)
                state.update(sample)
                state["ready_verified"] = (
                    sample["health_http"] == 200 and sample["ready_http"] == 200
                )
                if baseline is None and counters:
                    baseline = counters
                increasing = bool(
                    baseline
                    and any(value > baseline.get(name, value) for name, value in counters.items())
                )
                state["polling_verified"] = bool(state["ready_verified"] and increasing)
            else:
                state.update(
                    ready_verified=False, polling_verified=False, health_http=None, ready_http=None
                )
            state["checked_at"] = now()
            _save_state(root, state)
            print(
                json.dumps(
                    {
                        "mode": "connect",
                        **{
                            name: state[name]
                            for name in (
                                "running",
                                "ready_verified",
                                "polling_verified",
                                "cloud_call_verified",
                                "health_http",
                                "ready_http",
                            )
                        },
                    }
                ),
                flush=True,
            )
            time.sleep(15)
        return process.returncode
    finally:
        _stop_owned(process)
        state.update(
            running=False,
            ready_verified=False,
            polling_verified=False,
            stopped_at=now(),
            exit_code=process.returncode,
        )
        _save_state(root, state)


def connect(config, root, key):
    profile = prepare(config, root)
    flags = _flags(profile, config)
    env = _environment(key, config, root)
    try:
        try:
            doctor = subprocess.run(
                [config["tunnel_client"], "doctor", *flags, "--json", "--explain"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _save_state(
                root,
                {
                    "running": False,
                    "doctor_passed": False,
                    "reason": "doctor_timeout",
                    "checked_at": now(),
                    "cloud_call_verified": False,
                },
            )
            raise TunnelError("doctor_timeout") from None
        # stdout/stderr may contain arbitrary secrets or private paths. Never persist,
        # display or interpolate them into an exception, even after regex redaction.
        code = doctor.returncode
        del doctor
        _save_state(
            root,
            {
                "running": False,
                "doctor_passed": code == 0,
                "doctor_exit_code": code,
                "checked_at": now(),
                "cloud_call_verified": False,
            },
        )
        if code != 0:
            raise TunnelError("doctor_failed")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            _runtime_session(config, root, env, flags, attempt)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_SECONDS)
        raise TunnelError("client_retry_limit_reached")
    finally:
        env.pop("CONTROL_PLANE_API_KEY", None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Read-only local checks (default)")
    mode.add_argument(
        "--prepare",
        action="store_true",
        help="Create a key-free profile, without control-plane calls",
    )
    mode.add_argument(
        "--connect",
        action="store_true",
        help="Explicitly authorize doctor and the foreground Tunnel client",
    )
    args = parser.parse_args(argv)
    try:
        root, config = _root(), load_config()
        facts = preflight(config, root)
        if not args.prepare and not args.connect:
            print(json.dumps(facts))
            return 0
        with instance_lock(root):
            # Recheck under the lock, without trying to acquire our own lock twice.
            preflight(config, root, check_instance=False)
            _previous_instance(root)
            prepare(config, root)
            if args.prepare:
                print(
                    json.dumps(
                        {
                            "mode": "prepare",
                            "profile_prepared": True,
                            "tunnel_started": False,
                            "cloud_call_verified": False,
                        }
                    )
                )
                return 0
            key = _key()
            try:
                connect(config, root, key)
            finally:
                key = ""
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"available": False, "reason": "operator_stopped", "cloud_call_verified": False}
            )
        )
        return 130
    except TunnelError as error:
        print(json.dumps({"available": False, "reason": str(error), "cloud_call_verified": False}))
        return 1
    except Exception:
        # Never stringify an exception from a credential-bearing subprocess.
        print(
            json.dumps(
                {
                    "available": False,
                    "reason": "tunnel_setup_requires_review",
                    "cloud_call_verified": False,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
