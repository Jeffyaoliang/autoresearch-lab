"""Container-local resident advisory worker. Never starts a model turn."""

import json
import os
import time

from .delivery import Delivery, now
from .rpc import Proxy, binding, guidance_dir


def resume(proxy, bound):
    config = {
        "features." + key: False
        for key in [
            "shell_tool",
            "unified_exec",
            "apps",
            "hooks",
            "multi_agent",
            "plugins",
            "remote_plugin",
            "code_mode",
            "code_mode_host",
        ]
    }
    config.update({"web_search": "disabled", "tools.view_image": False})
    result = proxy.call(
        "thread/resume",
        {
            "threadId": bound["session_id"],
            "excludeTurns": True,
            "cwd": bound["cwd"],
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "config": config,
        },
    )
    if result["thread"]["id"] != bound["session_id"]:
        raise ValueError("wrong_binding")
    if result.get("approvalPolicy") != "never" or result.get("sandbox") != {
        "type": "readOnly",
        "networkAccess": False,
    }:
        raise ValueError("permission_mismatch")
    return result


def write_state(root, alias, status, count):
    path, temporary = root / "worker-state.json", root / "worker-state.tmp"
    if temporary.is_symlink() or path.is_symlink():
        raise ValueError("unsafe_state")
    data = {
        "checked_at": now(),
        "status": status,
        "pid": os.getpid(),
        "binding": alias,
        "delivery_count_this_process": count,
        "model_processing_started": False,
        "execution_authorized": False,
    }
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main():
    if os.name == "nt":
        raise ValueError("guidance_worker_requires_unix")
    import fcntl

    root, bound = guidance_dir(), binding()
    lock = os.open(
        root / "worker.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        count = 0
        while True:
            proxy = None
            try:
                proxy = Proxy(bound)
                resume(proxy, bound)
                delivery, last = Delivery(root, proxy, bound), 0
                while True:
                    if time.monotonic() - last > 30:
                        proxy.call(
                            "thread/read", {"threadId": bound["session_id"], "includeTurns": False}
                        )
                        last = time.monotonic()
                    for path in sorted((root / "inbox").glob("request_*.json"))[:100]:
                        key = path.name[len("request_") : -5]
                        if delivery.status(key)["state"] == "queued_for_bound_codex":
                            if delivery.deliver(key)["codex_received"]:
                                count += 1
                    write_state(root, bound["session_alias"], "ready_waiting_for_guidance", count)
                    time.sleep(3)
            except Exception:
                write_state(
                    root, bound["session_alias"], "connection_or_delivery_requires_review", count
                )
                time.sleep(10)
            finally:
                if proxy:
                    proxy.close()
    finally:
        os.close(lock)


if __name__ == "__main__":
    main()
