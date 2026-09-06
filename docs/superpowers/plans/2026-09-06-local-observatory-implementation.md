# 本机 Observatory 实施计划

> **For implementers:** 先完整阅读设计文档 `docs/superpowers/specs/2026-09-06-local-observatory-design.md`。该文档是产品边界、存储语义和不变量的权威来源。本计划不授权提交；每个阶段完成后由用户决定是否提交。

**目标：** 将 LansCoder 的项目级运行时 session 替换为 `~/.lanscoder` 中的集中 JSONL journal，并基于同一事实源提供 Langfuse 风格本机 trace、原始证据、Trace Explorer、Trace Detail、Session Replay、TUI `/observe` 与 CLI `lanscoder observe`。

**技术约束：** Python 3.11、标准库 HTTP server、pytest、ruff；不添加依赖，不访问真实 provider 或网络。新代码必须保持 `app → core/agent → observability/context/storage` 的单向依赖；Web 不能被 agent/core import。

## 阶段 0：基线与边界保护

- [ ] 完整阅读以下文件及直接测试：
  - `lanscoder/context/events.py`、`lanscoder/context/store.py`、`lanscoder/context/writer.py`
  - `lanscoder/session/{index,catalog,bootstrap,new,resume,fork}.py`
  - `lanscoder/{app/factory.py,core/session.py,cli.py}`
  - `lanscoder/agent/{loop,tool_execution,permission_resume,subagent_engine,background,observer}.py`
  - `tests/test_session_catalog.py`、`tests/test_agent_contracts.py`、`tests/test_delegate_tool.py`、`tests/test_background_jobs.py`。
- [ ] 记录 `git status --short`；保留用户已有的删除与修改，绝不恢复、清理或暂存它们。
- [ ] 新增失败测试，锁定默认运行时根为用户目录、项目 `.lanscoder` 仅被 skills discovery 使用。
- [ ] 运行窄测试与 `venv/bin/python -m ruff check`；每个后续阶段至少重复其相关窄测试。

## 阶段 1：集中路径与 journal 基础

**新增模块：**

```text
lanscoder/storage/
  paths.py              # LansCoderPaths 与项目状态路径
  locking.py             # macOS/Linux/Windows 跨进程 advisory lock
  payloads.py            # 内容寻址 payload 的原子写入/验证
lanscoder/journal/
  models.py              # schema v1 不可变信封与 payload ref
  store.py               # append、read、tail recovery、sequence 分配
  recovery.py            # 损坏分类与证据保留
lanscoder/session/
  access.py              # SessionAccessPolicy 与 project identity
  branch.py              # SessionBranchContext、branch tree 与 active projection
```

