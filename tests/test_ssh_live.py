"""Opt-in real loopback SSH + isolated wheel deployment + simulated Codex RPC.

Run RESEARCH_HUB_TEST_SSH=1 uv run pytest tests/test_ssh_live.py -q -s after uv build.
No GPU, production account, public listener, or model turn is used.
"""

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

from research_hub.deployment.ssh import deploy, invoke
from research_hub.guidance.runtime import query_guidance
from research_hub.transports.ssh import exchange

pytestmark = pytest.mark.skipif(
    os.environ.get("RESEARCH_HUB_TEST_SSH") != "1" or os.name == "nt",
    reason="opt-in isolated POSIX OpenSSH integration",
)


class CodexFixture:
    def __init__(self, path, cwd):
        self.path, self.cwd, self.calls = path, cwd, []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen()
        self.server.settimeout(0.2)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.connection, args=(client,), daemon=True).start()

    def connection(self, client):
        def exact(n):
            data = b""
            while len(data) < n:
                part = client.recv(n - len(data))
                if not part:
                    raise EOFError
                data += part
            return data

        try:
            client.settimeout(15)
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                header += exact(1)
            key = header.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
            accept = base64.b64encode(
                hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
            )
            client.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            while not self.stop.is_set():
                header = exact(2)
                size = header[1] & 127
                if size == 126:
                    size = struct.unpack("!H", exact(2))[0]
                elif size == 127:
                    size = struct.unpack("!Q", exact(8))[0]
                assert size < 65536
                mask = exact(4)
                body = exact(size)
                message = json.loads(bytes(v ^ mask[i % 4] for i, v in enumerate(body)))
                method = message["method"]
                self.calls.append(message)
                assert method in {
                    "initialize",
                    "initialized",
                    "thread/read",
                    "thread/resume",
                    "thread/inject_items",
                }
                if "id" not in message:
                    continue
                result = {}
                if method in {"thread/read", "thread/resume"}:
                    assert message["params"]["threadId"] == "persisted-test-thread"
                    result = {
                        "thread": {"id": "persisted-test-thread", "cwd": self.cwd},
                        "approvalPolicy": "never",
                        "sandbox": {"type": "readOnly", "networkAccess": False},
                    }
                body = json.dumps({"id": message["id"], "result": result}).encode()
                frame = (
                    bytes([129, len(body)])
                    if len(body) < 126
                    else bytes([129, 126]) + struct.pack("!H", len(body))
                )
                client.sendall(frame + body)
        except (EOFError, OSError):
            pass
        finally:
            client.close()

    def close(self):
        self.stop.set()
        self.server.close()
        self.thread.join(timeout=2)


