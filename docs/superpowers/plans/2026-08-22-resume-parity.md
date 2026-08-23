# Resume 视图 Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `/resume` 后的 TUI 与退出前的最终显示逐字一致：thinking 子行保留真实时长（`Thought for Ns`），后台通知显示友好行而非 `<task_notification>` XML。

**Architecture:** 「store 是唯一真相源、loop 是唯一写点、TUI 只重放或在其既有读回窗口读」。Phase 1 在 `_complete_once` 统一测量 reasoning 阶段时长写进 `ProviderDiagnostics.reasoning_seconds`（现 `asdict` 落盘，store 事件格式不变），TUI 在 `_finish_chat_turn` 从 runner 的本回合窗口读回并覆盖/物化 thinking 子行。Phase 2 把通知的 label/error 写进 part metadata，live 与 replay 共用同一 formatter。

**Tech Stack:** Python 3.12（slots dataclass、anyio、pytest、asyncio 测试）、Textual TUI、JSONL append-only store。

**Spec:** `docs/superpowers/specs/2026-08-22-resume-parity-design.md`

## Global Constraints

- 不加新 store 事件种类，不加新流事件种类；`lanscoder/providers/*` 适配器不改（除 `types.py` 的字段）。
- 所有新字段可选，读侧 `.get()` 兜底；旧会话降级：thinking 显示 `Thought`、通知回退工具名。
- 仅 Task 2 需要反转 `tests/test_projector.py:185-191`；**`173-183` 必须保留**（pin `_finalize_thinking` 不覆盖预设时长）。
- 依赖方向单向：app 层可引用下层/同层；`agent/` 不引用 `app/`。
- 提交信息格式 `{feat,fix,test}: <imperative>`，只提交本任务文件；不 commit 除非另有指示，本计划按 TDD 逐任务提交，最终是否提交以用户确认执行方式为准。

---

## 文件结构总览

- `lanscoder/providers/types.py` — `ProviderDiagnostics.reasoning_seconds` 字段
- `lanscoder/agent/loop.py` — `_complete_once` 测量 + `import time`；`flush_background_notifications()`（公开冲刷）
- `lanscoder/app/runtime.py` — runner 本回合 reasoning 缓存 `last_turn_reasonings` + `flush_background_notifications()` 委托
- `lanscoder/app/ports.py` — `CurrentSessionLike` 补 `rebuild_view`
- `lanscoder/app/projector.py` — `append_thinking(duration_seconds=...)`；replay 读 `reasoning_seconds`；notification 分支用 formatter
- `lanscoder/app/tui.py` — `_finish_chat_turn` 顶部 reconcile；`_handle_subagent_completed` 用 formatter
- `lanscoder/app/transcript_view.py` — `background_notification_ui_text`
- `lanscoder/agent/background.py` — `BackgroundNotification.error`
- `lanscoder/agent/session.py` + `lanscoder/context/writer.py` — 通知 metadata `background_label`/`background_error`
- `lanscoder/app/factory.py` — `on_shutdown` 组合冲刷 + `mcp_manager.close`

测试文件：`tests/test_agent_context_loop.py`、`tests/test_projector.py`、`tests/test_app_runtime.py`、`tests/test_app_tui.py`、`tests/test_session_transcript.py`、`tests/test_background_jobs.py`、`tests/test_app_factory.py`。

---

## Task 1: reasoning 时长测量（ProviderDiagnostics + loop）

**Files:**
- Modify: `lanscoder/providers/types.py:53-59`
- Modify: `lanscoder/agent/loop.py:330-358`（`_complete_once`）
- Test: `tests/test_agent_context_loop.py`

**Interfaces:**
- Consumes: 无（新代码）
- Produces: `ProviderDiagnostics.reasoning_seconds: float | None`；`ChatResponse.diagnostics.reasoning_seconds` 在流式/非流式两条分支按阶段/整轮耗时填充

- [ ] **Step 1: 写失败测试**

在 `tests/test_agent_context_loop.py` 追加（文件已有 `FakeProvider`/`ObservingStreamingProvider` 等 dataclass provider 与 `_run_streaming` helper）：

```python
@dataclass
class ReasoningStreamingProvider(ChatProvider):
    response: ChatResponse

    @property
    def name(self) -> str:
        return "reasoning-stream"

    @property
    def model(self) -> str:
        return "reasoning-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("streaming test should not call complete")

    async def astream(self, request: ChatRequest):
        yield ChatStreamEvent(kind="message_started")
        yield ChatStreamEvent(kind="reasoning_delta", text="think part ")
        yield ChatStreamEvent(kind="reasoning_delta", text="two")
        await asyncio.sleep(0.01)
        yield ChatStreamEvent(kind="text_delta", text="answer")
        yield ChatStreamEvent(kind="message_completed", response=self.response)


def test_streaming_measurement_records_reasoning_seconds() -> None:
    response = ChatResponse(
        provider="reasoning-stream",
        model="reasoning-stream-model",
        content="answer",
        diagnostics=ProviderDiagnostics(reasoning="think part two"),
    )
    loop = create_agent_loop(
        session=_memory_session(),
        provider=ReasoningStreamingProvider(response),
        background_manager=None,
    )
    result = _run_streaming(loop, "你好")
    assert result.diagnostics.reasoning_seconds is not None
    assert result.diagnostics.reasoning_seconds > 0


def test_streaming_without_reasoning_keeps_reasoning_seconds_none() -> None:
    response = ChatResponse(provider="r", model="r", content="plain", diagnostics=ProviderDiagnostics())
    loop = create_agent_loop(session=_memory_session(), provider=ReasoningStreamingProvider(response), background_manager=None)
    result = _run_streaming(loop, "你好")
    assert result.diagnostics.reasoning_seconds is None
```

