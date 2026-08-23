# Resume 视图 Parity：thinking 时长与后台通知友好行

- 日期：2026-08-22（v2：按架构评审修订）
- 状态：设计待用户复核
- 范围：一个 spec、一个实施计划、两个阶段（Phase 1 duration parity，Phase 2 notification parity）

## 1. 背景与目标

### 1.1 现状问题

用户报告两个 `/resume` 后的视图退化：

1. **thinking 时长丢失**：live 回合结束时 thinking 折叠行显示 `Thought for 14s`（`transcript_view.py` 的 `child_collapsed_text` 用 `ChildItem.duration_seconds`）；`/resume` 重放后同一行只剩 `Thought`。根因：时长由 TUI 内存测量（`projector.py` 的 `append_thinking(track_duration=True)` → `_finalize_thinking` 用 `time.monotonic()`），消息持久化只存 `diagnostics.reasoning` 文本（`session.py:406-412` 的 `asdict(diagnostics)`），**时长从来不入 store**（`providers/types.py` 的 `ProviderDiagnostics` 无时间字段）。
2. **后台通知原始 XML 泄漏**：live 时子 agent 完成走 `_handle_subagent_completed`（`tui.py:381-393`）显示友好行 `✅ 子agent [label] 已完成`（只进 TUI 内存 transcript）；loop 同时把给模型看的 `<task_notification>` XML 原文写入 store（`loop.py:693` → `render_task_notification`，`background.py:439-465`）。`/resume` 重放 `replay_messages` 的 `role == "notification"` 分支（`projector.py:183-186`）把 XML 原文作为 SYSTEM 块渲染。

### 1.2 需求

- **退出前 TUI 的最终显示状态 == `/resume` 后的显示状态**（逐字一致，含时长秒数、通知友好行）。
- 保持架构干净、模块自包含；不改动 store 事件格式与流事件种类。

### 1.3 Parity 范围（显式声明）与非目标

Parity 承诺**只覆盖两个维度**：thinking 行的时长文案、后台通知行的友好文案。以下既有 live/resume 差异**不在本次范围**，也不为本次实现背锅：

- 非流式多轮回合的中间轮次文本：live 只显示最终文本，resume 重放每轮完整块（既有行为）。
- guidance 注入的用户消息在 resume 出现、live 不渲染（既有行为）。
- 通知投递延迟窗口：见 §5.4 决策。

同时非目标：不回溯修复历史会话文件（旧数据无 `reasoning_seconds` / `background_label`，优雅降级）；不改变给模型的上下文内容（通知 XML 原文仍作为模型可见内容，`context_builder.py:108-112` 不变）；不重做 thinking 行的折叠/展开交互。

## 2. 架构不变量

1. **store 是唯一真相源，loop 是唯一写点**：一切需跨重启保持的值，由 loop（或 loop 调用的 session/writer）在写消息时落进 store；TUI 不写 store，只重放，或在其既有的 store 读回窗口读。
2. **零新事件类型**：不加 store 事件种类（`store.py:20-25` 映射不变）、不加流事件种类（`StreamEventKind` 不变）。provider 适配器不改（纯解析）。
3. **跨层新表面最小**：仅 `ProviderDiagnostics.reasoning_seconds` 字段 + 通知 part metadata 的 `background_label` / `background_error` 两个可选 key。
4. **旧数据优雅降级**：所有新字段读侧 `.get()` 兜底；旧会话行为不劣于今天。
5. **同一个数值只产生一次、只存一处**：live 最终显示与 resume 重放读的是 store 里的同一条记录，一致性由构造保证，不靠近似。

## 3. 决策记录

