"""Opt-in real Docker CLI/daemon acceptance using only an isolated disposable container."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RESEARCH_HUB_TEST_DOCKER") != "1", reason="opt-in real Docker daemon acceptance"
)


def test_installed_cli_restarts_same_failed_container(tmp_path):
    docker = shutil.which("docker")
    assert docker, "Docker CLI required when live test explicitly enabled"
    host = "unix:///var/run/docker.sock"

    def call(*args):
        return subprocess.check_output(
            [docker, "--host", host, *args], text=True, stderr=subprocess.DEVNULL, timeout=45
        ).strip()

    name = "research-hub-test-" + uuid.uuid4().hex[:12]
    # The image must already exist; this test never pulls an arbitrary operator image.
    container = call(
        "container",
        "create",
        "--pull=never",
        "--name",
        name,
        "--restart=no",
        "busybox:1.37.0",
        "sh",
        "-c",
        "if [ -e /tmp/first-failure ]; then exec sleep 120; else touch /tmp/first-failure; exit 1; fi",
    )
    try:
        assert len(container) == 64
        call("container", "start", container)
        assert call("container", "wait", container) == "1"
        root = tmp_path.resolve() / "renewal"

        def hub(*args):
            reply = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "research_hub",
                    "renewal",
                    "docker",
                    *args,
                    "--state-dir",
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert reply.returncode == 0, reply.stdout + reply.stderr
            return json.loads(reply.stdout)

        assert (
            hub(
                "init",
                "--docker-host",
                host,
                "--docker-executable",
                docker,
                "--container",
                container,
            )["policy_enabled"]
            is False
        )
        target = json.loads((root / "target.private.json").read_text())
        assert target["container_id"] == container
        policy = root / "policy.private.json"
        policy.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "allowed_exit_codes": [1],
                    "max_restarts": 1,
                    "cooldown_seconds": 1,
                }
            )
        )
        time.sleep(1.1)
        assert hub("check")["status"] == "eligible"
        result = hub("once", "--apply")
        assert result["status"] == "accepted" and result["container_running"]
        assert result["restart_attempts"] == 1
        assert json.loads(call("container", "inspect", container))[0]["State"]["Running"]
        assert hub("once", "--apply")["status"] == "observing"
        records = json.loads((root / "ledger.json").read_text())["records"]
        assert len(records) == 1 and next(iter(records.values()))["status"] == "ACCEPTED"
        (root / "stop.flag").touch()
        reply = subprocess.run(
            [
                sys.executable,
                "-m",
                "research_hub",
                "renewal",
                "docker",
                "once",
                "--apply",
                "--state-dir",
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert json.loads(reply.stdout)["reason"] == "stop_flag_active"
        print(
            "Real Docker acceptance: pinned original container restarted after exit 1; independent Running observation; one durable start; duplicate poll idle; stop flag blocks."
        )
    finally:
        # Only the exact test-created ID is removed; no discovery or broad cleanup.
        call("container", "rm", "--force", container)
