# LansCoder Agent Observatory 设计

## 状态与目的

本设计定义 LansCoder 的本地单用户可视化观测平台。它服务于需要诊断 Agent 运行的开发者和维护者：从一次异常或失败运行中定位模型输入、上下文变化、工具执行、权限决策、计划偏离、耗时、成本和风险。

平台把 Coding Agent 视为由模型、系统提示、工具、权限策略、上下文和执行流程共同构成的系统。它必须保留一条可回溯的运行路径，使结论都能落到具体事件和原始证据上。

本设计不要求、也不宣称获取模型隐藏 chain-of-thought。provider 暴露的 reasoning 文本只能作为模型自述保存，不能作为因果真相。可作为观测证据的事实包括模型可见输入、显式计划、请求与响应、工具调用、权限决策、上下文变化、产物和验证输出。

后续评测系统将作为独立项目重新设计。本设计不定义其对象、接口、数据格式或工作流，也不为其保留兼容层。

## 已确认的产品决策

| 决策 | 选择 |
| --- | --- |
| 主入口 | Run-first：先诊断单条运行，再按 session、模型、状态、结果和风险浏览其他运行。 |
| Trace 默认视图 | 时间线；选中步骤后显示因果链和完整证据。 |
| 结果层 | 证据优先：验证输出、产物差异和最终声明先于任何聚合摘要。 |
| 过程层 | 显式计划与实际执行对齐，记录计划修订和偏离。 |
| 效率层 | 按模型请求、上下文构建和工具调用分解耗时与 token。 |
| 风险层 | 策略感知风险账本，关联动作、策略版本、权限决策和实际影响。 |
| 原始数据 | 在个人 Mac 本地明文保存；原始 payload 与索引分开。 |
| 查看与导出 | UI 默认脱敏，可显式显示原文；诊断导出默认使用脱敏投影。 |
| 承载形态 | 独立本地 Web 工作台；LansCoder TUI 提供运行状态和深链。 |

## 范围

### 本期包含

1. 一次运行的统一 Trace 采集、持久化、索引和浏览。
2. 结果、过程、效率、风险四层派生事实与证据下钻。
3. 根运行、权限恢复、前台子代理和后台工作的父子关系追踪。
4. 只监听 `localhost` 的单用户 Web 工作台和 TUI 深链。
5. 本地运行之间的筛选、排序和并排诊断查看；不计算能力分数或实验结论。

### 本期不包含

- 任何评测系统、打分、实验、基线/候选比较或 case 生成。
- 评测任务包、clean-room 执行、独立 verifier 编排或 promotion 流程。
- 多用户、云同步、账号、协作权限或远端数据服务。
- 自动接受或自动修改模型、提示词、工具或权限策略。
- 对隐藏 chain-of-thought 的采集、重建或评分。

## 用户体验与信息架构

用户从 TUI 的 `/observe` 或 `/observe <run-id>` 进入一条运行。前者打开当前 session 的最近根运行，后者打开指定运行。Web 工作台索引本机已采集的 session/run，并支持按 session、模型、状态、结果、风险和时间筛选。运行中的 run 必须标记为 `running`，其缺失字段不能被渲染为成功、零成本或无风险。

页面固定展示四个跨视图事实：运行状态、结果摘要、总耗时/token 和未处理风险数。主区域是按单调序号排序的 Trace 时间线。事件至少覆盖：任务输入、计划修订、上下文构建、provider 请求/响应、流式片段、工具生命周期、权限决策、压缩边界、验证输出、最终回答、取消与错误。

选中事件打开右侧证据面板：

- 模型请求：完整模型可见 messages、工具 schema、模型/采样配置、增量上下文与响应。
- 工具调用：参数、结果、错误、权限请求/决定、预写审查和受影响文件。
- 计划节点：关联动作、状态、偏离理由与计划修订差异。
- 验证输出：命令、退出状态、摘要、关联 artifact 和验证规则。
- 风险事件：规则/策略版本、决定、用户授权、后果和处理状态。

默认显示脱敏投影；“显示原文”是浏览器会话内、显式点击后的 localhost-only 请求，仅针对本地保存的原始 payload 生效，并以 `Cache-Control: no-store` 返回。原始 payload 不出现在列表、时间线或默认事件 API；刷新页面后恢复默认脱敏视图。诊断导出不复用原文显示状态。

## 四层观测模型

