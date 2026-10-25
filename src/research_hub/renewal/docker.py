"""Fixed Docker daemon/container backend. No create, exec, update, pull, or delete."""

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import signal
import threading
import time
from types import SimpleNamespace

from .restart import Container
from .renewal_guard import Rejected, digest

SELECTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ID = re.compile(r"[a-f0-9]{64}")


def connection(config):
    if config.get("backend") != "docker":
        raise Rejected("unsupported_restart_backend")
    executable = config.get("docker_executable") or shutil.which("docker")
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or not Path(executable).is_file()
    ):
        raise Rejected("docker_executable_unavailable")
    host = config.get("docker_host")
    if not isinstance(host, str) or not host.startswith("unix:///"):
        raise Rejected("explicit_local_unix_docker_host_required")
    path = host[len("unix://") :]
    if (
        str(PurePosixPath(path)) != path
        or ".." in PurePosixPath(path).parts
        or any(ord(c) < 32 for c in path)
    ):
        raise Rejected("invalid_docker_host")
    return {"backend": "docker", "docker_executable": executable, "docker_host": host}


def bounded_runner(args, *, env, timeout, **kwargs):
    process = subprocess.Popen(
        args,
        env=env,
        stdin=subprocess.DEVNULL,
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
        finally:
            stream.close()

    threads = [
        threading.Thread(target=drain, args=(process.stdout, output, 1024 * 1024), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, errors, 16384), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if exceeded.is_set() or time.monotonic() > deadline:
                raise Rejected("docker_command_limit_exceeded")
            time.sleep(0.02)
        for thread in threads:
            thread.join(timeout=2)
        if exceeded.is_set() or any(thread.is_alive() for thread in threads):
            raise Rejected("docker_command_limit_exceeded")
        return SimpleNamespace(
            returncode=process.returncode,
            stdout=output.decode("utf-8", errors="replace"),
            stderr=errors.decode("utf-8", errors="replace"),
        )
    finally:
        if process.poll() is None:
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=2)


class Docker:
    def __init__(self, config, runner=None):
        self.config = connection(config)
        self.target = config
        self.runner = runner or bounded_runner

    def command(self, args):
        # Ignore inherited context/host/API overrides: the operator pinned an exact Unix endpoint.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("DOCKER_")}
        try:
            reply = self.runner(
                [self.config["docker_executable"], "--host", self.config["docker_host"], *args],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if (
                reply.returncode
                or len(reply.stdout.encode()) > 1024 * 1024
                or len(reply.stderr.encode()) > 16384
            ):
                raise Rejected("docker_command_failed")
            return reply.stdout
        except (OSError, subprocess.SubprocessError):
            raise Rejected("docker_command_unavailable") from None

    def daemon(self):
        value = json.loads(self.command(["info", "--format", "{{json .ID}}"]))
        if not isinstance(value, str) or not value or len(value) > 256:
            raise Rejected("docker_daemon_identity_unavailable")
        return value

    def describe(self, selector):
        if not isinstance(selector, str) or not SELECTOR.fullmatch(selector):
            raise Rejected("invalid_container_selector")
        data = json.loads(self.command(["container", "inspect", selector]))
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise Rejected("docker_inspect_schema_invalid")
        return data[0]

    @staticmethod
    def fingerprint(data):
        # Hash environment/mount configuration locally; do not persist raw inspect output.
        values = {key: data[key] for key in ("Image", "Config", "HostConfig", "Mounts")}
        host = values["HostConfig"]
        if host.get("AutoRemove") is not False or host.get("RestartPolicy", {}).get("Name") not in {
            "",
            "no",
        }:
            raise Rejected("docker_native_restart_or_autoremove_conflict")
        return digest(values)

    def pin(self, selector):
        daemon = self.daemon()
        data = self.describe(selector)
        if not ID.fullmatch(data.get("Id", "")) or not isinstance(data.get("Name"), str):
            raise Rejected("docker_container_identity_invalid")
        if self.daemon() != daemon:
            raise Rejected("docker_daemon_changed")
        return {
            **self.config,
            "daemon_id": daemon,
            "container_id": data["Id"],
            "container_name": data["Name"],
            "profile": self.fingerprint(data),
            "identity": digest([daemon, data["Id"]]),
        }

    def inspect(self):
        if not ID.fullmatch(self.target.get("container_id", "")):
            raise Rejected("full_container_id_required")
        if self.daemon() != self.target["daemon_id"]:
            raise Rejected("docker_daemon_changed")
        data = self.describe(self.target["container_id"])
        if (
            data["Id"] != self.target["container_id"]
            or data["Name"] != self.target["container_name"]
        ):
            raise Rejected("docker_container_identity_changed")
        state = data["State"]
        if type(state.get("ExitCode")) is not int or not isinstance(state.get("Status"), str):
            raise Rejected("docker_state_schema_invalid")
        return Container(
            digest([self.target["daemon_id"], data["Id"]]),
            self.fingerprint(data),
            state["Status"],
            state["StartedAt"],
            state["FinishedAt"],
            state["ExitCode"],
        )

    def start(self, expected):
        # Recheck identity immediately before the fixed-ID write, never resolve a mutable name.
        current = self.inspect()
        if (
            current != expected
            or current.identity != self.target["identity"]
            or current.profile != self.target["profile"]
            or current.state != "exited"
        ):
            raise Rejected("docker_target_changed_before_start")
        self.command(["container", "start", self.target["container_id"]])
