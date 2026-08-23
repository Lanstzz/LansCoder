# 子 agent 取消链路 + TUI 停止面板 — 设计文档

日期：2026-08-20
状态：设计定稿，待实施计划

## 1. 背景与问题

两个相关缺陷：

**A. 子 agent 不可取消。**
- 前台子 agent：用户双击 Esc 只取消了父 turn 的 `CancellationToken`，但 `_run_inline` / `_run_isolated` 构造子 `AgentLoop` 时**没有传 `cancellation_token`**（subagent.py），子 loop 的 `_check_cancelled()` 对 None token 直接 no-op，子 `ToolExecutor` 又用 `cancellation_context(None)` 把线程局部 token 覆盖成 None（loop.py:191 → tool_execution.py:530）。父 loop 被同步 delegate 阻塞在同一线程、自己的 `_check_cancelled` 也跑不到。结果：子 agent 跑完才返回，期间底部面板一直显示"还在跑"。
- 后台子 agent：`BackgroundJobManager.cancel()` 会 `job.token.cancel()`（background.py:437），job 线程在 `cancellation_context(job.token)` 里跑（background.py:279）——机制在，但子 loop 同样没接 token，取消不生效。

**B. 用户无入口管理子 agent。**
- 取消后台子 agent 只能靠模型调 `background_cancel` 工具，用户没有任何快捷键。
- 底部面板（`#subagent-panel`，tui.py:260 `_refresh_subagent_progress`）只读展示，无选中、无操作。

## 2. 目标

- 子 agent 真正可取消：前台经父 token、后台经 job token。
- TUI 面板支持选择（↑/↓ + 鼠标点击）与停止（`x`），对齐 Claude Code 的 `/tasks` 交互：选行高亮、`x` 停止、Esc 返回。
- 停止与"中断 turn"语义分离：`x` 停止选中的子 agent；Esc 退出选择模式；未选中时 Esc 双击仍中断 turn。
- 面板明确提示按键，让用户知道停止能力存在。

**非目标：**
- 不把前台 delegate 改成独立 job（"只停子 agent 不动 turn"需要改动 delegate 同步返回模型，不做）。停止前台子 agent 的效果 = 中断当前 turn。
- 不做 `/tasks` 命令式面板入口（用 down 键 + 鼠标点击进入，更轻）。

## 3. 设计

### 3.1 Part A — 取消链路

`lanscoder/agent/subagent.py`：

1. `_run_inline` / `_run_isolated` 构造子 `AgentLoop` 时传 `cancellation_token=current_cancellation_token()`。
   - 前台线程：delegate 在父 `cancellation_context` 内执行（tool_execution.py:530），`current_cancellation_token()` = 父 token。
   - 后台 job 线程：在 `cancellation_context(job.token)` 内执行（background.py:279），`current_cancellation_token()` = job token。
   - 因此同一行代码同时打通前台与后台。子 loop 的 `_check_cancelled`、子 ToolExecutor、子工具的 `current_cancellation_token()` 全部感知。
2. 取消结果语义化：子 loop 因取消抛 `AgentCancelledError`，或返回 `finish_reason=="interrupted"` 的响应时，**直接上抛 `AgentCancelledError`**，不包装成 "Subagent failed"。父 loop 已有 `except AgentCancelledError`（loop.py:772）→ `_append_interrupted_tool_results()` → interrupted 收尾。
   - `_run_inline`：在宽 `except Exception` 前加 `except AgentCancelledError: raise`；`response.finish_reason == "interrupted"` → `raise AgentCancelledError()`。
   - `_run_isolated`：因有内外两层宽 `except Exception`（"隔离 coder 执行失败"与"隔离执行初始化失败"），两层前都要加 `except AgentCancelledError: raise`。
3. 导入：`from lanscoder.runtime.cancellation import AgentCancelledError, current_cancellation_token`。

协作式取消的固有边界：子 agent 若正卡在不可中断的长 provider 调用/长工具里，中止要等那次调用返回后的下一个检查点，面板会短暂显示 `cancelling…`。

### 3.2 Part B — TUI 面板选择 + x 停止

**状态机抽成纯模块 `lanscoder/app/subagent_panel_state.py`**（便于单测，Textual 层保持薄）：