- **Fix 1 路线：A′（选定）**——loop 在 `_complete_once` 统一测量，写入 `diagnostics.reasoning_seconds`；TUI 回合收尾从 store 读回覆盖 live 子行。弃用路线 B（provider 各测各的：测量逻辑复制到两个解析器，且非流式仍需 loop 兜底）；弃用路线 A1（loop 合成 `reasoning_completed` 流事件：一个数走两条通道 + 重试污染，`append_thinking` 只查 kind 不查 finished 会二次合并值）；弃用路线 C（store 事件扩展、UI 写回：UI 拿不到消息 id、block≠message 映射歧义、`rebuild/catalog/truncate` 都要加折叠）。
- **非流式语义（选定 (a)）**：非流式回合 loop 量整轮调用耗时；TUI 在收尾读回点物化 thinking 行。live 与 resume 一致，并修复现存"live 不显示、resume 却显示一行 Thought"的反向不对称。
- **碎片化（选定）**：一个 spec、一个计划、两个阶段。
- **架构评审修订（采纳）**：reconcile 位置、非流式物化 DOM 策略、消息对位合并语义、测试契约修正、运行时复用 `AgentChatRunner` 的本回合窗口、`CurrentSessionLike` 协议补 `rebuild_view`。见 §4.3、§4.4、§5.4、§7。

## 4. Phase 1 —— thinking 时长 parity

### 4.1 数据模型

`providers/types.py` 的 `ProviderDiagnostics` 增加：

```python
reasoning_seconds: float | None = None
```

`session.append_assistant_response`（`session.py:398-414`）现有 `asdict(response.diagnostics)` 自动落盘这一字段。`context/writer.py`、`context/store.py` 零改动。

语义定义（写入字段文档注释）：
- 流式：`reasoning_seconds` = 从首个 `reasoning_delta` 到首个 `text_delta` / `tool_call_started` / `message_completed` 的墙钟间隔（reasoning 阶段本身）。
- 非流式：整轮 `complete` 调用耗时（无法分解阶段，属近似）；仅在响应带 `diagnostics.reasoning` 时写入。
- 现有适配器事实（`openai_compatible.py:201-218`、`anthropic_provider.py:219-224,270-271`）：**见过 `reasoning_delta` ⟺ 最终 `diagnostics.reasoning` 非空**。因此流式路径下测量保底成立；replay/read 侧的"测不到但有行→None"分支仅作为防御保留，正常不可触发。

### 4.2 测量（loop，唯一写点）

`agent/loop.py` 的 `_complete_once`（`loop.py:336-355`）两条分支：

- 流式分支：在 `async for event in self.provider.astream(...)` 内维护局部状态——首个 `reasoning_delta` 记录 `started_at`；随后首个 `text_delta` / `tool_call_started` / `message_completed` 计算间隔。流结束后设置 `final_response.diagnostics.reasoning_seconds`。若从未见 `reasoning_delta`，保持 `None`。
- 非流式分支：`run_sync(self.provider.complete, ...)` 前后 `time.monotonic()` 计时；返回 `response` 且 `response.diagnostics.reasoning` 非空时设 `reasoning_seconds = elapsed`。
- `reasoning_delta` 之后紧跟工具调用时，边界取首个 `tool_call_started`——与 TUI live 的结算触发（`tool_event`）对齐。
- **落盘时序**：测量必须在 `loop.py:482` / `loop.py:444` 的 `append_assistant_response`（`_complete_turn`）之前完成——内联在 `_complete_once` 尾部满足。流式 provider 用同一 `diagnostics` 实例构造事件与最终 response（`openai_compatible.py:240-248`），故 loop 设置的 `reasoning_seconds` 与 TUI 拿到的 `response.diagnostics` 天然同实例同数。
- 重试（`_complete_once_with_recovery`）：只有最终提交的那次调用测得的数值会进入落盘的响应；失败流的测值随异常丢弃（`del self.last_stream_events[...]` 只清事件，不减测量态），store 只含最终值。

需要 `import time`（stdlib）。测量逻辑内联在 `_complete_once` 的局部状态内，不独立成模块（CLAUDE.md：inline single-use helpers）。

### 4.3 live 渲染（TUI）

**流程**：流式回合维持现状——projector 墙钟（`started_at` → `_finalize_thinking`）作为**临时显示值**，让回合进行中有 "Thought for Xs"。

**回合收尾读回（reconcile）**：

