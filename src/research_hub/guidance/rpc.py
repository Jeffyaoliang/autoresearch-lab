"""Unix WebSocket RPC restricted to one operator-configured existing thread."""

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import struct
import time

from research_hub.config import state_dir
from .queue import validate_alias

ALLOWED = {
    "initialize",
    "config/read",
    "thread/resume",
    "thread/read",
    "thread/loaded/list",
    "thread/inject_items",
}


def guidance_dir():
    root = Path(os.environ.get("RESEARCH_HUB_GUIDANCE_DIR", str(state_dir() / "guidance")))
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise ValueError("unsafe_guidance_root")
    return root


def validate_binding(data):
    if not isinstance(data, dict):
        raise ValueError("invalid_binding")
    validate_alias(data.get("session_alias"))
    if not isinstance(data.get("session_id"), str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", data["session_id"]
    ):
        raise ValueError("invalid_binding")
    for key in ("cwd", "codex_home"):
        value = data.get(key)
        if (
            not isinstance(value, str)
            or not PurePosixPath(value).is_absolute()
            or ".." in PurePosixPath(value).parts
            or "\x00" in value
        ):
            raise ValueError("invalid_binding")
    if "socket_path" in data and (
        not isinstance(data["socket_path"], str)
        or not PurePosixPath(data["socket_path"]).is_absolute()
        or "\x00" in data["socket_path"]
    ):
        raise ValueError("invalid_binding")
    return dict(data)


def binding():
    path = guidance_dir() / "binding.private.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("unsafe_binding")
    return validate_binding(json.loads(path.read_text()))


class Proxy:
    def __init__(self, binding):
        self.binding = validate_binding(binding)
        self.seq = 0
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(15)
        try:
            self.s.connect(
                binding.get(
                    "socket_path",
                    binding["codex_home"] + "/app-server-control/app-server-control.sock",
                )
            )
            key = base64.b64encode(os.urandom(16)).decode()
            self.s.sendall(
                (
                    "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: "
                    + key
                    + "\r\nSec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                if len(header) > 8192:
                    raise ValueError("oversize_upgrade")
                header += self._exact(1)
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            )
            headers = {
                row.partition(b":")[0].strip().lower(): row.partition(b":")[2].strip()
                for row in header.split(b"\r\n")[1:]
                if b":" in row
            }
            if (
                not header.startswith(b"HTTP/1.1 101 ")
                or headers.get(b"sec-websocket-accept") != accept
            ):
                raise ValueError("websocket_upgrade_rejected")
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "research_hub_guidance", "version": "0.1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized"})
        except Exception:
            self.s.close()
            raise

    def _exact(self, size):
        output = b""
        while len(output) < size:
            part = self.s.recv(size - len(output))
            if not part:
                raise EOFError("rpc_disconnected")
            output += part
        return output

    def _frame(self, body, op=1):
        mask, size = os.urandom(4), len(body)
        head = (
            bytes([128 | op, 128 | size])
            if size < 126
            else (
                bytes([128 | op, 254]) + struct.pack("!H", size)
                if size < 65536
                else bytes([128 | op, 255]) + struct.pack("!Q", size)
            )
        )
        self.s.sendall(head + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(body)))

    def _send(self, data):
        self._frame(json.dumps(data, ensure_ascii=True).encode())

    def _receive(self):
        body = b""
        while True:
            header = self._exact(2)
            op, size = header[0] & 15, header[1] & 127
            if size == 126:
                size = struct.unpack("!H", self._exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._exact(8))[0]
            if header[1] & 128 or size > 4 * 1024 * 1024:
                raise ValueError("invalid_server_frame")
            data = self._exact(size)
            if op == 8:
                raise EOFError("rpc_closed")
            if op == 9:
                self._frame(data, 10)
                continue
            if op == 10:
                continue
            if op not in [0, 1]:
                raise ValueError("invalid_opcode")
            body += data
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("oversize_rpc")
            if header[0] & 128:
                return json.loads(body)

    def call(self, method, params):
        if method not in ALLOWED:
            raise ValueError("rpc_method_not_allowed")
        if (
            method.startswith("thread/")
            and method != "thread/loaded/list"
            and params.get("threadId") != self.binding["session_id"]
        ):
            raise ValueError("thread_binding_mismatch")
        self.seq += 1
        request_id = self.seq
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            data = self._receive()
            if not isinstance(data, dict):
                raise ValueError("invalid_rpc_result")
            if data.get("id") == request_id:
                if "error" in data:
                    raise ValueError("rpc_rejected")
                if not isinstance(data.get("result"), dict):
                    raise ValueError("invalid_rpc_result")
                return data["result"]
            if "id" in data and "method" in data:
                self._send(
                    {
                        "id": data["id"],
                        "error": {"code": -32601, "message": "unsupported_by_guidance_bridge"},
                    }
                )
        raise TimeoutError("rpc_timeout")

    def close(self):
        self.s.close()
