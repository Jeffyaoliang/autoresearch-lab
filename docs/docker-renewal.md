# Docker container renewal / Docker 容器自动续起

This backend restarts **one existing, failed container on a live Docker host**. It does not depend on SCO, SSH, MCP or Codex. It runs on the Docker host and uses an explicit local Unix socket. It does not create a replacement container, provision GPU capacity, recover a powered-off host, renew a cloud quota/lease, or restore research from checkpoints.

Docker already offers [native restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/). If you only need ordinary automatic restart, use Docker's built-in policy. This guard adds a pinned daemon/container identity, a durable attempt ledger, an operator-selected exit-code list, cooldown and a lifetime attempt budget. It requires the container's native restart policy to be `no` and `AutoRemove=false`; it never silently changes either setting. Use one controller for this container.

## Install on the Linux host

Install this package and a working Docker CLI on the host with access to the chosen Docker Unix socket. The host and daemon must remain alive. For example, after installing with `uv sync --locked --no-editable`:

```sh
uv run --no-editable research-hub renewal docker init \
  --state-dir /home/researcher/.local/state/research-hub-docker \
  --docker-host unix:///var/run/docker.sock \
  --container research-container
```

`init` is read-only toward Docker: it resolves the operator's existing container name/ID, records its **full container ID and daemon ID**, hashes its image/configuration/mount profile, and creates a new private state directory. Environment values from Docker inspect are hashed and are not copied into configuration or printed. Existing state directories are refused, including partial initialization. Rootless Docker can use its explicit `unix:///run/user/1000/docker.sock` endpoint. Optional `--docker-executable` pins an absolute CLI path; otherwise the executable is resolved once during init and then saved.

The guard ignores inherited `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_API_VERSION` and other `DOCKER_*` variables. Each command explicitly addresses the saved Unix socket. It verifies the daemon ID, full container ID, name, image/config profile and absence of a competing native restart policy before restarting. Names are never resolved again for writes. A recreated container with the same name is a different target and needs a separately reviewed initialization. Merely losing a container does not authorize creating another.

The new `policy.private.json` starts **disabled**:

```json
{
  "enabled": false,
  "allowed_exit_codes": [1, 137],
  "max_restarts": 3,
  "cooldown_seconds": 60
}
```

Review the allowed nonzero exit codes and limits, then set `enabled` to `true`. Exit code zero is excluded. Code 137 can indicate OOM, a manual kill, or other causes; exit codes **do not prove why a container stopped**. To intentionally stop a controlled container, create its guard's `stop.flag` and stop the watcher/service before stopping the container. The guard cannot infer human intent or serialize arbitrary external Docker operators.

## Check, start once, or watch

```sh
# Read-only: no start, ledger write, lock creation, or service installation.
uv run --no-editable research-hub renewal docker check \
  --state-dir /home/researcher/.local/state/research-hub-docker

# Explicitly apply at most one eligible start, then independently inspect the result.
uv run --no-editable research-hub renewal docker once --apply \
  --state-dir /home/researcher/.local/state/research-hub-docker

# Poll every 15 seconds for up to one hour; writes require BOTH --apply and enabled policy.
uv run --no-editable research-hub renewal docker watch --apply \
  --state-dir /home/researcher/.local/state/research-hub-docker \
  --interval 15 --max-seconds 3600
```

The only mutating Docker command is `docker --host <fixed-socket> container start <full-id>`, which starts the existing container with its existing configuration and entrypoint. The guard does not call `exec`, `create`, `pull`, `update`, delete, or a model API. **The container's own entrypoint does run** and may execute whatever workload the operator configured.

Running/restarting containers are observed without another start. Paused, dead, removing or never-started containers are blocked. An exited container must have an allowed nonzero exit code, valid lifecycle timestamps, an elapsed cooldown, an enabled policy and remaining budget. Configuration/lifecycle is rechecked immediately before dispatch. Each attempted start consumes budget across watcher/process restarts, including uncertain requests; do not delete the ledger to bypass/recover its history. A missing/corrupt ledger fails closed.

## Confirmation, uncertainty and stopping

Before calling Docker, the guard fsyncs `DISPATCHING` intent under a local exclusive lock. After dispatch it records uncertainty, then checks for a later `StartedAt` against the pinned container identity. A successful CLI exit alone is insufficient. A new lifecycle that is running produces `accepted`; one that has already exited produces `restart_observed`, **not a healthy-container claim**. Neither result establishes application, GPU or research-task health.

