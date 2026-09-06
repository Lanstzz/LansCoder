# 本机 Observatory 设计

- 日期: 2026-09-06
- 状态: 已确认，待实施
- 范围: LansCoder 第一版本机单用户观测平台

## 1. 目标与非目标

目标是为 LansCoder 提供类似 Langfuse 的本机可观测性：跨项目查询 trace，查看模型、工具、权限、子代理与后台任务的完整证据，并从 session journal 重放历史。

第一期仅支持本机单用户。它不包含云端服务、账户或协作、远程采集、SDK、评测、提示词管理、成本估算、导出或分享。

## 2. 已确认产品边界

- TUI 输入 `/observe` 打开本地浏览器，优先深链当前或最近 trace。
- `lanscoder observe` 打开全局 Trace Explorer。
- `/recall` 是不可变 journal 上的“仅恢复对话”：每个用户消息都是 checkpoint，recall 创建 session 内新 branch，不截断历史，也不在本期恢复代码。
- 页面包含 Trace Explorer、Trace Detail 与 Session Replay；视觉和信息密度参考 Langfuse。
- 原始证据本地明文保存，页面默认显示原文。现有分享与脱敏行为不改变。
- 全量采集，无采样、无自动清理；不估算成本，只保存 provider 返回的原始与规范化 token usage。
- 流式响应不保存逐 delta 内容；保存首个输出时间、增量计数和最终响应。
- 记录项目绝对路径、项目名、Git HEAD、分支与 dirty 状态，不保存完整 diff。
- 观测 recorder、payload、索引或 Web 读取失败采用 fail-open，不得改变模型、工具、权限或最终回复；对应 trace 使用 `incomplete` 完整性标记。session/context journal 的必需持久化写入是独立边界：若其失败则 fail-closed，报告持久化错误且不保证结果可恢复，不能以“全部采集 fail-open”掩盖该错误。

## 3. 存储边界

运行时数据唯一根目录为 `~/.lanscoder`。不迁移、不读取、不兼容旧的 `<project>/.lanscoder` 运行时数据。

```text
~/.lanscoder/
  sessions/
    sess_<id>.jsonl
  payloads/
    <sha256>
  indexes/
    sessions.json
    traces.json
  locks/
    index.lock
    sess_<id>.lock
  recovery/
    tails/
  tmp/
    clipboard/
  projects/
    <project-path-hash>/
      permissions.json
      model_state.json
      memory/
  memory/
  skills/
```

`sessions/*.jsonl` 是唯一事实源。`indexes/*.json` 是可删除、可从 journal 重建的物化查询索引，不是第二份事实源。大型原始输入、完整 provider 请求/响应、超长工具输出、context archive 和附件先原子写入内容寻址的 `payloads/<sha256>`，journal 只保存经哈希、长度和媒体类型验证的引用。archive 与 attachment 的替换必须保留现有 `retrieve_archive` 所需的 metadata 映射。剪贴板中转文件使用 `tmp/clipboard/`，不会绕过 `--storage-root` 写到用户目录。

`<project>/.lanscoder` 不再存 session、索引、权限、模型状态、记忆、payload 或 observatory 数据。它只可选地保存项目维护的 `.lanscoder/skills/**`；项目配置继续是根目录的 `lanscoder.toml`。

项目级权限、模型选择和记忆仍按项目路径哈希隔离，但存于全局根的 `projects/<id>/`。这既允许统一 session/trace 查询，又防止授权和偏好跨项目泄漏。

项目 identity 使用 `Path.resolve(strict=False)` 后的规范化路径哈希。用户入口只能通过 `SessionAccessPolicy.create_primary`、`open_primary` 和 `fork_primary` 打开或创建 session：TUI、CLI `--resume-session`、`--session-id`、factory、bootstrap、resume 和 fork 均须调用。它只允许当前 project_id 的 `primary` session 恢复或 fork，拒绝既有 id 的 create 及所有非 primary session open。子代理只能由内部 `ChildSessionFactory.create_child` 创建，调用时强制提供 parent session、parent trace、project_id、`kind=subagent` 和 worktree metadata；业务代码不能直接调用低层 `AgentSession.create/resume` 构造器。子代理 worktree 继承父项目 project_id，worktree path 只存 metadata。后台任务不创建独立 session，而是归属父 session 的独立 background trace。

