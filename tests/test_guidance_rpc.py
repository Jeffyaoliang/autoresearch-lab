import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from research_hub.guidance import rpc
from research_hub.guidance.worker import resume


class FakeSocket:
    def __init__(self, data=b""):
        self.data, self.sent = data, b""

    def recv(self, size):
        output, self.data = self.data[:size], self.data[size:]
        return output

    def sendall(self, data):
        self.sent += data


class ProtocolTests(unittest.TestCase):
    def proxy(self, data=b""):
        proxy = object.__new__(rpc.Proxy)
        proxy.s, proxy.binding, proxy.seq = FakeSocket(data), {"session_id": "example-thread"}, 0
        return proxy

    def test_forbidden_rpc_and_wrong_thread(self):
        for method in ["turn/start", "command/exec", "thread/shellCommand", "process/spawn"]:
            with self.assertRaises(ValueError):
                self.proxy().call(method, {})
        with self.assertRaises(ValueError):
            self.proxy().call("thread/inject_items", {"threadId": "other"})

    def test_masked_send(self):
        proxy = self.proxy()
        proxy._frame(b"abc")
        raw = proxy.s.sent
        self.assertEqual(raw[:2], b"\x81\x83")
        self.assertEqual(bytes(value ^ raw[2:6][i % 4] for i, value in enumerate(raw[6:])), b"abc")

    def test_fragmented_receive_with_ping(self):
        proxy = self.proxy(b'\x01\x06{"id":' + b"\x89\x01x" + b"\x80\x021}")
        self.assertEqual(proxy._receive(), {"id": 1})
        self.assertEqual(proxy.s.sent[0], 138)

    def test_invalid_frame_and_eof(self):
        with self.assertRaises(ValueError):
            self.proxy(b"\x81\x7f" + struct.pack("!Q", 4 * 1024 * 1024 + 1))._receive()
        with self.assertRaises(EOFError):
            self.proxy()._receive()

    def test_real_rpc_error_is_not_acknowledged(self):
        body = json.dumps(
            {"id": 1, "error": {"code": -1, "message": "private server detail"}}
        ).encode()
        with self.assertRaisesRegex(ValueError, "^rpc_rejected$"):
            self.proxy(bytes([129, len(body)]) + body).call(
                "thread/read", {"threadId": "example-thread"}
            )

    def test_binding_is_explicit_local_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bound = {
                "session_alias": "example_session",
                "session_id": "example-thread",
                "cwd": "/example/project",
                "codex_home": "/example/codex",
            }
            (root / "binding.private.json").write_text(json.dumps(bound))
            with patch.dict("os.environ", {"RESEARCH_HUB_GUIDANCE_DIR": str(root)}):
                self.assertEqual(rpc.binding(), bound)
            with patch.dict("os.environ", {"RESEARCH_HUB_GUIDANCE_DIR": "relative"}):
                with self.assertRaises(ValueError):
                    rpc.binding()

    def test_resume_rejects_weaker_permissions(self):
        class Proxy:
            def call(self, method, params):
                self.method, self.params = method, params
                return {
                    "thread": {"id": "example-thread"},
                    "approvalPolicy": "never",
                    "sandbox": {"type": "readOnly", "networkAccess": True},
                }

        proxy = Proxy()
        with self.assertRaisesRegex(ValueError, "permission_mismatch"):
            resume(proxy, {"session_id": "example-thread", "cwd": "/example/project"})
        self.assertEqual(proxy.method, "thread/resume")