- **位置**：`_finish_chat_turn`（`tui.py:666-686`）**顶部、`_is_current_chat_turn(token)` 守卫通过之后、pending/nudge 提交之前**。理由（架构评审 P1-1）：`_finish_chat_turn` 会因排队输入/待处理后台完成推进 token 并启动新 worker；若 reconcile 放在 `_write_chat_response` 末尾，token 已失配、整段被跳过——而"turn 内后台任务完成再续 nudge turn"恰是后台并发的主力场景，parity 会在主场景失效。移到此位置后 `_run_chat_turn`、`_resume_permission_turn`、`_run_nudge_turn` 三个入口的统一收尾都覆盖（reconcile 幂等，可重复执行）。
- **窗口来源**：不复用 TUI 侧快照。`AgentChatRunner` 已有按回合的 store 窗口（`app/runtime.py:336,346` 的 `before_count`、`_refresh_turn_output` 的 `messages[before_count:]` 切片）。由 runner 新增只读方法 `turn_assistant_reasonings() -> list[tuple[str, float | None]]`（本回合尾段 assistant 消息中带 reasoning 的 `(text, reasoning_seconds)` 按序列表）；TUI 在 reconcile 调用它。store 读逻辑维持单一归属（runtime），TUI 不再自管 `rebuild_view()` + 快照。`CurrentSessionLike` 协议（`app/ports.py:30-32`）补 `rebuild_view` 声明，把 TUI 早已鸭子调用的能力正式化。
- **对位与合并语义**：TUI 维护"本回合已处理 reasoning 计数 i"；对 runner 返回的第 i 条 `(text, seconds)`：若 TUI 当前 assistant block 内已有第 i 个 thinking 子行（流式建立），覆盖其 `duration_seconds = seconds` 并 `_refresh_child_row`；若无，物化 `append_thinking(text, track_duration=False)` 再填 `seconds`。**合并规则必须复制 `append_thinking`（`projector.py:58-71`）**：若 block 最后一个 child 是未结算 THINKING（相邻 reasoning-only 消息连发会合并成一行），则跳过不物化、不覆盖——否则 live 会出现 replay 没有的行，直接打破 parity。（架构评审 P1-3）
- **非流式物化 DOM 顺序**：物化的 thinking 行必须插到该 assistant block 的 markdown 正文**之前**，保持 `render_block_into`（`tui_view.py:306-321`）"thinking → markdown → tools"契约。**禁止走 `_ensure_stream_block_rows`**（`tui_view.py:346-366`）：它在非流式回合会新建空的 streaming markdown widget 且行序错位。新增专用挂载助手（insert-before 目标 markdown widget，或其占位 slot）。（架构评审 P1-2）

### 4.4 重放（resume）

- `projector.py` 的 `_reasoning_from_message` 扩展（或旁路）：除 `reasoning` 文本外读取 `diagnostics.get("reasoning_seconds")`。
- `replay_messages` 的 assistant 分支：`append_thinking(reasoning, track_duration=False, duration_seconds=seconds)`。`append_thinking` 增加可选 `duration_seconds: float | None = None`：非 None 时创建后即设 `child.duration_seconds`（`finished` 后续照常由 `tool_event`/`end_turn` 结算置 True）。**注意 `_finalize_thinking`（`projector.py:41-44`）只在 `started_at` 非 None 时覆盖 duration**——track_duration=False 时不覆盖预设值，这条实现前提是 Phase 1 依赖的语义，保留并加将注释。缺 key → `None` → 渲染 `Thought`（与旧行为一致）。

### 4.5 Phase 1 边缘

- 中断回合：token 门禁使 reconcile 不执行，不改既有行为（残留未结算 thinking 由 `end_turn` 结算成 "Thought"）。
- 错误响应/部分回合：reconcile 只对已落盘消息生效，幂等，无害。
- 权限暂停跨段回合：reconcile 在每段收尾执行且幂等；runner 的 `before_count` 跨段持有，尾段完整。
- 多轮工具循环：每一轮独立测量、独立落盘；runner 尾段按消息序返回，reconcile 按序对位。
- guidance/回合内注入的用户消息：其 assistant 回复带推理则计入尾段列表。
- 旧会话消息缺 `reasoning_seconds`：读侧 `None` → "Thought"。

## 5. Phase 2 —— 通知友好行 parity