- [ ] 定义 `LansCoderPaths(storage_root, project_root)`：默认根为 `Path.home() / ".lanscoder"`；提供 sessions、payloads、indexes、locks、recovery、全局 memory/skills 与按规范化项目路径哈希得到的 project state 路径。
- [ ] `LansCoderPaths` 还必须是 archive、attachment、clipboard tmp、permission、model state、项目/全局 memory 和 global skills 的唯一来源；archive 与 attachment 统一转换为内容寻址 `PayloadRef`，并保留 `retrieve_archive` 所需的 archive metadata 映射；clipboard 使用 `paths.tmp / "clipboard"`。所有 factory、core 装配、服务和测试不得绕过 paths 对象直接拼接用户目录。
- [ ] 将 CLI `--data-root` 改为 `--storage-root`；所有 factory、core 装配、服务和测试改用明确的 paths 对象，而不是含义混杂的 `data_root`。global skill root 同样可注入，测试不得读取真实用户目录。
- [ ] 定义 schema v1 journal envelope：`schema_version`、`sequence`、`event_id`、`occurred_at`、`kind`、`session_id`、可选 trace/observation/parent ids、`branch_id` 与 JSON `data`。创建事件 id、trace id、observation id、branch id 生成函数。`session.created` 的 envelope `branch_id` 和 data `root_branch_id` 必须相同；所有影响 context、transcript、archive、task plan 和 pending tool state 的事件必须通过 `SessionBranchContext` 写入 `branch_id`。这项在 writer/schema 第一阶段完成，不能留待 recall reducer 后置补加。
- [ ] 实现 `SessionAccessPolicy` 与 `project_id_for_path(Path.resolve(strict=False))`。用户入口只可调用 `create_primary`、`open_primary`、`fork_primary`：CLI `--resume-session` / `--session-id`、TUI、factory、`SessionBootstrap`、resume、fork 均经该 policy；它只允许当前 project 的 `kind=primary` session resume/fork，已有 id 不可 create，任何非 primary 或其他 project session 一律不可 open。仅内部 `ChildSessionFactory.create_child` 可建立 `kind=subagent` session，并强制带 parent session、parent trace、project id 和 worktree metadata；低层 `AgentSession.create/resume` 只供这些入口内部使用，业务代码不得绕过 policy。后台任务不创建独立 session。worktree child 继承父 project_id，仅另存 worktree path metadata。
- [ ] 实现每 session 锁下的追加：校验 session id、恢复损坏尾行、分配 sequence、单次写入 JSONL + 换行、flush/fsync。只有尾行恢复可使用临时文件与同目录原子 replace；业务代码不得重写 journal。
- [ ] 固定锁算法：writer 持有并释放 session 写锁后才取得 index 锁，以 session `last_sequence` watermark 增量合并；rebuild 按稳定顺序逐个持 session 读锁形成快照、全部释放后才取得 index 锁 replace；绝不嵌套 session/index 锁，尾行恢复只能在对应 session 锁内执行。移除 `planning/service.py` 独立的 task-plan 写入锁；所有 task-plan mutation（包括 background completion）统一进入 session writer 的 task-plan 专用 branch-aware 原子 API。该 API 在唯一 session 写锁内确认目标 branch 上下文、重建目标 branch projection，并将 `expected_revision` 与该 branch 当前 task-plan revision 比较；成功后执行本地 reducer、分配 sequence 并追加 event，冲突由调用方重新读取并重试。`dispatch_branch_context.branch_head_sequence_at_dispatch` 只作定位与审计依据，不作为完成时的过期写入门槛；完成时将 `task_plan_revision` 作为 `expected_revision`，始终以最新 branch projection 执行 CAS。锁内不得执行 provider 调用或其他长操作，不实现通用 CAS 框架。
- [ ] 实现尾行恢复：把非完整最后一行复制到 `recovery/tails/`，截断到最后完整换行，写包含截尾 hash、字节范围和恢复时间的 `journal.recovered` 事件；中间行损坏、sequence 断裂或 schema 错误只报告 `corrupt`，不改原文件。尾行恢复是 journal 唯一受控可变操作，不能被 `/recall` 或其他业务流程复用。
- [ ] 实现内容寻址 payload：先写临时文件、flush/fsync、计算 SHA-256、原子放到最终路径，返回携带 hash、size、media type 的引用；读取时重新核验。
- [ ] 把 `JsonlSessionStore` 和 `SessionEvent` 的现有 API 替换或拆分为 journal + session view reducer。不得保留旧项目目录、旧 schema 或迁移逻辑；删除 `app/factory.py` 启动阶段对 `SessionIndex.prune_empty()` 的调用，并移除该自动清理语义，运行时不得自动清理任何 session。

**测试：** 同 session 多进程追加不丢记录、不重号；payload 先于引用；尾行恢复保留片段并记录 hash/字节范围；中间损坏不会被掩盖；root branch/每个 context event 的 branch id；CLI/headless/TUI 的 project/kind access 拒绝；默认路径不创建项目运行时目录。

## 阶段 2：session replay 与可重建索引

**新增或重构模块：**

