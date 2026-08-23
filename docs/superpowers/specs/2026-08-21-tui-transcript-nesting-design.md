# TUI Transcript 嵌套化：对齐 Claude Code 显示模型

日期：2026-08-21（v2：按对抗性源码审核修订，见文末修订记录）
状态：已获逐节批准 + 审核修订待复审
分支：main @ 4e01bb8

## 背景与目标

当前 TUI 把用户消息、assistant 文本、thinking 块、tool 调用、tool 结果、权限请求等全部八种事件**平铺**进同一条 transcript（`lanscoder/app/tui_state.py` 的 `TuiTranscript`），对话流被过程噪音淹没。瞬态状态显示在**顶栏 topbar**，远离输入框。

目标：对齐 Claude Code 的 TUI 模型 —— 对话流以 user/assistant 文本为主体；thinking 与工具调用作为 assistant 回合内的**可折叠子条目**；运行中的活动状态显示在**输入框上方的瞬态活动区**；权限请求做成**输入框上方的交互式按钮确认**、不在 transcript 留永久条目。

持久化事实（审核已核实）：assistant 消息的 parts 内 text 与 tool_call 有序共存；`tool_result` 是**独立消息**，与其 tool_call 通过两边 metadata 共有的 `tool_call_id` 配对（`context/writer.py:175-199 / 363-367`，`PartKind` 见 `context/models.py:14-21`）。reasoning 内容不落 parts，整块存在 assistant 消息 `metadata["diagnostics"]["reasoning"]`（`agent/session.py:405-412`；provider 侧 `openai_compatible.py:122-123`）。显示层只是把这些拍平了。

## 需求决策记录（逐条确认过）

1. **transcript 形态**：tool、thinking 在最终 transcript 里是 **assistant 回合内可折叠子条目**，默认折叠一行、点击展开。
2. **瞬态区位置**：**输入框上方新增活动区**，仅 agent 活动时出现。
3. **resume**：历史恢复时**保留折叠子条目**——工具子项通过 `tool_call_id` 配对重建；thinking 子项**从 assistant 消息 `metadata["diagnostics"]["reasoning"]` 投影为消息级整块**（非逐 chunk，2026-08-21 按源码审核补正）。压缩管线后该 metadata 是否完整保留尚待测试（见第 5 节）。
4. **权限请求**：同步对齐为**输入框上方交互式按钮确认**，过程结果不留 transcript 永久条目；被拒/失败的工具本身保留为 `tool` 子条目（状态见第 4 节 denied/error 说明）。
5. **thinking 展示**：折叠行显示截断摘要 + 可展开全文；连续块合并计数；运行时流式逐段进入关联折叠条目。

## 第 1 节 · 数据结构：嵌套视图模型

替换 `lanscoder/app/tui_state.py` 的平铺模型：

```
TranscriptBlocks（顶层顺序列表 = 对话流）
├── Block(kind=USER, text)
├── Block(kind=ASSISTANT, text_slot, children, streaming_state)
│   ├── ChildItem(kind=THINKING, preview, body, expanded)   # 运行时逐 chunk；resume 整块投影
│   └── ChildItem(kind=TOOL, name, status, preview, body, expanded)  # 按 tool_call_id 键控
├── Block(kind=SYSTEM / COMMAND / ERROR, text)   # 全局性条目，保持顶层
```