`LansCoderPaths` 是全部本地运行时路径（session、payload、archive、attachment、clipboard tmp、index、permission、model state、memory、global skills）的唯一来源。显式命令行根目录覆盖改为清晰的 `--storage-root`，默认 `~/.lanscoder`，用于测试和隔离运行。

## 4. 原子性、并发与恢复

- 每个 session 使用跨进程 advisory lock；追加在锁内以单条 JSONL 记录完成并 `fsync`。
- 每条记录有 session 内单调递增的 `sequence`。多个进程对同一 session 不会交错写入。
- payload 必须先原子落盘并校验哈希，成功后才写入引用它的 journal 事件。
- journal 持久化后才更新 session/trace 索引。索引更新使用全局锁、临时文件、`fsync` 和原子 replace；失败只导致索引重建，不阻断 agent。
- 末尾未完成 JSONL 记录被保留到 `recovery/tails/`，读取恢复至最后完整记录，并给受影响 trace 加 `incomplete`。
- 中间记录损坏、sequence 断裂或 schema 验证失败不会被静默修复：session 为 `corrupt`，不能恢复；观测页面显示可诊断摘要。
- payload 缺失或哈希不匹配不会隐藏 trace，只显示证据不可用并标记 `incomplete`。

锁顺序固定为：writer 取得并释放 session 写锁，之后才取得 index 锁并按 `last_sequence` watermark 增量更新；rebuild 以稳定顺序取得各 session 的读锁形成快照，释放后才取得 index 锁替换索引。任何 reader 只有持有对应 session 锁时才能执行尾行恢复。此顺序禁止 session/index 锁嵌套，避免死锁与并发追加误判。task-plan 不保留独立的写入锁，也不得与 session/index 锁嵌套；所有 task-plan mutation（包括后台完成）统一通过 session writer 的 task-plan 专用 branch-aware 原子 API 执行。该 API 在唯一 session 写锁内确认目标 branch 上下文、重建 plan，并将调用方的 `expected_revision` 与该 branch 当前 task-plan revision 比较；成功后执行本地 reducer、分配 sequence 并追加事件，冲突返回给调用方重新读取并重试。`dispatch_branch_context.branch_head_sequence_at_dispatch` 只用于定位与审计，不作为完成时的过期写入门槛；完成时始终以最新 branch projection 和 `task_plan_revision` 执行 CAS。锁内不得执行 provider 调用或其他长时间操作，也不建设通用 CAS 框架。

尾行恢复是 journal 唯一受控的可变例外：只可截去不完整的最后一条记录，并以 `journal.recovered` 记录被截尾数据的 SHA-256、字节范围和恢复时间。它不是 `/recall` 或任何业务操作可用的重写机制。

## 5. Journal schema

所有事件采用 schema version 1 的不可变信封：

```json
{
  "schema_version": 1,
  "sequence": 42,
  "event_id": "evt_...",
  "occurred_at": "2026-09-06T10:23:45.678Z",
  "kind": "observation.ended",
  "session_id": "sess_...",
  "trace_id": "trc_...",
  "observation_id": "obs_...",
  "parent_observation_id": "obs_...",
  "branch_id": "brn_...",
  "data": {
    "outcome": "succeeded",
    "duration_ms": 17412,
    "output_ref": {"sha256": "...", "media_type": "application/json", "size_bytes": 18420}
  }
}
```

`occurred_at` 用于全局排序；`duration_ms` 用于可靠耗时计算。事件始终追加，投影负责将生命周期还原为当前状态。