```text
lanscoder/session/
  projection.py          # journal → SessionView / SessionRecord
  index.py               # session index 原子物化与全量重建
lanscoder/observability/
  projection.py          # journal → TraceRecord / Observation tree
  index.py               # 全局 trace materialized index
```

- [ ] 将现有 session 事件规范化为 `session.created`、`session.metadata_updated`、`message.appended`、`session.recalled` 及明确的 context/task/background 事件；更新 writer、runtime state replay、transcript、share、fork 与 recall consumers。
- [ ] 实现 append-only recall reducer：每个用户消息形成 `RecallCheckpoint`；它与 L3 `ContextCompactionCheckpoint` 是不同模型。`/recall <message-id>` 追加 envelope `branch_id=new_branch_id` 的 `session.recalled {new_branch_id, parent_branch_id, base_sequence, excluded_target_message_id}`，将原输入回填 TUI，且绝不调用 `truncate_before_message()`、删除 journal 或 `background_manager.abandon_since()`。构建 branch topology 时扫描全 journal 的 `session.recalled`；`session.recalled` 只切换 branch，不重放为 context message。精确投影为 `Project(root, cutoff) = root 的 branch_id 匹配且 sequence <= cutoff 的事件`；`Project(child, cutoff) = Project(parent, min(cutoff, child.base_sequence)) + child 的 branch_id 匹配且 sequence <= cutoff 的事件`，初始 cutoff 为无穷大。无 branch_id 的全局观测事件不能进入上下文投影。这保证 sibling branch 在同一 JSONL 中交错追加也不会混入。重复 recall 只可选 active branch 可见 checkpoint，旧 branch 永久保留并仅在 Replay/Trace Detail 可读。
- [ ] 让 `AgentSession.resume`、`context/runtime_replay.py`、`ContextWindowManager`/L3 service、planning service、`session/transcript.py`、share 和 fork 全部改用 active projection；recall 后重新构造 `SessionBranchContext`、writer `current_turn`、runtime state 与 pending permission。不得有任何 consumer 继续从全量 journal 推断活动上下文。
- [ ] 在 `session.created` 保存 `kind`（默认 `primary`）、项目路径/id/名称、Git HEAD/branch/dirty 快照与 schema version。禁止保存 diff。
- [ ] 将 `SessionCatalog` 及 `SessionIndex` 改为从 journal 投影；普通列表、resume、fork 仅接受当前规范化 `project_id` 的 `kind=primary`。服务层在恢复/fork 前再次校验 journal 项目匹配；直接传入 subagent 或其他项目 id 时返回明确的不可恢复错误。后台只有关联 trace、没有可传入的 session id。跨项目历史只由 Observatory 浏览。
- [ ] 建立 `sessions.json` 和 `traces.json`：在全局 index lock 下写临时文件、fsync、replace。索引可丢弃；缺失、过期、损坏或漏事件时从 session journals 重建。
- [ ] 重构 `ForkSessionService`：只允许 primary source，仅读取 active projection；目的 session 先写全新的 `session.created(root_branch_id=...)`，再以目的 session 的 event/message/part/checkpoint ids 重建可验证事实，不能复制 source 的 branch tree 或留下跨 session 悬空引用。payload 可复用内容寻址引用；archive metadata 仍须能供 `retrieve_archive` 查找。
- [ ] 移动权限、模型状态和项目记忆至 `projects/<project-id>/`；用户 memory 与全局 skills 保留在 `~/.lanscoder` 原有路径。`<project>/.lanscoder/skills` 仍由 skills discovery 读取。将 archive metadata 作为 branch-aware journal facts 保存（archive id、payload ref、检索所需 logical metadata）；`retrieve_archive` 只从 active projection 解析它们，并验证重启后 payload 与 metadata 仍可读取。

**测试：** 删除两个 index 后结果一致；resume/fork 隐藏并拒绝 subagent、后台和其他项目 session，且 CLI `--resume-session` / `--session-id` 不能绕过 policy；session replay、transcript、share、context compaction 与 append-only branch recall 在新 schema 下保持工作；重启后 recall、连续 recall、旧 branch L3 checkpoint、fork 只含 active projection、旧 branch 只读可审计；两个项目的权限与模型选择不互相可见。