- **回合边界**：一条 `USER` 之后的所有相邻 assistant 片段（含各自 children）合并进同一个 `ASSISTANT` 块，直到下一条 `USER / SYSTEM / COMMAND / ERROR`。分组依据消息序列（`message_id` 顺序），工具配对依据 `tool_call_id`（跨消息）。
- **ChildItem 归属**：thinking 增量、tool 事件追加到当前正在展开的 `ASSISTANT` 块；`end_turn()` 后新建块。运行时的 TOOL child 以 `tool_call_id` 键控（并行只读批会是 `started,started,finished,finished` 交错）。
- **PERMISSION 不进块**：权限请求完全走瞬态交互区；无永久记录。
- **thinking 合并**：连续 thinking 块并入单条 child，运行时时逐 chunk 累计；resume 时每条 assistant 消息投出一条整块 child。预览只显截断摘要；合并计数表达为 `(+N consecutive thinking blocks)`。
- 枚举精简：块级五种（`USER/ASSISTANT/SYSTEM/COMMAND/ERROR`）+ 子项级两种（`THINKING/TOOL`）。移除 `active_tool / recent_tools`（瞬态状态改由活动区组件持有）。

## 第 2 节 · 构建与更新路径

**一个投影器，两条入口。** 新增 `lanscoder/app/projector.py` 的 `TranscriptProjector`，纯逻辑、零 Textual 依赖，持有"当前 assistant 块"状态并暴露确定操作：

```
start_user(text)
append_assistant_text(chunk)   # 流式文本 → 当前块 text_slot，按段追加
append_thinking(chunk)         # reasoning_delta → THINKING child（body 全量累计，preview 截断）
tool_event(event)              # started/finished/denied → 按 tool_call_id 创建或更新 TOOL child
end_turn()                     # 下一条 USER 到来，收尾当前块
close_stream_segment()         # 现有"按工具事件切段"语义：切段后新文本落入下一段
```

- **运行时**：`tui.py` / `tui_view.py` 现有事件处理器改为调用投影器操作，不再直接平铺 `transcript.add(...)`。运行时的工具入口：`tui_view.py:198-229`（`ToolExecutionEvent.kind/tool_call/result/permission_request/prewrite_review`）；流式入口：`tui_view.py:169-184`（`ChatStreamEvent.kind=reasoning_delta/text_delta`）。
- **resume / 会话切换重建**：重放点在 `_replay_current_session`（`tui.py:1065-1119`），由 `rebuild_view()` 提供持久化消息，逐条投入全新投影器得到结构一致的块树；`_clear_output`（`tui.py:1037-1041`）置空。注意 `tui.py:1049` 一带只是内存条目的 `_rerender_transcript`，非重放点；`/compact` 只改 store、不重建 transcript。
- **resume 的投影器入口**：按持久化消息重放时——
  - text part → `append_assistant_text`（消息级整段）；
  - tool_call part → `tool_event` 建 child（`status=running` 起，等配对结果结算）；
  - `tool_result` 消息（自带 `ok` 与 `tool_call_id`）→ 结算对应 child 为 `success/error`；
  - assistant 消息 metadata 的 `diagnostics.reasoning` → 投一条整块 THINKING child。
  - 历史末尾若有挂起权限（`restore_pending_permission_execution`，`agent/session.py:236-276`），重放完成后**重新武装权限/审阅区**。
- **瞬态区状态机**（新组件持有 `active_tool / 计时 / 流式状态`）：控制输入框上方两个 widget —— **活动区**与**权限/审阅区**——的显示，同一时刻至多一个可见：
  `idle（两区都隐藏）→ running（活动区显示当前工具名 + 状态 + 计时）→ permission_wait / review_wait（活动区隐藏，权限/审阅区显示确认内容）→ idle`。
  回合结束在活动区显示 `elapsed · N tools`（复用 `turn_metrics_text`），短暂保留后隐藏。
- **顶栏迁移**：`_set_activity` / `_show_static_activity` 的顶栏活动渲染迁到活动区；`_topbar_text`（`tui_view.py:86-141`）去掉 status 段，顶栏只留常驻信息（模型 / 模式 / 标题）。
- **denied / interrupted 归一**：运行时 `denied` 只活在瞬态活动区；持久化侧 `tool_result` 只有 `ok` 布尔（`writer.py:175-192`），重建统一结算为 `success/error`。运行中 Ctrl-C 的在飞工具同样按 `error` 收尾，使运行时树与重放树同构。