所有会影响 session 上下文、transcript、context archive、task plan 或 pending tool state 的事件必须带 `branch_id`。所有归属 session 的 `trace.*`、session-local `trace.linked` 与 observation 也从 `TraceScope` 继承 `branch_id`，索引保存该字段；只有真正全局的诊断事件可省略。`session.created` 的 envelope `branch_id` 与 data `root_branch_id` 相同，并创建不可变 root branch；session writer 从显式的 `SessionBranchContext` 取得 branch id，不能由单个调用点自行猜测。

类别如下：

- `session.created`、`session.metadata_updated`、`message.appended`：session replay 的基础事实。
- `trace.started`、`trace.paused`、`trace.resumed`、`trace.ended`：一次用户输入的生命周期。
- `observation.started`、`observation.ended`：Langfuse 式 observation。
- `trace.linked`：子代理、后台任务的父子因果关系。
- `journal.recovered`、`observability.failed`：完整性与 fail-open 诊断。

trace 的投影状态严格为 `running`、`waiting_for_input`、`completed`、`failed` 或 `cancelled`。`incomplete` 是独立布尔完整性标记，不是状态。observation 的类型为 `agent`、`generation`、`tool` 或 `event`；结束事件用 `succeeded`、`failed`、`cancelled`、`skipped` 或 `scheduled` 表达执行结果。

每个 trace 的生命周期转换都必须留下可重建的结构化证据。成功终态的 `trace.ended` 记录规范化 final output 或 `output_ref`；失败记录结构化 error，取消记录 cancel reason，no-generation 记录明确的 `outcome` 与原因。等待用户输入不是终态，不写 `trace.ended`，而写 `trace.paused` 和 pending summary，之后可由 `trace.resumed` 继续同一 trace；持久化的合法 `trace.paused` 不标记 `incomplete`。只有执行中断且既没有 `trace.paused` 也没有 `trace.ended` 时，trace 才投影为未闭合并标记 `incomplete`，不得猜测为成功或取消。

一次用户输入建立根 trace。权限或 `ask_user` 暂停该 trace；恢复仍使用同一个 trace，但开始新的 `agent` 执行片段，以免人工等待时间被误计为 agent 工作时间。暂停事件与 pending tool-call session metadata 必须同时保存 `trace_id`、`tool_call_id`、`pending_kind`，使进程重启后的恢复仍能唯一找到同一 trace。

## 6. Recall、checkpoint 与 branch

`session` 是 `/resume` 的稳定单位，`branch` 是 session 内可供 agent 使用的一条上下文路径，`RecallCheckpoint` 是每条用户消息之前的隐式边界。它与 L3 context compaction 的 `ContextCompactionCheckpoint` 是不同对象，绝不可复用同名或语义。每个 primary session 只有一条 active branch；`/resume` 在当前项目内每个 primary session 只显示一次，恢复时进入最新 active branch。它绝不把 recall 前的历史 branch 显示成另一条可恢复 session。

`/recall <message-id>` 对齐 Claude Code 的 **Restore conversation**：回到目标用户消息之前，恢复该消息原文到输入框供编辑或重新发送，并追加 envelope `branch_id=new_branch_id` 的 `session.recalled`。事件 data 精确定义为 `{new_branch_id, parent_branch_id, base_sequence, excluded_target_message_id}`：`base_sequence` 是该目标用户消息之前最后一个可见事件，目标消息自身不在新 branch 的祖先前缀中。后续所有 context-affecting event 写入新 branch。recall 必须移除当前 `background_manager.abandon_since()` 的取消行为；已调度任务继续结束自己的 trace，不能因 branch 切换而丢失 completion callback。

