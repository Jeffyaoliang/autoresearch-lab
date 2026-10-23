"""Private singleton request, denial and hash-chained audit; no execution queue."""

from pathlib import Path
from hashlib import sha256
import os, json, fcntl
from .policy import spec, REQUEST_ID, data_dir, load_settings
from .events import now


def encoded(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


class Store:
    def __init__(self, root=None, session_id=None):
        self.session_id = session_id if session_id is not None else load_settings().session_id
        spec(self.session_id)
        self.root = Path(root) if root is not None else data_dir()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir() or self.root.is_symlink() or self.root.resolve() != self.root:
            raise ValueError("unsafe_store_root")

    def read(self, name):
        if name not in ["request.json", "trigger-denial.json", "preflight.json"]:
            raise ValueError("file_not_allowed")
        p = self.root / name
        if not p.exists() and not p.is_symlink():
            return None
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            raw = f.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversize_record")
        return json.loads(raw)

    def create(self, name, data):
        if name not in ["request.json", "trigger-denial.json"]:
            raise ValueError("file_not_allowed")
        try:
            fd = os.open(
                self.root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            if self.read(name) != data:
                raise ValueError("idempotency_conflict")
            return False
        with os.fdopen(fd, "wb") as f:
            f.write(encoded(data))
            f.flush()
            os.fsync(f.fileno())
        self.sync()
        return True

    def sync(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def prepare(self):
        return self.create("request.json", spec(self.session_id))

    def write_preflight(self, data):
        p = self.root / "preflight.json"
        tmp = self.root / "preflight.tmp"
        if p.is_symlink():
            raise ValueError("unsafe_preflight")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(encoded(data))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        self.sync()

    def validate_audit(self, raw):
        if len(raw) > 1024 * 1024:
            raise ValueError("audit_limit")
        prev = "0" * 64
        seq = 0
        rows = []
        for line in raw.splitlines():
            row = json.loads(line)
            digest = row.pop("record_sha256")
            if (
                row.get("sequence") != seq + 1
                or row.get("previous_sha256") != prev
                or sha256(encoded(row)).hexdigest() != digest
            ):
                raise ValueError("audit_chain_invalid")
            row["record_sha256"] = digest
            rows.append(row)
            seq += 1
            prev = digest
        return rows

    def audit(self, event, payload):
        if event not in ["prepared", "preflight_complete", "preflight_failed", "trigger_denied"]:
            raise ValueError("audit_event_not_allowed")
        fd = os.open(
            self.root / "audit.jsonl", os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "r+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            raw = f.read(1024 * 1024 + 1)
            rows = self.validate_audit(raw)
            row = {
                "sequence": len(rows) + 1,
                "previous_sha256": rows[-1]["record_sha256"] if rows else "0" * 64,
                "request_id": REQUEST_ID,
                "observed_at": now(),
                "event": event,
                "payload": payload,
            }
            row["record_sha256"] = sha256(encoded(row)).hexdigest()
            f.write(encoded(row) + b"\n")
            f.flush()
            os.fsync(f.fileno())
            return row

    def read_audit(self):
        try:
            fd = os.open(self.root / "audit.jsonl", os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as f:
            return self.validate_audit(f.read(1024 * 1024 + 1))


class OnceClaim:
    """Future execution reservation primitive. Never called by this disabled CLI.
    A successful claim is not delivery. After a crash it MUST NOT be auto-retried.
    """

    def __init__(self, root, session_id):
        self.root = Path(root)
        self.session_id = session_id
        spec(self.session_id)

    def reserve(self):
        try:
            fd = os.open(
                self.root / "execution.claim",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as f:
            f.write(
                encoded(
                    {"request_id": REQUEST_ID, "spec_sha256": spec(self.session_id)["spec_sha256"]}
                )
            )
            f.flush()
            os.fsync(f.fileno())
        Store(self.root, self.session_id).sync()
        return True
