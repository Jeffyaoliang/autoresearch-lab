"""Non-executing mailbox. Queue acceptance is not delivery to a model."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

SCHEMA = 1
POLICY = "analysis_advisory_only_v1"
SECRET = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{12,}|-----BEGIN [^-]*PRIVATE KEY-----|Bearer\s+\S+|(?:api[_ -]?key|access[_ -]?key|secret[_ -]?key|password)\s*[:=]\s*\S+|\b[A-F0-9]{32,}\b|https?://[^/\s]+@)"
)


def validate_message(message):
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > 2000
        or len(message.encode("utf-8")) > 8000
        or any(ord(c) < 32 and c not in "\n\t" for c in message)
    ):
        raise ValueError("invalid_message")
    if SECRET.search(message):
        raise ValueError("possible_secret_rejected")


def validate_alias(alias):
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", alias):
        raise ValueError("invalid_binding")
    return alias


def sync_directory(path):
    # Windows has no portable directory fsync. File content is still flushed.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class GuidanceQueue:
    def __init__(self, root, binding):
        self.root = Path(root)
        self.binding = validate_alias(binding)
        if not self.root.is_dir() or self.root.is_symlink() or self.root.resolve() != self.root:
            raise ValueError("unsafe_queue_root")

    def _path(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}", key):
            raise ValueError("invalid_request_id")
        return self.root / ("request_" + key + ".json")

    def _read(self, path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("invalid_record")
        raw = path.read_bytes()
        data = json.loads(raw)
        expected = {
            "schema_version",
            "request_id",
            "binding",
            "policy",
            "created_at",
            "message",
            "message_sha256",
            "state",
        }
        if (
            not isinstance(data, dict)
            or set(data) != expected
            or type(data["schema_version"]) is not int
            or data["schema_version"] != SCHEMA
            or data["binding"] != self.binding
            or data["policy"] != POLICY
            or data["state"] != "queued_waiting_for_codex_binding"
            or self._path(data["request_id"]) != path
        ):
            raise ValueError("invalid_record")
        validate_message(data["message"])
        if datetime.fromisoformat(data["created_at"]).tzinfo is None:
            raise ValueError("invalid_record_time")
        if hashlib.sha256(data["message"].encode()).hexdigest() != data["message_sha256"]:
            raise ValueError("record_hash_mismatch")
        if path.read_bytes() != raw:
            raise ValueError("record_changed")
        return data

    def _receipt(self, data, duplicate=False):
        return {
            "request_id": data["request_id"],
            "binding": self.binding,
            "policy": POLICY,
            "state": data["state"],
            "created_at": data["created_at"],
            "message_sha256": data["message_sha256"],
            "duplicate": duplicate,
            "codex_received": False,
            "execution_authorized": False,
        }

    def submit(self, request_id, message):
        path = self._path(request_id)
        validate_message(message)
        digest = hashlib.sha256(message.encode()).hexdigest()
        data = {
            "schema_version": SCHEMA,
            "request_id": request_id,
            "binding": self.binding,
            "policy": POLICY,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": message,
            "message_sha256": digest,
            "state": "queued_waiting_for_codex_binding",
        }
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
        except FileExistsError:
            old = self._read(path)
            if old["message_sha256"] != digest:
                raise ValueError("idempotency_conflict")
            return self._receipt(old, True)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(self.root)
        return self._receipt(self._read(path))

    def status(self, request_id):
        return self._receipt(self._read(self._path(request_id)))
