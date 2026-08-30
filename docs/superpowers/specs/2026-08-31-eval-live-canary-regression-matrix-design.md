# eval_harness live model canary 与 Harbor regression matrix 设计

## 状态与范围

本设计完成 TASK-005 当前 checkpoint 的最后一项：定义真实模型 canary
和 Harbor regression matrix。配套的 `fresh_model` runner 已按本设计实现；本次
实现不改变 `AgentLoop`、工具语义或 provider 接口。

当前实现边界如下：

- `interaction_replay` 是已经可用的离线确定性模式，由 scripted provider
  驱动当前 runtime，并作为 pull request 的硬门禁。
- `fresh_model` 由 `eval_harness run` 直接执行真实 provider，并生成与离线
  runner 相同结构的脱敏 trace、verifier 结果和 scorecard。
- Harbor 是外部 regression/canary adapter。它可以运行真实模型，但结果
  必须与离线 harness 的 scorecard 分开解释。

## 目标

真实模型评测需要回答两个不同问题：

1. 当前 runtime 是否仍能安全、完整地完成一组小型可重复任务？
2. 在真实代码库和外部 verifier 上，某个 provider/model/config 的能力是否
   相对基线发生变化？

第一个问题由 live canary 回答，第二个问题由 Harbor matrix 回答。两者都
   生成可追踪的运行证据，但不把非确定的模型全文输出当作 golden bytes。

## 三条评测车道

| 车道 | 运行器 | 数据 | 用途 | 结果类型 |
| --- | --- | --- | --- | --- |
| offline gate | `eval_harness run` | 11 个人工 fixture + provider tape | PR/本地回归、runtime 确定性 | 五类 hard gate，必须全绿 |
| live canary | `fresh_model` direct runner | 固定人工微型 fixture | provider/runtime 集成、统计能力趋势 | hard gate + 阈值/置信区间 |
| Harbor regression | `LansCoderHarborAgent` | 固定 Harbor 任务集 | 真实仓库、真实 verifier、模型能力 | reward/通过率 + 基础设施分类 |

live canary 和 Harbor 均不得替代 offline gate；offline gate 失败时先修复
runtime 或 fixture，再解释真实模型结果。

## live model canary

### Case 与运行配置

实现 `fresh_model` 时沿用现有 `CaseManifest` 的 case identity、fixture、
artifact/delivery 断言和安全规则，新增的真实模型配置放在运行配置中，
不写入 portable case：

```json
{
  "provider": "openai-compatible",
  "model": "provider/model",
  "base_url": "configured outside the repository",
  "repetitions": 3,
  "max_provider_calls": 120,
  "max_tool_rounds": 120,
  "max_turn_seconds": 3600,
  "context_window": 100000,
  "compaction_strategy": "l1_l2_l3"
}
```

API key、完整 endpoint（如果含租户信息）和原始 provider transcript 只能
来自进程环境或仓库外的本地 secret/capsule，不能进入 case、trace、
scorecard 或 Git。

首批 canary case 固定覆盖：

1. 无工具直接交付；
2. 读取、修改并验证单文件；
3. 多文件修改与测试；
4. 工具失败后的恢复；
5. session resume / compaction（仅在专门的 canary 配置启用）。

每个 provider/model/config 至少运行 3 次；模型、温度/采样参数、上下文
窗口、压缩策略、工具/调用/时间上限必须写入 run identity，才能比较不同
运行。canary 运行不得偷偷 fallback 到 scripted provider。

### 门禁与指标

每次运行仍生成 fresh `trace.jsonl`、`scorecard.json` 和 artifacts。五类
verifier gate 的含义不变：

- `trace`：schema、identity、provider request/outcome、生命周期和完整性；
- `artifact`：fixture 产物与声明的 diff；
- `recovery`：错误、取消、超时、resume 和重复调用的状态闭合；
- `security`：portable trace 脱敏、禁止路径和不泄漏秘密；
- `delivery`：最终交付存在且与 case 断言一致。

这五类 gate 是 canary 的硬失败条件。模型输出的文本、时间戳、随机 ID、
request ID 和 trace digest 不做全文 golden 比较。统计指标至少包括：

- case 通过率、五类 gate 通过率；
- provider calls/errors、tool calls/errors/retries；
- elapsed time、token usage（provider 提供时）；
- session resumes、compaction 次数与成功次数；
- failure taxonomy：provider、tool、recovery、artifact、security、delivery。

比较规则：

- PR 不运行 live canary 作为唯一门禁；可选 smoke 只能报告结果；
- 定时 canary 以固定 repetitions 计算通过率和 p50/p95 指标，并保留样本数；
- 基线比较只报告 hard-gate regression 和数值 delta；小样本不宣称模型
  能力显著改善；
