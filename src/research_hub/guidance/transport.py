"""Fixed-target operator transport: OpenSSH or the legacy Windows SCO adapter."""

import base64
import json
import os
from pathlib import PurePosixPath, PureWindowsPath
import queue
import re
import shlex
import subprocess
import sys
import threading
import time

from research_hub.config import read_config
from .runtime import validate_request

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PROMPT = re.compile(r"(?:^|[\r\n])[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[^\r\n]{0,160}[#$] ?$")
AUTH_PROMPT = re.compile(
    r"password|access.?key|secret.?key|bearer|login|密码|口令|yes/no|y/n|are you sure", re.I
)


def load_remote_config():
    data = read_config("remote.json")
    if data.get("transport") == "ssh":
        from research_hub.transports.ssh import validate_config

        return validate_config(data)
    if data.get("transport", "sco") != "sco":
        raise ValueError("unsupported_remote_transport")
    for key in ("workspace_name", "task_name", "worker_name"):
        if not isinstance(data.get(key), str) or not IDENTIFIER.fullmatch(data[key]):
            raise ValueError("invalid_remote_config")
    for key in ("owner_user_id", "owner_user_name", "sco_executable", "remote_python"):
        if not isinstance(data.get(key), str) or not data[key] or "\x00" in data[key]:
            raise ValueError("invalid_remote_config")
    executable = data["sco_executable"]
    if not (PurePosixPath(executable).is_absolute() or PureWindowsPath(executable).is_absolute()):
        raise ValueError("absolute_sco_executable_required")
    return data


def remote_directory(config, key):
    value = config.get(key)
    if (
        not isinstance(value, str)
        or not PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
        or "\x00" in value
    ):
        raise ValueError("invalid_remote_path")
    return value


def verify_remote(config):
    response = subprocess.run(
        [
            config["sco_executable"],
            "acp",
            "jobs",
            "describe",
            config["task_name"],
            "--workspace-name=" + config["workspace_name"],
            "--format=json",
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if response.returncode or len(response.stdout) > 262144:
        raise ValueError("remote_identity_unavailable")
    current = json.loads(response.stdout)
    owner = current.get("ownership", {})
    if (
        current.get("name") != config["task_name"]
        or current.get("workspace_name") != config["workspace_name"]
        or owner.get("user_id") != config["owner_user_id"]
        or owner.get("user_name") != config["owner_user_name"]
        or current.get("state") != "RUNNING"
    ):
        raise ValueError("remote_identity_or_state_mismatch")


def spawn_sco_winpty(args):
    """Preserve the SCO CLI command prefix with the Windows WinPTY backend."""
    from winpty import Backend, PtyProcess

    # SCO consumes the first command-line token as argv[0] under WinPTY.
    # Include its executable there so the actual "acp" command survives.
    # This is specific to SCO; Python launchers parse the same input differently.
    return PtyProcess.spawn([args[0], *args], dimensions=(40, 140), backend=Backend.WinPTY)


def run_remote(command, config):
    if config.get("transport") == "ssh":
        from research_hub.transports.ssh import run_remote as run_ssh

        return run_ssh(command, config)
    if config.get("transport", "sco") != "sco":
        raise ValueError("unsupported_remote_transport")
    if os.name != "nt":
        raise ValueError("sco_pty_requires_windows")
    verify_remote(config)
    args = [
        config["sco_executable"],
        "acp",
        "jobs",
        "exec",
        config["task_name"],
        "--workspace-name=" + config["workspace_name"],
        "--worker-name=" + config["worker_name"],
    ]
    process = spawn_sco_winpty(args)
    chunks = queue.Queue()

    def reader():
        while True:
            try:
                chunks.put(process.read(4096))
            except Exception:
                return

    threading.Thread(target=reader, daemon=True).start()
    output, sent = "", False
    deadline = time.monotonic() + 55
    try:
        while time.monotonic() < deadline:
            try:
                chunk = chunks.get(timeout=0.3)
                output += chunk
                if len(output) > 262144:
                    raise ValueError("remote_output_too_large")
                if "\x1b[c" in chunk:
                    process.write("\x1b[?1;2c")
                if "\x1b[6n" in chunk:
                    process.write("\x1b[1;1R")
            except queue.Empty:
                pass
            clean = ANSI.sub("", output)
            if not sent:
                if AUTH_PROMPT.search(clean):
                    raise ValueError("interactive_authorization_required")
                if PROMPT.search(clean):
                    process.write("unset HISTFILE; set +o history; " + command + "\r\n")
                    sent = True
            match = re.search(r"RAH_DATA_([A-Za-z0-9+/=]+)_RAH_END", re.sub(r"\s+", "", clean))
            if match:
                if not sent:
                    raise ValueError("unexpected_remote_receipt")
                metadata = json.loads(base64.b64decode(match.group(1), validate=True))
                return {"metadata": metadata, "command_sent": True}
            if not process.isalive():
                break
        raise ValueError("remote_receipt_unavailable")
    finally:
        process.close(force=True)


def main():
    data = json.loads(sys.stdin.read(16001))
    validate_request(data)
    config = load_remote_config()
    remote_root = remote_directory(config, "remote_guidance_dir")
    encoded = base64.b64encode(json.dumps(data, ensure_ascii=True).encode()).decode()
    command = (
        "RESEARCH_HUB_GUIDANCE_DIR="
        + shlex.quote(remote_root)
        + " "
        + shlex.quote(config["remote_python"])
        + " -I -B -m research_hub.guidance.endpoint "
        + shlex.quote(encoded)
    )
    print(json.dumps(run_remote(command, config)))


if __name__ == "__main__":
    main()
