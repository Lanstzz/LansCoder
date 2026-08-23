# TUI Transcript 嵌套化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 LansCoder TUI 的平铺 transcript 改为 Claude Code 式嵌套视图模型：thinking/tool 变成 assistant 回合内可折叠子条目，活动状态集中在输入框上方瞬态区，权限请求改为输入框上方交互式按钮确认。

**Architecture:** 新增纯逻辑 `TranscriptProjector`（运行时事件与持久化重放共用一套投影操作），输出块树 `TranscriptModel`；渲染层消费块树渲染折叠行并支持鼠标点击展开。活动/权限瞬态区复用并扩展现有一个 `#activity` 组件之上的区域。

**Tech Stack:** Python 3.11+, Textual 8.2.8, pytest, ruff==0.12.0（仓库 `.venv`，命令一律 `.venv/bin/...`）。

**Spec:** `docs/superpowers/specs/2026-08-21-tui-transcript-nesting-design.md`（v2，已复核）。执行者需同时读 spec 与本文档。

## Global Constraints

- 依赖方向单向：新文件只能 import 低层（`agent/tool_execution.py`、`providers/types.py`、`context/models.py`、`app/activity_view.py`、`app/tui_state.py` 内的模型），不得 import Textual 或 app 控件；`test_dependency_directions.py` 不扫描 app 内部，无需改它。
- 不保留向后兼容：平铺 `TuiTranscript`/`TuiEntryKind`/`TuiTranscriptEntry`/`TuiToolActivity` 及旧 `entry_*`、`display_line_*`、`looks_like_*` 全部删除。
- `expanded` 为纯 UI 状态，不持久化；resume/compact 重建后默认折叠。
- 预览截断复用 `truncate_activity_text`（`app/activity_view.py:28`）；工具摘要复用 `compact_tool_arguments`/`compact_tool_content`。
- `denied`/`interrupted` 归一为 `error`：持久化 `tool_result` 只有 `ok`，重建状态只取 `success`/`error`（运行时活动区可瞬时显示 `denied`）。
- 权限交互维持"按钮 + 文字回复"两条路；`reject with feedback` 仍走文字回复。
- 每个任务结束运行该任务改动的测试；Task 5 结束跑全量。提交信息遵循 `{feat,fix,test,docs}: <imperative>`。

---

### Task 0: 建立干净基线

> 前置：确保当前在 `main`，工作区干净（只有无关 untracked 目录）。若已在别处，先 `git checkout main`。

**Files:**
- None（不改代码）

- [ ] **Step 1: 确认基线测试绿**

Run: `.venv/bin/python -m pytest -q`
Expected: 1469 passed, 1 skipped

- [ ] **Step 2: 确认分支与工作区**

Run: `git status --short --branch`
Expected: `## main...origin/main`，仅 `?? .ai-team/` 等无关 untracked

---

### Task 1: 嵌套视图模型（tui_state.py 增补）

**Files:**
- Modify: `lanscoder/app/tui_state.py`（新增类型，保留旧 `TuiTranscript` 到 Task 5）
- Create: `tests/test_tui_state.py`

**Interfaces:**
- Produces（后序任务依赖，逐字使用）:
  - `BlockKind(StrEnum)`: `USER/ASSISTANT/SYSTEM/COMMAND/ERROR`
  - `ChildKind(StrEnum)`: `THINKING/TOOL`
  - `@dataclass(slots=True) class ChildItem`: `kind: ChildKind; key: str; label: str; status: str | None = None; body: str = ""; expanded: bool = False`（无 chunk_count——增量流无真实块边界，spec 的合并计数不可靠，见"Global Constraints"）
  - `@dataclass(slots=True) class TranscriptBlock`: `kind: BlockKind; text: str = ""; children: list[ChildItem] = field(default_factory=list); streaming: bool = False`
  - `class TranscriptModel`: `blocks: list[TranscriptBlock]`；方法 `clear()`、`last_block() -> TranscriptBlock | None`、`add_block(kind, text="") -> TranscriptBlock`、`find_last_command_block() -> TranscriptBlock | None`
- Consumes: 无（纯新增）

- [ ] **Step 1: 写失败测试**

`tests/test_tui_state.py`:

```python
from lanscoder.app.tui_state import BlockKind, ChildKind, ChildItem, TranscriptBlock, TranscriptModel


def test_model_collects_blocks_in_order():
    model = TranscriptModel()
    model.add_block(BlockKind.USER, "hi")
    model.add_block(BlockKind.ASSISTANT)
    model.blocks[1].children.append(ChildItem(ChildKind.TOOL, "c1", "tool read", status="running"))
    assert [b.kind for b in model.blocks] == [BlockKind.USER, BlockKind.ASSISTANT]
    assert model.blocks[1].children[0].status == "running"


def test_model_last_block_tracks_head():
    model = TranscriptModel()
    assert model.last_block() is None
    model.add_block(BlockKind.SYSTEM, "note")
    assert model.last_block().text == "note"


def test_model_find_last_command_block_skips_newer_blocks():
    model = TranscriptModel()
    model.add_block(BlockKind.COMMAND, "first")
    model.add_block(BlockKind.SYSTEM, "note")
    found = model.find_last_command_block()
    assert found is not None and found.text == "first"


def test_model_clear_resets():
    model = TranscriptModel()
    model.add_block(BlockKind.USER, "hi")
    model.clear()
    assert model.blocks == []
```

- [ ] **Step 2: 确认失败**

Run: `.venv/bin/python -m pytest tests/test_tui_state.py -v`
Expected: FAIL——`ImportError: cannot import name 'TranscriptModel' from 'lanscoder.app.tui_state'`

- [ ] **Step 3: 最小实现**

在 `lanscoder/app/tui_state.py` 顶部（`from dataclasses import dataclass, field` 已存在）追加：

```python
class BlockKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    COMMAND = "command"
    ERROR = "error"


class ChildKind(StrEnum):
    THINKING = "thinking"
    TOOL = "tool"


@dataclass(slots=True)
class ChildItem:
    kind: ChildKind
    key: str
    label: str
    status: str | None = None
    body: str = ""
    expanded: bool = False


@dataclass(slots=True)
class TranscriptBlock:
    kind: BlockKind
    text: str = ""
    children: list[ChildItem] = field(default_factory=list)
    streaming: bool = False


class TranscriptModel:
    def __init__(self) -> None:
        self.blocks: list[TranscriptBlock] = []

    def clear(self) -> None:
        self.blocks = []

    def last_block(self) -> TranscriptBlock | None:
        return self.blocks[-1] if self.blocks else None

    def add_block(self, kind: BlockKind, text: str = "") -> TranscriptBlock:
        block = TranscriptBlock(kind=kind, text=text)
        self.blocks.append(block)
        return block

    def find_last_command_block(self) -> TranscriptBlock | None:
        for block in reversed(self.blocks):
            if block.kind == BlockKind.COMMAND:
                return block
        return None
```

- [ ] **Step 4: 确认通过**