- 默认基线是同一 case、同一 runtime 配置下的上一次通过 run，模型/provider
  变化时必须新建 baseline lineage，不跨模型直接比较绝对分数。

### 证据目录

每次 canary 的目录建议为：

```text
<run-root>/
  manifest.json       # provider/model/config identity，不含 secret
  runs/<repeat>/
    trace.jsonl
    scorecard.json
    artifacts/
  summary.json        # 汇总、样本数、阈值结果、failure taxonomy
```

目录默认在仓库外。portable trace 只保留脱敏值、摘要和稳定指纹；如确需
恢复原始 provider 内容，使用仓库外加密 capsule，且 capsule 不进入
scorecard 或发布物。

## Harbor regression matrix

Harbor 的 matrix 是一组显式 job manifest，而不是把所有维度拼成一个不可
审计的命令。每个 cell 必须记录：dataset/version、task IDs、provider/model、
context/compaction 配置、attempt 数、feedback policy、Harbor 版本、代码
commit、job 目录和结果分类。

### 维度

| 维度 | 最小要求 |
| --- | --- |
| dataset | Aider Polyglot 与 SWE-bench Pro 分开统计，记录版本/来源 |
| task set | 固定 smoke 集、固定 regression 集；扩展集另算，不覆盖历史基线 |
| model | provider、model、endpoint label、reasoning effort |
| runtime | 代码 commit、context window、compaction strategy、调用/工具/时间上限 |
| repeats | smoke 至少 1 次；regression 至少 2 次；canary 对比时保持一致 |
| repair policy | 首轮 agent、是否允许 verifier feedback、最多 repair 次数 |
| environment | Harbor 版本、镜像、架构、Docker/依赖安装状态 |

### 推荐的初始 cells

| Cell | 目的 | 规模 | 门禁/解释 |
| --- | --- | --- | --- |
| H0 offline boundary | 确认 harness 与 Harbor 解耦 | 不运行 Harbor | 由 offline 11-case gate 覆盖 |
| H1 provider smoke | 验证真实 provider、安装和容器链路 | 每个 provider/model 1 个固定小任务，1 repeat | 基础设施必须成功；reward 作为 smoke 证据 |
| H2 Aider regression | 验证真实仓库修复能力 | 固定 Aider Polyglot smoke/regression 集，2 repeats | reward、verifier、timeout/provider error 分开汇总 |
| H3 SWE-bench Pro regression | 验证另一类真实 verifier | 固定 SWE-bench Pro smoke 集，2 repeats | 不与 Aider 分数混合；记录镜像和架构 |
| H4 scheduled matrix | 发现模型/config 变化 | H2/H3 的选定 provider × model × config | 只比较同 lineage baseline，失败需分类和复核 |

首批固定任务集应复用已有 Harbor 冒烟经验，并把实际 task ID 写进 job
manifest；任务集变更必须产生新的 matrix 版本，不得静默替换 baseline。

### Harbor 结果分类

Harbor 的最终汇总至少拆成以下类别：

- `agent_pass`：任务 verifier 通过；
- `agent_fail`：环境正常但任务 verifier 失败；
- `provider_error`：认证、限流、网络或 provider 响应失败；
- `infra_error`：镜像、Docker、依赖安装或 Harbor 自身失败；
- `timeout_or_budget`：达到时间、工具轮数或 provider call 上限；
- `missing_result`：缺少 reward/verifier 结果，不能当作 agent 失败。

只有 `agent_pass`/`agent_fail` 进入能力通过率；其余类别单独报告，并作为
运行健康度指标。Aider feedback repair 只在其官方协议允许的 cell 启用，
不能把 repair 后结果与单轮结果放在同一个 baseline lineage。

### Harbor 与 portable trace 的关系

Harbor job 的原始日志和 `lanscoder-session.jsonl` 是仓库外的运行证据。
需要进入 harness 回归时，先用 `eval_harness extract` 生成 hash-only portable
case，并把可恢复内容放在本地加密 capsule；不能直接提交 Harbor transcript、
私有源码、secret 或大体积数据。Harbor reward/verifier 不能反向注入 Agent
prompt，也不能污染 artifact verifier 的输入。

## 执行顺序

1. 继续把 offline 11-case suite 作为每次变更的必跑门禁。
2. 使用已实现的 `fresh_model` runner，复用现有 recorder/verifier/scorecard，
   先跑 H1 等价的 1-case provider smoke。
3. 加入固定 canary repetitions、summary 和同 lineage baseline compare。
4. 用 Harbor H1 验证 provider/容器链路，再启用 H2/H3 regression cells。
5. 定时运行 H4；任何能力回归与基础设施失败分开处理。

本设计与 runner 完成后，下一项工程工作是按 H1-H4 执行并维护 canary/Harbor
matrix；真实大规模运行仍需显式的 provider、数据集、Docker 和运行预算。