## 阶段 3：观测模型与 fail-open recorder

**新增模块：**

```text
lanscoder/observability/
  models.py              # Trace/Observation 类型、状态、摘要 DTO
  recorder.py            # JournalTraceRecorder 实现
  protocol.py            # TraceRecorder、TraceScope 协议/空实现
  context.py             # active trace/observation ContextVar 与 resume lookup
  git.py                 # 只读 Git snapshot
```

- [ ] 在 agent 可依赖的低层 protocol 中定义 `TraceRecorder`；提供 no-op recorder。TraceRecorder 的观测/payload/index 写入捕获异常、记录内存诊断并返回，绝不将存储错误抛回 agent。session/context/replay 必需事件是不同的 fail-closed 边界：持久化失败必须报告错误，不能承诺最终结果仍可恢复；验收中的 fail-open 只适用于 recorder-only 故障。
- [ ] 实现 `JournalTraceRecorder`：开始/暂停/恢复/结束 trace，开始/结束 observation，链接 trace，并更新派生 trace index。recorder 首次失败时最多尽力写入一次 `observability.failed`；该写入不得再次触发 recorder，避免递归失败。
- [ ] trace 投影仅产生 `running`、`waiting_for_input`、`completed`、`failed`、`cancelled`；`incomplete` 从 recorder 失败、恢复事件、缺失 payload 或未闭合生命周期推导。定义生命周期证据：成功 `trace.ended` 必须有 normalized final output 或 `output_ref`；失败、取消和 no-generation 必须有结构化原因；waiting 只写 `trace.paused` 与 pending summary，之后可恢复同一 trace，合法 pause 不标记 `incomplete`；只有执行中断且既没有 `trace.paused` 也没有 `trace.ended` 时才标记未闭合，且不得猜测终态。
- [ ] 扩展 `lanscoder/providers/types.py` 的 `TokenUsage`，加入 JSON-safe `usage_details`；OpenAI-compatible 从 `prompt_tokens_details` 和 `completion_tokens_details` 保留可用整数字段（如 `cached_tokens`、`reasoning_tokens`），Anthropic 保留 `cache_creation_input_tokens`、`cache_read_input_tokens` 等返回字段。更新 `streaming.py`：usage 是累计快照时逐字段采用最后一个非空值，只有 adapter 明确给出 delta 时才相加，绝不重复累计最终 usage。缺失时保留 `{}`，不伪造或用估算值填充。
- [ ] 记录原始 payload 的规则：普通小文本内嵌；完整 provider request/response、超长工具输出、附件和原始 structured evidence 经 payload reference 保存。generation 始终保存 JSON 可序列化的 `normalized_request`、`normalized_response`；仅 provider adapter 能安全序列化时保存 `provider_raw_response`，流式改存 `stream_summary`，不得将其伪标为 raw response。保存 usage 与 `usage_details`，不计算成本；序列化适配器优先 `model_dump(mode="json")`，其次 JSON 容器，否则仅记录类型与安全摘要。
- [ ] 定义 agent/generation/tool/event observation 的开始/结束字段、结束 outcome、duration、错误和父 observation 关系；每一个 session-local trace、trace link 与 observation 从 `TraceScope` 强制继承 `branch_id`，trace index 保存它，只有真正全局的诊断可省略。只有 generation 在流式时保存首输出时间和计数，不保存 delta 文本。将 model/provider、temperature、max tokens、reasoning effort、tool choice、usage details、error、token/耗时、工具名、observation counts 与受控 metadata/tags 抽取为索引查询摘要。metadata/tags 只能是扁平、JSON-safe、长度受限的白名单字段，不能将 provider raw metadata 建索引。

