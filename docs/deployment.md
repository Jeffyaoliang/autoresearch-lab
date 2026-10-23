# 现用部署链路的通用化脚本

这些脚本从现有 Research Agent Hub 部署整理而来，改用包入口和仓库外的显式配置。它们覆盖 Windows 采集守护、计划任务，以及 OpenAI 官方 Tunnel 客户端的安装和前台启动。安装脚本位于仓库和源码包的 `scripts/windows/`；Python 入口包含在 wheel 中。

## 三个独立的运行部分

1. Windows 计划任务启动 `research-hub supervise`，由它守护本机 `research-hub http`。Bridge 在 loopback 上运行并写入经过筛选的 `collector-state.json`。
2. 官方 `tunnel-client` 通过出站连接接收 ChatGPT 请求，再调用 `research-hub stdio`。这条路径不需要公开本机 HTTP 服务。指导投递、回执和研究记录查询都来自这个 stdio 适配器。
3. 服务器上的 `research_hub.guidance.worker` 将收件箱中的指导注入绑定的 Codex 会话。它需要独立安装和运行，停止 Windows Tunnel 不会停止这个 worker 或研究进程。

官方接入约定见 [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。需要已有 Tunnel ID、具有对应权限的运行密钥及 ChatGPT 工作区接入权限。下面的脚本不会创建云端 Tunnel、密钥或插件。

## Windows 安装

在仓库根目录的 PowerShell 中操作。需要已安装 `uv`；使用平台采集或 SCO 传输时，还需要现有的 SCO CLI 和本机登录状态。脚本不安装或修改平台账号。默认运行目录为 `%LOCALAPPDATA%/ResearchHub/runtime`，状态目录为 `%LOCALAPPDATA%/ResearchHub`，可通过参数指定另一套目录。

```powershell
# 按 uv.lock 安装包及 Windows 传输依赖，不启动任何服务。
.\scripts\windows\prepare-runtime.ps1

# 注册新的采集任务，生成本地令牌和默认关闭采集的 bridge.json。
# 不覆盖同名任务；此命令不立即启动任务。
.\scripts\windows\install-service.ps1
```

`prepare-runtime.ps1` 使用独立的 `UV_PROJECT_ENVIRONMENT` 和非 editable 安装，不把运行目录放进源码树。`install-service.ps1` 以当前用户注册启动和登录触发器，使用 Limited/S4U，并直接启动已安装的 Python 包；它没有隧道密钥，也不会启动远端实验。模板中的平台采集默认关闭。先在状态目录的 `bridge.json` 中填写自己的可执行文件、工作区和归属字段，再启用采集。已有配置和令牌会保留。

```powershell
# 配置完成后，显式启动本机采集任务。
Start-ScheduledTask -TaskName 'ResearchHub-Collector'

# 下载官方客户端，核验发布资产的 SHA-256，仅安装而不启动。
.\scripts\windows\prepare-tunnel.ps1
```

`prepare-tunnel.ps1` 默认解析官方最新发布，也支持 `-Version` 指定发布标签；官方资产没有 SHA-256 时，需要通过 `-ExpectedSha256` 提供已核验的摘要。已有客户端目录不会被覆盖。安装结果返回实际可执行文件路径；将其填入状态目录的 `tunnel.json`，模板见 [tunnel.example.json](../examples/tunnel.example.json)。`tunnel_id` 使用从 Platform 复制的完整值，`http_proxy` 可为 `null` 或显式代理地址。

如果使用非默认目录，对准备与启动脚本传入一致的 `-RuntimeDirectory`，对服务安装与启动脚本传入一致的 `-StateDirectory`。

## Tunnel 预检、配置和启动

```powershell
# 默认只预检：检查本地配置、客户端和采集快照；不读取密钥。
.\scripts\windows\Start-ChatGPTBridge.ps1

# 仅生成不含字面密钥的本地 profile，不启动云端连接。
.\scripts\windows\Start-ChatGPTBridge.ps1 -PrepareProfile

# 显式连接：使用进程环境中的运行密钥，或在本地终端隐藏输入。
.\scripts\windows\Start-ChatGPTBridge.ps1 -AuthorizeTunnel
```

Python 的对应入口是 `research-hub tunnel`、`research-hub tunnel --prepare` 与 `research-hub tunnel --connect`，运行前设置同一个 `RESEARCH_HUB_STATE_DIR`。默认预检和准备不调用控制平面；预检只读取本地配置、快照、包和实例状态，不发送 HTTP 请求。连接期间的健康检查只访问 loopback。采集来源缺失或过期时，不将其报告为可用。

连接时，运行密钥只通过 `CONTROL_PLANE_API_KEY` 进程环境引用交给官方客户端；也可由启动器在交互终端通过隐藏输入取得。命令行和生成的 profile 不含字面密钥，不把原始 Doctor 输出保存为日志。只有 Doctor 成功才进入 `run`。运行状态保存在私有状态目录，健康检查只使用 loopback，退出时清理启动器自己创建的客户端。保持窗口打开，Ctrl+C 结束这一条 Tunnel 连接。

单实例锁防止同一状态目录重复启动。客户端异常退出使用有限重试；本地采集守护则使用有上限的重启退避。它们不会从旧状态文件读取 PID 并接管或终止其他进程。

## 远端指导与验收

先在服务器安装相同版本的包，按照[指导与进度](components-guidance.md)配置远端私有收件箱、固定会话绑定和本地 `remote.json`，再显式启动：

```sh
export RESEARCH_HUB_STATE_DIR=/srv/example/state
python -m research_hub.guidance.worker
```

本机的包状态目录通过 `remote.json` 指向远端安装和工作目录。worker 需要自己的常驻进程；安装 Windows 采集任务不会自动安装远端 worker。`progress.json` 的审核字段和来源新鲜度也需要与远端一致。

最后在 ChatGPT 接入已有 Tunnel，核对四个工具：`list_research_tasks`、`get_research_progress`、`submit_research_guidance`、`get_research_guidance_status`。先读取状态，再用一个可识别的请求 ID 投递测试指导并查询回执。指导会写入绑定会话上下文，仍不调用 `turn/start` 或执行实验。

客户端健康、ready 和轮询活动分别是连接证据，不能代替 ChatGPT 实际工具调用，也不能证明科研执行成功。本仓库的自动验证使用假客户端、合成状态与本地进程夹具；没有将这次脚本整理部署到现有运行环境。