`_memory_session()` 是该文件里已有的 session 构造（见现有测试用例，如 `_session()`/`store` 的惯用法；若该文件没有现成 helper，用一个临时 `JsonlSessionStore` 建 `AgentSession`，与文件内其他测试一致。实现时照现有用例的 session 构造方式写一行复用）。

- [ ] **Step 2: 运行确认失败（reasoning_seconds 恒 None）**

`./.venv/bin/python -m pytest tests/test_agent_context_loop.py -k "reasoning_seconds" -v`
Expected: FAIL（字段不存在 → AttributeError）

- [ ] **Step 3: 实现**

`lanscoder/providers/types.py`：
```python
@dataclass(slots=True)
class ProviderDiagnostics:
    reasoning: str | None = None
    # 流式：首个 reasoning_delta 到首个 text_delta/tool_call_started/message_completed 的墙钟间隔；
    # 非流式：整轮 complete 调用耗时（近似），仅在有 reasoning 时写入。
    reasoning_seconds: float | None = None
    raw_finish_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
```

`lanscoder/agent/loop.py` 顶部加 `import time`。`_complete_once`（当前 335-358 行）改为：

```python
        if not streaming:
            started_at = time.monotonic()
            response = await anyio.to_thread.run_sync(self.provider.complete, prepared.request)
            if response.diagnostics.reasoning:
                response.diagnostics.reasoning_seconds = max(0.0, time.monotonic() - started_at)
        else:
            start_event_count = len(self.last_stream_events)
            final_response: ChatResponse | None = None
            reasoning_started_at: float | None = None
            reasoning_seconds: float | None = None
            try:
                async for event in self.provider.astream(prepared.request):
                    self._check_cancelled()
                    self.last_stream_events.append(event)
                    self._observer.on_stream_event(event)
                    kind = event.kind
                    if kind == "reasoning_delta":
                        if reasoning_started_at is None:
                            reasoning_started_at = time.monotonic()
                    elif reasoning_started_at is not None and reasoning_seconds is None and kind in {"text_delta", "tool_call_started", "message_completed"}:
                        reasoning_seconds = max(0.0, time.monotonic() - reasoning_started_at)
                    if kind == "message_completed":
                        final_response = event.response
                if final_response is None:
                    raise ProviderError(
                        ProviderErrorKind.API_ERROR,
                        "provider stream ended without message_completed event",
                    )
            except ProviderError:
                del self.last_stream_events[start_event_count:]
                raise
            if reasoning_seconds is not None:
                final_response.diagnostics.reasoning_seconds = reasoning_seconds
            response = final_response
```

- [ ] **Step 4: 运行确认通过**

`./.venv/bin/python -m pytest tests/test_agent_context_loop.py -k "reasoning_seconds" -v` Expected: PASS

再跑全文件确认无回归：`./.venv/bin/python -m pytest tests/test_agent_context_loop.py -q`

- [ ] **Step 5: Commit**

```bash
git add lanscoder/providers/types.py lanscoder/agent/loop.py tests/test_agent_context_loop.py
git commit -m "feat: record reasoning phase duration in provider diagnostics"
```

---

## Task 2: 重放恢复时长（append_thinking + replay_messages）

**Files:**
- Modify: `lanscoder/app/projector.py:58-71`（`append_thinking`）、`138-187`（`_reasoning_from_message` + `replay_messages`）
- Test: `tests/test_projector.py`

**Interfaces:**
- Consumes: `ProviderDiagnostics.reasoning_seconds`（Task 1）
- Produces: `TranscriptProjector.append_thinking(chunk, *, track_duration=True, duration_seconds=None)`；`replay_messages` 对带 `diagnostics.reasoning_seconds` 的消息产出 `finished=True` 且 `duration_seconds` 等于该值的 thinking 子行

- [ ] **Step 1: 反转/新增失败测试**

`tests/test_projector.py`——把 `test_projector_replay_thinking_finished_without_duration`（185-191 行）改为"重放带秒数"契约；`build_messages()` 的 assistant 消息 metadata 增加 `reasoning_seconds`；保留 `test_projector_append_thinking_track_duration_false_leaves_no_duration`（173-183 行）不动，并新增两条：

```python
def build_reasoning_messages(*, seconds):
    from types import SimpleNamespace

    def text(s):
        return SimpleNamespace(kind="text", content=s, metadata={})

    return [
        SimpleNamespace(role="user", parts=[text("hi")]),
        SimpleNamespace(
            role="assistant",
            parts=[text("answer")],
            metadata={"diagnostics": {"reasoning": "replayed", "reasoning_seconds": seconds}},
        ),
    ]


def test_projector_replay_e2e_restores_reasoning_duration():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(p, build_reasoning_messages(seconds=12.5))
    thinking = [c for c in model.blocks[1].children if c.kind == ChildKind.THINKING]
    assert thinking and thinking[0].finished is True
    assert thinking[0].duration_seconds == 12.5


def test_projector_replay_missing_duration_stays_thought():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(p, build_reasoning_messages(seconds=None))
    thinking = [c for c in model.blocks[1].children if c.kind == ChildKind.THINKING]
    assert thinking and thinking[0].duration_seconds is None


def test_projector_replay_consecutive_reasoning_only_messages_merge_once():
    """相邻 reasoning-only 消息在重放中必须合并成一个子行(与 live 合并语义对位)。"""
    from types import SimpleNamespace

    def reasoning_only(seconds):
        return SimpleNamespace(
            role="assistant",
            parts=[SimpleNamespace(kind="text", content="", metadata={})],
            metadata={"diagnostics": {"reasoning": f"r{seconds}", "reasoning_seconds": seconds}},
        )

    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    replay_messages(p, [reasoning_only(3.0), reasoning_only(4.0)])
    p.end_turn()
    thinking = [c for c in model.blocks[1].children if c.kind == ChildKind.THINKING]
    assert len(thinking) == 1
    assert thinking[0].duration_seconds == 3.0  # 首个 reasoning 的秒数胜出
```

