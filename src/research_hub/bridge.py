"""Read-only Research Bridge. No shell, scheduling, GPU actions, or raw log export."""

from __future__ import annotations
import argparse, hashlib, hmac, json, math, os, re, subprocess, threading, time
from datetime import datetime, timezone
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
import uvicorn

from .config import state_dir, read_config

ROOT = state_dir()
CONFIG = ROOT / "bridge.json"
TOKEN = ROOT / "http-token.txt"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
METRICS = {
    "step",
    "epoch",
    "loss",
    "train_loss",
    "eval_loss",
    "reward",
    "success_rate",
    "accuracy",
    "episodes",
}
STATES = {
    "WAITING",
    "INIT",
    "QUEUEING",
    "CREATING",
    "STARTING",
    "RUNNING",
    "RESTARTING",
    "SUSPENDING",
    "SUSPENDED",
    "SUCCEEDED",
    "FAILED",
    "DELETING",
    "DELETED",
    "RECOVERING",
}
SENSITIVE = re.compile(
    r"password|secret|token|api[_-]?key|access[_-]?key|authorization|bearer|credential|private.?key|sk-[a-z0-9]|AKIA[A-Z0-9]|/mnt/|[A-Z]:\\|ignore.previous|system.prompt",
    re.I,
)
COLLECTOR_STATE = ROOT / "collector-state.json"
START = time.monotonic()
LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def config():
    try:
        return read_config("bridge.json")
    except ValueError:
        return {"sco_enabled": False, "sco_executable": "", "approved_reports": {}}


def error(code):
    return {"available": False, "reason": code, "observed_at": now(), "live_research_data": False}


def alias(raw):
    return "task_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def number(v):
    return type(v) in {int, float} and math.isfinite(v) and abs(v) < 1e15


def sanitize_metrics(values):
    if not isinstance(values, dict):
        return {}
    return {k: v for k, v in values.items() if k in METRICS and number(v)}


def metrics_from_log(text):
    """Only named finite numeric metrics from non-sensitive lines; never export text."""
    result = {}
    for line in text.splitlines()[-2000:]:
        if SENSITIVE.search(line):
            continue
        for key, value in re.findall(
            r"\b(step|epoch|loss|train_loss|eval_loss|reward|success_rate|accuracy|episodes)\s*[:=]\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b",
            line,
        ):
            num = float(value)
            if number(num):
                result[key] = num
    return result