| 层 | 首要问题 | 页面主视图 | 必须能下钻的证据 |
| --- | --- | --- | --- |
| 结果 | 运行完成了吗，输出是否可用？ | Evidence-first result | 验证输出、artifact diff、最终声明、验证规则。 |
| 过程 | 计划如何演变，执行在哪里偏离？ | Plan vs actual | 计划 revision、动作映射、偏离、失败和重试。 |
| 效率 | 哪些请求、上下文或工具造成耗时和 token？ | Timing and token breakdown | 墙钟、等待、输入/输出 token、重试和并行 lane。 |
| 风险 | 是否越权、误操作或泄露？ | Policy-aware risk ledger | 动作、策略版本、权限决定、用户授权、实际影响。 |

摘要只用于导航，不能替代证据。任何“变慢”“失败”或“有风险”的结论都必须能回到对应事件和 payload。

## Trace 主干

Trace 是追加式、可版本化的事实日志，而不是 UI 事件日志。每个事件有稳定 `event_id`，归属 `run_id`、`session_id`、`turn_id`，并通过 `parent_event_id` 和可选 causal links 表示关系。记录墙钟时间和单调序号；UI 不得依赖墙钟排序来恢复因果。

`run_id` 代表一次由用户输入或 nudge 发起的根执行，不是一次 provider 调用。它从创建到终止只有 `running`、`waiting_for_input`、`completed`、`cancelled`、`error` 五个状态；`incomplete` 是可叠加的完整性标记和原因列表。权限/用户输入恢复沿用原 `run_id`；每次 provider 调用仍生成独立 request event。

子代理和后台工作各自创建 `run_id`，并在创建时写入不可变的 `root_run_id`、`parent_run_id`、`parent_event_id` 和（后台工作适用时）`background_job_id`。父 run 的派发事件、子 run 的开始/结束/取消事件和后台 job 的完成/取消/失败事件必须互相可回链；子 run 的终态必须回写父 run 的关联摘要。当前阶段展示这些关系和时间线 lane，但不计算跨 run 的能力分数或实验聚合。

等待输入必须通过持久化的 `pending_request_id → run_id` 映射恢复：在权限确认或 `ask_user` 提示对 UI 可见之前写入映射；恢复后读取同一映射取得 root run；仅当该请求被决议且运行进入终态后删除映射。映射缺失、冲突或指向不存在 run 时，恢复路径仍不得中断 Agent，但必须创建 `observation.incomplete` 事实并标明不能证明的关联。

后台 job 不跨进程恢复。启动恢复发现仍为 `running` 且带 `background_job_id` 的子 run 时，必须将该子 run 与其 root run 标为 `incomplete`，原因是 `process_restarted_while_background_running`；不得伪造为 completed 或 cancelled。

原始 payload、标准化事件和派生指标分离：

- 原始 payload：本机明文文件，保存完整本地证据和 artifact 引用。
- Trace event：只保存 UI/查询所需的结构、字段摘要、digest 和 `payload_ref`。
- 派生事实：由 Trace 生成，可删除重建；不得成为唯一证据源。

### 最小事件契约

每个事件的公共信封为：

```json
{
  "schema_version": 1,
  "event_id": "evt-...",
  "run_id": "run-...",
  "session_id": "session-...",
  "turn_id": "turn-...",
  "sequence": 42,
  "kind": "provider.request",
  "started_at": "2026-09-04T...Z",
  "elapsed_ms": 1540,
  "parent_event_id": "evt-...",
  "causal_event_ids": ["evt-..."],
  "payload_ref": "payloads/...json",
  "payload_sha256": "...",
  "safe_summary": {"...": "..."},
  "provenance": {"runtime_version": "...", "policy_version": "..."}
}
```

`payload_ref` 指向原始本地文件；`payload_sha256` 是原始 canonical JSON UTF-8 bytes 的完整性摘要，不是加密或访问控制；`safe_summary` 是默认可展示/导出的脱敏投影；`provenance` 说明事件由哪个运行时、provider、策略或工具版本产生。所有字段可向后追加；破坏性变化必须提高 `schema_version`。

写入顺序固定为：原始 payload 写入同目录临时文件并 `fsync`、计算 digest、原子 rename 到最终文件、append Trace event、最后更新可重建索引。单进程 `threading.Lock` 覆盖 sequence 分配、event append、run/pending/index 更新，保证后台任务与前台事件不会复用 sequence 或产生半条索引记录。启动恢复时删除未引用的临时文件，保留 orphan payload/event 并把对应 run 标为 `incomplete`；不得静默修复或删除原始证据。