**测试：** no-op 与故障 recorder 不改变 fake provider 返回、工具结果、权限决策或取消，且 `observability.failed` 不递归；OpenAI-compatible、Anthropic 和流式 usage details 的 parse/merge；payload 引用可读取并验证；丢失 payload 使 trace incomplete 但仍可列出；成功终态可读取 final output 或 `output_ref`，失败/取消/no-generation 各有结构化原因，合法 `trace.paused` 不标记 `incomplete`，只有无 pause/ended 的中断才投影为未闭合；状态投影处理未闭合 trace。

## 阶段 4：核心生命周期采集

**修改文件：** `lanscoder/core/runtime.py`、`lanscoder/core/session.py`、`lanscoder/agent/loop.py`、`lanscoder/agent/tool_execution.py`、`lanscoder/agent/permission_resume.py`、`lanscoder/context/{provider_summarizer,llm_compact,manager}.py`、相关 writer 入口。

- [ ] 在 `AgentChatRunner` 创建用户输入根 trace，并在 runner 的成功、异常、取消和等待输入出口可靠结束或暂停 trace。新 session 无 trace 时保留 session filter 深链状态。
- [ ] 在 `AgentLoop` 为每个连续执行片段建立 `agent` observation；权限/ask_user 进入等待时结束该片段、暂停根 trace；暂停事件与 pending tool-call metadata 一并持久化 `trace_id`、`tool_call_id`、`pending_kind`；恢复时复用 trace id、写 `trace.resumed` 并开始新 agent observation。
- [ ] 包装 `AgentLoop._complete_once` 以采集每个 generation。保存完整 normalized request/response、provider/model、可选安全 raw response、stream summary、finish reason、usage、usage details、模型参数与 diagnostics；retry 和 prompt-too-long 恢复必须留下各自 generation/error event。
- [ ] 为 L3 定义携带完整 `TraceScope(session_id, branch_id, parent_trace_id, parent_observation_id)` 和捕获时 `SessionBranchContext` 的 `LlmCompactRequest`。向 `ProviderLlmCompactSummarizer` 注入低层 generation recorder；`llm_compact` retry loop 将单调 `attempt_index` 传至每一次实际 `provider.complete()`，每个 attempt 记录 `operation=compaction` generation，使用 scope 的 parent observation，包含 request、response、usage、usage details、error 与 retry 序号。`commit_candidate()` 接受捕获的 branch context，仅向该 branch 追加 compaction/archive 事实，不能覆盖另一 branch；已经失活的 branch 结果保留可审计但不进入 active projection。手动 `/compact` 没有 active user trace 时创建独立 root trace；不可用一个笼统 compaction event 代替该 generation。
- [ ] 通过 stream event 回调记录首个 reasoning/text/tool 输出的单调时间转换结果和各类 delta 数量；不保留增量内容。
- [ ] 将 `ToolExecutor` 现有 `started`、`finished`、`prewrite_review`、`permission_requested`、`denied`、`interrupted`、`background_started` 映射为 tool/event observations；每一个模型 tool-call 都必须有 tool observation：执行的调用 `started/ended`，被拒绝、等待权限、预写失败或中断的调用也有 ended observation。将 prewrite、permission、用户决定和 ask_user 建为子 event；用 `tool_call.id` 关联开始和结束，正确表达并行调用。
- [ ] 将 permission resume 的 allow/deny、已预览写入结果和 request id 写入同一 trace。按持久化 session metadata 中未闭合 tool-call 的 `trace_id + tool_call_id + pending_kind` 唯一恢复，不能仅靠进程内 ContextVar 或重新推导 request id。
- [ ] 在 compaction、task plan、skill、MCP 激活与 background notification 的既有持久化入口写 `event` observations，且不建立对 UI 的依赖。

