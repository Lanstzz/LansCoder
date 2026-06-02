from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.providers.types import ChatResponse, ToolCall
from firstcoder.tools.types import ToolResult


def test_writer_appends_user_assistant_tool_messages_with_valid_parts(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    user_id = writer.append_user_message("你好")
    assistant_id = writer.append_assistant_response(
        ChatResponse(
            provider="fake",
            model="fake-model",
            content="我要调用工具",
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "abc"})],
            finish_reason="tool_calls",
        )
    )
    tool_id = writer.append_tool_result(
        tool_call=ToolCall(id="call_1", name="echo", arguments={"text": "abc"}),
        result=ToolResult(name="echo", ok=True, content="echo:abc", data={"length": 3}),
    )

    view = store.rebuild_session_view("sess_test")

    assert [message.id for message in view.messages] == [user_id, assistant_id, tool_id]
    assert [message.role for message in view.messages] == ["user", "assistant", "tool"]
    assert view.messages[0].parts[0].kind == "text"
    assert view.messages[1].parts[0].kind == "text"
    assert view.messages[1].parts[1].kind == "tool_call"
    assert view.messages[1].parts[1].metadata["tool_call_id"] == "call_1"
    assert view.messages[2].parts[0].kind == "tool_result"
    assert view.messages[2].parts[0].metadata["tool_call_id"] == "call_1"
    assert view.messages[2].parts[0].metadata["data"] == {"length": 3}


def test_writer_appends_session_created_once_when_requested(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    writer.append_session_created(title="demo")

    view = store.rebuild_session_view("sess_test")
    assert view.metadata["session_id"] == "sess_test"
    assert view.metadata["title"] == "demo"