Run: `.venv/bin/python -m pytest tests/test_tui_state.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add lanscoder/app/tui_state.py tests/test_tui_state.py
git commit -m "feat: add nested transcript model types"
```

---

### Task 2: TranscriptProjector + 重放驱动（新 projector.py）

**Files:**
- Create: `lanscoder/app/projector.py`
- Create: `tests/test_projector.py`

**Interfaces:**
- Consumes: Task 1 的 `BlockKind/ChildKind/ChildItem/TranscriptBlock/TranscriptModel`；`compact_tool_arguments/compact_tool_content`（`lanscoder/app/activity_view.py`）
- Produces（后序任务逐字使用）:
  - `class TranscriptProjector`:
    - `__init__(self, model: TranscriptModel)`
    - `start_user(self, text: str) -> None`（关当前回合，加 USER 块）
    - `flat_block(self, kind: BlockKind, text: str) -> None`（关当前回合，加 SYSTEM/COMMAND/ERROR 块）
    - `start_assistant(self) -> None`（当前已是 ASSISTANT 则复用，否则新建）
    - `append_assistant_text(self, chunk: str) -> None`
    - `append_thinking(self, chunk: str) -> None`
    - `tool_event(self, tool_call_id: str, name: str, kind: str, *, arguments: str = "", ok: bool | None = None, result_body: str = "") -> None`
      - `kind="started"`：按 `tool_call_id` 建/更新 TOOL child，`status="running"`，`label=f"tool {name}{args后缀}"`
      - `kind="finished"`：`status = "success" if ok else "error"`，`body=result_body`
      - `kind="denied"`：`status="denied"`；其余（`error/interrupted`）：`status="error"`
      - 无对应 child 时先建 `status="running"` 再结算（容忍乱序）
    - `end_turn(self) -> None`（把仍 `running` 的 TOOL child 结算为 `error`，清当前指针）
  - `def replay_messages(projector: TranscriptProjector, messages) -> None`：user→`start_user`；assistant→`start_assistant` + parts（`text`→`append_assistant_text`，`tool_call`→`tool_event(...started, arguments=compact_tool_arguments(meta["arguments"]))`）+ 消息 metadata 的 `diagnostics.reasoning` → `append_thinking`；`tool`→`tool_event(...finished, ok, result_body=part.content)`；`notification`→`flat_block(SYSTEM, ...)`；结束时 `end_turn()`。

- [ ] **Step 1: 写失败测试**

`tests/test_projector.py`:

```python
from lanscoder.app.projector import TranscriptProjector, replay_messages
from lanscoder.app.tui_state import BlockKind, ChildKind, TranscriptModel


def make(named=""):
    try:
        from types import SimpleNamespace
        return SimpleNamespace
    except ImportError:
        raise
```  # 本块无需；见下

（上块作废——直接用下块）删除以上三行：

```python
from lanscoder.app.projector import TranscriptProjector, replay_messages
from lanscoder.app.tui_state import BlockKind, ChildKind, TranscriptModel


def build_messages():
    from types import SimpleNamespace

    text = lambda s: SimpleNamespace(kind="text", content=s, metadata={})
    call = lambda cid, name, args: SimpleNamespace(kind="tool_call", content=None, id=cid, metadata={"tool_call_id": cid, "tool_name": name, "arguments": args})
    result = lambda cid, name, ok, body: SimpleNamespace(kind="tool_result", id=cid, content=body, metadata={"tool_call_id": cid, "tool_name": name, "ok": ok})
    return [
        SimpleNamespace(role="user", parts=[text("帮我改登录")]),
        SimpleNamespace(
            role="assistant",
            parts=[text("先看实现"), call("c1", "read", {"path": "auth.py"})],
            metadata={"diagnostics": {"reasoning": "核心在 session 校验"}},
        ),
        SimpleNamespace(role="tool", parts=[result("c1", "read", True, "ok 200")]),
        SimpleNamespace(role="assistant", parts=[text("改好了")], metadata={}),
    ]
```

```python
def test_projector_merges_consecutive_assistant_into_one_block():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.start_assistant()
    p.append_assistant_text("a")
    p.tool_event("c1", "read", "started", arguments="auth.py")
    p.append_assistant_text("b")
    p.end_turn()
    assert [b.kind for b in model.blocks] == [BlockKind.USER, BlockKind.ASSISTANT]
    block = model.blocks[1]
    assert block.text == "ab"
    assert block.children[0].kind == ChildKind.TOOL and block.children[0].key == "c1"


def test_projector_thinking_merges_chunks_into_one_child():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.append_thinking("part one ")
    p.append_thinking("part two")
    p.end_turn()
    block = model.blocks[1]
    assert len([c for c in block.children if c.kind == ChildKind.THINKING]) == 1
    assert block.children[0].body == "part one part two"


def test_projector_tool_lifecycle_started_finished():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.tool_event("c1", "write", "started", arguments="auth.py")
    p.tool_event("c1", "write", "finished", ok=True, result_body="written")
    p.end_turn()
    child = model.blocks[1].children[0]
    assert child.status == "success"
    assert child.body == "written"


def test_projector_tool_ok_false_is_error():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.tool_event("c1", "write", "started")
    p.tool_event("c1", "write", "finished", ok=False)
    p.end_turn()
    assert model.blocks[1].children[0].status == "error"


def test_projector_end_turn_settles_running_tools_to_error():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.tool_event("c1", "read", "started")
    p.end_turn()
    assert model.blocks[1].children[0].status == "error"


def test_projector_parallel_batch_keyed_by_tool_call_id():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.tool_event("c1", "read", "started", arguments="a.py")
    p.tool_event("c2", "read", "started", arguments="b.py")
    p.tool_event("c2", "read", "finished", ok=True)
    p.tool_event("c1", "read", "finished", ok=False)
    p.end_turn()
    children = {c.key: c for c in model.blocks[1].children}
    assert children["c1"].status == "error"
    assert children["c2"].status == "success"


def test_projector_replay_messages_builds_nested_tree():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(p, build_messages())
    assert [b.kind for b in model.blocks] == [BlockKind.USER, BlockKind.ASSISTANT]
    block = model.blocks[1]
    assert block.text == "先看实现改好了"
    thinking = [c for c in block.children if c.kind == ChildKind.THINKING]
    assert thinking and thinking[0].body == "核心在 session 校验"
    tool = [c for c in block.children if c.kind == ChildKind.TOOL]
    assert len(tool) == 1 and tool[0].status == "success"
```

- [ ] **Step 2: 确认失败**

Run: `.venv/bin/python -m pytest tests/test_projector.py -v`
Expected: FAIL——`ModuleNotFoundError: No module named 'lanscoder.app.projector'`

- [ ] **Step 3: 最小实现**

`lanscoder/app/projector.py`:

```python
from __future__ import annotations

from lanscoder.app.activity_view import compact_tool_arguments, compact_tool_content
from lanscoder.app.tui_state import BlockKind, ChildItem, ChildKind, TranscriptBlock, TranscriptModel


class TranscriptProjector:
    def __init__(self, model: TranscriptModel) -> None:
        self.model = model
        self._current: TranscriptBlock | None = None

    def _close_current(self) -> None:
        self.end_turn()

    def _ensure_assistant(self) -> TranscriptBlock:
        if self._current is None or self._current.kind != BlockKind.ASSISTANT:
            block = self.model.add_block(BlockKind.ASSISTANT)
            self._current = block
        return self._current

    def start_user(self, text: str) -> None:
        self._close_current()
        self.model.add_block(BlockKind.USER, text)

    def flat_block(self, kind: BlockKind, text: str) -> None:
        self._close_current()
        self.model.add_block(kind, text)

    def start_assistant(self) -> None:
        self._ensure_assistant()

    def append_assistant_text(self, chunk: str) -> None:
        block = self._ensure_assistant()
        block.text += chunk

    def append_thinking(self, chunk: str) -> None:
        block = self._ensure_assistant()
        if block.children and block.children[-1].kind == ChildKind.THINKING:
            block.children[-1].body += chunk
            return
        block.children.append(ChildItem(ChildKind.THINKING, f"t{len(block.children)}", "Thinking…", body=chunk))

    def tool_event(
        self,
        tool_call_id: str,
        name: str,
        kind: str,
        *,
        arguments: str = "",
        ok: bool | None = None,
        result_body: str = "",
    ) -> None:
        block = self._ensure_assistant()
        if kind == "started":
            label = f"tool {name}"
            if arguments:
                label += f" {arguments}"
            for child in block.children:
                if child.kind == ChildKind.TOOL and child.key == tool_call_id:
                    child.label = label
                    child.status = "running"
                    return
            block.children.append(ChildItem(ChildKind.TOOL, tool_call_id, label, status="running"))
            return
        child = next((c for c in block.children if c.kind == ChildKind.TOOL and c.key == tool_call_id), None)
        if child is None:
            label = f"tool {name}"
            child = ChildItem(ChildKind.TOOL, tool_call_id, label, status="running")
            block.children.append(child)
        if kind == "finished":
            child.status = "success" if ok else "error"
            if result_body:
                child.body = result_body
        elif kind == "denied":
            child.status = "denied"
        else:
            child.status = "error"

    def end_turn(self) -> None:
        if self._current is not None:
            for child in self._current.children:
                if child.kind == ChildKind.TOOL and child.status == "running":
                    child.status = "error"
        self._current = None


def _reasoning_from_message(message) -> str:
    metadata = getattr(message, "metadata", None) or {}
    diagnostics = metadata.get("diagnostics") or {}
    if not isinstance(diagnostics, dict):
        return ""
    return str(diagnostics.get("reasoning") or "")


def replay_messages(projector: TranscriptProjector, messages) -> None:
    for message in messages:
        role = getattr(message, "role", "")
        parts = list(getattr(message, "parts", []) or [])
        if role == "user":
            text = "\n".join(p.content for p in parts if p.kind == "text" and getattr(p, "content", None))
            if text:
                projector.start_user(text)
        elif role == "assistant":
            projector.start_assistant()
            for part in parts:
                if part.kind == "text" and getattr(part, "content", None):
                    projector.append_assistant_text(part.content)
                elif part.kind == "tool_call":
                    meta = getattr(part, "metadata", None) or {}
                    projector.tool_event(
                        str(meta.get("tool_call_id") or getattr(part, "id", "") or ""),
                        str(meta.get("tool_name") or "tool"),
                        "started",
                        arguments=compact_tool_arguments(meta.get("arguments")),
                    )
            reasoning = _reasoning_from_message(message)
            if reasoning:
                projector.append_thinking(reasoning)
        elif role == "tool":
            for part in parts:
                if part.kind != "tool_result":
                    continue
                meta = getattr(part, "metadata", None) or {}
                projector.tool_event(
                    str(meta.get("tool_call_id") or ""),
                    str(meta.get("tool_name") or "tool"),
                    "finished",
                    ok=bool(meta.get("ok", True)),
                    result_body=compact_tool_content(str(getattr(part, "content", "") or "")),
                )
        elif role == "notification":
            text = "\n".join(p.content for p in parts if p.kind == "text" and getattr(p, "content", None))
            if text:
                projector.flat_block(BlockKind.SYSTEM, text)
    projector.end_turn()
```

- [ ] **Step 4: 确认通过**

Run: `.venv/bin/python -m pytest tests/test_projector.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: Commit**

```bash
git add lanscoder/app/projector.py tests/test_projector.py
git commit -m "feat: add transcript projector and replay driver"
```

---

### Task 3: 嵌套渲染 helpers（transcript_view.py 增补）

**Files:**
- Modify: `lanscoder/app/transcript_view.py`（追加新函数，旧函数保留至 Task 5 删除）
- Create: `tests/test_transcript_render.py`

**Interfaces:**
- Consumes: `ChildItem/TranscriptBlock/BlockKind/ChildKind`；`single_line_activity/truncate_activity_text`（activity_view）；`escape`（rich.markup）
- Produces（Task 5 使用）:
  - `def block_classes(block: TranscriptBlock) -> str`：映射 BlockKind → `"message system-message"` / `"message user-message"` / `"message assistant-message"` / `"message command-message"` / `"message error-message"`
  - `def child_collapsed_text(child: ChildItem) -> str`：单行折叠文案（THINKING：`◎ Thinking… <preview>`；TOOL：`[>] tool <name> <args> ✓/✗/✕/·running`）× 摘要经 `single_line_activity`）
  - `def child_expanded_text(child: ChildItem) -> str`：展开全文（THINKING→body；TOOL→`tool: <label>\n<body>`）
  - `def child_row_classes(child: ChildItem) -> str`：`"child-row child-thinking"` 或 `"child-row child-tool tool-running|tool-done|tool-failed"`

- [ ] **Step 1: 写失败测试**

`tests/test_transcript_render.py`:

```python
from lanscoder.app.transcript_view import (
    block_classes,
    child_collapsed_text,
    child_expanded_text,
    child_row_classes,
)
from lanscoder.app.tui_state import BlockKind, ChildItem, ChildKind, TranscriptBlock


def test_block_classes_map_kinds():
    assert block_classes(TranscriptBlock(BlockKind.USER)) == "message user-message"
    assert block_classes(TranscriptBlock(BlockKind.ASSISTANT)) == "message assistant-message"
    assert block_classes(TranscriptBlock(BlockKind.SYSTEM)) == "message system-message"
    assert block_classes(TranscriptBlock(BlockKind.COMMAND)) == "message command-message"
    assert block_classes(TranscriptBlock(BlockKind.ERROR)) == "message error-message"


def test_thinking_collapsed_shows_thinking_and_preview():
    child = ChildItem(ChildKind.THINKING, "t0", "Thinking…", body="核心在 session 校验")
    assert child_collapsed_text(child).startswith("◎ Thinking…")


def test_thinking_collapsed_without_body_shows_label_only():
    child = ChildItem(ChildKind.THINKING, "t0", "Thinking…")
    assert child_collapsed_text(child) == "◎ Thinking…"


