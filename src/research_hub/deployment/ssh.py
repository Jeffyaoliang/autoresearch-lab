"""Explicit operator commands for a standard OpenSSH target; never an MCP shell tool."""

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import zipfile

from research_hub.config import state_dir
from research_hub.guidance.rpc import validate_binding
from research_hub.transports.ssh import (
    absolute_remote_path,
    decode_receipt,
    exchange,
    python_command,
    validate_config,
)


def load(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("invalid_setup_config")
    data = json.loads(path.read_text())
    return validate_config(data)


def invoke(config, action, payload=None):
    from . import ssh_remote

    source = Path(ssh_remote.__file__).read_text()
    executable = config["remote_python"]
    if action == "deploy":
        executable = config.get("bootstrap_python", "python3")
        if executable != "python3":
            absolute_remote_path(executable)
    raw = exchange(
        config,
        python_command(executable, source),
        json.dumps({**config, **(payload or {}), "action": action}).encode(),
        timeout=180 if action == "deploy" else 45,
    )
    result = decode_receipt(raw)
    if result.get("ok") is False:
        raise ValueError("ssh_remote_setup_or_check_failed")
    return result


def deploy(config, wheel, progress=None):
    root = absolute_remote_path(config["remote_root"])
    if root == "/" or any(
        config[key] != root + suffix
        for key, suffix in (
            ("remote_python", "/runtime/bin/python"),
            ("remote_guidance_dir", "/guidance"),
            ("remote_state_dir", "/state"),
        )
    ):
        raise ValueError("deployment_paths_must_match_root")
    bound = validate_binding(config["binding"])
    if bound["session_alias"] != config["session_alias"]:
        raise ValueError("session_alias_mismatch")
    wheel = Path(wheel)
    if wheel.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("wheel_too_large")
    with zipfile.ZipFile(wheel) as archive:
        if "research_hub/guidance/endpoint.py" not in archive.namelist():
            raise ValueError("invalid_hub_wheel")
    if not re.fullmatch(r"autoresearch_hub-[A-Za-z0-9_.-]+\.whl", wheel.name):
        raise ValueError("invalid_hub_wheel_name")
    raw = wheel.read_bytes()
    # Refuse conflicting local state BEFORE making any remote changes.
    target = state_dir() / "remote.json"
    remote = {
        key: value
        for key, value in config.items()
        if key not in {"binding", "remote_root", "bootstrap_python"}
    }
    if target.is_symlink() or (target.exists() and json.loads(target.read_text()) != remote):
        raise ValueError("local_remote_config_conflict_use_separate_state_dir")
    if progress is not None:
        from research_hub.progress.projection import projection_config

        progress = json.loads(json.dumps(projection_config(progress)))
        local_progress = state_dir() / "progress.json"
        if local_progress.is_symlink() or (
            local_progress.exists() and json.loads(local_progress.read_text()) != progress
        ):
            raise ValueError("local_progress_config_conflict")
    result = invoke(
        config,
        "deploy",
        {
            "wheel": base64.b64encode(raw).decode(),
            "wheel_sha256": hashlib.sha256(raw).hexdigest(),
            "wheel_name": wheel.name,
            "binding": bound,
            "progress": progress,
        },
    )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.exists():
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(remote, stream, indent=2)
    if progress is not None and not local_progress.exists():
        fd = os.open(local_progress, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(progress, stream, indent=2)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deploy/check an operator-owned POSIX SSH target")
    parser.add_argument("action", choices=["deploy", "doctor", "worker-start"])
    parser.add_argument("--config", required=True, help="private operator SSH JSON config")
    parser.add_argument("--wheel", help="built autoresearch-hub wheel, required for deploy")
    parser.add_argument(
        "--start-worker",
        action="store_true",
        help="explicitly resume the bound advisory thread read-only",
    )
    parser.add_argument("--progress-config", help="optional reviewed progress projection JSON")
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        if args.progress_config and args.action != "deploy":
            parser.error("--progress-config is only valid for deploy")
        if args.start_worker and args.action != "deploy":
            parser.error("--start-worker is only valid for deploy")
        if args.action == "deploy":
            if not args.wheel:
                parser.error("deploy requires --wheel")
            result = deploy(
                config,
                args.wheel,
                json.loads(Path(args.progress_config).read_text())
                if args.progress_config
                else None,
            )
            if args.start_worker:
                result["worker"] = invoke(config, "worker-start")
                result["doctor"] = invoke(config, "doctor")
        else:
            result = invoke(config, args.action)
        print(json.dumps(result, indent=2))
        return int(result.get("ready") is False or result.get("doctor", {}).get("ready") is False)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile):
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "ssh_setup_or_check_failed",
                    "next_step": "Check config, SSH key/known_hosts, remote Python, fixed thread and socket; see docs/ssh.md.",
                }
            ),
            file=sys.stderr,
        )
        return 1