### 5.1 数据模型

`agent/background.py` 的 `BackgroundNotification`（`lines 110-122`）增加：

```python
error: str | None = None
```

`_notification_for`（`background.py:276-297`）从 `job.error` 填充（live 失败行使用同一字段，当前未拷贝，为新增）。`label` / `status` / `tool_name` 已存在。字段集与 `make_text_result` 的占位 key（`tools/types.py`）无交集，无碰撞。

### 5.2 写点（loop）

`_append_background_notifications`（`loop.py:687-700`）调用 `session.append_background_notification` 时新增 `label=notification.label`、`error=notification.error`。

签名链（均为加可选 kwarg、默认 None）：
- `agent/session.py` `append_background_notification`
- `context/writer.py` `append_background_notification` → part metadata 增加 `background_label`、`background_error`（与现有 `background_tool_name`、`background_status` 并列）。

### 5.3 渲染（单一 formatter）

app 层 `lanscoder/app/transcript_view.py` 新增：

```python
def background_notification_ui_text(*, label: str | None, tool_name: str, status: str, error: str | None) -> str
```

映射（与现有 live 输出逐字一致）：
- `completed` → `✅ 子agent [{label or tool_name}] 已完成`
- `failed` → `❌ 子agent [{label or tool_name}] 失败: {error or '未知错误'}`
- 其他 → `⚠️ 子agent [{label or tool_name}] {status}`

- **live**：`_handle_subagent_completed`（`tui.py:381-393`）改为调用同一 formatter，入参来自 `job`。
- **replay**：`replay_messages` 的 `role == "notification"` 分支改为从 part metadata 读 `background_label` / `background_tool_name` / `background_status` / `background_error`，用同一 formatter 产出友好行，渲染为 SYSTEM 块。不再渲染 XML 原文。
- live 与 replay 共用同一函数、同一入参解析 → 逐字一致由构造保证。
- `render_task_notification` / `context_builder` 不动：模型仍收到 XML 原文。part metadata 经 store rebuild 保留（`store.py:184-206`，`test_background_jobs.py:617-619` 已证明）。

### 5.4 投递延迟与退出冲刷（决策点）

现状：live 的友好行在 job 完成瞬间由 `_handle_subagent_completed` 渲染；store 消息要等 loop 下次进 `_append_background_notifications`（`loop.py:546`/`loop.py:687-700`）才落盘。若 job 在临近退出时完成（nudge 未及投递），会出现 live 有行、store 无行的窗口。现存没有任何退出冲刷钩子（`background_manager.shutdown()` `background.py:406-409` 定义了但从未被调用）。

**已决策：A**。给会话 teardown 增加一次冲刷——新增 loop/runner 的公开方法（如 `flush_background_notifications()`，内部调 `_append_background_notifications()`），并在组合根退出路径调用（TUI `on_unmount` 经既有的 `on_shutdown` 注入回调，或 CLI 退出路径）。冲刷是 loop 的写点能力、由组合根调用，保持"loop 是唯一写点"原则。无新事件类型。测试：模拟"完成→未投递→退出"，断言 store 有该通知。

## 6. 数据流示意

```
[流式] provider.astream --reasoning_delta--> loop 测起止
                                          |--> final_response.diagnostics.reasoning_seconds
                                          |--> _complete_turn -> session.append_assistant_response -> asdict 落盘(resolve)
                                          |--> observer -> TUI 墙钟临时值 (live)
TUI 回合收尾 _finish_chat_turn(顶部, token 守卫内):
    runner.turn_assistant_reasonings() --store 尾段--> 覆盖/物化子行 duration_seconds(含合并规则) -> 刷新行
引发 /resume: rebuild_view -> replay_messages -> 读 diagnostics.reasoning_seconds -> 子行时长
[通知] job 完成 -> _on_subagent_completed -> formatter -> live 友好行
      loop 下次边界 _append_background_notifications -> writer metadata(label,error) 落盘
      /resume: part metadata -> 同一 formatter -> SYSTEM 友好行
```

## 7. 测试策略（TDD，先写失败测试）