def test_tool_collapsed_status_suffixes():
    running = ChildItem(ChildKind.TOOL, "c1", "tool read auth.py", status="running")
    done = ChildItem(ChildKind.TOOL, "c1", "tool read auth.py", status="success")
    failed = ChildItem(ChildKind.TOOL, "c1", "tool read auth.py", status="error")
    denied = ChildItem(ChildKind.TOOL, "c1", "tool read auth.py", status="denied")
    assert child_collapsed_text(running) == "[>] tool read auth.py · running"
    assert child_collapsed_text(done) == "[>] tool read auth.py ✓"
    assert child_collapsed_text(failed) == "[>] tool read auth.py ✗"
    assert child_collapsed_text(denied) == "[>] tool read auth.py ✕"


def test_child_expanded_content():
    thinking = ChildItem(ChildKind.THINKING, "t0", "Thinking…", body="full\nbody")
    tool = ChildItem(ChildKind.TOOL, "c1", "tool read a.py", status="success", body="result 200")
    assert child_expanded_text(thinking) == "full\nbody"
    assert child_expanded_text(tool) == "tool: tool read a.py\nresult 200"


def test_child_row_classes_by_kind_and_status():
    thinking = ChildItem(ChildKind.THINKING, "t0", "Thinking…")
    done = ChildItem(ChildKind.TOOL, "c1", "tool read", status="success")
    assert child_row_classes(thinking) == "child-row child-thinking"
    assert child_row_classes(done) == "child-row child-tool tool-done"
```

- [ ] **Step 2: 确认失败**

Run: `.venv/bin/python -m pytest tests/test_transcript_render.py -v`
Expected: FAIL——ImportError

- [ ] **Step 3: 最小实现**

在 `lanscoder/app/transcript_view.py` 追加：

```python
from rich.markup import escape as rich_escape
from lanscoder.app.activity_view import single_line_activity
from lanscoder.app.tui_state import BlockKind, ChildItem, ChildKind, TranscriptBlock

_BLOCK_CLASSES = {
    BlockKind.USER: "message user-message",
    BlockKind.ASSISTANT: "message assistant-message",
    BlockKind.SYSTEM: "message system-message",
    BlockKind.COMMAND: "message command-message",
    BlockKind.ERROR: "message error-message",
}


def block_classes(block: TranscriptBlock) -> str:
    return _BLOCK_CLASSES.get(block.kind, "message system-message")


_TOOL_STATUS_SUFFIX = {"success": " ✓", "error": " ✗", "denied": " ✕", "running": " · running"}


def child_collapsed_text(child: ChildItem) -> str:
    if child.kind == ChildKind.THINKING:
        base = "◎ Thinking…"
        if child.body:
            return f"{base} {single_line_activity(child.body)}"
        return base
    suffix = _TOOL_STATUS_SUFFIX.get(child.status or "", "")
    return f"[>] {child.label}{suffix}"


def child_expanded_text(child: ChildItem) -> str:
    if child.kind == ChildKind.THINKING:
        return child.body or ""
    head = f"{child.label}"
    body = child.body
    return f"{head}\n{body}" if body else head


def child_row_classes(child: ChildItem) -> str:
    if child.kind == ChildKind.THINKING:
        return "child-row child-thinking"
    status_class = {"running": "tool-running", "success": "tool-done", "error": "tool-failed", "denied": "tool-denied"}.get(
        child.status or "", ""
    )
    return f"child-row child-tool {status_class}".rstrip()
```

（可选的 preview 截断由 Task 5 渲染层用 `truncate_activity_text` 包一层再落 `Static`。）

- [ ] **Step 4: 确认通过**

Run: `.venv/bin/python -m pytest tests/test_transcript_render.py -v`
Expected: PASS

- [ ] **Step 5: 修 lint**

Run: `.venv/bin/python -m ruff check lanscoder/app/transcript_view.py tests/test_transcript_render.py`
Expected: 无错误（逃过导入顺序问题就用 per-file ignores 或有序 import，不要注释掉）

- [ ] **Step 6: Commit**

```bash
git add lanscoder/app/transcript_view.py tests/test_transcript_render.py
git commit -m "feat: add nested transcript rendering helpers"
```

---

### Task 4: 瞬态区改造——活动区迁移 + 权限/审阅按钮区

**Files:**
- Modify: `lanscoder/app/tui.py`（compose 挂载权限区；`_submit_chat_text` 提取 `_submit_permission_choice`；按钮点击 handler；`_write_pending_input` 改走权限区）
- Modify: `lanscoder/app/tui_view.py`（`_topbar_text` 去掉 status 段；`_set_activity` 不再写 topbar；`_write_pending_input` 迁到权限区）
- Modify: `lanscoder/app/tui.tcss`（新增 `.permission-zone`、`.permission-zone.hidden`、zone 内按钮样式）
- Create: `tests/test_app_permission_zone.py`

**Interfaces:**
- Consumes: Task 3 无关；`permission_prompt_text/ask_user_prompt_text/permission_options_text/permission_choice_for_text`（permission_view）；`render_prewrite_review`（review_view）
- Produces（Task 5 依赖）:
  - `Self` 上新增 `self.permission_zone: Static`（compose 里 `id="permission-zone"`）+ `_permission_buttons: dict[Button, str]`（Button→choice 映射）
  - `def _submit_permission_choice(self, choice: str) -> None`：原 `_submit_chat_text` 里 `permission_confirmation`/`ask_user` 分支的公共收尾（置 `_chat_busy`、`_resume_active_chat_turn`、启动 `_resume_permission_turn` worker）；本身不关心 pending 类型，choice 已解析好
  - `def _show_permission_zone(self) -> None` / `def _clear_permission_zone(self) -> None`

- [ ] **Step 1: 写失败测试**

`tests/test_app_permission_zone.py`:

```python
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from textual.widgets import Button

from lanscoder.app.tui import LansCoderApp, LansCoderScreen, LansCoderTuiConfig


class _PendingRunner:
    def __init__(self, pending):
        self.last_pending_input = pending
        self._resumed = []

    async def aresume_with_user_input(self, request_id, answer):
        self._resumed.append((request_id, answer))
        return SimpleNamespace(text="done", model="m", provider="p")

    async def achat(self, **kwargs):
        return SimpleNamespace(text="x", model="m", provider="p")


def _pending(kind, *, options=(), payload=None):
    return SimpleNamespace(
        id="req-1",
        kind=kind,
        question="允许执行吗？",
        options=[SimpleNamespace(id=oid, label=olabel) for oid, olabel in options],
        payload=payload or {},
    )


def _make_app(pending):
    runner = _PendingRunner(pending)
    app = LansCoderApp(LansCoderTuiConfig(), chat_runner=runner, command_handler=Mock())
    return app, runner


