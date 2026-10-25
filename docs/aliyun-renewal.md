# Alibaba Cloud ECS start guard / 阿里云 ECS 自动启动

This minimal adapter starts **one existing, pinned ECS instance** after observing `Stopped`, using the official `aliyun` CLI. It does not create a replacement, purchase/renew a subscription, recover released spot capacity, manage ECI/ACK containers, or restore an experiment. Start the watcher on another machine that remains online when the target ECS instance stops. ECS boot automation or Docker's own configuration must separately restore the container/workload inside the instance.

## Preconditions and identity

Install the official Alibaba Cloud CLI and configure an existing named credential profile using the [official credentials workflow](https://www.alibabacloud.com/help/en/cli/configure-credentials/). Hub reads neither its credential file nor AccessKeys and does not save credential values. Supply the expected Alibaba **account ID**, region and exact existing instance ID; a CLI profile name is not sufficient identity verification. Each inspection checks STS `GetCallerIdentity`, then ECS `DescribeInstances` with exactly that region/ID. It pins account, region, instance ID, creation time, and a hash of instance type/image/zone/billing mode/VPC/security-group configuration. Raw API responses are not exported.

Required operations are `sts:GetCallerIdentity`, `ecs:DescribeInstances` and `ecs:StartInstance`. Use the official RAM API authorization tables to scope permissions to the intended account/region/instance; this code does not install RAM policies. It never calls Create/Run/Renew/Stop/Delete, modifies billing, or sets `InitLocalDisk=true`.

## Configure, check and watch

```sh
uv run --no-editable research-hub renewal aliyun-ecs init \
  --state-dir /home/researcher/.local/state/research-hub-aliyun \
  --profile-name research \
  --account-id 1234567890123456 \
  --region cn-hangzhou \
  --instance-id i-replaceWithYourInstanceId
```

All values above are placeholders. Optional `--aliyun-executable` selects an absolute CLI path; otherwise init pins the executable resolved from PATH. Initialization only queries the cloud, then creates new private local state. Existing state directories are not overwritten. Do not put AccessKeys, STS tokens or passwords in Hub arguments or files.

`policy.private.json` starts disabled:

```json
{
  "enabled": false,
  "max_starts": 3,
  "cooldown_seconds": 60
}
```

Enable it only for an instance you intend to start whenever it is stopped. **Stopped does not prove expiry or failure**: this policy also encompasses manual stops. Place `stop.flag` in the state directory and stop the watcher before intentional maintenance. Starting a stopped instance may resume billing; this limit counts start attempts, not cost or GPU-hours.

```sh
uv run --no-editable research-hub renewal aliyun-ecs check \
  --state-dir /home/researcher/.local/state/research-hub-aliyun
uv run --no-editable research-hub renewal aliyun-ecs once --apply \
  --state-dir /home/researcher/.local/state/research-hub-aliyun
uv run --no-editable research-hub renewal aliyun-ecs watch --apply \
  --state-dir /home/researcher/.local/state/research-hub-aliyun \
  --interval 30 --max-seconds 3600
```

Both `--apply` and an enabled policy are required for new starts. Check is read-only. The ledger tracks **locally observed stopped episodes**, not an invented provider stop timestamp or Docker exit code. Init records a stopped episode if present. If the target was running at init, the first apply/watch observation of Stopped persists its cooldown start; a later iteration can start it. Pure read-only checks do not persist new observations, so they cannot advance the observation history themselves.

The API is asynchronous: a successful `StartInstance` response or `Starting` state is not acceptance. Only an independent observation of the pinned instance in `Running` marks the request accepted; it says nothing about application/container health or who caused a concurrent start. The guard does not call Codex or start model turns directly, but the instance's own boot services can run workloads.

## Budget, uncertainty and exclusions

The guard durably saves intent under a local lock **before** calling StartInstance, uses CLI `--retry-count 0` to disable hidden API retries, and bounds CLI output and runtime. Timeout, disconnection, error or a lingering Starting/Stopped state leaves an uncertain record. It is never resent automatically; later Running observations can reconcile it. A crash before actual dispatch can require manual reconciliation. Attempt count and uncertainty persist across process restarts. Do not delete the ledger, switch state directories or reset it to bypass an uncertain operation.

Wrong accounts/regions/IDs, changed pinned configuration, missing/ambiguous instances, unknown lock information, financial/security/recycling locks, expired prepaid subscriptions and spot instances are blocked. Only non-spot `PostPaid` and unexpired `PrePaid` instances are handled. Insufficient capacity, a released instance or expired subscription requires operator action; no fallback creates another billable resource. An economy-mode stop may not have capacity available for a later start, per the official StartInstance documentation.

Run one controller/state directory per instance. There is no distributed cloud lock or compare-and-swap promise against arbitrary external operators. Watch is foreground and time-bounded; use your existing service manager for continuous/reboot operation. No scheduler, cloud policy, SSH, firewall or production service is changed by installation.

## Evidence and official API references

This adapter has **offline tests using documented CLI/API-shaped fixtures**, covering exact target/account/profile pins, locks/spot rejection, disabled policy, cooldown/budget, asynchronous state changes, durable intent and no retry after timeout. No Alibaba CLI/account or paid ECS resource was used for live acceptance in the development environment. Passing these tests must not be described as real-cloud validation. Validate first against your own explicitly selected test instance and check its billing and boot behavior.

- [GetCallerIdentity](https://www.alibabacloud.com/help/en/ram/developer-reference/api-sts-2015-04-01-getcalleridentity)
- [DescribeInstances](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-describeinstances)
- [StartInstance and its asynchronous state/authorization rules](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-startinstance)
- [CLI profile, retry and timeout options](https://www.alibabacloud.com/help/en/cli/command-line-options)

## 中文说明

本次先接入阿里云 **ECS 已有实例的自动启动**。固定账户、地域、实例 ID、创建时间和配置摘要；发现已停止、观察冷却期已过、策略允许且次数未耗尽时，调用官方 CLI 的 StartInstance。请求前记录，之后独立查询到 Running 才确认；Starting 或请求超时都不会触发重复发送。

它不是 ECI／ACK 容器适配，也不会购买实例、续费、找回被回收的抢占式实例或恢复实验检查点。ECS 内的 Docker／研究任务需要自己的开机启动或恢复配置。监控程序应运行在目标实例之外的常在线机器上。

按上面的 init 创建私有配置，审核并开启 policy，再使用 check／once --apply／watch --apply。默认不允许启动；只读检查不会写观察记录。手动停机前先设置 stop.flag 并停止 watcher，避免把维护停机当作需要启动。次数限制不等于费用预算。

当前完成的是实现和离线接口／状态机测试，**还没有真实阿里云账户验收**。原有 SCO 与 Docker 续起继续保留。后续先根据真实部署反馈修正这一项，不把没有验证的其他云平台列成已支持。