- 行模型：`SubagentRow(id, label, status, cancellable, cancel_requested)`；稳定 id：前台 `"fg"`，后台 `job.id`。
- `build_rows(foreground, jobs) -> list[SubagentRow]`：前台排前，后接 `active_jobs()`。
- `move_selection(rows, selected_id, direction) -> str | None`：↑/↓ 移动，边界 clamp。
- `can_enter_selection(rows, down_recall) -> bool`：down 无更新历史可回 且 有可取消的运行中子 agent。
- `stop_target(rows, selected_id) -> str | None`：选中项的停止目标（`"fg"` / job id / None）。
- `has_running(rows) -> bool`：面板是否有运行中的行。

**`lanscoder/app/tui.py`**：

- 状态：`_subagent_selected: str | None`、`_subagent_select_mode: bool`。
- `_refresh_subagent_progress`：
  - 用 `build_rows` 渲染；选中行加 `.selected` 类高亮。
  - `cancel_requested` 的行显示 `cancelling…`。
  - 每次重建按 id 重定位选中项；选中项消失即清空选中、退出选择模式。
  - 提示行：未进入选择模式且有子 agent → `↓ 进入选择 · 点击选择子agent`；选择模式 → `↑/↓ 选择 · x 停止 · Esc 返回`。
- `on_key`：
  - ESC：选择模式 → 退出选择返回；否则走现有 `_handle_escape_interrupt`（双击中断 turn）。
  - `x`：选择模式且有选中 → `stop_target`：后台 = `manager.cancel(job_id)`，前台 = `cancel_current_turn()`。停止后留在选择模式。
  - ↑/↓：选择模式 → `move_selection` 更新高亮；否则走历史回填；down 且 `can_enter_selection` → 进入选择模式（选中前台或第一个）。
  - 其他键：选择模式 → 先退出选择，再按原逻辑处理。
- 鼠标点击：面板每行 `Static` 设稳定 id，`on_click` 命中 `subagent-row-*` 行 → 选中 + 进入选择模式。

**`lanscoder/app/tui.tcss`**：`.selected` 高亮样式、提示行样式。

### 3.3 数据流

用户操作（ESC / x / ↑↓ / 鼠标点击）→ `on_key` / `on_click` → 更新选择状态 + 触发停止动作 → 0.5s 定时 `_refresh_subagent_progress` 重渲染高亮与提示。停止动作经 token 传播：`manager.cancel(job_id)` → `job.token.cancel()` → 后台子 loop 中止；`cancel_current_turn()` → 父 token → 前台子 loop 中止。

### 3.4 错误处理

- 取消是协作式：请求后子 agent 在下一个检查点中止；面板显示 `cancelling…`。
- 对已取消/已完成的选中项按 `x`：无效（no-op）。
- 子 agent 完成导致选中项消失：清空选中、退出选择模式。

## 4. 测试

### 4.1 Part A（`tests/test_delegate_tool.py` 或 `tests/test_background_jobs.py`）

- 前台：父 token 取消 → 子 agent 中止，`runner.run` 抛 `AgentCancelledError`（或父 loop 视作 interrupted），而非 "Subagent failed"。
- 后台：启动后台 job（子 agent 阻塞在 gate）→ `manager.cancel(job_id)` → 子 agent 中止、job 落 CANCELLED。

### 4.2 Part B（`tests/test_subagent_panel_state.py`）

- 纯函数单测：`build_rows` 顺序与 id、`move_selection` 边界、`can_enter_selection` 条件、`stop_target` 映射、`has_running`。

### 4.3 既有测试

- `tests/test_delegate_tool.py`、`tests/test_background_jobs.py`、`tests/test_app_tui.py` 相关用例保持绿色。

## 5. 验收标准

- 生产改动限定：`subagent.py`、`tui.py`、`tui.tcss`、新增 `subagent_panel_state.py`。
- 前台/后台子 agent 在 token 取消后均能中止，不被当失败。
- 面板：选择、高亮、`x` 停止、Esc 返回、提示行、`cancelling` 状态、鼠标点击均生效。
- 全量 `pytest -q` 全绿（既有 `test_mcp_integration.py` 环境性失败除外）+ `ruff check` + `ruff format --check`。

## 6. 范围外

- 前台 delegate 改独立 job（允许"只停子 agent 不动 turn"）。
- `/tasks` 命令式面板入口。
- 跨会话/跨 `/recall` 的子 agent 管理。
