"""Fixed-target official App Server client; no public Shell/process/config-write RPC."""

import base64, hashlib, json, os, socket, struct, time, stat, select
from pathlib import Path
from .policy import load_settings


def expected_peer(settings):
    """Validate the configured peer PID against its Linux process start time."""
    path = settings.peer_manifest
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != settings.principal_uid or st.st_mode & 0o077:
        raise ValueError("unsafe_peer_manifest")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("oversize_peer_manifest")
    try:
        data = json.loads(raw)
        pid = data["pid"]
        ticks = str(data["start_ticks"])
        if type(pid) is not int or pid <= 0:
            raise ValueError("invalid_peer_pid")
        actual = Path("/proc", str(pid), "stat").read_text().split(") ", 1)[1].split()[19]
        if actual != ticks:
            raise ValueError("app_server_pid_reused")
    except (KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_peer_manifest") from exc
    return pid


ALLOWED = frozenset(
    {
        "initialize",
        "thread/read",
        "thread/loaded/list",
        "thread/resume",
        "turn/start",
        "turn/interrupt",
        "thread/turns/list",
        "thread/items/list",
        "thread/inject_items",
        "thread/backgroundTerminals/list",
        "thread/backgroundTerminals/clean",
        "permissionProfile/list",
    }
)


class RPCError(Exception):
    def __init__(self, error):
        self.error = error
        super().__init__("official_rpc_rejected_" + str(error.get("code")))


class ExecutorRPC:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else load_settings()
        if not hasattr(socket, "SO_PEERCRED"):
            raise ValueError("linux_peer_credentials_required")
        binding = {"session_id": self.settings.session_id}
        self.events = []
        self.binding = binding
        self.seq = 0
        self.methods_sent = []
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(15)
        self.s.connect(str(self.settings.socket_path))
        peer = struct.unpack("3i", self.s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer[0] != expected_peer(self.settings) or peer[1] != self.settings.principal_uid:
            self.s.close()
            raise ValueError("unexpected_server_peer")
        self.peer_pid = peer[0]
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
        if not header.startswith(b"HTTP/1.1 101 ") or accept not in header:
            raise ValueError("websocket_upgrade_rejected")
        self.call(
            "initialize",
            {
                "clientInfo": {"name": "research_hub_executor", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self._send({"method": "initialized"})

    def _exact(self, n):
        out = b""
        while len(out) < n:
            part = self.s.recv(n - len(out))
            if not part:
                raise EOFError("rpc_disconnected")
            out += part
        return out

    def _frame(self, body, op=1):
        mask = os.urandom(4)
        n = len(body)
        head = (
            bytes([128 | op, 128 | n])
            if n < 126
            else (
                bytes([128 | op, 254]) + struct.pack("!H", n)
                if n < 65536
                else bytes([128 | op, 255]) + struct.pack("!Q", n)
            )
        )
        self.s.sendall(head + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(body)))

    def _send(self, data):
        self._frame(json.dumps(data, ensure_ascii=True).encode())

    def _receive(self):
        body = b""
        while True:
            h = self._exact(2)
            op = h[0] & 15
            n = h[1] & 127
            if n == 126:
                n = struct.unpack("!H", self._exact(2))[0]
            if n == 127:
                n = struct.unpack("!Q", self._exact(8))[0]
            if h[1] & 128 or n > 4 * 1024 * 1024:
                raise ValueError("invalid_server_frame")
            data = self._exact(n)
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
            if h[0] & 128:
                return json.loads(body)

    def call(self, method, params):
        if method not in ALLOWED:
            raise ValueError("rpc_method_not_allowed")
        if (
            method.startswith(("thread/", "turn/"))
            and method != "thread/loaded/list"
            and params.get("threadId") != self.binding["session_id"]
        ):
            raise ValueError("thread_binding_mismatch")
        self.s.settimeout(15)
        self.methods_sent.append(method)
        self.seq += 1
        rid = self.seq
        self._send({"id": rid, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            data = self._receive()
            if data.get("id") == rid:
                if "error" in data:
                    raise RPCError(data["error"])
                return data["result"]
            self.dispatch(data)
        raise TimeoutError("rpc_timeout")

    def dispatch(self, data):
        if "id" in data and "method" in data:
            self.events.append(
                {
                    "method": "executor/approval_denied",
                    "params": {"requested_method": data["method"]},
                }
            )
            self._send(
                {
                    "id": data["id"],
                    "error": {
                        "code": -32601,
                        "message": "executor_does_not_approve_escalation_or_external_tools",
                    },
                }
            )
        elif data.get("params", {}).get("threadId") == self.binding["session_id"]:
            self.events.append(data)

    def receive_notification(self, timeout):
        if not select.select([self.s], [], [], max(0.01, timeout))[0]:
            raise socket.timeout()
        self.s.settimeout(15)
        self.dispatch(self._receive())

    def close(self):
        self.s.close()
