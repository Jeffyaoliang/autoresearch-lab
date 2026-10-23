"""Explicit service entry points and a local, synthetic demonstration."""

from __future__ import annotations

import argparse
import json
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
    args = parser.parse_args(argv)
    if args.command == "demo":
        print(json.dumps(demo(), ensure_ascii=False, indent=2))
    elif args.command == "stdio":
        from .mcp_server import mcp

        mcp.run(transport="stdio")
    elif args.command == "http":
        from .bridge import main as serve_bridge

        serve_bridge()
    return 0