为避免同一 JSONL 中 sibling branch 的追加事件交错，active projection 使用以下精确定义。令 `Project(branch, cutoff)` 为 sequence 有序的 branch-local 事件：root branch 返回 `branch_id=root_branch_id` 且 `sequence <= cutoff` 的事件；child branch 返回 `Project(parent_branch_id, min(cutoff, base_sequence))`，再追加 `branch_id=child_branch_id` 且 `sequence <= cutoff` 的事件。初始 `cutoff` 为无穷大。构建 branch tree 时读取完整 journal 的 `session.recalled` 拓扑事件；但 `session.recalled` 本身只切换 branch，绝不作为 message/context event 重放。任何无 `branch_id` 的全局观测事件也不得进入上下文投影。因此 assistant、tool、compaction、task-plan、pending permission 和背景调度都不会误混入新上下文。`AgentSession.resume`、`runtime_replay`、context manager、L3 compact、planning、transcript 和 fork 都必须读取 active projection；历史 replay 使用完整 branch tree。recall 后 writer 的 `SessionBranchContext`、`current_turn` 与 pending state 必须从 active view 重新计算。

recall 后再次 recall 只列 active branch 可见路径上的用户 checkpoint；每次都从选中点前建立新的 active branch。历史 branch 不提供 `/resume`、`/fork` 或隐式 branch switch 入口。新 branch 的首个 trace 写 `trace.linked`，关系为 `rewind_from`，将其和被回退的历史 trace 关联。

`/fork` 只复制 active projection，目标 session 创建全新的 root branch 和 session-scoped ids。仍在运行或之后完成的 background job 保留自身 child trace，并按其持久化的最小 `dispatch_branch_context` 判断：若该 branch 已非 active，就追加 `detached_from_active_branch`，其通知不得重新进入 recall 后的 active context。完成时的 task-plan 更新仍以 dispatch branch 的 projection 读写，并通过 session writer 的 branch-aware 原子 mutation API 将调用方的 `expected_revision` 与该 branch 当前 task-plan revision 比较后，将 `task_plan_updated` 写回 dispatch branch，既不污染 active branch，也不丢失旧 branch 的真实执行历史。`dispatch_branch_context` 最小字段固定为 `{branch_id, branch_head_sequence_at_dispatch, project_id, task_plan_revision}`；其中 `task_plan_revision` 作为完成时的 `expected_revision`，`branch_head_sequence_at_dispatch` 只用于定位与审计，不能作为过期写入门槛，完成时不得直接据此覆盖计划，必须从最新 branch projection 经该 API 更新。

本期不实现文件恢复。当前 `/recall` 从未恢复代码，而完整代码 checkpoint 需要单独处理 shell、外部改动、symlink、worktree 与子代理边界；用户应使用 Git。已有 `/compact` 保持负责上下文摘要，`/recall` 不承担 summarize 选项。

## 7. 采集模型

采集必须在核心执行路径发生，不从 TUI 文本或历史 transcript 推断。agent/core 只依赖 `TraceRecorder` 协议；具体 journal recorder 属于新的 `lanscoder/observability/` 域，避免 Web/TUI 反向依赖核心。

| 对象 | 当前接入点 | 证据 |
|---|---|---|
| 根 trace / agent | `AgentChatRunner`、`AgentLoop.run_user_turn`、`_run_tool_loop` | 用户输入、项目/Git 快照、轮次、限制、结束状态 |
| generation | `AgentLoop._complete_once` | 完整请求/响应、provider、model、usage、finish reason、首输出、重试和错误 |
| tool | `ToolExecutor` 生命周期事件 | 调用参数、结果、耗时、并行关系、拒绝或中断 |
| 权限 | `ToolExecutor`、`PermissionResumeHandler` | 请求、预写审查、用户决定、恢复结果 |
| 上下文与计划 | context manager、writer 生命周期 | 压缩、上下文快照、任务计划和后台通知 |
| 子代理 | `SubagentEngine` | 角色、任务、worktree、usage、父子 trace 关联 |
| 后台任务 | `BackgroundJobManager` | 调度、实际执行、完成、取消和通知送达 |

generation 在 provider 请求前开始。流式 generation 在第一个 reasoning、text 或 tool-call 输出处记录首输出时间并累加 delta 数，但不保存 delta 原文。每一次 retry 是独立 generation；失败和后续成功均保留。

