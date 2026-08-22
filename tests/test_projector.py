from types import SimpleNamespace

from lanscoder.app.projector import TranscriptProjector, replay_messages
from lanscoder.app.tui_state import BlockKind, ChildKind, TranscriptModel


def build_messages():
    from types import SimpleNamespace

    def text(s):
        return SimpleNamespace(kind="text", content=s, metadata={})

    def call(cid, name, args):
        return SimpleNamespace(kind="tool_call", content=None, id=cid, metadata={"tool_call_id": cid, "tool_name": name, "arguments": args})

    def result(cid, name, ok, body):
        return SimpleNamespace(kind="tool_result", id=cid, content=body, metadata={"tool_call_id": cid, "tool_name": name, "ok": ok})

    return [
        SimpleNamespace(role="user", parts=[text("帮我改登录")]),
        SimpleNamespace(
            role="assistant",
            parts=[text("先看实现"), call("c1", "read", {"path": "auth.py"})],
            metadata={"diagnostics": {"reasoning": "核心在 session 校验", "reasoning_seconds": 2.5}},
        ),
        SimpleNamespace(role="tool", parts=[result("c1", "read", True, "ok 200")]),
        SimpleNamespace(role="assistant", parts=[text("改好了")], metadata={}),
    ]


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
    # 正文是 children 里的 TEXT_RUN 条目,工具事件切断文本段
    assert [c.kind for c in block.children] == [ChildKind.TEXT_RUN, ChildKind.TOOL, ChildKind.TEXT_RUN]
    assert block.text == "ab"
    assert [c.body for c in block.children if c.kind == ChildKind.TEXT_RUN] == ["a", "b"]


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


def test_projector_finished_before_started_settles_orphan_to_success():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.tool_event("c1", "write", "finished", ok=True)
    child = model.blocks[1].children[0]
    assert child.kind == ChildKind.TOOL and child.key == "c1"
    assert child.status == "success"


def test_projector_denied_maps_to_denied_status():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.tool_event("c1", "write", "started")
    p.tool_event("c1", "write", "denied")
    assert model.blocks[1].children[0].status == "denied"


def test_projector_first_text_finalizes_thinking_with_duration():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.append_thinking("reasoning...")
    child = model.blocks[1].children[0]
    assert child.finished is False
    assert child.started_at is not None
    assert p.append_assistant_text("answer") is True
    assert child.finished is True
    assert child.duration_seconds is not None
    assert p.append_assistant_text(" more") is False


def test_projector_tool_event_finalizes_thinking_before_started():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.append_thinking("deciding...")
    assert p.tool_event("c1", "read", "started", arguments={"path": "a.py"}) is True
    thinking = model.blocks[1].children[0]
    tool = model.blocks[1].children[1]
    assert thinking.finished is True
    assert thinking.duration_seconds is not None
    assert tool.key == "c1"
    assert tool.name == "read"
    assert tool.arguments == "{'path': 'a.py'}"
    assert tool.label == "tool read {'path': 'a.py'}"


def test_projector_end_turn_finalizes_leftover_thinking():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.append_thinking("never answered")
    p.end_turn()
    child = model.blocks[1].children[0]
    assert child.finished is True
    assert child.duration_seconds is not None


def test_projector_append_thinking_track_duration_false_leaves_no_duration():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.append_thinking("replayed reasoning", track_duration=False)
    p.append_assistant_text("answer")
    child = model.blocks[1].children[0]
    assert child.finished is True
    assert child.started_at is None
    assert child.duration_seconds is None


def test_projector_replay_thinking_restores_duration_from_metadata():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(p, build_messages())
    thinking = [c for c in model.blocks[1].children if c.kind == ChildKind.THINKING]
    assert thinking and thinking[0].finished is True
    assert thinking[0].duration_seconds == 2.5


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


def test_projector_replay_tool_keeps_full_arguments_and_result():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(p, build_messages())
    tool = next(c for c in model.blocks[1].children if c.kind == ChildKind.TOOL)
    assert tool.name == "read"
    assert tool.arguments == "{'path': 'auth.py'}"
    assert tool.body == "ok 200"


def test_projector_replay_thinking_sits_before_tool_children():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(p, build_messages())
    kinds = [c.kind for c in model.blocks[1].children]
    # canonical 序:thinking 在前,文本段被工具边界切成两段
    assert kinds == [ChildKind.THINKING, ChildKind.TEXT_RUN, ChildKind.TOOL, ChildKind.TEXT_RUN]


def test_projector_replay_splits_text_runs_at_tool_boundaries():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    replay_messages(p, build_messages())
    block = model.blocks[1]
    runs = [c for c in block.children if c.kind == ChildKind.TEXT_RUN]
    assert [c.body for c in runs] == ["先看实现", "改好了"]
    assert block.text == "先看实现改好了"


def test_projector_text_chunks_merge_into_one_run():
    model = TranscriptModel()
    p = TranscriptProjector(model)
    p.start_user("hi")
    p.append_assistant_text("he")
    p.append_assistant_text("llo")
    p.end_turn()
    block = model.blocks[1]
    runs = [c for c in block.children if c.kind == ChildKind.TEXT_RUN]
    assert len(runs) == 1
    assert runs[0].body == "hello"
    assert block.text == "hello"


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
