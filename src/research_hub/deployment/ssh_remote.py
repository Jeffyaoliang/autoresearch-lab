"""Stdlib-only remote installer and checks, invoked only by the SSH operator CLI."""

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def private_json(path, value):
    if path.is_symlink():
        raise ValueError("unsafe_path")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)


def deploy(data):
    import pwd
    import zipfile

    if pwd.getpwuid(os.getuid()).pw_name != data["expected_remote_user"]:
        raise ValueError("remote_user_mismatch")
    if sys.version_info < (3, 11):
        raise ValueError("python_311_required")
    root = Path(data["remote_root"])
    if root == Path("/") or root.resolve() != root or not root.parent.is_dir():
        raise ValueError("unsafe_deployment_root")
    wheel = base64.b64decode(data["wheel"], validate=True)
    if hashlib.sha256(wheel).hexdigest() != data["wheel_sha256"]:
        raise ValueError("wheel_hash_mismatch")
    identity = {key: data[key] for key in ("binding", "wheel_sha256", "progress")}
    marker = root / "deployment.private.json"
    if root.exists():
        if (
            marker.is_symlink()
            or not marker.is_file()
            or marker.stat().st_size > 65536
            or json.loads(marker.read_text()) != identity
        ):
            raise ValueError("deployment_root_already_exists")
    else:
        root.mkdir(mode=0o700)
        private_json(marker, identity)
    for name in ("guidance", "state", "runtime"):
        path = root / name
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("unsafe_path")
    for name in ("guidance", "state"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    for name in ("inbox", "claims", "receipts"):
        path = root / "guidance" / name
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("unsafe_path")
        path.mkdir(mode=0o700, exist_ok=True)
    if data.get("progress") is not None:
        path = root / "state" / "progress.json"
        if path.exists():
            if path.is_symlink() or json.loads(path.read_text()) != data["progress"]:
                raise ValueError("progress_config_conflict")
        else:
            private_json(path, data["progress"])
    bound = root / "guidance" / "binding.private.json"
    if bound.exists():
        if bound.is_symlink() or json.loads(bound.read_text()) != data["binding"]:
            raise ValueError("binding_conflict")
    else:
        private_json(bound, data["binding"])
    import re

    if not re.fullmatch(r"autoresearch_hub-[A-Za-z0-9_.-]+\.whl", data["wheel_name"]):
        raise ValueError("invalid_hub_wheel_name")
    wheel_path = root / data["wheel_name"]
    if wheel_path.is_symlink():
        raise ValueError("unsafe_path")
    fd = os.open(wheel_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(wheel)
    with zipfile.ZipFile(wheel_path) as archive:
        if "research_hub/guidance/endpoint.py" not in archive.namelist():
            raise ValueError("invalid_hub_wheel")
    subprocess.run(
        [sys.executable, "-m", "venv", str(root / "runtime")],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=90,
    )
    python = root / "runtime" / "bin" / "python"
    subprocess.run(
        [
            str(python),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            str(wheel_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    return {
        "installed": True,
        "wheel_sha256": data["wheel_sha256"],
        "model_processing_started": False,
    }


def doctor(data):
    import pwd
    from research_hub.guidance.rpc import Proxy, binding, guidance_dir

    checks = {
        "python_supported": sys.version_info >= (3, 11),
        "expected_remote_user": pwd.getpwuid(os.getuid()).pw_name == data["expected_remote_user"],
    }
    if not all(checks.values()):
        return {"ready": False, "checks": checks}
    root = guidance_dir()
    bound = binding()
    checks["session_alias_matches"] = bound["session_alias"] == data["session_alias"]
    checks["binding_matches_config"] = "binding" not in data or data["binding"] == bound
    proxy = None
    try:
        proxy = Proxy(bound)
        thread = proxy.call(
            "thread/read", {"threadId": bound["session_id"], "includeTurns": False}
        )["thread"]
        checks["bound_thread_accessible"] = (
            thread["id"] == bound["session_id"] and thread.get("cwd") == bound["cwd"]
        )
    except Exception:
        checks["bound_thread_accessible"] = False
    finally:
        if proxy:
            proxy.close()
    checks["worker_ready"] = False
    path = root / "worker-state.json"
    if path.is_file() and not path.is_symlink() and path.stat().st_size < 16384:
        try:
            from datetime import datetime, timezone

            state = json.loads(path.read_text())
            age = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(state["checked_at"].replace("Z", "+00:00"))
            ).total_seconds()
            os.kill(state["pid"], 0)
            checks["worker_ready"] = (
                0 <= age < 15
                and state["binding"] == bound["session_alias"]
                and state["status"] == "ready_waiting_for_guidance"
            )
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "model_processing_started": False,
        "platform_task_listing_supported": False,
    }


def start_worker(data):
    from research_hub.guidance.rpc import binding, guidance_dir
    import fcntl

    # Holding this exact worker lock means a worker is already running; do not duplicate it.
    root = guidance_dir()
    binding()
    fd = os.open(root / "worker.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"worker_already_running": True, "model_processing_started": False}
    finally:
        os.close(fd)
    program = "import signal;signal.signal(signal.SIGHUP,signal.SIG_IGN);from research_hub.guidance.worker import main;main()"
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", program],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(30):
        time.sleep(0.2)
        if process.poll() is not None:
            raise ValueError("worker_start_failed")
        path = root / "worker-state.json"
        if path.is_file() and not path.is_symlink():
            state = json.loads(path.read_text())
            if state.get("pid") == process.pid:
                return {"worker_started": True, "model_processing_started": False}
    raise ValueError("worker_start_not_confirmed")


def main():
    try:
        data = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024 + 1))
        if data["action"] == "deploy":
            result = deploy(data)
        else:
            import pwd

            if pwd.getpwuid(os.getuid()).pw_name != data["expected_remote_user"]:
                raise ValueError("remote_user_mismatch")
            os.environ["RESEARCH_HUB_GUIDANCE_DIR"] = data["remote_guidance_dir"]
            result = {"doctor": doctor, "worker-start": start_worker}[data["action"]](data)
        print("RAH_DATA_" + base64.b64encode(json.dumps(result).encode()).decode() + "_RAH_END")
    except Exception:
        # Raw exceptions may contain private paths or socket payloads.
        print(
            "RAH_DATA_"
            + base64.b64encode(b'{"ok":false,"reason":"ssh_remote_setup_or_check_failed"}').decode()
            + "_RAH_END"
        )


if __name__ == "__main__":
    main()