## 第 3 节 · 渲染、折叠展开与布局

**布局**（改 `tui_view.py` / `tui.tcss`，组件库保持 Textual 8.2.8）：

```
┌ topbar（常驻：模型/模式/标题）───────────────┐
├ transcript（VerticalScroll，块列表）───────────┤
│   Block(USER) · Block(ASSISTANT:              │
│     [◎ Thinking… 截断摘要]                   │
│     [[>] tool read (auth.py)]                 │
│     文本流 LansCoderMarkdown)                 │
├ 活动区（返回状态机切换显示/隐藏）────────────────┤
├ 权限/审阅区（仅 waiting 时显示，含 Button 组）────┤
├ input（TextArea）──────────────────────────────┤
```

- **折叠行渲染**：
  - THINKING：`◎ Thinking… <preview>`，连续块合并显示 `(+N consecutive thinking blocks)`。
  - TOOL：`[>] tool <name> <args摘要>`，状态着色沿用现有工具状态样式（running / done / failed），展开后显示 arguments + result，预览截断复用 `compact_tool_arguments` / `compact_tool_content`。
- **展开交互**：仅**鼠标点击**折叠行（Textual `Static` + `on_click`，先例见子 agent 行 `tui.py:586-593`）；折叠行 Static 需带稳定 id/class 供 pilot 选择器；hover 提示可展开。不做行聚焦、不加快捷键。
- **权限 / 审阅区**：`Button` 从 `pending.options` 动态生成（`Allow once` / `Allow always` / `Deny` 等），点击即提交，复用 `permission_view.py` 的判词分派；`review_wait` 用 `Accept` / `Reject` 按钮。`reject with feedback` 需附加文本，保留"点击后到输入框写 feedback"与文字回复两条路径。（Button 动态生成在本仓无先例，App 级测试兜底，见第 5 节。）

## 第 4 节 · 错误处理与边界

- **流式中断（Ctrl-C）**：flush 部分文本保留为最终内容；当前 THINKING child 保留已累计 body；在飞 TOOL child 统一收尾为 `error`；回合收尾；活动区回 idle；保留现有 `"Interrupted current turn."` SYSTEM 块。
- **流 / 网络错误**：`ERROR` 块保持顶层平铺；若发生在流式中途，先 flush、标记在飞 tool 为 error，再落 ERROR 块。
- **孤儿 tool 事件**：`end_turn()` 时把所有仍 `running` 的 child 统一标记为 `error`，并清空活动区 active_tool，不留悬挂状态。
- **COMMAND 块替换**：picker 选中器依赖"反查最后一条 COMMAND 并整体替换"（现 `tui.py:1023-1035` 遍历 `transcript.entries`）。块模型提供**等价的块级操作** `replace_last_command_block(text)`，否则 picker 用例失守。
- **权限/审阅跨 resume**：瞬态区不被持久化；resume 遇到挂起权限时重新武装权限区（见第 2 节）。
- **resume / compact 重建**：`expanded` 是纯 UI 状态、不持久化，重建后默认折叠。
- **权限等待无超时**：权限区停留直到用户选择，与现状一致。
- **预览截断按当前宽度**：preview 渲染时按列宽截断（沿用 `truncate_activity_text`），resize 自动重排。
- **并发**：投影操作与状态机都在 Textual 事件循环单线程内；后台 worker 沿用现有 post 机制。TOOL child 按 `tool_call_id` 键控以匹配并行批事件交错。
- **不兼容**：平铺 `TuiTranscript` / 旧 `TuiEntryKind` 八枚举全部移除，不保留兼容层。

## 第 5 节 · 测试计划与改动清单

