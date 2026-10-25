# Standard SSH deployment / 标准 SSH 部署

This route connects the MCP adapter directly to a POSIX research host through **system OpenSSH**. It requires neither SCO nor Windows. An H100, A100 or another GPU does not change this transport. The target must offer SSH access and a compatible existing Codex control socket; this package does not provision cloud machines or GPU drivers.

```text
MCP client → research-hub stdio → OpenSSH → POSIX host
  → private mailbox → advisory worker → fixed Codex thread context
```

## Prerequisites

- Local Python 3.11+, `uv`, and OpenSSH (`ssh` in PATH). macOS/Linux clients are covered by this implementation; Windows OpenSSH is supported by the command construction but needs separate live acceptance.
- An operator-owned POSIX target with Python 3.11+ **and venv/ensurepip**. Linux distributions may require their `python3-venv` package. No root access is needed for Hub installation. The parent of the chosen remote root must already exist. Paths must be absolute and canonical; resolve symlinked paths first.
- A working SSH config alias, e.g. `gpu-lab`, with an independently verified host key already in `known_hosts`, and key/agent authentication. Password prompts and first-use host-key prompts fail closed. Do not put keys or passwords in Hub JSON. `IdentityFile`, `Port`, `ProxyJump` and hostnames/IPs belong in your existing SSH config; `ssh_host` accepts a simple alias only. Optional `ssh_config` selects an absolute local config file. SSH agent use is local; forwarding is disabled.
- A running Codex App Server exposing its Unix control WebSocket and experimental `thread/inject_items`, plus an **existing persisted thread** accessible to the SSH account. Socket availability is a capability requirement; merely having the Codex CLI installed is insufficient. Default socket: `codex_home/app-server-control/app-server-control.sock`; an operator may set `binding.socket_path`.
- Use a **dedicated advisory thread**, not the thread actively controlling experiments: starting this worker calls `thread/resume` with read-only sandbox, no network, and tools disabled. It never calls `turn/start`. An empty thread without a persisted rollout may fail to resume.

## Install and check

From the repository root:

```sh
uv sync --locked --no-editable
uv build --wheel
```

Copy [the example](../examples/ssh-deployment.example.json) **outside the repository** as `ssh-deployment.private.json` and replace every placeholder. Set the remote root and its three derived paths consistently. `expected_remote_user` is the actual OS login name. If `python3` is too old, set `bootstrap_python` to an absolute Python 3.11+ executable.

Select an independent local state directory so existing deployments are not overwritten. This environment variable must also be present in the eventual MCP server process:

```sh
export RESEARCH_HUB_STATE_DIR="$HOME/.local/state/research-hub-ssh"
uv run --no-editable research-hub ssh deploy \
  --config /absolute/path/ssh-deployment.private.json \
  --wheel dist/autoresearch_hub-0.1.0-py3-none-any.whl
uv run --no-editable research-hub ssh doctor \
  --config /absolute/path/ssh-deployment.private.json
```

Deployment uploads the exact wheel over SSH stdin, checks its SHA-256, creates an isolated remote venv, installs **without dependencies or a package index**, creates private mailbox directories/binding, and writes local `remote.json`. Remote endpoint/probe/worker modules use only the standard library; the local MCP adapter has the normal project dependencies. No worker starts during installation. Doctor is read-only and initially reports `worker_ready=false` with a nonzero exit status.

The chosen remote directory must be new or contain the matching deployment marker. An identical deployment can be retried. A different wheel, binding, reviewed progress config, or conflicting local `remote.json` is rejected; use another state directory/root for a changed deployment. This command is not an in-place upgrade/migration tool and does not remove old state.

Explicitly start the worker, then check again:

```sh
uv run --no-editable research-hub ssh worker-start \
  --config /absolute/path/ssh-deployment.private.json
uv run --no-editable research-hub ssh doctor \
  --config /absolute/path/ssh-deployment.private.json
uv run --no-editable research-hub stdio
```

Alternatively add `--start-worker` to `ssh deploy`. A successful start is separate from doctor readiness: the worker can remain alive retrying an unavailable socket. Doctor checks the expected OS account, Python, fixed thread ID/directory, and a recent worker heartbeat. Startup is detached and guarded by the existing worker lock. It survives SSH disconnects, but **does not install reboot persistence or automatic process restart**. After a reboot, restart Codex and run `worker-start`; use your own service manager if required. The state file records its PID for operator-managed shutdown; do not kill unrelated Codex processes.

For ChatGPT/cloud use, separately configure the supported MCP connection/Tunnel. This SSH deployment does not automatically create that connection or expose a public listener.

