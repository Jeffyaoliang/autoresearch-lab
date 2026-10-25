"""Explicit Docker/ECS renewal operator CLI; importing starts no watcher."""

import argparse
import json
from pathlib import Path
import time

from .docker import Docker
from .restart import RestartGuard, read_private, write_private
from .renewal_guard import Rejected, digest

DEFAULT_POLICY = {
    "enabled": False,
    "allowed_exit_codes": [1, 137],
    "max_restarts": 3,
    "cooldown_seconds": 60,
}


def initialize(root, config, selector):
    # Pin the current daemon and full container ID before creating disabled policy/state.
    target = Docker(config).pin(selector)
    if root.resolve() != root:
        raise Rejected("unsafe_state_root")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_private(root / "target.private.json", target, exclusive=True)
    write_private(root / "policy.private.json", DEFAULT_POLICY, exclusive=True)
    write_private(
        root / "ledger.json",
        {"schema": 1, "target_sha256": digest(target), "records": {}},
        exclusive=True,
    )
    return {
        "status": "initialized",
        "policy_enabled": False,
        "backend": "docker",
        "start_dispatched": False,
    }


def initialize_aliyun(root, config):
    from datetime import datetime, timezone
    from .aliyun import Aliyun, DEFAULT_POLICY

    target, current = Aliyun(config).pin()
    if root.resolve() != root:
        raise Rejected("unsafe_state_root")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_private(root / "target.private.json", target, exclusive=True)
    write_private(root / "policy.private.json", DEFAULT_POLICY, exclusive=True)
    stopped = current["state"] == "Stopped"
    write_private(
        root / "ledger.json",
        {
            "schema": 1,
            "target_sha256": digest(target),
            "episode": 1 if stopped else 0,
            "stopped_since": datetime.now(timezone.utc).isoformat() if stopped else None,
            "records": {},
        },
        exclusive=True,
    )
    return {
        "status": "initialized",
        "policy_enabled": False,
        "backend": "aliyun-ecs",
        "start_dispatched": False,
    }


def build_guard(root, expected_backend=None):
    target = read_private(root / "target.private.json")
    if expected_backend is not None and target.get("backend") != expected_backend:
        raise Rejected("state_backend_mismatch")
    if target.get("backend") == "aliyun-ecs":
        from .aliyun import Aliyun, CloudGuard

        return CloudGuard(root, Aliyun(target), target)
    if target.get("backend") != "docker":
        raise Rejected("unsupported_restart_backend")
    return RestartGuard(root, Docker(target), target)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="research-hub renewal")
    parser.add_argument("backend", choices=["docker", "aliyun-ecs"])
    parser.add_argument("action", choices=["init", "check", "once", "watch"])
    parser.add_argument(
        "--state-dir",
        required=True,
        help="dedicated private state directory on the always-online controller host",
    )
    parser.add_argument("--region", help="ECS region, init only")
    parser.add_argument("--account-id", help="expected Alibaba account ID, init only")
    parser.add_argument("--instance-id", help="existing ECS instance ID, init only")
    parser.add_argument("--profile-name", help="existing aliyun CLI credential profile, init only")
    parser.add_argument("--aliyun-executable", help="absolute official aliyun CLI path, init only")
    parser.add_argument("--container", help="existing container name or ID, for init only")
    parser.add_argument("--docker-host", help="explicit unix:///path/to/docker.sock, for init only")
    parser.add_argument("--docker-executable", help="absolute Docker CLI path, for init only")
    parser.add_argument(
        "--apply", action="store_true", help="explicitly allow policy-eligible starts"
    )
    parser.add_argument("--interval", type=int, default=15, help="watch interval, 5–3600 seconds")
    parser.add_argument(
        "--max-seconds", type=int, default=3600, help="bounded watch duration, 1–86400 seconds"
    )
    args = parser.parse_args(argv)
    if not 5 <= args.interval <= 3600 or not 1 <= args.max_seconds <= 86400:
        parser.error("invalid watch interval/duration")
    if args.action in {"init", "check"} and args.apply:
        parser.error("--apply is only valid for once/watch")
    if args.action != "init" and any(
        [
            args.container,
            args.docker_host,
            args.docker_executable,
            args.region,
            args.account_id,
            args.instance_id,
            args.profile_name,
            args.aliyun_executable,
        ]
    ):
        parser.error("container/connection options are only valid for init")
    root = Path(args.state_dir).expanduser().absolute()
    try:
        if args.action == "init" and args.backend == "aliyun-ecs":
            if not all([args.region, args.account_id, args.instance_id, args.profile_name]) or any(
                [args.container, args.docker_host, args.docker_executable]
            ):
                parser.error(
                    "aliyun-ecs init requires region/account-id/instance-id/profile-name and no Docker options"
                )
            result = initialize_aliyun(
                root,
                {
                    "region": args.region,
                    "account_id": args.account_id,
                    "instance_id": args.instance_id,
                    "profile_name": args.profile_name,
                    "aliyun_executable": args.aliyun_executable,
                },
            )
        elif args.action == "init":
            if any(
                [
                    args.region,
                    args.account_id,
                    args.instance_id,
                    args.profile_name,
                    args.aliyun_executable,
                ]
            ):
                parser.error("Docker init does not accept Aliyun options")
            if not args.container or not args.docker_host:
                parser.error("init requires --container and --docker-host")
            result = initialize(
                root,
                {
                    "backend": "docker",
                    "docker_host": args.docker_host,
                    "docker_executable": args.docker_executable,
                },
                args.container,
            )
        elif args.action == "watch":
            deadline, last = time.monotonic() + args.max_seconds, None
            while True:
                result = build_guard(root, args.backend).run(apply=args.apply)
                if result != last:
                    print(json.dumps(result), flush=True)
                    last = result
                if result.get("reason") == "stop_flag_active":
                    return 0
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(args.interval, remaining))
            return (
                0  # Individual blocked/reconciliation states were emitted; no implicit retry write.
            )
        else:
            result = build_guard(root, args.backend).run(apply=args.apply)
        print(json.dumps(result, indent=2))
        return int(result["status"] in {"blocked", "reconciliation_required"})
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "renewal_setup_or_backend_unavailable"}))
        return 1