- [ ] **Step 2: 运行确认失败**

`./.venv/bin/python -m pytest tests/test_projector.py -q` Expected: FAIL（`test_projector_replay_thinking_finished_without_duration` 断言 duration None 仍通过但新契约失败/`AttributeError`）

- [ ] **Step 3: 实现**

`lanscoder/app/projector.py`：

```python
    def append_thinking(self, chunk: str, *, track_duration: bool = True, duration_seconds: float | None = None) -> None:
        block = self._ensure_assistant()
        if block.children and block.children[-1].kind == ChildKind.THINKING:
            block.children[-1].body += chunk
            return
        child = ChildItem(
            ChildKind.THINKING,
            f"t{len(block.children)}",
            "Thinking…",
            body=chunk,
            started_at=time.monotonic() if track_duration else None,
        )
        if duration_seconds is not None:
            child.duration_seconds = duration_seconds
            child.finished = True
        block.children.append(child)
```

`_reasoning_from_message` 改为返回元组，`replay_messages` 的 assistant 分支改读秒数（保留注释强调 `_finalize_thinking` 只在 `started_at` 非 None 时覆盖时长，此处 track_duration=False 保证不覆盖预设值）：

```python
def _reasoning_from_message(message) -> tuple[str, float | None]:
    metadata = getattr(message, "metadata", None) or {}
    diagnostics = metadata.get("diagnostics") or {}
    if not isinstance(diagnostics, dict):
        return "", None
    return str(diagnostics.get("reasoning") or ""), diagnostics.get("reasoning_seconds") or None
```

```python
        elif role == "assistant":
            projector.start_assistant()
            # 投影顺序决定显示顺序:thinking 先于同消息的 tool_call 与文本
            reasoning, reasoning_seconds = _reasoning_from_message(message)
            if reasoning:
                projector.append_thinking(reasoning, track_duration=False, duration_seconds=reasoning_seconds)
```

- [ ] **Step 4: 运行确认通过**

`./.venv/bin/python -m pytest tests/test_projector.py -q` Expected: PASS（`test_projector_append_thinking_track_duration_false_leaves_no_duration` 仍在且通过）

- [ ] **Step 5: Commit**

```bash
git add lanscoder/app/projector.py tests/test_projector.py
git commit -m "feat: restore thinking duration on transcript replay"
```

---

## Task 3: runner 本回合 reasoning 窗口 + 协议补全

**Files:**
- Modify: `lanscoder/app/runtime.py:242-370`（`AgentChatRunner` 字段与 `_start_turn`/`_resume_turn`/`_refresh_turn_output`/`anudge_turn`）
- Modify: `lanscoder/app/ports.py:30-32`
- Test: `tests/test_app_runtime.py`

**Interfaces:**
- Consumes: `diagnostics.reasoning_seconds`（Task 1）
- Produces: `AgentChatRunner.last_turn_reasonings: list[tuple[str, float | None, bool]]`（本回合按序的 `(reasoning 文本, 秒数, 该消息是否含 tool_call part)`——第三元组元素决定下一条 reasoning 是否开启新 thinking 行，仅 tool_call 会切断合并链，text 不会）；`CurrentSessionLike.rebuild_view() -> SessionView`

- [ ] **Step 1: 写失败测试**

`tests/test_app_runtime.py` 追加（该文件已有 runner+临时 store 的构造惯例，实现时照抄其 session 构造方式）：

```python
def _reasoning_session_with_messages(store, session_id="s1", seconds=(9.0,)):
    session = AgentSession(store=store, session_id=session_id)
    for sec in seconds:
        session.append_user_message("hi")
        message_id = new_message_id()
        parts = [MessagePart(id=new_part_id(), message_id=message_id, kind="text", content="answer", metadata={})]
        session.writer.append_assistant_parts(
            parts,
            message_id=message_id,
            metadata={"provider": "p", "model": "m", "diagnostics": {"reasoning": "r", "reasoning_seconds": sec}},
        )
    return session


def test_runner_last_turn_reasonings_reset_and_accumulate(tmp_path):
    store = JsonlSessionStore(tmp_path)
    session = _reasoning_session_with_messages(store)
    runner = AgentChatRunner(current_session=CurrentSessionState(session), provider=ReasoningStub(), tools=[])

    # 新回合:before_count 之后的带 reasoning 消息入列
    before_count = len(session.rebuild_view().messages)
    runner._start_turn(streaming=False)
    runner._accumulate_turn_reasonings(before_count)
    assert len(runner.last_turn_reasonings) == 1
    assert runner.last_turn_reasonings[0][0] == "r"
    assert runner.last_turn_reasonings[0][1] == 9.0
    assert runner.last_turn_reasonings[0][2] is False  # 纯 text 消息:不切断合并链(tool_call 才为 True)

    # 新回合重置
    session.append_user_message("again")
    before_count = len(session.rebuild_view().messages)
    runner._start_turn(streaming=False)
    runner._accumulate_turn_reasonings(before_count)
    assert runner.last_turn_reasonings == []
```

