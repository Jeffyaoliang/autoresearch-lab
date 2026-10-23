"""Authenticated local Unix caller for fixed tasks and status/stop/events."""

import argparse
import json
from pathlib import Path
import socket

from .policy import data_dir


def request(data, root=None):
    root = data_dir() if root is None else Path(root)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(10)
        stream.connect(str(root / "executor.sock"))
        stream.sendall((json.dumps(data) + "\n").encode())
        with stream.makefile("rb") as source:
            raw = source.readline(2 * 1024 * 1024)
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation", choices=["status", "text", "recover", "stop", "events", "retry_text"]
    )
    parser.add_argument("--request-id")
    parser.add_argument("--after", type=int, default=0)
    args = parser.parse_args()
    if args.operation in ["text", "recover"]:
        if not args.request_id:
            parser.error("--request-id required")
        data = {
            "operation": "submit",
            "request_id": args.request_id,
            "task": "text_acceptance" if args.operation == "text" else "rax_click_mouse",
        }
    elif args.operation in ["stop", "events", "retry_text"]:
        if not args.request_id:
            parser.error("--request-id required")
        data = {"operation": args.operation, "request_id": args.request_id}
        if args.operation == "events":
            data["after"] = args.after
    else:
        data = {"operation": "status"}
    print(json.dumps(request(data), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
