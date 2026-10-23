"""Private durable requests, bounded redacted events, hash-chained audit."""

import sqlite3, threading, json, hashlib, re, os, time
from datetime import datetime, timezone
from pathlib import Path
from .policy import data_dir

SECRET = re.compile(
    r"(?i)sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)\s*[:=]\s*[^\s,;]+|-----BEGIN[^-]*PRIVATE KEY-----|https?://[^\s/]+@"
)


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(v, key=""):
    if re.search(
        r"(?i)authorization|credential|access.?token|refresh.?token|api.?key|httpHeaders|encrypted|developer.?instructions|system.?instructions",
        key,
    ):
        return "[REDACTED]"
    if isinstance(v, str):
        s = SECRET.sub("[REDACTED]", v)
        return (
            s
            if len(s) < 32768
            else {
                "prefix": s[:32768],
                "truncated_bytes": len(s.encode()),
                "sha256": hashlib.sha256(s.encode()).hexdigest(),
            }
        )
    if isinstance(v, dict):
        return {
            k: clean(x, k)
            for k, x in v.items()
            if not re.search(r"(?i)reasoning|encrypted|image|audio", k)
        }
    if isinstance(v, list):
        return [clean(x) for x in v[:200]]
    return v


class State:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else data_dir()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or (self.root / "executor.sqlite3").is_symlink():
            raise ValueError("unsafe_state_path")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.root / "executor.sqlite3"), check_same_thread=False)
        os.chmod(self.root / "executor.sqlite3", 0o600)
        self.db.execute("pragma journal_mode=WAL")
        self.db.execute("pragma synchronous=FULL")
        self.db.executescript(
            "create table if not exists jobs(id text primary key,task text,state text,attempt integer default 0,turn_id text,created text,started real,ended real,result text,stop integer default 0);create table if not exists events(seq integer primary key autoincrement,at text,job text,method text,body text,previous text,digest text);create table if not exists meta(k text primary key,v text);"
        )
        self.db.commit()

    def audit(self, job, method, body):
        body = json.dumps(clean(body), ensure_ascii=True, sort_keys=True)
        with self.lock:
            prev = self.db.execute("select digest from events order by seq desc limit 1").fetchone()
            prev = prev[0] if prev else "0" * 64
            at = now()
            digest = hashlib.sha256((prev + at + str(job) + method + body).encode()).hexdigest()
            self.db.execute(
                "insert into events(at,job,method,body,previous,digest) values(?,?,?,?,?,?)",
                (at, job, method, body, prev, digest),
            )
            self.db.commit()

    def meta(self, k, value=None):
        with self.lock:
            if value is not None:
                self.db.execute("insert or replace into meta values(?,?)", (k, json.dumps(value)))
                self.db.commit()
            row = self.db.execute("select v from meta where k=?", (k,)).fetchone()
            return json.loads(row[0]) if row else None

    def submit(self, key, task):
        if (
            not isinstance(key, str)
            or not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_-]{7,63}", key)
            or task not in ["text_acceptance", "rax_click_mouse"]
        ):
            raise ValueError("invalid_fixed_task_request")
        with self.lock:
            old = self.db.execute("select task from jobs where id=?", (key,)).fetchone()
            if old:
                if old[0] != task:
                    raise ValueError("idempotency_conflict")
                return self.job(key)
            self.db.execute(
                "insert into jobs(id,task,state,created) values(?,?,?,?)",
                (key, task, "queued", now()),
            )
            self.db.commit()
            self.audit(key, "executor/submitted", {"task": task})
            return self.job(key)

    def retry_text(self, key):
        from .policy import MAX_RETRIES

        with self.lock:
            j = self.job(key)
            result = j["result"] or {}
            if (
                j["task"] != "text_acceptance"
                or j["state"] != "failed"
                or j["stop"]
                or j["attempt"] >= MAX_RETRIES + 1
            ):
                raise ValueError("retry_not_allowed")
            if (
                result.get("tool_items_observed") is not False
                or result.get("final_thread_status") != {"type": "idle"}
                or result.get("turn", {}).get("status")
                not in ["interrupted", "failed", "completed"]
            ):
                raise ValueError("uncertain_effects_no_replay")
            self.audit(
                key,
                "executor/safe_text_retry",
                {
                    "previous_turn_id": j["turn_id"],
                    "previous_attempt": j["attempt"],
                    "no_tool_items": True,
                },
            )
            self.update(key, state="queued", turn_id=None, ended=None)
            return self.job(key)

    def update(self, key, **changes):
        if not set(changes) <= set(
            ["state", "attempt", "turn_id", "started", "ended", "result", "stop"]
        ):
            raise ValueError("invalid_state_update")
        with self.lock:
            self.db.execute(
                "update jobs set " + ",".join(k + "=?" for k in changes) + " where id=?",
                tuple(json.dumps(clean(v)) if k == "result" else v for k, v in changes.items())
                + (key,),
            )
            self.db.commit()

    def job(self, key):
        with self.lock:
            cur = self.db.execute("select * from jobs where id=?", (key,))
            r = cur.fetchone()
            if not r:
                raise ValueError("unknown_request_id")
            d = dict(zip([v[0] for v in cur.description], r))
            d["result"] = json.loads(d["result"]) if d["result"] else None
            return d

    def jobs(self):
        with self.lock:
            ids = [r[0] for r in self.db.execute("select id from jobs order by created")]
        return [self.job(k) for k in ids]

    def events(self, key, after=0):
        with self.lock:
            rows = self.db.execute(
                "select seq,at,method,body,digest from events where job=? and seq>? order by seq limit 100",
                (key, after),
            ).fetchall()
        return [
            {"seq": r[0], "at": r[1], "method": r[2], "body": json.loads(r[3]), "sha256": r[4]}
            for r in rows
        ]