所有 provider 调用都必须按 generation 采集，包括 `context/provider_summarizer.py` 的 L3 compaction。`LlmCompactRequest` 必须携带完整 `TraceScope`（`session_id`、`branch_id`、`parent_trace_id`、`parent_observation_id`）和捕获时的 `SessionBranchContext`；`llm_compact` 的 retry loop 为每一次实际 provider attempt 传递单调的 `attempt_index`。`ProviderLlmCompactSummarizer` 为每个 attempt 创建 `operation=compaction` generation，使用该 parent observation，保存 request、response、usage、retry 序号和 error，不能仅记录一个 compaction event。`commit_candidate()` 必须接受这份 branch context，只向该 branch 追加 compaction/archive 事实，不能覆盖另一 branch 的状态；当该 branch 已不活跃时，结果仍可审计但不会进入 active projection。手动 `/compact` 若没有活跃用户 trace，则创建独立 root trace。

每个 generation 始终保存可 JSON 化的 `normalized_request` 与 `normalized_response`。仅当 provider adapter 可以安全序列化时才保存 `provider_raw_response`；流式响应保存 `stream_summary`、首输出、delta 计数、finish reason、usage 和最终规范化响应，页面不得将其标注为原始 provider response。`TokenUsage.usage_details` 是 JSON-safe 的 provider-specific map：OpenAI-compatible 保存 `prompt_tokens_details` 与 `completion_tokens_details` 的可用整数字段（包括 `cached_tokens`、`reasoning_tokens`），Anthropic 保存 `cache_creation_input_tokens` 与 `cache_read_input_tokens` 等返回字段；缺失时为 `{}`，不伪造。流式 adapter 将 usage 视为累计快照，按每个字段最后一个非空值合并，绝不把最终累计值跨 chunk 相加。索引另抽取 temperature、max tokens、reasoning effort、tool choice、model/provider、耗时、首输出、总 token、工具名、error、observation counts、受控 metadata/tags 作为 Explorer 可查询摘要。metadata/tags 只接受扁平、JSON-safe、长度受限的白名单字段，provider raw metadata 不能进入索引。

每一个模型 tool-call 都生成 tool observation。真正执行的调用使用 `started` / `finished`；被拒绝、等待权限、预写审查失败或被中断的调用也产生结束 observation。`prewrite_review`、`permission_requested`、用户决定与 `ask_user` 是其子 event；后台调度的父 tool 以 `scheduled` 结束并关联独立 child trace。当前 `ToolExecutionEvent` 已覆盖主要边界；recorder 在接收时写入时间。

## 8. 子代理与后台任务

子代理 session 由当前实现的临时对象改为持久化 journal，标记 `kind=subagent` 并保留完整证据。子代理创建独立 child trace，含 `parent_trace_id` 和触发它的 tool observation。Session Replay 默认只列 `kind=primary`；`subagent` 不会出现在 TUI `/resume`、`/fork`、普通 session 列表中，服务层也拒绝直接恢复或 fork。后台任务不是 session，而是归属父 session 的关联 trace。

后台任务是关联的独立 trace。调度 tool observation 结束为 `scheduled`，实际任务 trace 保存执行、结果、取消和通知送达；常规后台工具仍关联父 session。dispatch 先持久化 scheduled 和最小 `dispatch_branch_context`（`branch_id`、`branch_head_sequence_at_dispatch`、`project_id`、`task_plan_revision`），线程启动写 trace.started，completion callback 无论 branch 是否活跃均直接写 ended；任务计划更新通过 session writer 的 branch-aware 原子 mutation API，将 `task_plan_revision` 作为 `expected_revision`，从 dispatch branch 的最新 projection 比较当前 revision 后写回该 branch，通知投递另记 event。完成时的 dispatch context 只作定位与审计依据，不能直接覆盖计划。应用退出或崩溃留下的运行任务记录为未闭合且 `incomplete`，不能伪造 cancelled 或 succeeded。子代理和后台任务均可从父 trace 展开进入详情。

## 9. 本地 Web 与命令

