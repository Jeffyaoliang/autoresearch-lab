# Research Hub 代码整理说明

整理日期：2026-10-23。

## 这次完成的工作

把原型中的服务、建议投递、研究记录读取、执行策略、验收通道和续起状态机整理成 `src/research_hub` 包。模块通过包内导入连接，安装后可用 `research-hub` 命令运行合成演示或显式启动服务。

原有 Research Agent Hub 文件与 `autoresearch-lab` 中的研究文档原先分开维护。此次以私有仓库为整理目标，保留现有研究简介、评测计划和事件 schema，并加入实际代码与离线回归测试。

## 迁移对应

| 原型文件 / 目录 | 整理后的模块 |
| --- | --- |
| `bridge.py` | `research_hub.bridge` |
| `chatgpt_snapshot_mcp.py` | `research_hub.mcp_server` |
| `guidance_*.py` | `research_hub.guidance.*` |
| `research_progress_*.py` | `research_hub.progress.*` |
| `Research Executor/` 的核心策略、状态、RPC | `research_hub.executor.*` |
| `Acceptance Channel/` 的核心模块 | `research_hub.acceptance.*` |
| `Container Renewal/renewal_guard.py` | `research_hub.renewal.renewal_guard` |
| `Container Renewal/copy_guard.py` | `research_hub.renewal.copy_guard` |

后两者承担不同操作：前者续起既有实例，后者在批准范围内从固定模板创建下一代。

没有迁入多版数值动作试验、临时 TLS relay、服务安装器、密钥输入工具、现场 policy/ledger、上传脚本或原始运行记录。它们具有各自的部署绑定和试验背景，直接混入通用包会掩盖实现边界。

## 重要接口变化

1. 本地运行状态从源码目录移到 `RESEARCH_HUB_STATE_DIR`。缺配置时明确返回不可用或配置错误。
2. 固定账户、目录和会话改为部署时显式配置；包中没有默认生产绑定。
3. guidance 仍只投递建议；不启动模型回合或实验。投递状态不确定时保留去重状态，不自动重发。
4. progress 区分读取时间和源更新时间。旧记录仅在明确允许历史来源时返回，并始终标为历史记录。
5. executor 通过显式回调接入建议处理；取消了旧版自动寻找或接管进程的行为。非文本实验入口继续关闭。
6. renewal 的平台 runner 必须由调用方注入；原来会启动现场守护的 wrapper 未迁入。
7. Windows 的 PTY 依赖按需加载。基础包、合成演示和离线测试可在非 Windows 环境运行。

具体配置和局限分别记录在三个组件文档中。

## 使用现有部署时

此次整理不会自动更新现有 Windows 任务、远端 AFS 文件或运行中的研究进程。切换到新包需要在独立环境中安装，写入新的私有配置，核对会话与 peer 归属，然后分别验收只读查询、建议回执、恢复行为和实际科研执行。

不得将本次离线测试的通过理解为生产服务迁移、真实 GPU Campaign 或自主科研效果已经验收。

## 测试证据

测试覆盖状态筛选与来源时间、HTTP 身份验证、建议幂等投递、含糊回执后不重发、peer 与会话归属、执行门槛、续起预算及审批状态。所有集成夹具使用临时目录和假 transport/runner。

`examples/trace.example.json` 是合成事件，并根据原有 `schemas/trace.schema.json` 验证；它不代表真实科研数据或已完成 Benchmark。
