# 公开包真实链路验收（2026-10-24）

## 结论与范围

公开包在独立 Windows 环境和远端 Linux 环境完成了：真实任务状态采集 → MCP stdio 查询 → 建议入队 → Codex 官方上下文注入回执 → 同一请求去重。

这次测试使用独立的持久化测试会话和收件箱，没有向源研究会话投递测试指导，没有调用 `turn/start` 或启动科研实验。它验证的是接入与投递，不是自主科研效果。现有常驻部署没有切换到公开包。

初次云端调用返回 HTTP 429。当天后续恢复现有部署的 Tunnel 后，已连接插件的 `list_research_tasks` 返回 `available=true`、`reason=fresh_snapshot`，本机 health / ready 均为 HTTP 200。这证明现有部署的云端入口恢复；它使用原适配器，不能将此扩大为 ChatGPT → Tunnel → 独立公开包的云端验收。24 小时持续测试另行进行，完成前不计为通过。

## 真实检查

| 检查 | 结果 |
| --- | --- |
| Windows 独立环境按 lockfile 非 editable 安装，含 Windows 依赖 | 通过；Python 3.12.15 |
| 远端独立环境安装同一 wheel | 通过；使用已有 Python 3.14，未修改系统 Python |
| 公开包采集真实归属范围内的任务 | 通过；首批 20 项，来源时间经过新鲜度校验 |
| 真实 MCP stdio 初始化、工具列举与任务查询 | 通过 |
| 测试建议入队、官方上下文注入回执 | 通过；`received_in_codex_context` / `codex_received=true` |
| 相同 ID、相同内容再次提交 | 通过；`duplicate=true`，保留原始接收时间 |
| 独立核对官方线程和持久化记录 | 通过；测试消息出现一次，线程保持 idle |
| 测试 worker 清理 | 已停止本次创建的测试 worker |
| 模型与实验启动权限 | 回执均为 `model_processing_started=false`、`execution_authorized=false` |
| ChatGPT 云端插件调用 | 初次 HTTP 429；恢复现有部署后真实任务查询通过，未切换到公开包适配器 |
| 当前工位的 PowerShell 文件脚本测试 | 官方便携版 PowerShell 7.6.8 补测通过；未修改系统执行策略 |

私有配置、账户、任务名、会话 ID、原始记录和部署路径不随报告公开。

## 这次发现并修复的问题

WinPTY 启动 SCO 时，程序将命令行第一个 token 解释为进程名，原来的调用丢掉了 `acp` 前缀，导致 CLI 报 unknown command。`spawn_sco_winpty()` 在命令行中明确包含 executable token，已用真实 CLI 的 `acp jobs exec --help` 和真实容器连接验证。这个处理针对 SCO，不应直接套用到 Python 等不同参数解析方式的程序。

新增 `tests/test_windows_pty.py`，在装有 SCO 的 Windows 环境执行 help 命令验证参数，整个测试不登录、不访问容器。缺少真实 CLI 时明确跳过。Windows CI 同时包含该测试入口，不能把跳过视为真实 CLI 已验证。

## 部署中需要注意的实际条件

- 远端系统默认 Python 为 3.10，低于包要求的 3.11；安装器正确拒绝。必须显式选择满足要求的解释器。
- 旧部署记录中的任务已不是当前运行任务；适配器正确拒绝旧绑定。独立验收重新核对当前归属、工作区和运行状态后配置。
- 新建空线程没有可恢复的 rollout，不能直接作为本组件要求的既有持久化会话。验收使用独立持久化测试会话；没有放宽生产组件的恢复或投递校验。
- 原有配置、服务、隧道和研究任务没有被本次验证替换。

## 软件检查

本机回归：249 passed、42 subtests passed；38 项 Windows/PowerShell 相关测试因环境不具备而跳过。Ruff 检查与格式检查通过。

工位 Windows：官方便携版 PowerShell 7.6.8 下，`test_windows_scripts.py`、`test_windows_pty.py`、`test_supervisor.py`、`test_tunnel.py` 合计 **76 passed、7 subtests passed**，包括已安装真实 SCO 的 WinPTY 参数测试。下载包 SHA-256 与官方 GitHub release digest 一致；未改变系统执行策略。Windows PowerShell 5.1 的旧尝试被其默认脚本策略阻止，不将这次 PowerShell 7 的通过冒充为 5.1 已通过。

## 额外故障恢复与持续测试

在 macOS 的独立目录使用已安装公开包、示例配置及测试专用随机 token，启动真实 supervisor 和 Bridge。连续三次终止该 supervisor 自己创建的 Bridge，均自动恢复健康接口，耗时分别为 1.631、4.355、4.617 秒。重复启动相同状态目录的 supervisor 正常退出；正常停止后监听端口释放。没有停止生产进程。该结果对应 macOS 进程行为，不代替 Windows 崩溃恢复实测。

**24 小时持续测试正在运行，尚未验收。** 2026-10-24 23:02（北京时间）开始，每 60 秒采样，计划于次日约 23:02 完成。Windows 独立任务使用已安装公开包维持同一 MCP stdio 连接，周期性采集真实任务、查询新鲜快照，并检查原部署 Tunnel 的 health / ready。报告记录失败样本、调用耗时及不同采集时间；任何失败都会令最终结果不通过。测试不投递指导、不启动模型回合或科研实验、不读取运行密钥。即使 24 小时无失败，也只能证明这段观测期，不代表无限期在线。