- **loop 测量**：假流式 provider 依序发 `reasoning_delta` → `text_delta`，断言响应 `diagnostics.reasoning_seconds` 非 None 且 >0；`reasoning_delta` → `tool_call_started` 边界；无 reasoning → None；非流式假 provider 断言整轮耗时被写入（有 reasoning 时）。
- **存储**：既有 session 持久化测试补 `reasoning_seconds` 在 `asdict` 中的往返。
- **projector 重放**：**只反转 `tests/test_projector.py:185-191` 的 replay 契约**（重放带 `diagnostics.reasoning_seconds` 的消息 → 子行 `duration_seconds` 等于该值；缺 key → None）。**保留 `173-183` 不动**——它 pin 的是 `_finalize_thinking` 不覆盖预设时长的实现前提（spec §4.4 依赖它）。另加一条：相邻 reasoning-only 消息连发时重放合并为一行（与 live 合并规则对位）。
- **TUI reconcile**：假 chat_runner + `FakeCurrentSession.rebuild_view()`（`tests/test_app_tui.py:405-412`）——确认 reconcile 对空 view/None session 幂等无副作用；再以带 reasoning 的尾段驱动断言 `_finish_chat_turn` 后子行 `duration_seconds` 等于落盘值。**必测场景：turn 内有待处理后台完成（触发 nudge）→ token 推进后 reconcile 仍执行且行上有店值**（P1-1 回归）。非流式回合物化出新行且 DOM 顺序在正文前。
- **通知**：formatter 三态单测（含 `error`/`label` 兜底、else 分支）；writer 断言 metadata 含 `background_label` / `background_error`；replay 断言 transcript 块为友好行且不包含 `<task_notification>` 字符串；live handler 与 formatter 同输入同输出（parity）；**`RecordingSession`（`tests/test_app_tui.py:1471-1482`）签名补 `label`/`error` kwarg**。
- **边界**：旧消息缺 key 重放仍显示 "Thought" / 工具名退化（人+通知各一条）。
- **层边界**：`test_layer_boundaries.py` 保持通过（新增 `projector.py → transcript_view.py` import 无反向依赖，`import time` 无新依赖）。

## 8. 变更面清单

| 文件 | 变更 | 阶段 |
|---|---|---|
| `lanscoder/providers/types.py` | `ProviderDiagnostics.reasoning_seconds`（含语义注释） | 1 |
| `lanscoder/agent/loop.py` | `_complete_once` 测量 + `import time` | 1 |
| `lanscoder/app/runtime.py` | `turn_assistant_reasonings()`（复用 `before_count` 尾段） | 1 |
| `lanscoder/app/ports.py` | `CurrentSessionLike` 协议补 `rebuild_view` | 1 |
| `lanscoder/app/tui.py` | `_finish_chat_turn` 顶部 reconcile、`_handle_subagent_completed` 改用 formatter、非流式物化 insert-before 挂载助手 | 1+2 |
| `lanscoder/app/projector.py` | `append_thinking(duration_seconds=...)`、`replay_messages` 读 reasoning_seconds、notification 分支用 formatter | 1+2 |
| `lanscoder/app/transcript_view.py` | `background_notification_ui_text` | 2 |
| `lanscoder/agent/background.py` | `BackgroundNotification.error` + `_notification_for` 填充 | 2 |
| `lanscoder/agent/session.py` + `lanscoder/context/writer.py` | `append_background_notification` 加 `label`/`error` kwarg → metadata | 2 |
| 退出路径（TUI `on_unmount` / CLI 退出） | 冲刷待投递通知（§5.4 选项 A 时） | 2 |
| 测试 | 见 §7 | 1+2 |

## 9. 兼容与迁移

- 旧会话文件：无 `reasoning_seconds` → "Thought"；无 `background_label`/`background_error` → 工具名 + 通用文案。均不劣于现状。
- provider 适配器、store 事件 JSON 格式、流事件种类：不变。
- `asdict` 序列化：新字段默认 `None`，与既有 JSON 结构兼容。

## 10. 遗留决策点

1. ~~§5.4 通知退出冲刷~~ **已决策：A**（§5.4）。