## 采集边界

采集适配器必须位于既有层边界，而非让 UI 读取私有对象：

| 事实 | 优先采集点 |
| --- | --- |
| L1/L2 生命周期、流式消息 | `lanscoder.core` 的 `AgentEvent`。 |
| Provider 请求、响应、usage、错误 | transport wrapper；保存发送前 canonical request。 |
| 工具生命周期、结果、权限请求 | `TurnObserver` / L3 `tool_event_handler`。 |
| Session、计划、压缩与 checkpoint | `SessionEventWriter` 成功 append 后调用的通用 observer。 |
| 上下文预算 | `ContextInspector.inspect(...)` 在每次主请求组装后产生的报告，以及 request projection。 |
| 结果验证 | 运行时已有的结构化验证输出。 |

采集器只能观察，不得改变 agent 行为、重试语义、权限决定、provider request 或工具返回。采集回调单独捕获异常；采集失败只能写入 `observation.incomplete` 并通知 UI，不能导致正常 Agent 运行失败。

## 上下文与模型输入

每次 `provider.request` 必须记录影响模型输出的全部可见输入：系统提示版本/内容引用、用户输入与附件投影、历史消息、工具定义、tool choice、模型和采样选项、skills/memory/计划注入、上下文预算，以及 compaction/pruning/injection 前后的边界。

完整模型可见 request 应能从原始 payload 重建。若因 provider、历史版本或采集错误无法完成，运行必须标记 `context_complete=false`；该运行仍可诊断，但 UI 必须明确显示输入证据不完整。

## 数据处理与安全边界

原始 Trace 保存在个人 Mac 的本地工作目录中，不做加密。这是单用户开发环境的明确取舍。

脱敏在两处强制执行：

1. 默认 UI 投影隐藏命中敏感字段/值的内容，但允许本地显式显示原文。
2. 诊断导出一律使用脱敏投影，原始 payload 不得被复制进导出物。

`RedactionPolicy` 带版本，覆盖敏感字段名、常见 token/连接串/私钥文本模式、工具输出和附件元数据。它是确定性规则集，不引入 DLP 服务或模型判断。若某个 payload 无法解析、附件无法投影或脱敏器失败，则该对象 `exportable=false`，阻止导出，同时保留本地原始 Trace 供显式查看。

## 错误处理与完整性

- 任一采集器、脱敏器或派生器失败，运行仍完成；Trace 和相关指标标记 `incomplete` 并附失败原因。
- 事件写入采用 append-only；索引损坏可从 Trace 重建。
- 原始 payload 缺失、digest 不匹配、事件因果关系无效或 schema 不支持时，拒绝把该运行标记为完整。
- UI 不得把缺失数据渲染为 0、pass 或无风险；必须显示“未知/不完整”。
- 解析旧事件时保留未知 `kind`，使读取端可前向兼容。

## 分阶段交付

### 阶段 1：可诊断单条根运行及其子运行

建立统一 Trace 信封、原始 payload 存储、provider/工具/权限/计划/上下文采集和 Run-first Web 时间线。一次根运行的权限恢复、前台子代理和后台工作必须保持完整的父子关系及完成/取消链路，并在时间线中按 lane 展示。完成四层观测的证据下钻。

### 阶段 2：观测分析与诊断效率

加入运行筛选、并排诊断、时间/token 分解、关键路径展示和策略感知风险账本。此阶段仍只服务于本地运行观测，不引入评测任务、实验、评分或 case 管理。

### 后续独立项目：评测系统

未来的评测系统另行定义。它不复用本设计中已删除的旧 Eval Lab 对象、状态机或 promotion 约束；是否复用 Observatory 的基础 Trace，由未来设计单独决定。

## 成功标准

- 开发者可在一次失败运行中，从结果摘要在三次下钻内定位到模型输入、工具/权限、上下文或验证证据。
- 根运行、权限恢复、前台子代理和后台工作之间的关系可完整回链；未知终态明确标记为不完整。
- 启用观测不会改变 Agent 行为；观测不完整绝不伪装成正常或成功。
- 本地原始证据可查看，任何默认 UI 或诊断导出均不包含未脱敏敏感内容。