`ReasoningStub` 是文件里已有的某种 `ChatProvider` 存根（复用现有 stub 或 `object()` 均可在仅调用 runner 私有方法时通过构造）。

（如该文件无 `new_message_id`/`MessagePart` 导入，从 `lanscoder.context.models` 导入；`AgentSession`/`JsonlSessionStore` 导入方式照现有用例。）

- [ ] **Step 2: 运行确认失败**

`./.venv/bin/python -m pytest tests/test_app_runtime.py -k "last_turn_reasonings" -v` Expected: FAIL（`last_turn_reasonings` 未定义 → AttributeError）

- [ ] **Step 3: 实现**

`lanscoder/app/runtime.py` `AgentChatRunner` 字段区（约 259-265 行附近）加：

```python
    # 本回合按序的 (reasoning 文本, 秒数, 消息是否含 tool_call part)；
    # 供 TUI 收尾 reconcile 把 store 里的时长回填到 live thinking 子行。
    _turn_reasonings: list[tuple[str, float | None, bool]] = field(default_factory=list)
```

`_start_turn` 开头加 `self._turn_reasonings.clear()`（`_resume_turn` **不**重置，保证权限暂停跨段累计）。新增 helper + 接进 `_refresh_turn_output` 与 `anudge_turn` None 分支：

```python
    def _accumulate_turn_reasonings(self, before_count: int) -> None:
        messages = self.current_session.rebuild_view().messages[before_count:]
        for message in messages:
            if getattr(message, "role", None) != "assistant":
                continue
            reasoning, seconds = _reasoning_entry(message)
            if not reasoning and seconds is None:
                continue
            # 合并边界:只有 tool_call part 会把 thinking child 从块末尾顶走、
            # 切断相邻 reasoning 的合并链;text part 不会(finished 的 THINKING
            # 仍是末位,下一 reasoning 仍合并进它)。replay 与 live 同此语义。
            ended_with_tool = any(part.kind == "tool_call" for part in getattr(message, "parts", []) or [])
            self._turn_reasonings.append((reasoning, seconds, ended_with_tool))
```

`_refresh_turn_output` 末尾调用 `self._accumulate_turn_reasonings(before_count)`。`anudge_turn` 的 `if result.response is None:` 提前返回分支，在 return 前调用 `self._accumulate_turn_reasonings(before_count)`。

模块级 helper（放 `runtime.py` 顶部 helper 区）：

```python
def _reasoning_entry(message) -> tuple[str, float | None]:
    metadata = getattr(message, "metadata", None) or {}
    diagnostics = metadata.get("diagnostics") or {}
    if not isinstance(diagnostics, dict):
        return "", None
    return str(diagnostics.get("reasoning") or ""), diagnostics.get("reasoning_seconds") or None
```

`last_turn_reasonings` 只读属性（或直接暴露 `_turn_reasonings` 的命名 `last_turn_reasonings` 字段；实现时二选一，文档以 `runner.last_turn_reasonings` 为准，后续 Task 4/5 只依赖这个名字）：

```python
    @property
    def last_turn_reasonings(self) -> list[tuple[str, float | None, bool]]:
        return list(self._turn_reasonings)
```

`lanscoder/app/ports.py`：

```python
class CurrentSessionLike(Protocol):
    session_id: str

    def rebuild_view(self) -> object: ...
```

（`object` 返回保持宽松；运行时 `CurrentSessionState` 已实现。）

- [ ] **Step 4: 运行确认通过**

