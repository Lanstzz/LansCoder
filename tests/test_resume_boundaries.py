"""Resume / interrupt boundary semantics for the nested transcript.

These unit tests lock the projector contract that the App-level flows depend on:

- ``replay_messages`` projects message-level ``metadata["diagnostics"][
  "reasoning"]`` into a THINKING child of the assistant block (implemented in
  Tasks 2/5). Pinning it here means future rewrites cannot silently drop the
  reasoning projection after a session resume.
- ``TranscriptProjector.end_turn()`` settles a still-running TOOL child to
  ``error`` — the Ctrl-C / abort normalization used by
  ``LansCoderApp._interrupt_chat_turn`` when a turn is interrupted mid-tool.
"""

from pathlib import Path
from types import SimpleNamespace

from lanscoder.app.projector import TranscriptProjector, replay_messages
from lanscoder.app.tui_state import BlockKind, ChildKind, TranscriptModel
from lanscoder.context.compaction import CompactionPipeline
from lanscoder.context.models import AgentMessage, MessagePart, SessionView
from tests.test_context_compaction_pipeline import _message, _request, _tool_call, _tool_result


def test_replay_thinking_child_present_with_diagnostics() -> None:
    messages = [
        SimpleNamespace(
            role="user",
            parts=[SimpleNamespace(kind="text", content="hi", metadata={})],
            metadata={},
        ),
        SimpleNamespace(
            role="assistant",
            parts=[SimpleNamespace(kind="text", content=" analyzing", metadata={})],
            metadata={"diagnostics": {"reasoning": "step one"}},
        ),
    ]
    model = TranscriptModel()
    projector = TranscriptProjector(model)
    replay_messages(projector, messages)

    assert model.blocks[1].text == " analyzing"
    thinking = [c for c in model.blocks[1].children if c.kind == ChildKind.THINKING]
    assert thinking and thinking[0].body == "step one"


def test_interrupt_via_end_turn_settles_running_tool() -> None:
    """Ctrl-C 语义:未结算工具在回合收尾时归一为 error。"""
    model = TranscriptModel()
    projector = TranscriptProjector(model)
    projector.start_user("do it")
    projector.tool_event("c1", "read", "started")
    projector.append_assistant_text("partial text")
    projector.end_turn()

    assert model.blocks[1].text == "partial text"
    assert model.blocks[1].children[0].status == "error"


def test_replay_after_compaction_rebuilds_block_tree_without_exception(
    tmp_path: Path,
) -> None:
    """Post-compaction ``view.messages`` still replays into a nested block tree.

    Decision note (spec): reasoning is not guaranteed to survive compression,
    so this test deliberately does NOT assert a THINKING child after compaction.
    Today compaction only rewrites tool-result payloads and leaves the assistant
    message's ``diagnostics.reasoning`` metadata untouched, which means a
    THINKING child may in fact reappear on replay — but that is an accidental
    property of this compression level, not a contract, and future levels that
    drop reasoning are expected. The contract locked here is only that the
    compacted view replays without exception and produces a block tree.
    """

    result_content = "\n".join(f"lanscoder/app.py:{i}: def fn_{i}(): pass" for i in range(1, 200))
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_user", role="user", content="find the fn_ definitions"),
            _tool_call("c_compact", "grep", {"pattern": "fn_"}),
            _tool_result("c_compact", "grep", content=result_content),
            AgentMessage(
                id="msg_assist",
                session_id="sess_test",
                role="assistant",
                metadata={"diagnostics": {"reasoning": "step one"}},
                parts=[
                    MessagePart(
                        id="part_assist",
                        message_id="msg_assist",
                        kind="text",
                        content=" done",
                        metadata={},
                    )
                ],
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        _request(view=view, target_tokens=1, current_turn=10)
    )

    model = TranscriptModel()
    replay_messages(TranscriptProjector(model), result.view.messages)

    assert model.blocks
    assert any(block.kind == BlockKind.ASSISTANT for block in model.blocks)
    assert any(block.kind == BlockKind.USER for block in model.blocks)