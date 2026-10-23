"""Local operator-only preflight. Every trigger attempt fails closed.

No listener, MCP registration, free-text input, or enable/force flag is provided.
"""

import argparse
import json
import os
from pathlib import Path

from .policy import REQUEST_ID, data_dir, execution_gate, load_settings, spec
from .events import now
from .store import Store


def observe(settings=None):
    from .wire import ObservationClient

    settings = settings if settings is not None else load_settings()
    session_id = settings.session_id
    client = ObservationClient(settings)
    try:
        thread = client.call("thread/read", {"threadId": session_id, "includeTurns": False})[
            "thread"
        ]
        loaded = client.call("thread/loaded/list", {}).get("data", [])
        if thread.get("id") != session_id:
            raise ValueError("thread_binding_mismatch")
        result = {
            "observed_at": now(),
            "server_peer_pid": client.peer_pid,
            "thread_id": session_id,
            "thread_status": thread.get("status"),
            "thread_loaded": session_id in loaded,
            "active_turn_absent_at_snapshot": thread.get("status") == {"type": "idle"},
            "atomic_exclusive_start_verified": False,
            "rpc_methods_sent": list(client.methods_sent),
        }
    finally:
        client.close()
    proc = Path("/proc")
    if not proc.is_dir():
        raise ValueError("linux_process_inspection_required")
    holders = []
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            kinds = []
            for descriptor in (process / "fd").iterdir():
                try:
                    target = os.readlink(descriptor)
                    if session_id in target:
                        kinds.append("lock" if "lock" in target.lower() else "history")
                except OSError:
                    pass
            if kinds:
                holders.append({"pid": int(process.name), "kinds": sorted(set(kinds))})
        except OSError:
            pass
    result["session_holders"] = holders
    result["no_second_session_holder_observed"] = all(
        row["pid"] == client.peer_pid for row in holders
    )
    result["no_other_executors_guaranteed"] = False
    return result


def prepare(store, observer=None):
    fresh = store.prepare()
    store.audit("prepared", {"new_request": fresh, "execution_enabled": False})
    try:
        if observer is None:
            settings = load_settings()
            if settings.session_id != store.session_id:
                raise ValueError("preflight_binding_mismatch")
            observer = lambda: observe(settings)
        evidence = observer()
        if (
            not isinstance(evidence, dict)
            or evidence.get("thread_id") != store.session_id
            or type(evidence.get("active_turn_absent_at_snapshot")) is not bool
        ):
            raise ValueError("preflight_binding_or_status_invalid")
        store.write_preflight(evidence)
    except Exception as exc:
        store.audit(
            "preflight_failed", {"error_class": type(exc).__name__, "execution_enabled": False}
        )
        raise
    store.audit(
        "preflight_complete",
        {
            "idle_at_snapshot": evidence["active_turn_absent_at_snapshot"],
            "execution_enabled": False,
        },
    )
    return status(store)


def trigger(store):
    # Before any connection, claim, prompt submission or event subscription.
    gate = execution_gate()
    denial = {
        "request_id": REQUEST_ID,
        "spec_sha256": spec(store.session_id)["spec_sha256"],
        "decision": gate["decision"],
        "barriers": gate["barriers"],
        "turn_start_sent": False,
    }
    fresh = store.create("trigger-denial.json", denial)
    if fresh:
        store.audit("trigger_denied", {"decision": gate["decision"], "turn_start_sent": False})
    return {**denial, "duplicate_denial": not fresh, "execution_claim_created": False}


def status(store):
    request = store.read("request.json")
    if request is not None and request != spec(store.session_id):
        raise ValueError("request_integrity_failed")
    audit = store.read_audit()
    return {
        "request_id": REQUEST_ID,
        "spec_sha256": spec(store.session_id)["spec_sha256"],
        "prepared": request is not None,
        "gate": execution_gate(),
        "preflight": store.read("preflight.json"),
        "technical_readiness": False,
        "audit_records": len(audit),
        "audit_chain_valid": True,
        "turn_id": None,
        "turn_started_at": None,
        "turn_ended_at": None,
        "model_reply": None,
        "model_turn_started": False,
        "live_event_subscription_opened": False,
        "lifecycle_events": [],
        "pending_authorization_is_not_technical_readiness": True,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Isolated fixed-session acceptance; execution DISABLED"
    )
    parser.add_argument("operation", choices=["prepare", "status", "events", "trigger"])
    args = parser.parse_args()
    try:
        settings = load_settings()
        store = Store(data_dir(), settings.session_id)
        if args.operation == "prepare":
            result = prepare(store, lambda: observe(settings))
        elif args.operation == "trigger":
            result = trigger(store)
        elif args.operation == "events":
            result = {
                "request_id": REQUEST_ID,
                "live_lifecycle_events": [],
                "live_subscription_opened": False,
                "audit": store.read_audit(),
                "gate": execution_gate(),
            }
        else:
            result = status(store)
        print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "available": False,
                    "error_class": type(exc).__name__,
                    "reason": "acceptance_disabled_or_integrity_review_required",
                    "model_turn_started": False,
                }
            )
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
