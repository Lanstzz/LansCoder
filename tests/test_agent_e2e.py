from __future__ import annotations

from dataclasses import dataclass, field

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ToolCall
from firstcoder.tools.view import create_view_tool


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeProvider 没有剩余响应")
        return self.responses.pop(0)


def test_agent_single_turn_e2e_writes_and_rebuilds_session(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_e2e", agents_md="项目规则")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="收到")])

    response = AgentLoop(session=session, provider=provider).run_user_turn("你好")

    assert response.content == "收到"
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[0].role == "system"
    assert "项目规则" in provider.requests[0].messages[0].content

    view = store.rebuild_session_view("sess_e2e")
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[0].parts[0].content == "你好"
    assert view.messages[1].parts[0].content == "收到"


def test_agent_tool_call_e2e_uses_real_view_tool_and_persists_result(tmp_path) -> None:
    (tmp_path / "README.md").write_text("标题\n正文", encoding="utf-8")
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_e2e", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_view",
                        name="view",
                        arguments={"path": "README.md", "limit": 2},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="README 已读取"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[create_view_tool(tmp_path)],
    ).run_user_turn("读 README")

    assert response.content == "README 已读取"
    assert len(provider.requests) == 2
    assert "view" in [tool.name for tool in provider.requests[0].tools]
    assert provider.requests[1].messages[-2].role == "assistant"
    assert provider.requests[1].messages[-2].tool_calls[0].name == "view"
    assert provider.requests[1].messages[-1].role == "tool"
    assert provider.requests[1].messages[-1].tool_call_id == "call_view"
    assert "1: 标题" in provider.requests[1].messages[-1].content
    assert "2: 正文" in provider.requests[1].messages[-1].content

    view = store.rebuild_session_view("sess_e2e")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]
    assert view.messages[1].parts[0].kind == "tool_call"
    assert view.messages[2].parts[0].kind == "tool_result"
    assert view.messages[2].parts[0].metadata["tool_name"] == "view"
    assert view.messages[2].parts[0].metadata["ok"] is True


def test_agent_resume_e2e_replays_history_and_continues_turn(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    original = AgentSession.create(store=store, session_id="sess_e2e", agents_md="规则")
    first_provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="第一轮回复")])

    AgentLoop(session=original, provider=first_provider).run_user_turn("第一轮")

    resumed = AgentSession.resume(store=store, session_id="sess_e2e", agents_md="规则")
    second_provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="第二轮回复")])
    response = AgentLoop(session=resumed, provider=second_provider).run_user_turn("第二轮")

    assert response.content == "第二轮回复"
    assert len(second_provider.requests) == 1
    provider_roles = [message.role for message in second_provider.requests[0].messages]
    assert provider_roles == ["system", "user", "assistant", "user"]
    assert second_provider.requests[0].messages[1].content.endswith("第一轮")
    assert second_provider.requests[0].messages[2].content == "第一轮回复"
    assert second_provider.requests[0].messages[3].content.endswith("第二轮")

    view = store.rebuild_session_view("sess_e2e")
    assert [message.role for message in view.messages] == ["user", "assistant", "user", "assistant"]
    assert view.messages[-1].parts[0].content == "第二轮回复"