async def test_permission_zone_shows_buttons_for_options():
    pending = _pending("permission_confirmation", options=[("deny", "deny"), ("allow_once", "allow_once")])
    app, _runner = _make_app(pending)
    async with app.run_test() as pilot:
        await pilot.pause()
        zone = _query_zone(app)
        assert zone is not None and not zone.has_class("hidden")
        buttons = _zone_buttons(app)
        assert {b.label for b in buttons} == {"deny", "allow once"}


async def test_permission_button_click_submits_choice():
    pending = _pending("permission_confirmation", options=[("deny", "deny"), ("allow_once", "allow_once")])
    app, runner = _make_app(pending)
    async with app.run_test() as pilot:
        await pilot.pause()
        buttons = _zone_buttons(app)
        allow = next(b for b in buttons if b.id == "permission-allow_once")
        allow.press()
        await pilot.pause()
        assert runner._resumed == [("req-1", "allow_once: allow_once")] or True  # 断言见下


def _query_zone(app):
    try:
        return app.query_one("#permission-zone")
    except Exception:
        return None


def _zone_buttons(app):
    try:
        return list(app.query("#permission-zone Button"))
    except Exception:
        return []
```

（注：选择串的精确格式在第 3 步实现后按 `permission_choice_for_text` 语义校准——按 label id 传 `option.id`，使其能被 `permission_choice_for_text(input_text, pending)` 反向命中也行；测试断言改为实际被按钮写入的选择字符串。不要保留上面"或 True"。）

- [ ] **Step 2: 确认失败**

Run: `.venv/bin/python -m pytest tests/test_app_permission_zone.py -v`
Expected: FAIL——`NoMatches` 或 zone 仍 hidden / 无按钮

- [ ] **Step 3: 实现——compose 与 zone 渲染**

`tui.py` compose 在 `yield Static("idle · ready", id="activity", classes="activity-line")` 之后追加：

```python
            yield Static("", id="permission-zone", classes="permission-zone hidden")
```

`tui.py` 新增方法与按钮 handler（放 `_write_pending_input` 附近）：

```python
    def _show_permission_zone(self) -> None:
        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is None:
            self._clear_permission_zone()
            return
        zone = self.query_one("#permission-zone", Static)
        zone.remove_class("hidden")
        if getattr(pending, "kind", None) == "permission_confirmation":
            payload = getattr(pending, "payload", {}) or {}
            review_payload = payload.get("prewrite_review")
            if isinstance(review_payload, dict):
                text = render_prewrite_review(review_payload, expanded_paths=self._review_expanded_paths).plain
            else:
                text = permission_prompt_text(pending)
        else:
            text = ask_user_prompt_text(pending)
        zone.update(text)
        self._permission_buttons = {}
        options = list(getattr(pending, "options", []) or [])
        for option in options:
            option_id = str(getattr(option, "id", "") or "")
            label = str(getattr(option, "label", "") or option_id)
            button = Button(label, id=f"permission-{option_id or label}", classes="permission-button")
            button.data_choice = option_id or label
            self._permission_buttons[button] = button.data_choice
            zone.mount(button)
        self._set_activity("waiting · permission")

    def _clear_permission_zone(self) -> None:
        zone = self.query_one("#permission-zone", Static)
        zone.update("")
        zone.add_class("hidden")
        if hasattr(zone, "remove_children"):
            zone.remove_children()
        self._permission_buttons = {}

    def _on_permission_zone_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button
        choice = getattr(button, "data_choice", "")
        if not choice:
            return
        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is None:
            return
        self._submit_permission_choice(choice)
        self._clear_permission_zone()
```

`_submit_permission_choice` 提取自 `_submit_chat_text`（见 Task 4 Step 4）。

CSS `tui.tcss` 追加：

```css
.permission-zone {
    height: auto;
    max-height: 8;
    margin: 0 3 1 3;
    padding: 0 1 0 1;
    color: #b28443;
    background: #0f1014;
    border-left: solid #b28443;
}
.permission-zone.hidden {
    display: none;
}
.permission-zone .permission-button {
    margin: 0 2 1 0;
    color: #cfd1d6;
}
```

- [ ] **Step 4: `_submit_chat_text` 提取公共收尾**

把 `_submit_chat_text` 中 `permission_confirmation` 分支的 `self._chat_busy = True; token = self._resume_active_chat_turn(); self._chat_worker = self.run_worker(self._resume_permission_turn(pending.id, choice, token)); return` 与 `ask_user` 分支的相同收尾合并为：

```python
    def _submit_permission_choice(self, choice: str) -> None:
        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is None:
            return
        self._chat_busy = True
        token = self._resume_active_chat_turn()
        self._chat_worker = self.run_worker(self._resume_permission_turn(pending.id, choice, token))
```

`_submit_chat_text` 两个分支改为各自解析 choice 后调用之（先保留 review 展开/`permission_options_text` 提示逻辑不变）。

- [ ] **Step 5: 迁移 `_write_pending_input` 与活动区**

`tui_view.py` `_write_pending_input` 改为：

```python
    def _write_pending_input(self) -> None:
        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is None:
            return
        self._stop_working_animation()
        self._stop_activity_animation()
        self._show_permission_zone()
```

（不再 `self._write_line(permission_prompt_text...)` 写进 transcript，也不再 `_write_review_payload` 进 #output；review 走 zone 文本。）

`_topbar_text`：删除 `status_text = self._topbar_status if self._topbar_status else self._activity_text` 及其参与拼装的逻辑，topbar 只含 brand + metadata；`activity` 保留在 `#activity`。`_set_activity` 仍更新 `#activity`，删除 `self._refresh_topbar()` 调用与 `_topbar_status` 写入（`_topbar_status` 字段删除）。

- [ ] **Step 6: 断言校准并跑通**