**测试：** fake provider 的普通、流式、retry、L3 compaction retry 与失败请求（含无 active trace 的手动 compact root trace）；串行/并行工具；权限 pause/resume；ask_user；guardrail limit；cancel；trace 顺序、parent ids、usage、duration 与 `incomplete`；验证成功 trace 的 final output/output reference、失败/取消/no-generation 的结构化原因，以及合法 pause 不会被误标为 incomplete。

## 阶段 5：子代理与后台 trace

**修改文件：** `lanscoder/agent/subagent_engine.py`、`lanscoder/agent/background.py`、`lanscoder/agent/tool_execution.py`、`lanscoder/core/runtime.py`。

- [ ] 移除子代理完成后的 `delete_session` 行为；创建时写 `kind=subagent`、父 session、父 trace、触发 observation、角色、任务和 worktree metadata。
- [ ] 在 delegate 调用建立 child trace 与 `trace.linked`。前台与 worktree 子代理都保存完整 child journal；默认 session catalog 仍排除它们。
- [ ] 为 background dispatch 建立 `scheduled` tool observation 和归属父 session 的独立 background trace；它不创建独立 session。`/recall` 不能调用 `background_manager.abandon_since()` 或取消既有任务。dispatch 时持久化最小 `dispatch_branch_context`：`{branch_id, branch_head_sequence_at_dispatch, project_id, task_plan_revision}`；在线程提交前捕获 `contextvars.copy_context()`，并用捕获 context 的 `run` 执行线程函数以传播 trace scope；dispatch 先持久化 scheduled、线程启动写 `trace.started`、completion callback 无论 branch 是否仍活跃都直接写 ended，完成、失败、取消与通知送达均追加事件。completion 的 task-plan mutation 必须通过 session writer 的 branch-aware 原子 mutation API，将 `task_plan_revision` 作为 `expected_revision`，从 dispatch branch 的最新 projection 比较当前 revision 后写回 dispatch branch；`branch_head_sequence_at_dispatch` 只作定位与审计依据，不能作为过期写入门槛，dispatch context 不能直接覆盖计划。若该 branch 已非 active，追加 `detached_from_active_branch` 且绝不把通知或 task-plan mutation 写入 active context。进程退出或崩溃遗留的 running job 只投影为未闭合、`incomplete=true`、终态未知，不能伪造 cancelled 或 succeeded。
- [ ] 确保背景 worktree 清理不删除 journal 或 trace 证据。

**测试：** child session 在任务后存在并可由 trace detail 查询；resume/fork/普通 session API 拒绝其 id；父子链接、worktree metadata、background 成功/失败/取消/notification 均可投影；recall 后旧 branch background completion 保持 detached，task plan 仅更新 dispatch branch 且不污染 active context；两个从同一 revision 对不同 task 的并发 mutation 最终 revision 连续且两项更新均保留，冲突可重试。

## 阶段 6：本地观测 Web 服务与静态界面

**新增模块与资源：**

```text
lanscoder/observability/web/
  server.py              # ThreadingHTTPServer 生命周期与 browser launcher
  handler.py             # 只读路由与 JSON 响应
  api.py                 # trace/session/payload query service
  static/
    index.html
    app.js
    app.css
```

- [ ] 用标准库 `ThreadingHTTPServer` 仅绑定 `127.0.0.1` 和端口 `0`。没有 host 参数；服务生命周期能由 TUI 内嵌或 CLI 前台方式管理。
- [ ] 实现 `GET /healthz`、trace 列表/详情、session 列表/replay、payload 读取以及静态路由。query service 只从 journal/index/payload abstraction 读取，不添加写、删除、导出或远程采集入口。
- [ ] 服务仅接受 GET；对未知 API 路径返回 404，对其他方法返回只读 API 的 405。第一期不增加 Host 校验、CSP、CORS、`nosniff`、`no-referrer`、XSS 防护或文件权限收紧等多人/不可信浏览器安全设计。
- [ ] 实现 Explorer 的项目/session/status/时间过滤、model/provider、工具名、has_error、耗时、token、observation count 与受控 metadata/tags 过滤、稳定排序和 cursor pagination；Trace Detail 的 observation 树/时间线数据、模型参数、usage details、流式 summary、证据完整性和 branch/rewind links；只返回 primary session 的普通列表。
- [ ] 实现无构建前端：与已确认原型一致的高密度 Trace Explorer、Detail 右侧原始证据面板和 Session Replay。running/waiting detail 才轮询，其他页面不轮询。
- [ ] 在 `packages/lanscoder-core/pyproject.toml` 的实际 package-data 配置中纳入 `observability/web/static/*`，并用安装/资源读取测试确保 wheel 环境不会丢失页面资源。
- [ ] 用 `webbrowser.open` 尝试打开 URL；失败时把 URL 返回给调用方。

