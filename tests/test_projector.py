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
            metadata={"diagnostics": {"reasoning": "核心在 session 校验"}},
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