`./.venv/bin/python -m pytest tests/test_app_runtime.py -k "last_turn_reasonings" -v` Expected: PASS
`./.venv/bin/python -m pytest tests/test_layer_boundaries.py -q` Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lanscoder/app/runtime.py lanscoder/app/ports.py tests/test_app_runtime.py
git commit -m "feat: surface per-turn reasoning durations from runner"
```

---

## Task 4: TUI 收尾 reconcile

**Files:**
- Modify: `lanscoder/app/tui.py:666-686`（`_finish_chat_turn`）
- Test: `tests/test_app_tui.py`

**Interfaces:**
- Consumes: `runner.last_turn_reasonings`（Task 3，`getattr` 容错：`[]`）
- Produces: `LansCoderApp._apply_turn_reasoning_durations()` —— 在 `_finish_chat_turn` 顶部 token 守卫内调用；物化用 `projector.append_thinking(track_duration=False, duration_seconds=...)` + `_mount_child_row`（**不得**调 `_ensure_stream_block_rows`）

- [ ] **Step 1: 写失败测试**

`tests/test_app_tui.py` 追加。需要携带 `last_turn_reasonings` 的 runner 存根（测试内定义）：

```python
class ReasoningRecordingRunner:
    def __init__(self, reasonings):
        self.last_turn_reasonings = reasonings
        self.last_pending_input = None
        self.background_manager = None


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_backfills_reasoning_duration() -> None:
    runner = ReasoningRecordingRunner([("replayed think", 11.5, True)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app.projector.append_thinking("replayed think", track_duration=True)
        app.projector.end_turn()
        child = app.transcript.blocks[-1].children[0]
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
    assert child.duration_seconds == 11.5
    assert child.finished is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_reconcile_survives_nudge_token_advance() -> None:
    """P1-1 回归:turn 内待完成后台任务触发 nudge、token 提前推进,reconcile 仍执行。"""
    job = BackgroundJob(id="bg_1", tool_name="delegate", label="r", status="completed")
    runner = FakeSubagentRunner(pending=[job])
    runner.last_turn_reasonings = [("think", 5.0, True)]
    app = LansCoderApp(chat_runner=runner)
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app.projector.append_thinking("think", track_duration=True)
        app.projector.end_turn()
        child = app.transcript.blocks[-1].children[0]
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
    assert child.duration_seconds == 5.0
    assert runner.nudges == [True]  # nudge 照常触发


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_materializes_non_streaming_thinking_row() -> None:
    runner = ReasoningRecordingRunner([("offline think", 3.0, True)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
    block = app.transcript.blocks[-1]
    assert block.kind == BlockKind.ASSISTANT
    child = [c for c in block.children if c.kind == ChildKind.THINKING]
    assert len(child) == 1
    assert child[0].duration_seconds == 3.0
    # 物化行位于输出区,正文 markdown 随后由 _write_chat_response 挂载在其下方
    output = app.query_one("#output")
    assert [w.id for w in output.children if isinstance(w.id, str) and w.id.startswith("child-")]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_merges_consecutive_reasoning_only_entries() -> None:
    """相邻 reasoning-only 消息(无 parts)合并进同一子行,首个秒数胜出。"""
    runner = ReasoningRecordingRunner([("a", 3.0, False), ("b", 4.0, False)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
    block = app.transcript.blocks[-1]
    thinking = [c for c in block.children if c.kind == ChildKind.THINKING]
    assert len(thinking) == 1
    assert thinking[0].duration_seconds == 3.0
```

`FakeSubagentRunner` 在本文件已有（pending 参数构造，nudge 计数、`background_manager` 属性齐全）；`BlockKind`/`ChildKind` 均已从 `lanscoder.app.tui_state` 导入或在文件头部补导入。

- [ ] **Step 2: 运行确认失败**

`./.venv/bin/python -m pytest tests/test_app_tui.py -k "reasoning_duration or materializes_non_streaming or merges_consecutive or survives_nudge" -v` Expected: FAIL（时长未回填）

- [ ] **Step 3: 实现**

`_finish_chat_turn` 改为在 `_refresh_task_plan_panel_from_current_session()` 之前插入 reconcile：

```python
    def _finish_chat_turn(self, token: int) -> None:
        """回合收尾:回填 thinking 时长、刷新任务计划面板、解除忙状态,必要时续发排队/引导回合。"""
        if not self._is_current_chat_turn(token):
            return
        # reconcile 必须在 pending/nudge 推进 token 之前,否则旧回合的时长回填被整体跳过
        self._apply_turn_reasoning_durations()
        self._refresh_task_plan_panel_from_current_session()
        self._chat_busy = False
        self._chat_worker = None
        if getattr(self.chat_runner, "last_pending_input", None) is None:
            self._active_chat_turn = None

        pending_input = self._pending_user_input
        if pending_input is not None:
            ...
```

新增方法（放在 `_finish_chat_turn` 之后）：

```python
    def _apply_turn_reasoning_durations(self) -> None:
        """把本回合 store 里记录的 reasoning 秒数回填到 live thinking 子行。

        与 replay_messages 的 merge 规则逐一对位(append_thinking 只查末位
        kind==THINKING,text 不断链、tool_call 断链):以本回合起始基线为窗口,
        只配对窗口内子行;初始合并状态取"回合开始时块末位子行是否 THINKING"。
        物化仅发生在无对应 live 子行的场合(非流式回合),直接构造 ChildItem 挂载。
        """
        entries = getattr(self.chat_runner, "last_turn_reasonings", None) or []
        if not entries:
            return
        turn = self._active_chat_turn
        block = self.transcript.last_block()
        thinking = [c for c in (block.children if block else []) if c.kind == ChildKind.THINKING]
        after_baseline = thinking[turn.started_thinking_count :] if turn else thinking
        idx = 0
        last_was_thinking = (
            turn.started_last_child_was_thinking
            if turn
            else bool(block and block.children and block.children[-1].kind == ChildKind.THINKING)
        )
        for reasoning, seconds, ended_with_tool in entries:
            if last_was_thinking:
                # 本条目 reasoning 已并入上一行(其文本),时长归该行首条 reasoning;刷新可在外圈做
                last_was_thinking = not ended_with_tool
                continue
            if idx < len(after_baseline):
                current_child = after_baseline[idx]
            else:
                if block is None or block.kind != BlockKind.ASSISTANT:
                    self.projector.start_assistant()
                    block = self.transcript.last_block()
                current_child = ChildItem(
                    ChildKind.THINKING,
                    f"t{len(block.children)}",
                    "Thinking…",
                    body=reasoning,
                    duration_seconds=seconds,
                    finished=True,
                )
                block.children.append(current_child)
                block_index = len(self.transcript.blocks) - 1
                self._mount_child_row(self.query_one("#output"), block_index, current_child)
            idx += 1
            current_child.duration_seconds = seconds
            current_child.finished = True
            block_index = len(self.transcript.blocks) - 1
            self._refresh_child_row(block_index, current_child)
            last_was_thinking = not ended_with_tool
```

`_ActiveChatTurn`（`tui.py:110-115`）增加两个基线字段（`_begin_active_chat_turn` 捕获；`_resume_active_chat_turn` 沿用不重算）：`started_thinking_count: int = 0`（回合开始时块的 THINKING 子行数）与 `started_last_child_was_thinking: bool = False`（回合开始时块末位子行是否 THINKING）。`_begin_active_chat_turn` 在设置 `_active_chat_turn` 前按当前 transcript 块计算两者；`_run_chat_turn` 内 `_active_chat_turn is None` 的兜底新建路径同样捕获一次。直接驱动 `_finish_chat_turn` 的测试（无 active turn）走 None 默认分支，保持基线 (全量, 末位即时判断)。

关键点（评审 P1-2）：物化走 `_mount_child_row` 直接挂载；`_refresh_child_row` 内部在存在同 key 行时会复用。**禁止**在 reconcile 里调用 `_ensure_stream_block_rows`（非流式回合它会新建空 streaming markdown 且行序错位）。非流式顺序保证：reconcile 在本回合正文 `_write_chat_response` 之前执行，物化行先于正文，符合 `render_block_into` 的 thinking→markdown→tools 契约。

- [ ] **Step 4: 运行确认通过**

`./.venv/bin/python -m pytest tests/test_app_tui.py -k "reasoning_duration or materializes_non_streaming or merges_consecutive or survives_nudge" -v` Expected: PASS
`./.venv/bin/python -m pytest tests/test_app_tui.py -q` Expected: PASS（无回归）

- [ ] **Step 5: Commit**

```bash
git add lanscoder/app/tui.py tests/test_app_tui.py
git commit -m "feat: reconcile live thinking rows from store durations at turn end"
```

---

## Task 5: 退出冲刷待投递通知

**Files:**
- Modify: `lanscoder/agent/loop.py`（公开冲刷方法）
- Modify: `lanscoder/app/runtime.py`（runner 委托）
- Modify: `lanscoder/app/factory.py:282-293`（`on_shutdown` 组合）
- Test: `tests/test_app_factory.py`

**Interfaces:**
- Consumes: `Loop.background_manager`、`collect_completed(session_id=...)`
- Produces: `AgentLoop.flush_background_notifications()`、`AgentChatRunner.flush_background_notifications()`、factory 的 `on_shutdown` 先冲刷后 close

- [ ] **Step 1: 写失败测试**

`tests/test_app_factory.py` 追加（文件已有组合构建 helper；若难直接拿 runner，测试改为直测 loop/runner 两个方法，参考现有 `test_background_jobs.py` 的构造惯例）：

```python
def test_loop_flush_background_notifications_persists_pending(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession(store=store, session_id="f1")
    manager = BackgroundJobManager()
    job = BackgroundJob(id="bg_f", tool_name="delegate", session_id="f1", status="completed", label="researcher", error=None)
    manager._completed.append(job)
    loop = create_agent_loop(session=session, provider=_FakeProviderPassive(), background_manager=manager)
    loop.flush_background_notifications()
    view = session.rebuild_view()
    notifications = [m for m in view.messages if m.role == "notification"]
    assert len(notifications) == 1
    meta = notifications[0].parts[0].metadata
    assert meta["background_label"] == "researcher"
```

`_FakeProviderPassive` 用该文件现有最小 provider stub。`create_agent_loop` 与 `AgentLoop` 见 `lanscoder/app/runtime.py` / `lanscoder/agent/loop.py`。

（若 `create_agent_loop` 要求 provider 提供工具集等，直接构造 `AgentLoop(...)` 最小参数或复用文件内 fixture，实现时按现有用例定。）

- [ ] **Step 2: 运行确认失败**

`./.venv/bin/python -m pytest tests/test_app_factory.py -k flush_background -v` Expected: FAIL（方法不存在 → AttributeError；且 `_append_background_notifications` 已有，但公开方法缺失）

- [ ] **Step 3: 实现**

`lanscoder/agent/loop.py`（`_append_background_notifications` 旁）：

```python
    def flush_background_notifications(self) -> None:
        """冲刷当前会话所有已完成的待投递后台通知;退出前调用保证通知不丢。"""
        self._append_background_notifications()
```

`lanscoder/app/runtime.py` `AgentChatRunner`：

```python
    def flush_background_notifications(self) -> None:
        """把当前会话所有已完成的后台通知落盘(供退出前调用)。"""
        session = self.current_session.session
        for loop in self.loops:
            if getattr(loop, "session", None) is session:
                loop.flush_background_notifications()
```

`lanscoder/app/factory.py`：`on_shutdown=mcp_manager.close` 处改为组合回调（`chat_runner` 在函数作用域内已定义）：

```python
    def _close_session_and_mcp() -> None:
        chat_runner.flush_background_notifications()
        mcp_manager.close()

    ...
        on_shutdown=_close_session_and_mcp,
```

- [ ] **Step 4: 运行确认通过**

`./.venv/bin/python -m pytest tests/test_app_factory.py -k flush_background -v` Expected: PASS
`./.venv/bin/python -m pytest tests/test_app_factory.py -q` Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lanscoder/agent/loop.py lanscoder/app/runtime.py lanscoder/app/factory.py tests/test_app_factory.py
git commit -m "feat: flush pending background notifications before exit"
```

---

## Task 6: 通知 metadata 补充 label/error

**Files:**
- Modify: `lanscoder/agent/background.py:110-122, 276-297`
- Modify: `lanscoder/agent/session.py:489-510`
- Modify: `lanscoder/context/writer.py:294-327`
- Modify: `lanscoder/agent/loop.py:693-700`
- Test: `tests/test_session_transcript.py`、`tests/test_background_jobs.py`、`tests/test_app_tui.py`（`RecordingSession` fake）

**Interfaces:**
- Consumes: `BackgroundNotification.label/status/tool_name`（已有）、`job.error`（新增进 DTO）
- Produces: 通知 part metadata 新增 `background_label`、`background_error` 两个可选 key；`RecordingSession.append_background_notification` 同步补 `label`/`error` kwarg

- [ ] **Step 1: 写失败测试**

`tests/test_session_transcript.py` 追加：

```python
def test_background_notification_metadata_carries_label_and_error(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession(store=store, session_id="meta1")
    session.append_background_notification(
        content="<task_notification>...</task_notification>",
        job_id="j1",
        tool_name="delegate",
        status="failed",
        label="researcher",
        error="boom",
    )
    view = session.rebuild_view()
    notification = next(m for m in view.messages if m.role == "notification")
    meta = notification.parts[0].metadata
    assert meta["background_label"] == "researcher"
    assert meta["background_error"] == "boom"
```

同一文件再补：不传 label/error 时 metadata 不含这两个 key。

- [ ] **Step 2: 运行确认失败**

`./.venv/bin/python -m pytest tests/test_session_transcript.py -k background_notification_metadata -v` Expected: FAIL（metadata 缺 key → KeyError）

- [ ] **Step 3: 实现**

`lanscoder/agent/background.py` `BackgroundNotification` 增加 `error: str | None = None`（`task_plan_completion` 之后）；`_notification_for` 的构造加 `error=job.error`：

```python
        return BackgroundNotification(
            ...
            task_plan_completion=job.task_plan_completion,
            error=job.error,
            elapsed_seconds=...,
```

`lanscoder/agent/session.py`：

```python
    def append_background_notification(
        self,
        *,
        content: str,
        job_id: str,
        tool_name: str,
        status: str,
        task_id: str | None = None,
        observed_revision: int | None = None,
        label: str | None = None,
        error: str | None = None,
    ) -> str:
        message_id = self.writer.append_background_notification(
            content=content,
            job_id=job_id,
            tool_name=tool_name,
            status=status,
            task_id=task_id,
            observed_revision=observed_revision,
            label=label,
            error=error,
        )
```

`lanscoder/context/writer.py`（`append_background_notification`）：签名加 `label: str | None = None, error: str | None = None`；metadata 字典在保留现有键后追加：

```python
        if label is not None:
            metadata["background_label"] = label
        if error is not None:
            metadata["background_error"] = error
```

`lanscoder/agent/loop.py` `_append_background_notifications` 的调用增加 `label=notification.label, error=notification.error`。

`tests/test_app_tui.py` 的 `RecordingSession.append_background_notification` fake 签名同步补 `label=None, error=None`（不接参数也可，但要能接受新 kwarg 以免 TypeError）。

- [ ] **Step 4: 运行确认通过**

`./.venv/bin/python -m pytest tests/test_session_transcript.py tests/test_background_jobs.py tests/test_app_tui.py -q` Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lanscoder/agent/background.py lanscoder/agent/session.py lanscoder/context/writer.py lanscoder/agent/loop.py tests/test_session_transcript.py tests/test_app_tui.py
git commit -m "feat: persist background notification label and error in metadata"
```

---

## Task 7: 共享 formatter + live 使用

**Files:**
- Modify: `lanscoder/app/transcript_view.py`
- Modify: `lanscoder/app/tui.py:378-396`（`_handle_subagent_completed`）
- Test: `tests/test_app_tui.py`

**Interfaces:**
- Consumes: 通知 metadata（Task 6）
- Produces: `background_notification_ui_text(*, label, tool_name, status, error) -> str`

- [ ] **Step 1: 写失败测试**

`tests/test_app_tui.py` 追加：

```python
from lanscoder.app.transcript_view import background_notification_ui_text


def test_background_notification_ui_text_full_matrix() -> None:
    assert background_notification_ui_text(label="r", tool_name="delegate", status="completed", error=None) == "✅ 子agent [r] 已完成"
    assert background_notification_ui_text(label=None, tool_name="delegate", status="completed", error=None) == "✅ 子agent [delegate] 已完成"
    assert background_notification_ui_text(label="r", tool_name="delegate", status="failed", error="boom") == "❌ 子agent [r] 失败: boom"
    assert background_notification_ui_text(label="r", tool_name="delegate", status="failed", error=None) == "❌ 子agent [r] 失败: 未知错误"
    assert background_notification_ui_text(label="r", tool_name="delegate", status="cancelled", error=None) == "⚠️ 子agent [r] cancelled"
```

（若 emoji 断言与当前 live 输出有出入，以 `tui.py:386-391` 现状为准微调期望值；实现保持不变。）

- [ ] **Step 2: 运行确认失败**

`./.venv/bin/python -m pytest tests/test_app_tui.py -k background_notification_ui_text -v` Expected: FAIL（ImportError）

- [ ] **Step 3: 实现**

`lanscoder/app/transcript_view.py`：

```python
def background_notification_ui_text(*, label: str | None, tool_name: str, status: str, error: str | None) -> str:
    name = label or tool_name
    if status == "completed":
        return f"✅ 子agent [{name}] 已完成"
    if status == "failed":
        return f"❌ 子agent [{name}] 失败: {error or '未知错误'}"
    return f"⚠️ 子agent [{name}] {status}"
```

`lanscoder/app/tui.py` 顶部从 `lanscoder.app.transcript_view` 导入该函数；`_handle_subagent_completed` 替换构造文案：

```python
    def _handle_subagent_completed(self, job) -> None:
        """子agent 完成后写入 UI 结果行,空闲时补发一次引导回合。"""
        if not getattr(self, "is_mounted", False):
            return
        ui_msg = background_notification_ui_text(
            label=getattr(job, "label", None),
            tool_name=job.tool_name,
            status=job.status,
            error=getattr(job, "error", None),
        )
        self._ui_line(BlockKind.SYSTEM, ui_msg)

        if not self._chat_busy and self.chat_runner is not None:
            self._submit_nudge_turn()
```

- [ ] **Step 4: 运行确认通过**

`./.venv/bin/python -m pytest tests/test_app_tui.py -q` Expected: PASS（既有完成/失败 handler 测试文案不变）

- [ ] **Step 5: Commit**

```bash
git add lanscoder/app/transcript_view.py lanscoder/app/tui.py tests/test_app_tui.py
git commit -m "feat: share background notification formatter across live and resume"
```

---

## Task 8: 重放通知友好行

**Files:**
- Modify: `lanscoder/app/projector.py:183-186`
- Test: `tests/test_session_transcript.py`

**Interfaces:**
- Consumes: `background_notification_ui_text`（Task 7）、metadata `background_label/tool_name/status/error`（Task 6）
- Produces: replay 时 `role == "notification"` 消息渲染为友好 SYSTEM 行，不再出现 `<task_notification>` 原文

- [ ] **Step 1: 写失败测试**

`tests/test_session_transcript.py`（该文件已存续 transcript 重放相关测试；若更适合放 `tests/test_projector.py`，以贴近现有消息构造为原则）：

```python
def _notification_message(meta: dict) -> SimpleNamespace:
    return SimpleNamespace(
        role="notification",
        parts=[SimpleNamespace(kind="text", content="<task_notification>...", metadata=meta)],
    )


def test_replay_background_notification_renders_friendly_line() -> None:
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(
        p,
        [
            _notification_message(
                {
                    "background_tool_name": "delegate",
                    "background_status": "completed",
                    "background_label": "researcher",
                }
            )
        ],
    )
    assert len(model.blocks) == 1
    assert model.blocks[0].kind == BlockKind.SYSTEM
    assert model.blocks[0].text == "✅ 子agent [researcher] 已完成"
    assert "task_notification" not in model.blocks[0].text


def test_replay_background_notification_defaults_label_to_tool_name() -> None:
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(
        p,
        [_notification_message({"background_tool_name": "web_search", "background_status": "failed"})],
    )
    assert model.blocks[0].text == "❌ 子agent [web_search] 失败: 未知错误"
```

（`SimpleNamespace` 从 `types` 导入；`BlockKind`、`TranscriptModel`、`TranscriptProjector`、`replay_messages` 照 `tests/test_projector.py` 的导入。）

- [ ] **Step 2: 运行确认失败**

`./.venv/bin/python -m pytest tests/test_session_transcript.py -k replay_background_notification -v` Expected: FAIL（现渲染 `<task_notification>` 原文）

- [ ] **Step 3: 实现**

`lanscoder/app/projector.py` 顶部加 `from lanscoder.app.transcript_view import background_notification_ui_text`；`replay_messages` 的 notification 分支替换：

```python
        elif role == "notification":
            for part in parts:
                if part.kind != "text":
                    continue
                meta = getattr(part, "metadata", None) or {}
                if not isinstance(meta, dict):
                    meta = {}
                text = background_notification_ui_text(
                    label=meta.get("background_label"),
                    tool_name=str(meta.get("background_tool_name") or "tool"),
                    status=str(meta.get("background_status") or ""),
                    error=meta.get("background_error"),
                )
                if text:
                    projector.flat_block(BlockKind.SYSTEM, text)
```

- [ ] **Step 4: 运行确认通过**

`./.venv/bin/python -m pytest tests/test_session_transcript.py -k replay_background_notification -v` Expected: PASS
全量核对：`./.venv/bin/python -m pytest tests/test_layer_boundaries.py tests/test_background_jobs.py tests/test_recall.py -q` Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lanscoder/app/projector.py tests/test_session_transcript.py
git commit -m "feat: render friendly background notification line on resume"
```

---

## 自检对照

- Spec §4.2 测量（流式/非流式/重试）→ Task 1 ✓
- Spec §4.3 reconcile 位置与 runner 窗口、DOM 顺序、合并规则 → Task 3+4 ✓
- Spec §4.4 重放时长 → Task 2 ✓
- Spec §5.2 写点 label/error metadata → Task 6 ✓
- Spec §5.3 共享 formatter live+replay → Task 7+8 ✓
- Spec §5.4 退出冲刷（已决策 A）→ Task 5 ✓
- Spec §7 测试（含保留 173-183、RecordingSession kwarg、nudge 回归）→ Task 2/4/6 ✓
- 无占位符，各任务含可运行代码与验证命令。

身份管线提示：Task 3 `_reasoning_entry` 与 Task 2 `_reasoning_from_message` 的逻辑重复是刻意的（runner 与 projector 分属 app 层不同职责，各自内联；CLAUDE.md inline 原则）。若评审嫌重复，可收敛为一个 app 层 helper，但不得引入新模块。