## Reviewed progress and task listing

Pass `--progress-config /absolute/path/progress.private.json` to `ssh deploy` to validate and install the same reviewed `progress.json` locally and remotely. See the exact schema and manifest format in [guidance/progress](components-guidance.md). This option **does not generate a manifest** or convert SSH reachability into scientific evidence. Without reviewed configuration, `get_research_progress` reports unavailable. Keep source timestamps fresh; changed stage/status values require review under the existing projection rules.

`list_research_tasks` remains an SCO ACP platform collector. On an SSH deployment it returns `available=false`, `tasks=[]`, `coverage=not_applicable`, and `reason=platform_task_listing_not_supported`. It does not invent a RUNNING task or inspect arbitrary processes/GPU jobs. Guidance submit/status and reviewed progress use the new SSH route.

## Acceptance and failure checks

After doctor passes, submit one harmless advisory via MCP using a unique request ID, query its receipt, and submit the **same ID/text** again to check deduplication. Expect `received_in_codex_context`, `codex_received=true`, `model_processing_started=false`, `execution_authorized=false`. This confirms context injection, not model execution. If delivery becomes uncertain, inspect it; do not resend with a new ID.

Missing host keys, changed keys, authentication failures, unexpected OS accounts, missing/old Python, wrong bindings and missing sockets must fail. Transport output and runtime are bounded; raw SSH stderr, paths and remote exceptions are not exposed through MCP. Operator CLI errors are also redacted. A failed installation can leave its private root for inspection/retry; no rollback is claimed.

To reproduce the real loopback SSH integration test on a POSIX host with an executable `sshd`:

```sh
uv build --wheel
RESEARCH_HUB_TEST_SSH=1 uv run --no-editable pytest tests/test_ssh_live.py -q -s
```

It creates ephemeral keys and a loopback-only SSH daemon, installs the wheel into an isolated venv, and tests doctor, mailbox submit/status/dedup, reviewed manifest hash/projection and MCP stdio. **Codex RPC is simulated in this test.** It starts no model turn or GPU experiment. See [recorded scope](ssh-validation-2026-10-26.md). Linux may need its normal sshd privilege-separation directory prepared by the system administrator. The default test suite skips this opt-in test.

## 中文说明

这条新增路径是 **MCP 适配器 → 标准 OpenSSH → POSIX 服务器 → 收件箱／worker → 固定 Codex 会话上下文**，不依赖 SCO，也不要求 Windows 中转。GPU 是 H100 还是其他型号，不影响 SSH 传输。服务器必须已有可用的 SSH 和兼容的 Codex 控制 socket；安装器不负责创建云机器、安装驱动或给 Codex 新增不支持的接口。

先满足上面的前提：本地安装 Python 3.11+、uv、OpenSSH；远端有 Python 3.11+ 和 venv/ensurepip；SSH 别名、密钥／agent 和经过核实的 known_hosts 已配置。Hub 配置只写别名和路径，不写密码、私钥或 API key。可在 SSH 配置里使用 ProxyJump；不需要把服务器端口公开到公网。

把示例复制到仓库外，修改用户名、路径和**已有且已持久化的会话 ID**。配置独立的 `RESEARCH_HUB_STATE_DIR` 后，按上面的 `deploy → doctor → worker-start → doctor` 执行；也可在 deploy 时明确加 `--start-worker`。安装器上传 wheel、校验哈希、创建远端独立环境与私有收件箱，并生成本地 remote.json。重试相同部署可以，覆盖不匹配的已有部署会被拒绝；升级请使用新的目录。本地的 MCP 服务进程也必须使用同一个状态目录。

**worker 应绑定专门接收建议的会话。** 启动时会将它以只读、无网络、禁用工具的设置恢复，不适合直接绑定正在执行研究任务的会话。它只注入上下文，不启动模型回合。worker 脱离 SSH 连接运行，但目前没有安装开机自启；机器重启后需要恢复 Codex 并重新启动 worker。ChatGPT 的 MCP／Tunnel 入口仍需另行配置。

进度查询可通过 `--progress-config` 同时部署经过审核的配置；它不会替你生成科研记录。SSH 模式下，平台任务列表明确返回不支持，不能把“连得上服务器”当作“科研任务正在运行”。新路径已通过真实本机 SSH、独立包安装、建议投递去重和 MCP 查询测试；其中 Codex 协议端使用测试替身，尚不能据此声称已经验收所有云平台、真实 H100 或新的真实 Codex 部署。