**改动文件**
- 新增 `lanscoder/app/projector.py`：`TranscriptProjector`（核心新模块，只依赖 `agent/tool_execution.py`、`providers/types.py`、`context/models.py` 等低层，不 import Textual / app 控件）。
- `tui_state.py`：嵌套块模型 + 枚举精简，移除 `active_tool / recent_tools`。
- `tui_view.py`：事件处理器改调投影器；折叠行渲染与点击展开；活动区渲染迁移；`_write_line/_append_stream_text/_record_tool_activity/_set_activity/_show_static_activity` 迁出，`_topbar_text` 去 status 段。
- `tui.py`：compose 插活动区 + 权限按钮区；`_clear_output / _replay_current_session / _rerender_transcript / _interrupt_chat_turn / _submit_chat_text` 权限路径改一派；resume 权限重新武装；`replace_last_command_block` 等价操作。
- `transcript_view.py` / `activity_view.py`：平铺渲染 helper 删改（`entry_classes/entry_plain_text/entry_markdown_text/display_line_*/tool_event_entry_kind` 只服务顶层块或删除）；保留截断 / 状态文本工具供预览复用。
- `tui.tcss`：child-item、zone 样式。

**测试**
1. 新增 `tests/test_transcript_projector.py`（核心）：回合合并、thinking 合并计数、tool `started→finished/denied` 状态迁移、孤儿 tool 收尾、`end_turn` 语义、`close_stream_segment` 分段、并行批（`started,started,finished,finished`）按 `tool_call_id` 键控。
2. **resume 重建等价性**：同一持久化消息序列，全量重放 vs 运行时顺次操作 → 结构一致；含工具**跨消息配对**（`tool_call ⇄ tool_result` via `tool_call_id`）与 **thinking 从 metadata 投影**的专用用例。
3. **压缩后 thinking 重建**（审核 UNVERIFIED 义务）：经 `llm_compact` 管线后 `metadata["diagnostics"]["reasoning"]` 是否完整保留 → 重建仍有 thinking 子项；若压缩会丢，本测试先暴露，再定对策。
4. 折叠行格式与按列宽截断的预览（沿用 `truncate_activity_text` 现有测试）。
5. 权限按钮映射单测；活动区状态机单测（提炼纯逻辑）。
6. App 级 pilot 测试（沿用 `test_app_tui.py` 风格）：一次回合（user → 流式 → tool child → 结束）；**折叠行 `pilot.click`**（带 id 选择器）展开 / 收起；权限按钮出现并点击回包；resume 挂起权限重新武装；`replace_last_command_block`（picker）回归。
7. 迁移引用旧平铺 API 的用例（`tests/test_app_tui.py` 约 16+ 处），保证全绿。

## 范围外 / 明确不做

- 顶栏保留（常驻信息），但活动文本迁出。
- 不做完全瞬态化 tool / thinking（已否决）；不做渲染层虚拟分组（已否决）；不做事件源 + 共享投影器（已否决，避免过度设计；`session/transcript.py` 分享导出保持现状）。
- 不改存储格式 / 不加 thinking part kind（2026-08-21 决策：reasoning 从 metadata 投影）。
- 无向后兼容层。
- 权限交互维持"按钮 + 文字回复均可"，不引入独立输入模式。

## 修订记录

- **v2（2026-08-21）** 按对抗性源码审核修正三处必改：
  1. thinking 恢复机制改为从 `metadata["diagnostics"]["reasoning"]` 投影（原"parts 重放"拿不到 thinking，`PartKind` 无该项）。
  2. 持久化嵌套表述修正：`tool_result` 是独立消息、靠 `tool_call_id` 配对，不是"同消息有序 parts"。
  3. 补齐边界语义：重放点改为 `_replay_current_session`（原引 `tui.py:1049` 实为内存重渲染）；denied/interrupted 归一为 error 使双树同构；`replace_last_command_block` 等价操作；resume 权限区重新武装；并行批 `tool_call_id` 键控。
  并新增对应测试义务（压缩后 thinking 重建、跨消息配对、pilot.click 折叠行、Button 动态生成）。

## 后续

- 用户复审本文档。
- 获批后调用 writing-plans skill 生成实现计划。