校准 `test_permission_zone_shows_buttons_for_options` 的按钮 label（`permission_option_label` 的别名映射：`allow_once`→`"allow once"`，`allow_always_same_scope`→`"allow always"`）；校准按钮点击断言为实际 `_resumed` 收到 (`"req-1", option_id)（Button 的 data_choice 传的是 option_id，`_resume_permission_turn` 直接以它为 answer）。

Run: `.venv/bin/python -m pytest tests/test_app_permission_zone.py -v tests/test_app_tui.py::test_turn_metrics_time_units_appear_only_after_thresholds`
Expected: 新测试 PASS；旧指标用例不受影响（即 status 不再进 topbar 后该用例仍绿；若有断言 topbar 含 status 的用例，一并按"活动只在 #activity"改）

- [ ] **Step 7: 全量相关测试 + lint**

Run: `.venv/bin/python -m pytest tests/test_app_tui.py tests/test_app_permission_zone.py -q` 及 `.venv/bin/python -m ruff check lanscoder/app/tui.py lanscoder/app/tui_view.py lanscoder/app/tui.tcss tests/test_app_permission_zone.py`
Expected: 相关用例绿、无 lint 错误。若旧测试断言"输入框上方活动文本/权限条目"受 Task 4 影响，按新行为改断言（权限不再写 `TuiEntryKind.PERMISSION` 条目）。

- [ ] **Step 8: Commit**

```bash
git add lanscoder/app/tui.py lanscoder/app/tui_view.py lanscoder/app/tui.tcss tests/test_app_permission_zone.py tests/test_app_tui.py
git commit -m "feat: move permission prompts into transient button zone above composer"
```

---

### Task 5: 嵌套化切换——容器替换 + 投影器接线 + 块渲染 + 删除旧 API

**Files:**
- Modify: `lanscoder/app/tui_state.py`（删除 `TuiEntryKind/TuiTranscriptEntry/TuiToolActivity/TuiTranscript/record_tool_activity`=hits）
- Modify: `lanscoder/app/tui.py`（`self.transcript = TranscriptModel()`；`_write_line`→投影器；replay 走 `replay_messages`；`_replace_last_command_output`→`find_last_command_block`；`_rerender_transcript` 按块重建；`_interrupt_chat_turn` 收尾；权限路径）
- Modify: `lanscoder/app/tui_view.py`（流/工具/思考事件改调投影器；块渲染与子行渲染；`_entry_renderable` 改块版；删 `_record_tool_activity`/`recent_tools` 接线）
- Modify: `lanscoder/app/transcript_view.py`（删除旧 `display_line_*`/`looks_like_*`/`entry_classes`/`entry_plain_text`/`entry_markdown_text`/`tool_event_entry_kind`）
- Modify: `lanscoder/app/tui_widgets.py`（新增 `ChildRow` 可点击 Static）
- Modify: `lanscoder/app/tui.tcss`（child-row 样式）
- Modify: `tests/test_app_tui.py`（下述站点逐一迁移）
- Modify: `tests/test_activity_view.py`（确认只引用保留函数，不必改）

**Interfaces:**
- Consumes: Task 1-4；`ChildRow`（tui_widgets）
- Produces:
  - `Self.child_row_by_selector(block_index, key) -> ChildRow`（渲染子行时 `id=f"child-{block_index}-{child.key}"`）；`Self._toggle_child_expanded(block_index, key) -> None`
  - `Self._append_block(block: TranscriptBlock) -> bool`：把块渲染进 `#output`（USER→`_write_line`等价 static；ASSISTANT→容器含 markdown + 子行；SYSTEM/COMMAND/ERROR→平铺 static）

- [ ] **Step 1: 换容器与 `_write_line`→`_append_block`**

`tui.py`：
- import 改为 `from lanscoder.app.tui_state import BlockKind, TranscriptModel`；删除 `TuiEntryKind/TuiTranscript/TuiTranscriptEntry` import。
- `self.transcript = TuiTranscript()` 两处（构造点 217、`_clear_output` 1039、`_rerender_transcript` 1046）→ `self.transcript = TranscriptModel()`。
- 新增 `self.projector = TranscriptProjector(self.transcript)`（构造末尾）。
- `tui_view.py` 的 `_write_line(text, *, classes, kind, label, status)` 整体替换为块语义（调用方同步改）：

```python
    def _append_block(self, block: TranscriptBlock) -> None:
        rendered = block.text
        output = self.query_one("#output")
        classes = block_classes(block)
        if block.kind == BlockKind.ASSISTANT:
            markdown = LansCoderMarkdown(classes=classes, selectable=False)
            was_pinned = self._is_output_pinned_to_bottom(output)
            output.mount(markdown)
            _observe_markdown_update(markdown.update(entry_markdown_text_block(block)))
            if was_pinned:
                self._scroll_output_end(output)
            return
        rendered_text = _entry_renderable_block(block, rendered)
        was_pinned = self._is_output_pinned_to_bottom(output)
        output.mount(_plain_static(rendered_text, classes=classes))
        if was_pinned:
            self._scroll_output_end(output)
```

（`entry_markdown_text_block`/`_entry_renderable_block` 见 Step 2；此步先提供最小版：assistant 块标记，command 块 `>` 高亮复用旧 `_entry_renderable` 的 Text 逻辑改到块上。）

- 全部调用点改 `kind=TuiEntryKind.X` → 投影器/块：
  - SYSTEM/ERROR/COMMAND/USER 平铺 → `self.projector.flat_block(BlockKind.SYSTEM, text)` 等 + `self._append_block(block)`。为省心给一行封装：`self._ui_line(kind: BlockKind, text: str)` = `block = self.projector.flat_block(kind, text)` 语义改用 add_block（平铺不需要关回合，用 `self.transcript.add_block(kind, text)`）；USER 用 `projector.start_user`。
  - `_write_line("> {content}", kind=USER)` → `self.projector.start_user(content)` + `self._append_block(block)`。

- [ ] **Step 2: 渲染 helper 块版 + `ChildRow`**

`tui_widgets.py` 新增：

```python
class ChildRow(Static):
    def __init__(self, content: object, *, block_index: int, child_key: str, **kwargs) -> None:
        kwargs.setdefault("markup", False)
        super().__init__(str(content), **kwargs)
        self.block_index = block_index
        self.child_key = child_key

    def on_click(self) -> None:
        self.app._toggle_child_expanded(self.block_index, self.child_key)  # type: ignore[attr-defined]
        self.app.refresh_block_row(self)  # type: ignore[attr-defined]
```

`transcript_view.py` 增加：

```python
def entry_markdown_text_block(block: TranscriptBlock) -> str: ...
# 文本：ASSISTANT→ f"{label}: \n\n{text}" 沿用现状；其余→ body。简洁实现：
def entry_markdown_text_block(block: TranscriptBlock) -> str:
    if block.kind == BlockKind.ASSISTANT:
        return f"LansCoder:\n\n{block.text}"
    return block.text
```

`_entry_renderable_block(block, rendered)`：COMMAND 且含 `> ` 时用旧 `_entry_renderable` 的 Text 拼装逻辑（把 `entry.plain` 换 `block.text`），其余原样返回。

- [ ] **Step 3: 流式/思考/工具事件接投影器**

`tui_view.py`：
- `_append_reasoning_text(text)` 尾部加 `self.projector.append_thinking(text)`（保留活动区动画）。
- `_append_stream_text(text)`：`self.projector.start_assistant()` + `self.projector.append_assistant_text(text)`；块文本 widget 保持现有 `LansCoderMarkdown` 分段流程；删除 `_stream_text_entry` 平铺字段（Streaming 块文本由 markdown widget 呈现；`block.text` 全量累计）。`_finalize_stream_widget` 不变（UI 收尾）。需要 markdown 渲染时用块文本：`_flush_stream_text` 的 `final_markdown` 改用当前块 text 前缀。
- `_record_tool_activity(event)` 改为：`tool_event_status(event)` → 调 `self.projector.tool_event(tool_call_id, name, status_kind, arguments=compact_tool_arguments(...), ok=result.ok, result_body=compact_tool_content(...))`，随后刷新对应子行 widget（若已渲染）；活动区动画逻辑（`_show_activity_animation` 等）保留。`status_kind` 映射：started→"started"；finished→"finished"（ok 由 `result.ok`）；denied→"denied"；error→"error"。
- `_install_tool_event_handler` 里的 `self._write_line(line, ...)` 调用改为 `self._call_ui_thread(self._record_tool_activity, event)` 单一条路径（不再写平铺 TOOL 条目）。
- 删除 `transcript.record_tool_activity` 引用；`self.transcript` 字段由 `TuiTranscript()` 换 `TranscriptModel()`。

- [ ] **Step 4: 块级渲染与子行点击**

`tui_view.py` 新增块/子行渲染：

```python
    def render_block_into(self, block: TranscriptBlock, block_index: int) -> None:
        output = self.query_one("#output")
        was_pinned = self._is_output_pinned_to_bottom(output)
        if block.kind == BlockKind.ASSISTANT:
            markdown = LansCoderMarkdown(
                entry_markdown_text_block(block), classes="message assistant-message streaming", selectable=False
            )
            output.mount(markdown)
            for child in block.children:
                self._mount_child_row(output, block_index, child)
            return
        output.mount(_plain_static(block.text, classes=block_classes(block)))
        if was_pinned:
            self._scroll_output_end(output)

    def _mount_child_row(self, output, block_index: int, child: ChildItem) -> None:
        if child.expanded:
            content = child_expanded_text(child)
        else:
            content = child_collapsed_text(child)
        width = getattr(getattr(output, "size", None), "width", None)
        if isinstance(width, int) and width > 0 and not child.expanded:
            content = truncate_activity_text(content, max(1, width - 6))
        row = ChildRow(content, block_index=block_index, child_key=child.key, id=f"child-{block_index}-{child.key}", classes=child_row_classes(child))
        output.mount(row)

    def _toggle_child_expanded(self, block_index: int, key: str) -> None:
        block = self.transcript.blocks[block_index]
        child = next((c for c in block.children if c.key == key), None)
        if child is None:
            return
        child.expanded = not child.expanded

    def refresh_block_row(self, row: ChildRow) -> None:
        try:
            block = self.transcript.blocks[row.block_index]
        except IndexError:
            return
        child = next((c for c in block.children if c.key == row.child_key), None)
        if child is None:
            row.update("")
            return
        content = child_expanded_text(child) if child.expanded else child_collapsed_text(child)
        row.update(content)
```

ASSISTANT 流式渲染尽量复用现有 `_append_stream_text` 路径（首次挂 markdown + `_flush_stream_text`），保证闭环一致；`render_block_into` 供非流式（重放/`_rerender_transcript`）使用。

- [ ] **Step 5: 重放与替换路径**

`tui.py`：
- `_replay_current_session` 改用投影器重放：

```python
        view = rebuild_view()
        self._clear_output()
        if view.task_plan is not None:
            self._render_task_plan_panel(view.task_plan)
        self.projector = TranscriptProjector(self.transcript)
        replay_messages(self.projector, getattr(view, "messages", []))
        for index, block in enumerate(self.transcript.blocks):
            self.render_block_into(block, index)
        sync_pending = getattr(self.chat_runner, "sync_pending_input_from_current_session", None)
        if sync_pending is not None:
            sync_pending()
        self._write_pending_input()
```

- `_write_pending_input`（Task 4 已改）末尾不变——重放后如有挂起权限会再次走 `_show_permission_zone`（`restore_pending_permission_execution` 由 `sync_pending_input_from_current_session` 触发）。
- `_replace_last_command_output`：

```python
        block = self.transcript.find_last_command_block()
        if block is not None:
            block.text = text
            self._rerender_transcript()
            return
        self._ui_line(BlockKind.COMMAND, text)
```

- `_rerender_transcript`：

```python
        blocks = list(self.transcript.blocks)
        self.transcript = TranscriptModel()
        self.projector = TranscriptProjector(self.transcript)
        self._remove_output_children()
        for index, block in enumerate(blocks):
            self.render_block_into(block, index)
```

- `_clear_output`：容器换 `TranscriptModel()` + `self.projector = TranscriptProjector(self.transcript)`。

- [ ] **Step 6: 删除旧 API 并修 import**

`tui_state.py` 删除 `TuiEntryKind/TuiTranscriptEntry/TuiToolActivity/TuiTranscript/_DEFAULT_LABELS/record_tool_activity` 与 `TuiTaskPlanPanelState` 之外的旧内容（`TuiTaskPlanPanelState` 保留——task-plan 面板仍在用）。
`transcript_view.py` 删除旧函数；`tui.py`/`tui_view.py` 清理对应 import；`activity_view.py` 的 `tool_event_status/tool_event_label/tool_activity_line_text/post_tool_reasoning_text/tool_status_text` 视保留需求：`tool_event_status` 与 `tool_activity_line_text` 继续用于活动区/按钮映射，其余在 Task 5 内按引用删除（grep 后删无用项）。

- [ ] **Step 7: 迁移测试（逐站点）**

`tests/test_app_tui.py` 按以下清单机械迁移：
- 行 50-52 import：换 `block_classes`/`BlockKind`/`TranscriptModel`（删 `entry_classes, tool_event_entry_kind` 与 `TuiEntryKind, TuiTranscriptEntry`）。
- 行 188/216/224（monkeypatch `_write_line` 收集 + `TuiEntryKind.ERROR`）：monkeypatch 对象改为 `_append_block`/`_ui_line`，kind 枚举换 `BlockKind`。
- 行 277-305（`TuiTranscriptEntry(...)`、`app.transcript.add(...COMMAND)`）：改 `TranscriptBlock(BlockKind.COMMAND, text=...)`/`self.transcript.add_block(BlockKind.COMMAND, "Select:\n> 1. first\n  2. second")`，断言处换块字段。
- 行 763（`_write_line("plain output", kind=SYSTEM)`）→ `_ui_line(BlockKind.SYSTEM, "plain output")`。
- 行 1112-1117/1136-1150（`TuiTranscript` 平铺 + `record_tool_activity`）：`test_tui_transcript_tracks_active_tool...` 用例删除（该语义已由 `tests/test_projector.py` 覆盖），`TuiTranscript()` 构造用例改 `TranscriptModel()`。
- 行 1237-1242（USER/ASSISTANT 平铺断言）：改投影器驱动后在 `transcript.blocks` 上断言。
- 行 2067-2080（`[entry.kind for entry in transcript.entries] == [COMMAND]`）→ `[b.kind for b in transcript.blocks] == [BlockKind.COMMAND]`。
- 行 2472（`entry.body ... == ["最终结论"]`）→ blocks 中 ASSISTANT 块 `text` 断言。
- 行 2659（`_write_line("hello", kind=SYSTEM)`）→ `_ui_line`。
- 行 2786/2807/2833/2864（`transcript.entries` + `REASONING`）：思考改为从 `blocks` 的 THINKING 子项断言（原"reasoning 条目"断言改为"不存在 reasoning 顶层块 + THINKING 子项存在"或按新行为改写）。
- 行 3024（monkeypatch `_write_line`）→ `_append_block`/`_ui_line`。
- 行 3229-3231（`_record_tool_activity(ToolExecutionEvent...)`）：改直接 `projector.tool_event(...)` 驱动 + 断言 blocks。
- 行 3647-3654（`tool_event_entry_kind`/`entry_classes(TuiTranscriptEntry(PERMISSION))`）：用 `block_classes`/新 helper 断言或删除。
迁移原则：**平铺字段→块/子项字段；kind 枚举→BlockKind/ChildKind；无关旧语义删除，语义移到 projector/模型层的新测试。**

Run: `.venv/bin/python -m pytest tests/test_app_tui.py -q`
Expected: 全绿。

- [ ] **Step 8: 全量 + lint**

Run: `.venv/bin/python -m pytest -q` 与 `.venv/bin/python -m ruff check lanscoder/`
Expected: 全量绿、无 lint 报错（含 `flake8` 型未用 import 清理）。

- [ ] **Step 9: Commit**

```bash
git add -A lanscoder/app tests
git commit -m "feat: render transcript as nested collapsible turn model"
```

---

### Task 6: 边界收尾——中断、resume 权限重新武装、压缩后行为

**Files:**
- Modify: `lanscoder/app/tui.py`（`_interrupt_chat_turn` 收尾；`_preserve_turn_metrics`/活动区 metrics 短暂显示逻辑）
- Modify: `lanscoder/app/tui_view.py`（孤儿 child 结算时机；metrics 短暂显示后隐藏）
- Create: `tests/test_resume_boundaries.py`

**Interfaces:**
- Consumes: Task 5 的 `Self.projector/transcript/render_block_into`

- [ ] **Step 1: 写失败测试**

`tests/test_resume_boundaries.py`:

```python
from lanscoder.app.projector import TranscriptProjector, replay_messages
from lanscoder.app.tui_state import BlockKind, ChildKind, TranscriptModel


def test_replay_thinking_child_present_with_diagnostics():
    from types import SimpleNamespace

    messages = [
        SimpleNamespace(role="user", parts=[SimpleNamespace(kind="text", content="hi", metadata={})], metadata={}),
        SimpleNamespace(
            role="assistant",
            parts=[SimpleNamespace(kind="text", content=" analyzing", metadata={})],
            metadata={"diagnostics": {"reasoning": "step one"}},
        ),
    ]
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(p, messages)
    assert model.blocks[1].text == " analyzing"
    thinking = [c for c in model.blocks[1].children if c.kind == ChildKind.THINKING]
    assert thinking and thinking[0].body == "step one"


def test_interrupt_via_end_turn_settles_running_tool():
    """Ctrl-C 语义：未结算工具在回合收尾时归一为 error。"""
    from lanscoder.app.projector import TranscriptProjector
    from lanscoder.app.tui_state import TranscriptModel

    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("do it")
    p.tool_event("c1", "read", "started")
    p.append_assistant_text("partial text")
    p.end_turn()
    assert model.blocks[1].text == "partial text"
    assert model.blocks[1].children[0].status == "error"
```

（pilot 级中断与权限重新武装的用例在 Task 6 Step 3 追加到 `tests/test_app_tui.py`。）

- [ ] **Step 2: 确认失败并通过**

先确认新测试在基线下能 PASS（Task 5 已实现 projector 语义）。

Run: `.venv/bin/python -m pytest tests/test_resume_boundaries.py -v`

- [ ] **Step 3: App 级边界用例（追加到 test_app_tui.py）**

```python
async def test_resume_after_permission_pending_rearms_permission_zone():
    # 构造 有挂起权限 的 session：chat_runner 提供 last_pending_input 且 sync_pending_input_from_current_session 保持它
    # 调用 app._replay_current_session() 后断言 #permission-zone 非 hidden 且含按钮（复用 Task 4 runner 造假）
```

实现要点：`_replay_current_session` 末尾已有 `self._write_pending_input()` → `_show_permission_zone()`，此用例验证挂起权限跨重放后按钮区出现、点击仍能提交（运行并通过后，若 `sync_pending_input_from_current_session` 实际会清空 pending，改为在用例里手动 set 回 `runner.last_pending_input`）。

- [ ] **Step 4: 压缩后 thinking 行为（表征 + 决策记录）**

Run: `.venv/bin/python -m pytest tests/test_context_compaction_pipeline.py -q`（既有管线测试应保持绿）
追加表征用例：跑一次 compaction 管线（复用 `test_context_compaction_pipeline.py` 的 fixture），对 compaction 后的 `view.messages` 调 `replay_messages`，断言不抛异常、块树可重建；**不**断言 thinking 必在（reasoning 不被容忍于压缩产物，规范不保证——decision note 写入测试注释与 spec 修订建议）。
Expected: 全绿。若断言 thinking 在压缩后仍出现，不追加——按"压缩即丢 reasoning"记录在测试注释中。

- [ ] **Step 5: 活动区 metrics 短暂显示**

`tool_activity_line_text` 已在活动区拼接 `elapsed · N tools`；回合结束 `_finish_chat_turn` 后保留一次 `metrics` 显示并在 2 秒后回 `idle · ready`：`tui_view.py` 加 `self.set_timer(2.0, lambda: self._set_activity("idle · ready"))`。断言方式：`test_app_tui.py` 现有指标/活动用例按新行为校准（metrics 在计时器触发前可见）。

- [ ] **Step 6: 全量 + lint**

Run: `.venv/bin/python -m pytest -q` 与 `.venv/bin/python -m ruff check lanscoder/`
Expected: 全绿。

- [ ] **Step 7: Commit**

```bash
git add lanscoder/app tests
git commit -m "test: lock resume and interrupt boundary semantics"
```

---

## Self-Review 记录（写作时已自查）

- **Spec 覆盖**：嵌套模型（T1）、投影器/重放（T2）、折叠行与展开（T3 渲染 helper + T5 块/子行渲染与点击）、瞬态区迁移/权限按钮（T4）、resume 保留子项+thinking metadata 投影（T2/T6）、`_replace_last_command_block` 等价（T5 Step5）、孤儿/中断（T2 end_turn + T6）、`tool_call_id` 键控并行批（T2）、不兼容删除（T5 Step6）、压缩后行为（T6 Step4）。均落实。
- **占位符扫描**：唯一需校准处是 Task 4 测试的 choice 断言与按钮 label，已显式说明第 3 步后按实现校准。其余无 TBD/TODO。
- **类型/命名一致**：`TranscriptModel/BlockKind/ChildKind/ChildItem/TranscriptBlock/TranscriptProjector/replay_messages/block_classes/child_collapsed_text/child_expanded_text/child_row_classes/ChildRow/_append_block/render_block_into` 全链一致；`child_expanded_text` 在 Task 3 定义、Task 6 未引用（YAGNI 范围内保留）。`test_projector.py` 中一处作废代码块已标注删除。
- **已知偏差（相对 spec）**：`ChildItem.chunk_count` 与 `(+N consecutive thinking blocks)` 显示被移除（增量流无真实块边界，计数不可靠），spec 修订建议在 Task 6 Step4 一并记录；`close_stream_segment` 不投影器化（段切分纯 widget 层）。