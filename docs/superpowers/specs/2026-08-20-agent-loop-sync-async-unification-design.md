# AgentLoop sync/async 双树统一重构 — 设计文档

日期：2026-08-20
状态：设计定稿，待实施计划

## 1. 背景与问题

`lanscoder/agent/loop.py`（1450 行）是 agent 主循环编排器。其中存在 **sync/async 两棵平行的控制流树**：几乎每个关键方法都有一对实现（`_xxx_sync` / `_xxx_async` 或 `_xxx` / `_xxx_async`），逻辑逐行重复，只差"要不要 await"。共约 10 对，约 300 行重复代码。

### 成因

- provider 层有两个调用形态：`provider.complete()`（同步）和 `provider.astream()`（异步流式，供 UI 实时显示）。
- 非流式路径为了不阻塞 Textual UI 的事件循环，`run_user_turn(streaming=False)` 把整轮塞进 `anyio.to_thread.run_sync(...)` 的 worker 线程跑纯同步。
- 流式路径必须在事件循环里消费 `astream` 事件，工具执行经 `execute_interactive_async`（内部也是 `to_thread` 包同步）跳线程。

结果：同一套业务逻辑（问模型 → 执行工具 → 回喂 → 直到退出）被写了两遍。真正异步的只有 `provider.astream` 消费这一段。

### 代价

1. 修 bug 要改两处，改漏一处 → 流式/非流式行为漂移。
2. 读者要对每对方法做心智 diff。
3. 是 `loop.py` 膨胀成 god class 的最大单一原因。

## 2. 目标

- **全量统一成单一 async 核心**，删除全部同步树方法。
- **recovery 语义统一采用流式的丰富版本**：retryable 失败重试一次 → 回退同步 complete；prompt-too-long → compact 重试。
- **public API 完全不变**（`run_user_turn(streaming=...)` / `run_nudge_turn` / `resume_with_user_input`），生产代码除 `loop.py` 外零改动。
- 行为增强（刻意的）：非流式路径获得 retryable 重试能力。

## 3. 设计

### 3.1 目标方法结构（四层）

**入口层（3 个 public async，签名不变）**
```
run_user_turn(content, attachments, streaming=False)
run_nudge_turn(streaming=False)
resume_with_user_input(request_id, answer, streaming=False)
```
每个入口内部根据 `streaming` 决定调用 `_complete_once_with_recovery(streaming=...)`，不再分 sync/streaming 两套实现。

**provider 单步层（2 个，全部 async）**
```
async _complete_once_with_recovery(*, tool_choice, runtime_instruction, streaming)
async _complete_once(*, tool_choice, runtime_instruction, streaming)
    # prepare/reserve/check/record/report 逻辑对两种模式相同
    # streaming=True  → await provider.astream(...)：转发事件到 stream_event_handler，收集 last_stream_events
    # streaming=False → await anyio.to_thread.run_sync(provider.complete, request)
```

**循环核心层（3 个，全部 async、对流式无感）**
```
async _run_tool_loop(complete_once)                       # 原 _run_tool_loop_interactive_async
async _continue_tool_loop_from_response(complete_once)    # 原 _continue_..._async
async _run_task_plan_reconciliation_if_needed(complete_once)
```
只含 `await complete_once(...)` 与 `await execute_interactive_async(...)`。

**权限恢复层（2 个，全部 async）**
```
async _append_permission_resume_result(request_id, answer)
async _finish_permission_resume(pending, result)
```

### 3.2 删除清单（同步树 11 个方法）

`_run_user_turn_sync`、`_run_nudge_turn_sync`、`_resume_with_user_input_sync`、`_append_permission_resume_result`（sync 版）、`_finish_permission_resume`（sync 版）、`_run_tool_loop_interactive`（sync 版）、`_continue_tool_loop_from_response`（sync 版）、`_run_task_plan_reconciliation_if_needed`（sync 版）、`_complete_once`（sync 版）、`_complete_once_with_recovery`（sync 版）、`_stream_once_attempt`。

异步版去掉 `_async` 后缀成为唯一版本。

### 3.3 保留的共享方法（不动）

`_prepare_main_provider_request`、`_record_projection_consumed`、`_report_progress`、`_context_budget_for_view` / `context_budget_for_view`、`_compact_if_needed` / `_compact_for_prompt_too_long`、`_request_messages` / `_build_provider_messages` / `_main_chat_request`、`_provider_tool_definitions` / `_augment_tool_definition`、`_ensure_background_control_tools` / `_ensure_delegate_tool`、`_begin_turn`、`_validate_mcp_tool_call` / `_observe_mcp_search_result`、`_append_pending_guidance` / `_append_background_notifications`、`_check_provider_call_limit` / `_reserve_provider_call` / `_check_turn_timeout` / `_is_cancelled` / `_check_cancelled`、`_drop_unsupported_tool_calls` / `_drop_unsupported_tool_call_stream_events`、`_tool_round_limit_response` / `_limit_response` / `_interrupted_response` / `_pending_turn_result` / `_complete_turn`、`_pending_permission_for_resume` / `_prepare_permission_resume` / `_execute_resumed_permission_tool_call` / `_emit_finished_permission_resume` / `_resolve_pending_confirmation` / `_blocked_permission_resume_result`、`_final_reconciliation_instruction`、`_repair_interrupted_tool_calls_before_provider_request` / `_append_interrupted_tool_results` / `_emit_settlements` / `_emit_tool_event`、`replace_cancellation_token` / `clear_stream_events`。

