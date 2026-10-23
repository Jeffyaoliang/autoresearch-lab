"""Single-owner fixed-session actor. Private Unix RPC; task enums only, never Shell passthrough."""

import os, json, time, socket, socketserver, struct, threading, fcntl, subprocess, uuid
from pathlib import Path
from .policy import (
    MAX_GPU_HOURS,
    MAX_RETRIES,
    TEXT_PROMPT,
    TEXT_REPLY,
    TEXT_TIMEOUT_SECONDS,
    TURN_TIMEOUT_SECONDS,
    data_dir,
    feature_config,
    load_settings,
)
from .state import State, now
from .rpc import ExecutorRPC, RPCError, expected_peer


def auth_mode(settings):
    exe = os.readlink("/proc/" + str(expected_peer(settings)) + "/exe")
    result = subprocess.run(
        [exe, "login", "status"],
        env={
            "HOME": str(settings.auth_home),
            "PATH": "/usr/bin:/bin",
            "CODEX_HOME": str(settings.codex_home),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    output = (result.stdout + result.stderr).lower()
    return (
        result.returncode == 0 and "logged in using chatgpt" in output and "api key" not in output
    )


def holders(session_id):
    proc = Path("/proc")
    if not proc.is_dir():
        raise ValueError("linux_process_inspection_required")
    found = []
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            if any(session_id in os.readlink(fd) for fd in (p / "fd").iterdir()):
                found.append(int(p.name))
        except OSError:
            pass
    return sorted(set(found))


class Actor:
    def __init__(self, state, settings=None, rpc_factory=ExecutorRPC, advisory_callback=None):
        self.settings = settings if settings is not None else load_settings()
        self.s = state
        self.rpc = None
        self.current = None
        self.active_turn = None
        self.rpc_factory = rpc_factory
        self.advisory_callback = advisory_callback
        binding = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(self.settings).items()
        }
        previous = self.s.meta("executor_binding")
        if previous is not None and previous != binding:
            raise ValueError("executor_state_binding_mismatch")
        if previous is None:
            self.s.meta("executor_binding", binding)

    def connect(self):
        if self.rpc:
            self.rpc.close()
        self.rpc = self.rpc_factory(self.settings)
        self.rpc.s.settimeout(15)
        return self.rpc

    def ensure_connection(self):
        if self.rpc is None:
            self.connect()
            self.s.audit(
                None, "executor/idle_subscription_connected", {"peer_pid": self.rpc.peer_pid}
            )
        return self.rpc

    def thread(self):
        return self.rpc.call(
            "thread/read", {"threadId": self.settings.session_id, "includeTurns": False}
        )["thread"]

    def drain(self, job):
        events = self.rpc.events
        self.rpc.events = []
        for e in events:
            method = e.get("method", "unknown")
            p = e.get("params", {})
            if method.startswith("item/reasoning"):
                continue
            if p.get("item", {}).get("type") == "reasoning":
                p = {
                    "threadId": p.get("threadId"),
                    "turnId": p.get("turnId"),
                    "item": {"type": "reasoning", "content_exported": False},
                }
            self.s.audit(job, method, p)
            if method == "turn/started":
                self.active_turn = p.get("turn", {}).get("id")
                self.s.update(job, turn_id=self.active_turn)
            if method == "item/agentMessage/delta":
                self.reply += p.get("delta", "")
            if method == "item/completed" and p.get("item", {}).get("type") == "agentMessage":
                self.reply = p["item"].get("text", self.reply)
            if method == "item/started" and p.get("item", {}).get("type") not in [
                "userMessage",
                "agentMessage",
                "reasoning",
            ]:
                self.side_effects = True
            if method == "turn/completed":
                self.done = p.get("turn", {})

    def budget(self):
        campaign = self.s.meta("campaign")
        if not campaign:
            return {
                "max_gpu_hours": MAX_GPU_HOURS,
                "conservative_gpu_hours": 0,
                "remaining_gpu_hours": MAX_GPU_HOURS,
                "api_budget": 0,
            }
        elapsed = max(0, time.time() - campaign["started_epoch"])
        used = elapsed * campaign["charged_gpu_count"] / 3600
        return {
            "max_gpu_hours": MAX_GPU_HOURS,
            "conservative_gpu_hours": used,
            "remaining_gpu_hours": max(0, MAX_GPU_HOURS - used),
            "api_budget": 0,
            "charged_gpu_count": campaign["charged_gpu_count"],
        }

    def cancel(self, job, reason):
        self.s.audit(
            job, "executor/interrupt_requested", {"turn_id": self.active_turn, "reason": reason}
        )
        if self.active_turn:
            self.rpc.call(
                "turn/interrupt", {"threadId": self.settings.session_id, "turnId": self.active_turn}
            )

    def run(self, job):
        key = job["id"]
        self.current = key
        self.active_turn = None
        self.reply = ""
        self.done = None
        self.side_effects = False
        if job["stop"]:
            self.s.update(
                key,
                state="cancelled",
                ended=time.time(),
                result={"reason": "stopped_before_start", "turn_started": False},
            )
            return
        text = job["task"] == "text_acceptance"
        if not text:
            accepted = [
                j
                for j in self.s.jobs()
                if j["task"] == "text_acceptance"
                and j["state"] == "completed"
                and (j["result"] or {}).get("acceptance_passed")
            ]
            if not accepted:
                raise ValueError("text_acceptance_not_passed")
            if not self.s.meta("scoped_permissions_verified"):
                raise ValueError("credential_isolation_not_verified_execution_blocked")
            if self.budget()["remaining_gpu_hours"] <= 0:
                raise ValueError("gpu_budget_exhausted")
            raise ValueError("scoped_runtime_not_yet_enabled")
        self.connect()
        t = self.thread()
        if t["status"] == {"type": "notLoaded"}:
            self.rpc.call(
                "thread/resume",
                {
                    "threadId": self.settings.session_id,
                    "excludeTurns": True,
                    "cwd": str(self.settings.project_dir),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "config": feature_config(self.settings, True),
                },
            )
            t = self.thread()
        if t["status"] != {"type": "idle"}:
            raise ValueError("target_not_idle_no_new_turn_started")
        if any(pid != expected_peer(self.settings) for pid in holders(self.settings.session_id)):
            raise ValueError("foreign_cli_session_holder")
        if not auth_mode(self.settings):
            raise ValueError("subscription_only_auth_not_verified")
        cfg = feature_config(self.settings, text)
        # Credentials must stay outside model-generated command environments.
        args = {
            "threadId": self.settings.session_id,
            "excludeTurns": True,
            "cwd": str(self.settings.project_dir),
            "approvalPolicy": "never",
            "config": cfg,
        }
        args["sandbox"] = "read-only" if text else "workspace-write"
        res = self.rpc.call("thread/resume", args)
        self.s.audit(
            key,
            "executor/subscribed",
            {
                "thread_status": res["thread"].get("status"),
                "sandbox": res.get("sandbox"),
                "activePermissionProfile": res.get("activePermissionProfile"),
            },
        )
        if text and res.get("sandbox", {}).get("type") != "readOnly":
            raise ValueError("text_sandbox_not_read_only")
        if not text:
            raise ValueError("scoped_runtime_not_yet_enabled")
        attempt = job["attempt"] + 1
        if attempt > MAX_RETRIES + 1:
            raise ValueError("retry_limit_reached")
        # This durable start intent precedes the wire call. Crash ambiguity never auto-replays.
        self.s.update(
            key,
            state="starting",
            attempt=attempt,
            started=time.time(),
            ended=None,
            result=None,
            turn_id=None,
        )
        self.s.audit(
            key,
            "executor/start_intent",
            {
                "attempt": attempt,
                "task": job["task"],
                "client_user_message_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, key + ":" + str(attempt))
                ),
            },
        )
        params = {
            "threadId": self.settings.session_id,
            "input": [{"type": "text", "text": TEXT_PROMPT}],
            "clientUserMessageId": str(uuid.uuid5(uuid.NAMESPACE_URL, key + ":" + str(attempt))),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "effort": "low",
        }
        r = self.rpc.call("turn/start", params)
        self.active_turn = r["turn"]["id"]
        self.s.update(key, state="running", turn_id=self.active_turn)
        self.s.audit(key, "executor/start_ack", r)
        self.drain(key)
        end = time.monotonic() + (TEXT_TIMEOUT_SECONDS if text else TURN_TIMEOUT_SECONDS)
        interrupted = False
        grace = 0
        while not self.done:
            if not interrupted and (
                self.s.job(key)["stop"]
                or time.monotonic() > end
                or (not text and self.budget()["remaining_gpu_hours"] <= 0)
            ):
                self.cancel(key, "operator_stop_or_timeout_or_budget")
                interrupted = True
                grace = time.monotonic() + 20
            if interrupted and time.monotonic() > grace:
                raise TimeoutError("interrupt_ack_without_terminal_event")
            try:
                self.rpc.receive_notification(0.5)
            except socket.timeout:
                pass
            self.drain(key)
        final = self.thread()
        result = {
            "turn": self.done,
            "model_reply": self.reply,
            "final_thread_status": final.get("status"),
            "tool_items_observed": self.side_effects,
            "acceptance_passed": text
            and self.done.get("status") == "completed"
            and self.reply.strip() == TEXT_REPLY
            and not self.side_effects
            and final.get("status") == {"type": "idle"},
        }
        self.s.update(
            key,
            state="completed" if result["acceptance_passed"] else "failed",
            ended=time.time(),
            result=result,
        )
        self.s.audit(key, "executor/job_terminal", result)

    def advisory(self):
        # An embedding application may supply its reviewed, fixed-scope delivery adapter.
        # No sys.path mutation, imported live deployment or old-process takeover occurs.
        if self.advisory_callback is not None:
            self.advisory_callback(self)

    def loop(self):
        self.s.meta(
            "actor",
            {
                "pid": os.getpid(),
                "started_at": now(),
                "principal": "unix_uid_" + str(self.settings.principal_uid),
                "session_id": self.settings.session_id,
            },
        )
        for j in self.s.jobs():
            if j["state"] in ["starting", "running"]:
                self.s.update(
                    j["id"],
                    state="recovery_required",
                    result={
                        "reason": "prior_start_may_have_been_delivered_no_blind_replay",
                        "turn_id": j["turn_id"],
                    },
                )
                self.s.audit(j["id"], "executor/recovery_required", {"turn_id": j["turn_id"]})
        last_advisory = 0
        while True:
            pending = next((j for j in self.s.jobs() if j["state"] == "queued"), None)
            if pending:
                try:
                    self.run(pending)
                except Exception as e:
                    info = {"error_class": type(e).__name__, "reason": str(e)}
                    if isinstance(e, RPCError):
                        info["official_error"] = e.error
                    self.s.audit(pending["id"], "executor/job_error", info)
                    self.s.update(
                        pending["id"],
                        state="blocked" if not self.active_turn else "recovery_required",
                        ended=time.time(),
                        result=info,
                    )
                finally:
                    if self.rpc:
                        self.rpc.close()
                        self.rpc = None
                    self.current = None
                    self.active_turn = None
            elif time.monotonic() - last_advisory > 10:
                try:
                    self.advisory()
                except Exception as e:
                    self.s.audit(
                        None, "executor/advisory_unavailable", {"error_class": type(e).__name__}
                    )
                    if self.rpc:
                        self.rpc.close()
                        self.rpc = None
                finally:
                    last_advisory = time.monotonic()
            time.sleep(0.5)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        pid, uid, gid = struct.unpack(
            "3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid != self.server.settings.principal_uid:
            self.wfile.write(b'{"error":"unauthorized_unix_principal"}\n')
            return
        try:
            raw = self.rfile.readline(8193)
            if len(raw) > 8192:
                raise ValueError("request_too_large")
            d = json.loads(raw)
            op = d.get("operation")
            s = self.server.state
            if op == "submit" and set(d) == {"operation", "request_id", "task"}:
                r = s.submit(d["request_id"], d["task"])
            elif op == "retry_text" and set(d) == {"operation", "request_id"}:
                r = s.retry_text(d["request_id"])
            elif op == "status" and set(d) == {"operation"}:
                r = {
                    "actor": s.meta("actor"),
                    "jobs": s.jobs(),
                    "budget": self.server.actor.budget(),
                    "scope": "configured_fixed_session_only",
                }
            elif op == "events" and set(d) <= {"operation", "request_id", "after"}:
                r = s.events(d["request_id"], int(d.get("after", 0)))
            elif op == "stop" and set(d) == {"operation", "request_id"}:
                s.update(d["request_id"], stop=1)
                s.audit(
                    d["request_id"],
                    "executor/operator_stop",
                    {"principal_uid": uid, "caller_pid": pid},
                )
                r = s.job(d["request_id"])
            else:
                raise ValueError("operation_not_allowed")
            s.audit(
                d.get("request_id"),
                "executor/authenticated_operation",
                {"operation": op, "principal_uid": uid, "caller_pid": pid},
            )
            self.wfile.write((json.dumps(r, ensure_ascii=True) + "\n").encode())
        except Exception as e:
            self.wfile.write(
                (json.dumps({"error_class": type(e).__name__, "reason": str(e)}) + "\n").encode()
            )


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve(settings=None, root=None, advisory_callback=None):
    settings = settings if settings is not None else load_settings()
    if not hasattr(socket, "SO_PEERCRED"):
        raise ValueError("linux_peer_credentials_required")
    if os.getuid() != settings.principal_uid:
        raise ValueError("executor_owner_uid_mismatch")
    root = Path(root) if root is not None else data_dir()
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("unsafe_state_path")
    lock = os.open(root / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = State(root)
    actor = Actor(state, settings, advisory_callback=advisory_callback)
    sock = root / "executor.sock"
    if sock.exists() or sock.is_symlink():
        st = sock.lstat()
        if not __import__("stat").S_ISSOCK(st.st_mode):
            raise ValueError("unsafe_socket_path")
        sock.unlink()
    server = Server(str(sock), Handler)
    os.chmod(sock, 0o600)
    server.state = state
    server.actor = actor
    server.settings = settings
    # Keep the exclusive descriptor alive for the whole server lifetime.
    server.owner_lock = lock
    threading.Thread(target=actor.loop, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    serve()
