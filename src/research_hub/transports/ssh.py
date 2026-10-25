"""Bounded OpenSSH transport for fixed operator targets, without password prompts."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time

MAX_OUTPUT = 262144
MAX_INPUT = 8 * 1024 * 1024
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def absolute_remote_path(value):
    if (
        not isinstance(value, str)
        or not PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
        or any(ord(c) < 32 for c in value)
        or str(PurePosixPath(value)) != value
    ):
        raise ValueError("ssh_absolute_posix_path_required")
    return value


def validate_config(data):
    if not isinstance(data, dict) or data.get("transport") != "ssh":
        raise ValueError("invalid_ssh_config")
    allowed = {
        "transport",
        "ssh_host",
        "expected_remote_user",
        "ssh_executable",
        "ssh_config",
        "session_alias",
        "remote_python",
        "remote_guidance_dir",
        "remote_state_dir",
        "remote_root",
        "bootstrap_python",
        "binding",
    }
    if not set(data) <= allowed:
        raise ValueError("unknown_ssh_config_fields")
    from research_hub.guidance.queue import validate_alias

    validate_alias(data.get("session_alias"))
    for key in ("ssh_host", "expected_remote_user"):
        if not isinstance(data.get(key), str) or not HOST.fullmatch(data[key]):
            raise ValueError("invalid_ssh_target")
    executable = data.get("ssh_executable") or shutil.which("ssh")
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or not Path(executable).is_file()
    ):
        raise ValueError("openssh_executable_unavailable")
    if "ssh_config" in data:
        if not isinstance(data["ssh_config"], str):
            raise ValueError("ssh_config_file_unavailable")
        path = Path(data["ssh_config"])
        if not path.is_absolute() or not path.is_file():
            raise ValueError("ssh_config_file_unavailable")
    absolute_remote_path(data.get("remote_python"))
    for key in ("remote_guidance_dir", "remote_state_dir"):
        absolute_remote_path(data.get(key))
    return {**data, "ssh_executable": str(executable)}


def argv(config, command):
    config = validate_config(config)
    args = [config["ssh_executable"]]
    if "ssh_config" in config:
        args += ["-F", config["ssh_config"]]
    args += ["-T"]
    for option in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "ClearAllForwardings=yes",
        "ForwardAgent=no",
        "PermitLocalCommand=no",
        "RemoteCommand=none",
        "RequestTTY=no",
        "ConnectTimeout=10",
        "ConnectionAttempts=1",
        "ServerAliveInterval=10",
        "ServerAliveCountMax=2",
        "LogLevel=ERROR",
    ):
        args += ["-o", option]
    # One remote command, quoted for the target's POSIX shell. No local shell=True.
    return [*args, config["ssh_host"], command]


def _stop(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def exchange(config, command, payload=b"", *, timeout=55):
    """Drain bounded streams concurrently; never display raw SSH output or errors."""
    if not isinstance(payload, bytes) or len(payload) > MAX_INPUT:
        raise ValueError("ssh_input_too_large")
    process = subprocess.Popen(
        argv(config, command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    exceeded = threading.Event()
    output, errors = bytearray(), bytearray()

    def drain(stream, target, cap):
        try:
            while chunk := stream.read(4096):
                if len(target) + len(chunk) > cap:
                    exceeded.set()
                    return
                target.extend(chunk)
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    def feed():
        try:
            process.stdin.write(payload)
        except (OSError, ValueError):
            pass
        finally:
            process.stdin.close()

    threads = [
        threading.Thread(target=drain, args=(process.stdout, output, MAX_OUTPUT), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, errors, 16384), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if exceeded.is_set():
                raise ValueError("ssh_output_too_large")
            if time.monotonic() > deadline:
                raise ValueError("ssh_timeout")
            time.sleep(0.02)
        for thread in threads:
            thread.join(timeout=2)
        if exceeded.is_set():
            raise ValueError("ssh_output_too_large")
        if any(thread.is_alive() for thread in threads):
            raise ValueError("ssh_stream_not_closed")
        if process.returncode:
            text = bytes(errors).decode("utf-8", errors="replace").lower()
            if "host key verification failed" in text or "host identification has changed" in text:
                raise ValueError("ssh_host_key_verification_failed")
            if "permission denied" in text:
                raise ValueError("ssh_authentication_failed")
            raise ValueError("ssh_remote_command_failed")
        return bytes(output)
    finally:
        _stop(process)
        for thread in threads:
            thread.join(timeout=2)


def python_command(executable, source):
    encoded = base64.b64encode(source.encode()).decode()
    program = "import base64;exec(base64.b64decode(" + repr(encoded) + "))"
    return shlex.join([executable, "-I", "-B", "-c", program])


def decode_receipt(raw):
    matches = re.findall(rb"RAH_DATA_([A-Za-z0-9+/=]+)_RAH_END", raw)
    if len(matches) != 1:
        raise ValueError("ssh_receipt_missing_or_ambiguous")
    data = json.loads(base64.b64decode(matches[0], validate=True))
    if not isinstance(data, dict):
        raise ValueError("ssh_receipt_invalid")
    return data


def run_remote(command, config):
    config = validate_config(config)
    # Authenticated host keys identify the host; also verify the expected OS account.
    source = (
        "import os,pwd,subprocess,sys\n"
        + "if pwd.getpwuid(os.getuid()).pw_name != "
        + repr(config["expected_remote_user"])
        + ": raise ValueError('remote_user_mismatch')\n"
        + "sys.exit(subprocess.call(['/bin/sh','-c',"
        + repr(command)
        + "]))\n"
    )
    raw = exchange(config, python_command(config["remote_python"], source))
    return {"metadata": decode_receipt(raw), "command_sent": True}
