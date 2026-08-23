# 01-编排层 `agent/`

> 编排层是整个系统的**大脑**。它不关心 UI 长什么样，也不关心工具怎么实现，只回答一个问题：**一个用户回合如何被驱动到完成**。本文件聚焦主线——代理循环、工具流、权限协调；子代理引擎与护栏点到为止。

- [职责：为什么存在](#职责为什么存在)
- [关键组件](#关键组件)
- [一次回合的主时序](#一次回合的主时序)
- [分支路径](#分支路径)
  - [权限挂起与恢复](#权限挂起与恢复)
  - [上下文压缩触发](#上下文压缩触发)
  - [中断与轮次上限](#中断与轮次上限)
- [依赖关系](#依赖关系)
- [设计要点与取舍](#设计要点与取舍)

---

## 职责：为什么存在

如果只有"一堆工具"和"一个模型 API"，谁来决定**什么时候调用模型、什么时候执行工具、执行前要不要问用户**？编排层就是这些决策的归属地：

- **驱动回合**：一个用户回合不是一次 API 调用，而是"调模型 → 可能执行工具 → 再调模型"的多轮循环，直到模型给出最终回答。
- **集中权限执法**：所有工具的权限检查集中在 `PermissionCoordinator`，而不是散落在 33 个工具里。
- **连接各层**：它向下依赖 providers（模型）、tools（能力）、context（上下文）、session（存储），向上被 app（TUI/REPL/CLI）调用，是唯一知道所有组件的人。
- **对 UI 无感**：`AgentLoop` 不知道自己在 TUI、REPL 还是单次运行里。三种前端共用同一套编排逻辑。

## 关键组件

| 文件 | 类 | 职责 |
|------|-----|------|
| `loop.py` | `AgentLoop` | 回合引擎：驱动工具循环、编排权限恢复、触发上下文压缩、处理后台通知 |
| `loop_limits.py` | `AgentLoopLimits` | 回合限制：最大工具轮次、超时等，`AgentLoopStopReason` 定义停止原因 |
| `request_builder.py` | `RequestBuilder` | 把会话视图 + 工具定义组装成 provider 请求，计算上下文预算与投影指纹 |
| `tool_execution.py` | `ToolExecutor` | 执行工具调用：权限准备、并行只读批、取消上下文、事件派发 |
| `permission.py` | `PermissionCoordinator` | 单一执法闸门：预检、裁决、挂起、恢复、bypass 审查 |
| `permission_resume.py` | `PermissionResumeHandler` | 处理权限确认/ask_user 的恢复路径 |
| `tool_settlement.py` | `ToolCallSettlement` | 工具调用结算：把结果/中断补记到会话 |
| `guardrails.py` | `TurnGuardrails` | 回合护栏：调用次数、超时、受限响应 |
| `user_input.py` | `AgentTurnResult` / `AgentTurnStatus` | 回合结果与状态（completed / waiting_for_user_input） |
| `task_plan_policy.py` | `TaskPlanPolicy` | 任务计划策略：回合末按需做一次对齐 |
| `subagent_engine.py` | `SubagentEngine` | 子代理引擎（本文件点到为止，见下） |
| `session.py` | `AgentSession` | 会话运行时：追加消息/工具结果、执行工具、重建视图 |
| `observer.py` | `TurnObserver` | 回合观察者：把流式事件、工具事件转发给 UI |

> `subagent_engine.py` 是子代理引擎，通过装配根注入的 `child_runner_factory` 获取能力，**不反向 import `AgentLoop`**——这是 `test_layer_boundaries.py` 锁定的边界之一。

## 一次回合的主时序

以 TUI 中输入一条指令为例（`AgentLoop.run_user_turn`）：

```
用户输入 "读 README 并总结"
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│ AgentLoop.run_user_turn                                      │
│                                                             │
│  ① _prepare_main_provider_request                           │
│     ├─ RequestBuilder.context_budget_for_view ── 计算预算     │
│     ├─ ContextWindowManager.compact_if_needed ── 自动压缩     │
│     │    （若超预算，见「分支：上下文压缩触发」）                │
│     └─ RequestBuilder.build ── 组装 ChatRequest              │
│                                                             │
│  ② _complete_once ── 调 provider（同步或流式）                │
│     ├─ provider.complete / provider.astream                 │
│     ├─ TurnObserver.on_stream_event ── 流式事件转发 UI        │
│     └─ 返回 ChatResponse（content / tool_calls）             │
│                                                             │
│  ③ _run_tool_loop ── 工具循环                                │
│     │  while response.tool_calls:                            │
│     │    session.append_assistant_response                   │
│     │    ToolExecutor.execute_interactive_async              │
│     │      ├─ PermissionCoordinator.prepare ── 权限预检       │
│     │      │    （DENY→拒绝结果；ASK→挂起，见「分支：权限挂起」）│
│     │      ├─ session.execute_tool_call ── 真正执行工具        │
│     │      └─ session.append_tool_result ── 记录结果          │
│     │    _complete_once ── 带工具结果回到模型                  │
│     │                                                       │
│  ④ 回合结束                                                  │
│     ├─ TaskPlanPolicy.final_reconciliation_instruction ──    │
│     │    按需做一次任务计划对齐                               │
│     └─ session.append_assistant_response ── 追加最终回答       │
│        AgentTurnResult(COMPLETED)                            │
└─────────────────────────────────────────────────────────────┘
   │
   ▼
[app/] 渲染最终回答
```

要点：

- **工具循环是关键**：`_run_tool_loop` 反复执行"调模型 → 若模型要调工具则执行 → 把结果送回模型"，直到模型不再请求工具、触达轮次上限、或等待用户输入。
- **权限预检在执行前**：每个工具调用先经 `PermissionCoordinator.prepare`，返回放行 / 拒绝 / 挂起三种结果，只有放行才真正执行。
- **请求组装在每轮都做**：`_prepare_main_provider_request` 每轮都重新计算预算、可能触发压缩，因为工具结果会改变上下文大小。

## 分支路径

### 权限挂起与恢复

当工具调用需要用户确认时（`PermissionCoordinator.prepare` 返回 ASK），回合**不是失败**，而是进入 `WAITING_FOR_USER_INPUT` 状态：

```
模型请求调用 write_file
   │
   ▼
PermissionCoordinator.prepare
   │  decision = ASK（或 prewrite review）
   ▼
store_pending_request ── 挂起：记录待确认的工具调用与 deferred 调用
   │
   ▼
AgentTurnResult(status=WAITING_FOR_USER_INPUT, pending_input)
   │
   ▼
[app/] 展示权限确认（TUI 弹出选项 / REPL 打印 1/2/3）
   │
   ▼ 用户选择 allow_once / deny / reject_with_feedback / allow_always
AgentLoop.resume_with_user_input(request_id, answer)
   │
   ▼
PermissionResumeHandler.handle
   ├─ _resolve_pending_confirmation ── 把用户选择翻译为 PermissionDecision
   ├─ 允许 → 执行该工具调用并继续工具循环
   ├─ 拒绝 → 生成拒绝结果，把反馈带回模型
   └─ 结果记回会话，回到 _run_tool_loop
```

这套机制让"询问用户"成为回合的一个**暂停点**而不是失败路径——会话被持久化，进程重启后也能从挂起点恢复（`AgentSession.restore_pending_permission_execution`）。

### 上下文压缩触发

压缩发生在两个时机：

1. **请求前自动触发**（`_prepare_main_provider_request` 内）：每次组装请求前计算预算，若超过高水位，`ContextWindowManager.compact_if_needed` 执行压缩（L1 路由压缩 → L2 归档 → L3 LLM 摘要 → hard-truncate 兜底，详见 [03-模型与上下文](03-model-context.md)），成功后用压缩后的视图重建请求。
2. **提示过长恢复**（`_complete_once_with_recovery`）：模型返回 `prompt too long` 类错误时，先压缩上下文再重试一次；可重试错误则重试一次后降级为非流式。

### 中断与轮次上限

- **轮次上限**：`AgentLoopLimits.max_tool_rounds` 触达时，`TurnGuardrails.limit_response` 生成受限响应，`AgentLoopStopReason.TOOL_ROUND_LIMIT` 结束回合，避免无限循环。
- **用户中断**（Esc）：`CancellationToken` 触发，`AgentLoop` 捕获 `AgentCancelledError`，用 `ToolCallSettlement` 把被打断的工具调用以 `interrupted` 状态补记到会话，保证会话事件流完整可恢复。

## 依赖关系

```
[app/] 表现层
   │  调用 ChatRunner（app/runtime.py 的 AgentChatRunner）
   ▼
[agent/] 编排层
   │
   ├─▶ providers/   ChatProvider（complete / astream）
   ├─▶ tools/       ToolRegistry、Tool 定义（只读接口，不反向依赖）
   ├─▶ permissions/ PermissionManager、策略（只读接口）
   ├─▶ context/     ContextBuilder、ContextWindowManager、store
   ├─▶ session/     AgentSession 背后是 JSONL 存储
   ├─▶ planning/    TaskPlanPolicy 使用任务计划投影
   └─▶ subagent/    子代理类型定义（类型层面）
```

**编排层不知道 UI 的存在**；它通过 `TurnObserver` 等抽象向外发事件，谁订阅谁渲染。**工具与权限也不知道编排层**——`lanscoder.tools` 永不 import `lanscoder.agent`（测试锁定）。

## 设计要点与取舍

- **单一执法闸门**：所有权限检查集中在 `PermissionCoordinator`。收益：策略变更只改一处；风险：协调器成为关键路径，所以它被设计为纯裁决 + 挂起，不掺入工具实现。
- **回合即状态机**：`COMPLETED` / `WAITING_FOR_USER_INPUT` 两种终态，挂起是可恢复的暂停而非错误。这让"断点续跑"成为可能。
- **同步/异步统一**：`run_user_turn` 同时支持同步与流式两条路径，内部收敛到同一套工具循环（v1.1.0 的同步/异步统一）。
- **恢复优先于重试**：provider 可重试错误重试一次，`prompt too long` 先压缩再重试，其余错误上抛。压缩是恢复手段而非常规路径。
- **循环依赖用测试钉死**：`agent.loop` 不 import `agent.subagent_engine`、`tools` 不 import `agent`，历史上两次循环依赖都被测试锁定防止回归。

下一篇：[02-能力层](02-capability.md)
