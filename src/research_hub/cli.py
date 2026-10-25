"""Explicit service entry points and a local, synthetic demonstration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from datetime import datetime, timezone


def demo() -> dict:
    """Run an in-memory fixture. No platform, credential, file, or model access."""
    from .mcp_server import project_snapshot

    clock = datetime.now(timezone.utc)
    fixture = {
        "schema_version": 1,
        "collector": "acp_read_only_status",
        "snapshot": {
            "available": True,
            "source": "SCO_ACP_READ_ONLY",
            "task_scope": "verified_user_name_prefix_and_user_id",
            "coverage": "first_page_only_max_20",
            "observed_at": clock.isoformat(),
            "tasks": [{"task_id": "task_0123456789ab", "state": "RUNNING"}],
        },
    }
    projected = project_snapshot(json.dumps(fixture).encode(), clock).model_dump()
    return {
        "mode": "synthetic_demo",
        "real_platform_queried": False,
        "model_turn_started": False,
        "experiment_executed": False,
        "projection": projected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research Hub components")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="project one synthetic task snapshot, entirely offline")
    sub.add_parser("stdio", help="serve the narrow MCP snapshot and advisory interface")
    sub.add_parser("http", help="serve the configured read-only collector on loopback")
    supervisor = sub.add_parser("supervise", help="supervise only the local read-only collector")
    supervisor.add_argument("--state-dir", help="explicit private collector state directory")
    tunnel = sub.add_parser(
        "tunnel", help="preflight or explicitly connect the official MCP tunnel"
    )
    tunnel.add_argument("--preflight", action="store_true")
    tunnel.add_argument("--prepare", action="store_true")
    tunnel.add_argument("--connect", action="store_true")
    sub.add_parser("ssh", help="deploy/check a standard OpenSSH target", add_help=False)
    incoming = list(argv) if argv is not None else __import__("sys").argv[1:]
    if incoming and incoming[0] == "ssh":
        from .deployment.ssh import main as ssh_main

        return ssh_main(incoming[1:])
    args = parser.parse_args(argv)
    if args.command == "demo":
        print(json.dumps(demo(), ensure_ascii=False, indent=2))
    elif args.command == "stdio":
        from .mcp_server import mcp

        mcp.run(transport="stdio")
    elif args.command == "http":
        from .bridge import main as serve_bridge

        serve_bridge()
    elif args.command == "supervise":
        from .deployment.supervisor import main as supervise

        if args.state_dir:
            os.environ["RESEARCH_HUB_STATE_DIR"] = str(Path(args.state_dir).expanduser().absolute())
        return supervise()
    elif args.command == "tunnel":
        from .deployment.tunnel import main as connect_tunnel

        options = [
            flag
            for flag, enabled in (
                ("--preflight", args.preflight),
                ("--prepare", args.prepare),
                ("--connect", args.connect),
            )
            if enabled
        ]
        return connect_tunnel(options)
    return 0