**测试：** 临时 journal 通过 HTTP API 的列表、详情、replay、pagination/filter；loopback server 地址；只读 GET/405；浏览器打开失败回退。

## 阶段 7：TUI 与 CLI 接线

**修改文件：** `lanscoder/cli.py`、`lanscoder/app/factory.py`、`lanscoder/app/router.py`、新增 `lanscoder/app/observe_commands.py`、`lanscoder/app/tui.py`、测试夹具。

- [ ] 新增 `lanscoder observe [--storage-root PATH]` 子命令。它创建独立前台 loopback server，使用指定或默认 storage root，尝试打开全局 Explorer，在 Ctrl-C 时优雅关闭。
- [ ] 增加 `ObserveCommandHandler` 到 `CompositeCommandHandler`，支持 TUI `/observe`。它使用当前 active trace、当前 session 最近 trace 或 session filter 的优先级构造深链。
- [ ] TUI 运行中的 server 通过受控 manager 在需要时启动，退出时关闭；不能阻塞 Textual UI、agent loop 或 permissions。
- [ ] 更新 help、命令解析和错误文案；确保 `/resume` 与 picker 仅通过 `SessionAccessPolicy` 展示/接受当前 project 的 primary session，不能由显式 id 绕过。

**测试：** CLI parser、独立 server shutdown、TUI command route、active/recent/empty session 深链优先级、打开浏览器失败回退文本、subagent id 的 resume/fork 拒绝。

## 阶段 8：集成验证与文档收尾

- [ ] 用 fake provider 跑跨项目集成场景：主 trace → generation → 权限暂停/恢复 → 子代理 → 后台完成；验证 Explorer/Detail/Replay 投影与原始 payload。另覆盖 OpenAI-compatible/Anthropic usage details、L3 compaction generation 与 CLI session access policy。
- [ ] 删除 `indexes/` 并重启查询服务，断言重建输出等价；写入损坏尾行，断言恢复目录、截尾 hash/字节范围事件和 `incomplete`；验证中间损坏拒绝恢复。
- [ ] 运行每个改动模块的 ruff、相关窄 pytest，最后运行 `venv/bin/python -m pytest` 与 `venv/bin/python -m ruff check lanscoder tests`。
- [ ] 手动检查 `/observe` 与 `lanscoder observe`：服务只在 loopback、页面无写操作、原文证据可展开、子代理未出现在恢复列表。
- [ ] 更新用户文档，明确全局数据位置、本地明文原始证据、保留策略、`/observe`、CLI 命令和项目 `.lanscoder` 仅用于 skills 的边界。
- [ ] `git status --short` 复核只包含本任务实际修改。除非用户另行明确授权，否则不提交。

## 完成门槛

- Journal 是唯一事实源；删除索引后可重建 session/trace/replay。
- 一次用户输入能展示完整 Langfuse 风格 trace，含模型、工具、权限、子代理、后台与原始证据。
- recorder、payload、索引故障均为 fail-open，不改变 agent 结果；session/context journal 的必需持久化故障明确 fail-closed。
- 子代理证据永久可查，但绝不可被 resume/fork 或作为普通 session 列出。
- Web 仅 loopback、只读、无新增依赖；全量测试与 ruff 通过。
