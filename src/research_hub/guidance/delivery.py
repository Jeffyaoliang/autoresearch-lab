"""Claim before injection; an uncertain RPC is never automatically retried."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from .queue import GuidanceQueue, sync_directory
from .rpc import validate_binding


def now():
    return datetime.now(timezone.utc).isoformat()


def create_once(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(path.parent)


class Delivery:
    def __init__(self, root, proxy, binding):
        self.root = Path(root)
        self.binding = validate_binding(binding)
        self.queue = GuidanceQueue(self.root / "inbox", self.binding["session_alias"])
        self.proxy = proxy
        for name in ["claims", "receipts"]:
            path = self.root / name
            if not path.is_dir() or path.is_symlink():
                raise ValueError("unsafe_delivery_root")
        # An alias cannot silently be reassigned while old requests/receipts remain.
        target = {
            key: self.binding[key] for key in ("session_alias", "session_id", "cwd", "codex_home")
        }
        target["socket_path"] = self.binding.get(
            "socket_path",
            self.binding["codex_home"] + "/app-server-control/app-server-control.sock",
        )
        identity = {
            "binding_sha256": hashlib.sha256(
                json.dumps(target, sort_keys=True).encode()
            ).hexdigest()
        }
        path = self.root / "delivery-binding.json"
        try:
            create_once(path, identity)
        except FileExistsError:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 1024
                or json.loads(path.read_text()) != identity
            ):
                raise ValueError("persisted_binding_mismatch")

    def status(self, key):
        result = self.queue.status(key)
        path = self.root / "receipts" / (key + ".json")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
                raise ValueError("unsafe_receipt")
            data = json.loads(path.read_text())
            fields = {
                "request_id",
                "binding",
                "message_sha256",
                "state",
                "received_at",
                "codex_received",
                "model_processing_started",
                "execution_authorized",
                "receipt_kind",
            }
            if (
                not isinstance(data, dict)
                or set(data) != fields
                or data.get("request_id") != key
                or data.get("message_sha256") != result["message_sha256"]
                or data.get("binding") != self.binding["session_alias"]
                or data.get("state") != "received_in_codex_context"
                or data.get("codex_received") is not True
                or data.get("model_processing_started") is not False
                or data.get("execution_authorized") is not False
                or data.get("receipt_kind") != "official_thread_inject_items_ack"
                or datetime.fromisoformat(data["received_at"]).tzinfo is None
            ):
                raise ValueError("invalid_receipt")
            result.update(data)
        elif (self.root / "claims" / (key + ".json")).exists() or (
            self.root / "claims" / (key + ".json")
        ).is_symlink():
            result.update(
                state="delivery_pending_or_uncertain_do_not_resubmit", codex_received=False
            )
        else:
            result["state"] = "queued_for_bound_codex"
        result.update(model_processing_started=False, execution_authorized=False)
        return result

    def deliver(self, key):
        old = self.status(key)
        if old["state"] != "queued_for_bound_codex":
            return old
        record = self.queue._read(self.queue._path(key))
        try:
            create_once(
                self.root / "claims" / (key + ".json"),
                {
                    "request_id": key,
                    "message_sha256": record["message_sha256"],
                    "claimed_at": now(),
                },
            )
        except FileExistsError:
            return self.status(key)
        text = (
            "Research Hub advisory context. No execution authorization.\nRequest ID: "
            + key
            + "\n"
            + record["message"]
        )
        ack = self.proxy.call(
            "thread/inject_items",
            {
                "threadId": self.binding["session_id"],
                "items": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    }
                ],
            },
        )
        if not isinstance(ack, dict):
            raise ValueError("ambiguous_delivery_ack")
        create_once(
            self.root / "receipts" / (key + ".json"),
            {
                "request_id": key,
                "binding": self.binding["session_alias"],
                "message_sha256": record["message_sha256"],
                "state": "received_in_codex_context",
                "received_at": now(),
                "codex_received": True,
                "model_processing_started": False,
                "execution_authorized": False,
                "receipt_kind": "official_thread_inject_items_ack",
            },
        )
        return self.status(key)