A timeout/disconnect/error with no verified new lifecycle produces `reconciliation_required`. That lifecycle is never started again automatically, even after restarting the watcher. Later read-only observations can reconcile a new lifecycle. A crash after intent persistence but before the actual Docker command can therefore require operator reconciliation even if no start happened. Do not reset state or retry under a new state directory to hide uncertainty.

The state directory has `target.private.json`, `policy.private.json`, `ledger.json` and local lock/temp files. Create `stop.flag` there to block actions and make a watcher exit. Do not run two state directories/controllers for the same container: the local lock coordinates only one state directory, and Docker start has no distributed compare-and-swap contract. Docker processes have bounded stdout/stderr and a 30-second timeout; raw inspect/environment/error output is not returned through this CLI.

`watch` is a foreground, time-bounded process, not an installed service. The CLI does not change system services, Docker policy, SSH, firewall or reboot configuration. If you need continuous/reboot operation, run it under your existing service manager. Example **user systemd** unit, with paths/executable replaced for your installation:

```ini
[Unit]
Description=Research Hub fixed Docker container restart guard
ConditionPathExists=!/home/researcher/.local/state/research-hub-docker/stop.flag

[Service]
ExecStart=/home/researcher/autoresearch-lab/.venv/bin/research-hub renewal docker watch --apply --state-dir /home/researcher/.local/state/research-hub-docker --interval 15 --max-seconds 86400
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

Install/enable this unit yourself only after local acceptance. User service availability across logout/reboot depends on the host's existing user-service/linger configuration. `systemctl --user stop <unit>` stops the watcher; neither it nor `stop.flag` stops the research container. The daemon/container must already exist after reboot; this guard does not claim to recover an unavailable Docker host.

## Tests and backend boundary

`renewal.restart.Backend` defines `inspect() -> Container` and `start(expected_snapshot)`. `RestartGuard` handles durable intent, reconciliation, budget and policy; `renewal.docker.Docker` supplies Docker-specific identity checks and fixed commands. Existing SCO restart/copy guards retain their platform-specific lifecycle/expiry rules unchanged. A future provider needs its own identity mapping and actual API implementation; adding this interface is not provider support.

Offline tests cover eligibility, identity/config changes, cooldown/budget, intentional stop flag, process-crash intent, uncertain no-resend behavior, later reconciliation and private-output boundaries. Real Linux Docker acceptance runs separately in the `docker-renewal` CI job with an isolated BusyBox container: first exit 1, then restart the exact ID, independently observe Running, verify a single ledger record and stop-flag behavior, and remove only that test container. The development Mac has no Docker CLI/daemon, so local tests do not claim live Docker acceptance. Workflow logs are the source for that live result.

To reproduce real Docker acceptance, pre-pull the exact test image on an isolated Docker host, then:

```sh
docker --host unix:///var/run/docker.sock image pull busybox:1.37.0
RESEARCH_HUB_TEST_DOCKER=1 uv run --no-editable pytest -q -s tests/test_docker_live.py
```

## 中文说明

这次新增的是 **Linux／Docker 上已有容器的自动续起**，不依赖 SCO。它在宿主机运行，固定 Docker daemon 和完整容器 ID；选定的非零退出码、冷却时间和次数预算都符合时，启动同一个容器。启动前落盘记录，启动后重新查询确认。超时后不会盲目再发一次；记录和次数限制跨进程保留。

Docker 原生已有自动重启。如果只是需要一般故障重启，优先考虑原生策略；我们这层用于需要额外身份核验、记录和次数控制的情况。两种控制器不能同时接管同一容器，初始化时会拒绝原生自动重启或自动删除配置。

使用上面的 `init → 修改私有 policy 的 enabled 和退出码 → check → once --apply → watch --apply`。初始化不会启动容器，也不会覆盖已有状态目录。watch 默认前台运行一小时；持续运行和开机恢复需由已有服务管理器接管，文中给出了 systemd 用户服务示例。停止容器前先放置 `stop.flag` 并停止守护进程，因为退出码不能可靠区分故障与手动操作。

**它不能让已经关机的宿主机恢复，也不能代替云平台重新申请 GPU／创建到期后的下一代实例。** GPU 计算环境和实验检查点恢复也未包含在内。SCO 原来的实例续起／复制下一代逻辑继续保留；其他云平台仍需单独接入。Docker 启动会运行原容器 entrypoint，所以这里不把“容器重启”描述成“实验状态已恢复”或“模型尚未执行”。
