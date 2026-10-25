"""Data-only submit/status boundary around the configured operator transport."""

import json
import os
import re
import signal
import subprocess
import sys
import threading

from research_hub.config import read_config
from .queue import POLICY, validate_alias, validate_message

LOCK = threading.Lock()
ENV_ALLOW = {
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "TEMP",
    "TMP",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "USERDOMAIN",
    "USERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "SSH_AUTH_SOCK",
    "LANG",
    "PYTHONPATH",
    "RESEARCH_HUB_STATE_DIR",
    "RESEARCH_HUB_GUIDANCE_DIR",
}
FIELDS = {
    "available",
    "reason",
    "request_id",
    "binding",
    "policy",
    "state",
    "created_at",
    "message_sha256",
    "duplicate",
    "codex_received",
    "execution_authorized",
    "model_processing_started",
    "received_at",
    "receipt_kind",
}
REASONS = {
    "invalid_request",
    "invalid_request_id",
    "invalid_message",
    "possible_secret_rejected",
    "idempotency_conflict",
    "pilot_queue_quota_reached",
    "operation_not_allowed",
    "guidance_unavailable_or_requires_review",
}


def validate_request(data):
    if not isinstance(data, dict) or data.get("operation") not in ["submit", "status"]:
        raise ValueError("invalid_request")
    keys = (
        {"operation", "request_id", "message"}
        if data["operation"] == "submit"
        else {"operation", "request_id"}
    )
    if (
        set(data) != keys
        or not isinstance(data["request_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}", data["request_id"])
    ):
        raise ValueError("invalid_request_id")
    if data["operation"] == "submit":
        validate_message(data["message"])


def project_result(data, key, expected_binding=None):
    if (
        not isinstance(data, dict)
        or not set(data) <= FIELDS
        or type(data.get("available")) is not bool
        or data.get("execution_authorized") is not False
        or data.get("model_processing_started") is not False
    ):
        raise ValueError("invalid_guidance_result")
    if not data["available"]:
        if data.get("reason") not in REASONS:
            raise ValueError("invalid_guidance_result")
        return data
    alias = validate_alias(
        expected_binding
        if expected_binding is not None
        else read_config("remote.json").get("session_alias")
    )
    if (
        data.get("request_id") != key
        or data.get("binding") != alias
        or data.get("policy") != POLICY
    ):
        raise ValueError("binding_mismatch")
    state = data.get("state")
    if state not in [
        "queued_for_bound_codex",
        "delivery_pending_or_uncertain_do_not_resubmit",
        "received_in_codex_context",
    ]:
        raise ValueError("invalid_guidance_state")
    if (
        data.get("codex_received") is not (state == "received_in_codex_context")
        or not isinstance(data.get("message_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", data["message_sha256"])
    ):
        raise ValueError("invalid_guidance_result")
    if state == "received_in_codex_context":
        if data.get("receipt_kind") != "official_thread_inject_items_ack":
            raise ValueError("missing_delivery_ack")
    elif data.get("receipt_kind") is not None:
        raise ValueError("unexpected_delivery_ack")
    return data


def subprocess_env():
    env = {key: value for key, value in os.environ.items() if key.upper() in ENV_ALLOW}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def stop_process(process):
    """Stop only the operator subprocess created by this boundary."""
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate(timeout=10)


def run_module(module, payload=None):
    process = subprocess.Popen(
        [sys.executable, "-B", "-m", module],
        env=subprocess_env(),
        stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    try:
        raw, _ = process.communicate(
            None if payload is None else json.dumps(payload, ensure_ascii=True).encode(), timeout=80
        )
        if process.returncode or len(raw) > 65536:
            raise ValueError("operator_reader_failed")
        return json.loads(raw)
    finally:
        stop_process(process)


def query_guidance(data):
    try:
        validate_request(data)
        alias = validate_alias(read_config("remote.json").get("session_alias"))
        with LOCK:
            output = run_module("research_hub.guidance.transport", data)
            return project_result(output["metadata"], data["request_id"], alias)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {
            "available": False,
            "reason": "guidance_unavailable_or_requires_review",
            "execution_authorized": False,
            "model_processing_started": False,
        }