def test_real_ssh_install_guidance_receipt_progress_and_failures(tmp_path, monkeypatch):
    import pwd

    root = tmp_path.resolve()
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not Path(sshd).is_file():
        pytest.fail("OpenSSH sshd is required for this explicitly enabled test")
    for name in ("host", "client"):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)], check=True
        )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    user = pwd.getpwuid(os.getuid()).pw_name
    (root / "authorized_keys").write_bytes((root / "client.pub").read_bytes())
    (root / "authorized_keys").chmod(0o600)
    (root / "sshd_config").write_text(f"""Port {port}
ListenAddress 127.0.0.1
HostKey {root}/host
PidFile {root}/sshd.pid
AuthorizedKeysFile {root}/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes yes
AllowUsers {user}
""")
    public = (root / "host.pub").read_text().split()
    (root / "known_hosts").write_text(f"[127.0.0.1]:{port} {public[0]} {public[1]}\n")
    (root / "ssh_config").write_text(f"""Host hub-test
 HostName 127.0.0.1
 Port {port}
 User {user}
 IdentityFile {root}/client
 IdentitiesOnly yes
 UserKnownHostsFile {root}/known_hosts
""")
    log = (root / "sshd.log").open("w")
    daemon = subprocess.Popen(
        [sshd, "-D", "-e", "-f", str(root / "sshd_config")], stdout=log, stderr=subprocess.STDOUT
    )
    # macOS Unix socket paths are short; the socket lives in a separate private short path.
    import tempfile

    short = Path(tempfile.mkdtemp(prefix="rah-rpc-", dir="/tmp")).resolve()
    fixture = CodexFixture(short / "rpc.sock", str(root))
    remote = root / "remote"
    config = {
        "transport": "ssh",
        "ssh_host": "hub-test",
        "expected_remote_user": user,
        "ssh_config": str(root / "ssh_config"),
        "session_alias": "test_advisory",
        "remote_root": str(remote),
        "remote_python": str(remote / "runtime/bin/python"),
        "bootstrap_python": str(Path(sys.executable).resolve()),
        "remote_guidance_dir": str(remote / "guidance"),
        "remote_state_dir": str(remote / "state"),
        "binding": {
            "session_alias": "test_advisory",
            "session_id": "persisted-test-thread",
            "cwd": str(root),
            "codex_home": str(root / "codex"),
            "socket_path": str(short / "rpc.sock"),
        },
    }
    monkeypatch.setenv("RESEARCH_HUB_STATE_DIR", str(root / "local"))
    worker_pid = None
    try:
        for _ in range(30):
            if daemon.poll() is not None:
                pytest.fail("sshd exited: " + (root / "sshd.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        assert exchange(config, "printf ssh_connected") == b"ssh_connected"
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "project": "ssh-test",
                    "current_stage": "fixture",
                    "status": "Verified",
                    "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "checks": {"gpu_access": {"status": "Verified"}},
                    "execution": {
                        "episodes_started": 0,
                        "simulators_started": 0,
                        "generated_programs_executed": 0,
                        "task_success": None,
                    },
                }
            )
        )
        progress = {
            "project": "ssh-test",
            "manifest_path": str(manifest),
            "evidence_ref": "isolated_test_fixture",
            "approved_stage": "fixture",
            "approved_status": "Verified",
            "check_names": ["gpu_access"],
        }
        wheel = Path("dist/autoresearch_hub-0.1.0-py3-none-any.whl").resolve()
        assert deploy(config, wheel, progress)["installed"]
        assert deploy(config, wheel, progress)["installed"]  # identical retry, no rebinding
        assert invoke(config, "doctor")["checks"]["bound_thread_accessible"]
        assert not invoke(config, "doctor")["ready"]  # no worker yet
        assert invoke(config, "worker-start")["worker_started"]
        state = json.loads((remote / "guidance/worker-state.json").read_text())
        worker_pid = state["pid"]
        assert invoke(config, "doctor")["ready"]
        assert invoke(config, "worker-start")["worker_already_running"]
        request = {
            "operation": "submit",
            "request_id": "ssh_test_0001",
            "message": "Review this isolated fixture; do not execute an experiment.",
        }
        submitted = query_guidance(request)
        assert submitted["available"], submitted
        for _ in range(15):
            receipt = query_guidance({"operation": "status", "request_id": "ssh_test_0001"})
            if receipt.get("codex_received"):
                break
            time.sleep(0.4)
        assert receipt["codex_received"] and not receipt["model_processing_started"], receipt
        assert query_guidance(request)["duplicate"]
        from research_hub.progress.runtime import query_progress

        assert query_progress()["available"]
        from research_hub.guidance.runtime import run_module

        probe = run_module("research_hub.progress.reader")
        assert (
            probe["metadata"]["sources"][0]["sha256"]
            == hashlib.sha256(manifest.read_bytes()).hexdigest()
        )
        import asyncio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def check_mcp():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "research_hub", "stdio"],
                env={"RESEARCH_HUB_STATE_DIR": str(root / "local")},
            )
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    task_result = await session.call_tool("list_research_tasks", {})
                    assert (
                        task_result.structuredContent["reason"]
                        == "platform_task_listing_not_supported"
                    )
                    progress_result = await session.call_tool("get_research_progress", {})
                    assert progress_result.structuredContent["recorded_stage"] == "fixture"
                    status_result = await session.call_tool(
                        "get_research_guidance_status", {"request_id": "ssh_test_0001"}
                    )
                    assert status_result.structuredContent["codex_received"]

        asyncio.run(check_mcp())
        injections = [call for call in fixture.calls if call["method"] == "thread/inject_items"]
        assert len(injections) == 1
        assert all(call["method"] != "turn/start" for call in fixture.calls)
        with pytest.raises(ValueError):
            invoke({**config, "expected_remote_user": "wrong-user"}, "doctor")
        (root / "known_hosts").write_text("")
        with pytest.raises(ValueError, match="ssh_host_key_verification_failed"):
            exchange(config, "true")
        print(
            "Real OpenSSH: isolated wheel installed twice, doctor, worker, submit/status/dedup, one simulated RPC injection, manifest hash, identity/host-key failures passed."
        )
    finally:
        if worker_pid:
            os.kill(worker_pid, signal.SIGTERM)
        daemon.terminate()
        daemon.wait(timeout=10)
        log.close()
        fixture.close()
        shutil.rmtree(short)