def run_read(command, limit=262144):
    """Fixed commands only. Bound time and output, including stderr; never log either."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
        )
    except OSError:
        return None, "cli_unavailable"

    def stop_local_client_tree():
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()

    chunks = []
    size = 0
    overflow = threading.Event()

    def drain():
        nonlocal size
        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                overflow.set()
                stop_local_client_tree()
                break
            chunks.append(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        stop_local_client_tree()
        process.wait(timeout=5)
        reader.join(2)
        return None, "upstream_timeout"
    reader.join(2)
    if overflow.is_set() or reader.is_alive():
        return None, "upstream_response_too_large"
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if code:
        if "no active profile" in text.lower() or "sco init" in text.lower():
            return None, "authentication_required"
        return None, "upstream_read_failed"
    return text, None


def parse_jobs(payload, owner_prefix=None, owner_id=None):
    data = json.loads(payload)
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if isinstance(data, dict):
        data = next((data[k] for k in ["jobs", "items"] if isinstance(data.get(k), list)), None)
    if not isinstance(data, list):
        raise ValueError("Unverified upstream schema")
    output = []
    owner_mismatch = False
    for job in data[:20]:
        if not isinstance(job, dict):
            continue
        if owner_prefix or owner_id:
            owner = job.get("ownership")
            if not isinstance(owner, dict):
                raise ValueError("Missing ownership fields")
            if (owner_prefix and not str(owner.get("user_name", "")).startswith(owner_prefix)) or (
                owner_id and owner.get("user_id") != owner_id
            ):
                owner_mismatch = True
                continue
        raw = job.get("name") or job.get("id") or job.get("job_id")
        state = job.get("state")
        if not isinstance(raw, str) or not SAFE_ID.fullmatch(raw):
            continue
        if not isinstance(state, str) or state not in STATES:
            state = "UNKNOWN"
        output.append({"raw_id": raw, "task_id": alias(raw), "state": state})
    if owner_mismatch:
        raise ValueError("Upstream owner filter mismatch")
    if data and not output:
        raise ValueError("Unverified job schema")
    return output


def jobs_internal():
    cfg = config()
    workspace = cfg.get("workspace")
    if not cfg.get("sco_enabled"):
        return None, error("source_disabled")
    if not workspace:
        return None, error("authentication_and_workspace_not_configured")
    if not isinstance(workspace, str) or not SAFE_ID.fullmatch(workspace):
        return None, error("invalid_local_workspace_config")
    cli = cfg["sco_executable"]
    cmd = [
        cli,
        "acp",
        "jobs",
        "list",
        "--workspace-name=" + workspace,
        "--page-size=20",
        "--page-token=1",
        "--format=json",
    ]
    prefix = cfg.get("owner_user_name_prefix")
    owner_id = cfg.get("owner_user_id")
    if not prefix or not owner_id:
        return None, error("verified_owner_scope_required")
    if not SAFE_ID.fullmatch(prefix) or not SAFE_ID.fullmatch(owner_id):
        return None, error("invalid_local_owner_config")
    cmd.append("--user-name=" + prefix)
    text, fail = run_read(cmd)
    if fail:
        return None, error(fail)
    try:
        return parse_jobs(text, owner_prefix=prefix, owner_id=owner_id), None
    except (ValueError, TypeError):
        return None, error("upstream_schema_unverified")


def status():
    with LOCK:
        jobs, fail = jobs_internal()
        if fail:
            return fail
        return {
            "available": True,
            "live_research_data": True,
            "observed_at": now(),
            "source": "SCO_ACP_READ_ONLY",
            "coverage": "first_page_only_max_20",
            "task_scope": "verified_user_name_prefix_and_user_id",
            "tasks": [{k: v for k, v in j.items() if k != "raw_id"} for j in jobs],
        }


def task_progress(task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"task_[a-f0-9]{12}", task_id):
        return error("invalid_task_alias")
    with LOCK:
        jobs, fail = jobs_internal()
        if fail:
            return fail
        task = next((j for j in jobs if j["task_id"] == task_id), None)
        if task is None:
            return error("task_not_in_current_allowed_page")
        cfg = config()
        text, fail = run_read(
            [
                cfg["sco_executable"],
                "acp",
                "jobs",
                "stream-logs",
                "--workspace-name=" + cfg["workspace"],
                task["raw_id"],
                "--follow=false",
            ]
        )
        result = {
            "available": True,
            "live_research_data": True,
            "task_id": task_id,
            "state": task["state"],
            "observed_at": now(),
            "metrics": {},
            "metrics_provenance": "unavailable",
            "raw_logs_exported": False,
        }
        if fail:
            result["log_read_status"] = fail
        else:
            result.update(
                metrics=metrics_from_log(text),
                metrics_provenance="bounded_sco_log_read",
                log_read_status="read",
            )
        return result


def approved_report(report_id):
    if not isinstance(report_id, str) or not re.fullmatch(r"report_[a-z0-9_-]{1,48}", report_id):
        return error("invalid_report_alias")
    cfg = config()
    record = cfg.get("approved_reports", {}).get(report_id)
    if not isinstance(record, dict) or record.get("reviewed_for_export") is not True:
        return error("report_not_approved")
    # Remote AFS paths are never interpreted as Windows files.
    if record.get("source_kind") not in {"approved_json", "windows_approved_json"}:
        return error("remote_source_access_unverified")
    path = Path(record.get("path", ""))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        return error("approved_report_not_available")
    if path.stat().st_size > 262144:
        return error("approved_report_too_large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return error("approved_report_invalid")
    if not isinstance(data, dict):
        return error("approved_report_invalid")
    timestamp = data.get("observed_at")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        return error("report_observation_time_required")
    state = data.get("state")
    state = state if isinstance(state, str) and state in STATES else "UNKNOWN"
    return {
        "available": True,
        "live_research_data": False,
        "report_id": report_id,
        "source": "approved_json_snapshot",
        "observed_at": parsed.isoformat(),
        "fetched_at": now(),
        "state": state,
        "metrics": sanitize_metrics(data.get("metrics")),
        "raw_text_exported": False,
    }


def read_collector_state():
    try:
        data = json.loads(COLLECTOR_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def collect_once():
    """Collect sanitized platform metadata only. No task execution, raw logs or remote file reads."""
    previous = read_collector_state()
    result = status()
    attempt = now()
    record = {
        "schema_version": 1,
        "collector": "acp_read_only_status",
        "last_attempt_at": attempt,
        "poll_count": int(previous.get("poll_count", 0)) + 1,
        "last_success_at": attempt if result.get("available") else previous.get("last_success_at"),
        "consecutive_failures": 0
        if result.get("available")
        else int(previous.get("consecutive_failures", 0)) + 1,
        "research_control_access_verified": False,
        "snapshot": result,
    }
    temporary = ROOT / "collector-state.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=True, indent=2), encoding="utf-8")
    os.replace(temporary, COLLECTOR_STATE)
    return record


def collector_loop():
    while True:
        try:
            collect_once()
        except Exception:
            # Fail closed without logging private upstream exceptions.
            fallback = {
                "schema_version": 1,
                "collector": "acp_read_only_status",
                "last_attempt_at": now(),
                "snapshot": error("collector_internal_error"),
            }
            try:
                temporary = ROOT / "collector-state.tmp"
                temporary.write_text(json.dumps(fallback), encoding="utf-8")
                os.replace(temporary, COLLECTOR_STATE)
            except OSError:
                pass
        try:
            interval = max(30, min(int(config().get("collection_interval_seconds", 60)), 3600))
        except (OSError, ValueError, TypeError):
            interval = 60
        time.sleep(interval)


def health():
    cfg = config()
    collected = read_collector_state()
    snapshot = collected.get("snapshot", {})
    age = None
    try:
        age = max(
            0,
            (
                datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["observed_at"])
            ).total_seconds(),
        )
    except (ValueError, TypeError, KeyError):
        pass
    success = bool(snapshot.get("available")) and age is not None and age < 180
    return {
        "service": "research-bridge",
        "read_only": True,
        "uptime_seconds": round(time.monotonic() - START, 1),
        "observed_at": now(),
        "sco_installed": Path(cfg["sco_executable"]).is_file(),
        "workspace_configured": bool(cfg.get("workspace")),
        "owner_scope_configured": bool(cfg.get("owner_user_id")),
        "sco_authentication": "verified_by_recent_successful_read"
        if success
        else "not_currently_verified",
        "collector": {
            "poll_count": collected.get("poll_count", 0),
            "last_success_at": collected.get("last_success_at"),
            "consecutive_failures": collected.get("consecutive_failures", 0),
            "snapshot_age_seconds": round(age, 1) if age is not None else None,
            "fresh_source_snapshot": success,
        },
        "research_control": "remote_afs_path_known_access_unverified",
        "remote_afs_access_verified": False,
        "chatgpt_connection": "not_attested_by_bridge",
        "reports_approved": len(cfg.get("approved_reports", {})),
        "live_research_data": success,
        "platform_status_only": True,
    }


mcp = FastMCP(
    "Research Bridge", host="127.0.0.1", port=8765, json_response=True, stateless_http=True
)
READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


@mcp.tool(annotations=READ)
def get_bridge_health() -> dict:
    """Read connection facts. Service health never means the research source is connected."""
    return health()


@mcp.tool(annotations=READ)
def list_research_tasks() -> dict:
    """Read a bounded ACP task-status page. Task names, commands, users and paths are withheld."""
    return status()


@mcp.tool(annotations=READ)
def get_task_progress(task_id: str) -> dict:
    """Read status and allowlisted numeric metrics for a previously listed opaque task alias."""
    return task_progress(task_id)


@mcp.tool(annotations=READ)
def get_experiment_report(report_id: str) -> dict:
    """Read only explicitly approved structured report snapshots; never arbitrary paths or raw logs."""
    return approved_report(report_id)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    return JSONResponse(health())


class LoopbackBearer:
    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/healthz":
            headers = dict(scope.get("headers", []))
            value = headers.get(b"authorization", b"")
            if not hmac.compare_digest(value, b"Bearer " + self.token.encode()):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
            origin = headers.get(b"origin")
            if origin and origin not in {b"http://127.0.0.1:8765", b"http://localhost:8765"}:
                await JSONResponse({"error": "origin_not_allowed"}, status_code=403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def main():
    secret = TOKEN.read_text(encoding="ascii").strip()
    if len(secret) < 32:
        raise RuntimeError("Missing local authentication token")
    ROOT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=collector_loop, name="read-only-status-collector", daemon=True).start()
    uvicorn.run(
        LoopbackBearer(mcp.streamable_http_app(), secret),
        host="127.0.0.1",
        port=8765,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