### 3.4 provider 单步层细节

统一后的 `_complete_once(*, tool_choice, runtime_instruction, streaming)`：
1. `_prepare_main_provider_request(...)`（不变）
2. `_reserve_provider_call()`、`_check_turn_timeout()`、`_check_cancelled()`（不变）
3. 分叉：
   - `streaming=True`：`async for event in self.provider.astream(prepared.request)`，转发 `stream_event_handler`、收集 `last_stream_events`，`message_completed` 时取最终 response；stream 提前结束抛 `ProviderError`。
   - `streaming=False`：`await anyio.to_thread.run_sync(self.provider.complete, prepared.request)`。
4. `_record_projection_consumed(prepared)`、`_report_progress(response)`（不变）

### 3.5 统一 recovery 逻辑

`_complete_once_with_recovery(*, tool_choice, runtime_instruction, streaming)`：
```
retryable_failures = 0
while True:
    try:
        return await _complete_once(...)
    except ProviderError as exc:
        if exc.retryable:
            if retryable_failures == 0:
                retryable_failures += 1
                continue
            return await _complete_once(streaming=False)   # 回退同步 complete
        if not exc.requires_compaction:
            raise
        result = _compact_for_prompt_too_long(...)
        if result is None or result.status != "success":
            raise
        return await _complete_once(...)                   # compact 后重试
```

- streaming 失败时清理已收到的部分事件（原 `_stream_once_attempt` 的 `del self.last_stream_events[start_event_count:]` 逻辑），仅 `streaming=True` 时生效。
- 注意：非流式路径的"回退同步 complete"（`streaming=False`）与自身是同一条路径，语义上等于再做一次无 recovery 的原始调用后抛出，功能正确。

### 3.6 行为变化（已确认接受）

1. **线程模型**：非流式从"整轮一个 worker 线程"变为"循环在事件循环 + 每次 provider 调用跳线程"。功能等价。
2. **非流式获得 retryable 重试**（行为增强）。
3. `_drop_unsupported_tool_call_stream_events` 只在 `streaming=True` 且 `last_stream_events` 非空时生效。

### 3.7 线程局部状态与 asyncio.run 嵌套安全性

- 主循环路径唯一 thread-local 是 `BackgroundJobManager._current_job_id`，只在后台 job 线程设置，主循环不读 → 线程模型变化无影响。
- `SubagentRunner._run_inline` / `_run_isolated` 在方法体内 `asyncio.run(loop.run_user_turn(...))`，要求当前线程无运行中的事件循环。统一后工具执行始终经 `execute_interactive_async` → `anyio.to_thread` 跳 worker 线程，delegate 工具永远在无事件循环的 worker 线程里跑 `asyncio.run` → 安全。**前提：工具执行必须保持走 to_thread。**

## 4. 测试

### 4.1 现有护栏

`tests/test_agent_context_loop.py` 有大量 streaming / 非 streaming 用例，重构后必须两套全绿。其它受影响文件：`test_app_runtime.py`、`test_app_tui.py`、`test_app_factory.py`、`test_agent_e2e.py` 等。

### 4.2 测试迁移（必须）

以下 5 个测试文件共 ~23 处**直接调用待删的私有同步方法**，需迁移到 public 异步入口（或统一后的私有方法）：

| 文件 | 方法 | 处数 | 迁移方式 |
|------|------|------|---------|
| `tests/test_agent_e2e.py` | `_run_user_turn_sync` | 12 | 同步测试 → `asyncio.run(loop.run_user_turn(...))` |
| `tests/test_session_resume_service.py` | `_run_user_turn_sync` | 2 | 同上 |
| `tests/test_agent_skill_flow.py` | `_run_user_turn_sync` | 4 | 同上 |
| `tests/test_background_jobs.py` | `_run_user_turn_sync` / `_run_nudge_turn_sync` | 3 | 同步测试 → `asyncio.run(...)`；若个别为 anyio 异步测试 → `await loop.run_user_turn(...)` |
| `tests/test_model_request_options.py` | `_complete_once` / `_stream_once` | 2 | `asyncio.run(loop._complete_once(streaming=False))` / `asyncio.run(loop._complete_once(streaming=True))` 保持测试意图 |

迁移规则：同步 `def test_` → `asyncio.run(...)` 包装；anyio 异步测试 → `await`。逐个调用点判断。

### 4.3 新增用例

- **非流式 retryable 重试**：mock provider 先抛一次 retryable `ProviderError` 再成功，断言 `run_user_turn(streaming=False)` 最终成功（验证统一 recovery 对非流式生效）。

## 5. 验收标准

- 生产代码改动仅在 `lanscoder/agent/loop.py`；`app/runtime.py`、`subagent.py`、`factory.py`、provider/tool_executor/session 零改动。
- `_run_user_turn_sync` / `_run_nudge_turn_sync` 等同步方法全仓库无引用（除历史注释）。
- `tests/test_agent_context_loop.py` 全绿（streaming + 非 streaming）。
- 全量 `pytest -q` 全绿，唯一允许失败为既有的 `test_mcp_integration.py`（缺 `mcp.server.fastmcp`，环境问题，与本次无关）。
- `ruff check` + `black --check` 通过。

## 6. 范围外（本设计不处理）

- `AgentSession`（723 行）、`ToolExecutor`（645 行）的拆解。
- 其它死代码（`AgentLoopLimits.summary()`、`AgentTurnResult` 便捷属性、`tool_flow.py` re-export）。
- `tools → agent` 循环依赖（见 `handoff.md`）。