不新增 Web 框架。使用 Python 标准库本地 HTTP server 加包内静态 HTML/CSS/JavaScript，适合无 SDK、无认证、只读的本机单用户范围。第一期不增加面向多人或不可信浏览器场景的 Web 安全设计。

- 仅绑定 `127.0.0.1`，端口由系统分配；不提供 host 覆盖。
- 只提供 GET，不提供写、删除、导出、远程采集接口。

```text
/                              Trace Explorer
/traces/<trace-id>              Trace Detail
/sessions/<session-id>          Session Replay

GET /api/v1/traces
GET /api/v1/traces/<id>
GET /api/v1/sessions
GET /api/v1/sessions/<id>/replay
GET /api/v1/payloads/<sha256>
GET /healthz
```

`/observe` 在 TUI 启动进程内服务，运行中 trace 优先深链；没有运行中 trace 时深链当前 session 最近 trace；新 session 则带 session filter 打开 Explorer。TUI 退出后该服务退出。CLI 语法为 `lanscoder observe [--storage-root PATH]`；它启动独立前台服务、打开全局 Explorer，并运行到 `Ctrl-C`。浏览器无法自动打开时显示 URL，不影响 agent。

只有正在运行或等待输入的 trace detail 做短间隔刷新；历史列表和 replay 读取索引或静态 journal 投影。

## 10. 视图

Trace Explorer 提供全局 trace 列表与项目、session、状态、时间范围、model/provider、工具名、has_error、耗时、token、observation count 与受控 metadata/tags 过滤。Trace Detail 用层级时间线表现 agent、generation、tool、permission、子代理和后台关联；右侧证据面板默认显示原始输入、输出与 metadata。Session Replay 按主 session 的 branch tree 重放，突出 active branch、以只读方式显示 historical branches，并可进入关联 trace；不把子代理作为可恢复 session 露出。

## 11. 故障策略、测试与验收

测试使用 pytest、临时目录、fake provider、fixture 和标准库 HTTP 客户端；禁止真实模型 API 或网络访问。

必须覆盖：

- 全局路径、跨进程追加、sequence、payload 原子引用、索引重建和尾行恢复。
- 用户回合、generation、工具、retry、流式首输出、权限暂停/恢复、取消和 `incomplete`。
- 子 session 留存但不可 resume、父子 trace、后台调度/完成/取消。
- loopback 限制、API 分页/过滤、payload 读取和无写接口。
- `/observe` 深链、空 session 降级、`lanscoder observe` 与浏览器打开失败回退。
- recall 的连续 branch、原输入回填、resume 不重复 session、历史 branch 只读、recall/background 关系与不可变 journal。
- task-plan mutation 的 branch-aware 原子性：两个从同一 revision 并发完成的更新不会产生重复 revision 或丢失另一更新，冲突可重新读取并重试；旧 branch completion 不会写入 active branch。
- trace 生命周期证据：成功终态可读取 final output 或 payload reference；失败、取消和 no-generation 可读取结构化原因；waiting 只暂停并可恢复；未闭合 trace 标记为 `incomplete`，不被猜测为成功或取消。

验收条件：

1. 默认运行只向 `~/.lanscoder` 写运行时数据；项目 `.lanscoder` 不产生 session 或观测文件。
2. 删除索引后可从 JSONL 重建相同 session、trace 列表和 replay。
3. 任一用户输入可在 Explorer 中打开，完整查看 generation、工具、权限、子代理及原始证据。
4. recorder、payload 或 index 故障不改变 agent 行为，仅以 `incomplete` 或诊断事件反映；session/context journal 必需写入失败则明确 fail-closed，不承诺结果可恢复。
5. 子代理证据可查但绝不出现在 `/resume`、`/fork` 或普通 session 列表。
6. 服务不暴露非 loopback 地址，也不提供写、导出或远程采集能力。
7. 修改文件的 ruff、相关窄测试和完整 pytest 均通过。
8. `/recall` 永不截断 journal；多次 recall 后 `/resume` 仍只显示一次当前项目 session，Session Replay 可审计完整 branch tree。
