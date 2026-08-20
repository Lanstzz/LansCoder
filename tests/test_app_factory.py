from dataclasses import dataclass, field
from pathlib import Path
import threading

import pytest

from lanscoder.app.runtime import create_agent_loop
from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.agent.session import AgentSession
from lanscoder.app.factory import create_lanscoder_app
from lanscoder.app.model_state import ModelStateStore
from lanscoder.app.router import CompositeCommandHandler
from lanscoder.app.runtime import AgentChatRunner
from lanscoder.config.settings import AppConfig
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.llm_compact import LlmCompactService
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse, ProviderCapabilities, ToolCall
from lanscoder.tools.write import create_write_tool
from lanscoder.tools.types import make_text_result
from lanscoder.mcp.models import McpServerStatus, McpToolDescription


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = ProviderCapabilities()
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class FakeMcpManager:
    def __init__(self, tools=(), statuses=()) -> None:
        self.tools_value = tools
        self.statuses_value = statuses
        self.connect_calls = 0
        self.close_calls = 0

    def connect_all(self) -> None:
        self.connect_calls += 1

    def connect_all_in_background(self) -> None:
        self.connect_calls += 1

    def tools(self):
        return self.tools_value

    def statuses(self):
        return self.statuses_value

    def doctor(self, name: str):
        return next((status for status in self.statuses_value if status.name == name), None)

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]):
        return {"content": [{"type": "text", "text": "ok"}]}

    def close(self) -> None:
        self.close_calls += 1


def test_factory_connects_mcp_once_and_merges_discovered_tools(tmp_path: Path) -> None:
    manager = FakeMcpManager(
        tools=(("demo", McpToolDescription("ping", "Ping", {"type": "object", "properties": {}})),),
        statuses=(McpServerStatus("demo", "connected", tool_count=1),),
    )
    app = create_lanscoder_app(
        project_root=tmp_path,
        provider=FakeProvider([]),
        session_id="sess_test",
        mcp_manager_factory=lambda configs: manager,
    )

    assert manager.connect_calls == 1
    assert "write" in [tool.name for tool in app.current_session.session.tool_registry.tools()]
    assert "mcp__demo__ping" in [tool.name for tool in app.current_session.session.tool_registry.tools()]
    assert "mcp_tool_search" in app.current_session.session.tool_registry.names()
    search = app.current_session.session.tool_registry.get("mcp_tool_search")
    assert search is not None
    result = search.executor(query="ping demo")
    assert result.data["mcp_tool_search"]["activated_tools"] == ["mcp__demo__ping"]
    assert "/mcp list" in app.command_handler.handle("/help").output


def test_factory_keeps_builtin_tools_when_mcp_connection_fails(tmp_path: Path) -> None:
    manager = FakeMcpManager(statuses=(McpServerStatus("demo", "failed", error="safe failure"),))
    app = create_lanscoder_app(
        project_root=tmp_path,
        provider=FakeProvider([]),
        session_id="sess_test",
        mcp_manager_factory=lambda configs: manager,
    )

    assert "write" in [tool.name for tool in app.current_session.session.tool_registry.tools()]
    assert "mcp__demo__ping" not in [tool.name for tool in app.current_session.session.tool_registry.tools()]
    assert "mcp_tool_search" not in app.current_session.session.tool_registry.names()


def test_factory_custom_tools_mode_does_not_append_mcp_tools(tmp_path: Path) -> None:
    manager = FakeMcpManager(
        tools=(("demo", McpToolDescription("ping", "Ping", {"type": "object", "properties": {}})),),
    )
    app = create_lanscoder_app(
        project_root=tmp_path,
        provider=FakeProvider([]),
        session_id="sess_test",
        tools=[],
        mcp_manager_factory=lambda configs: manager,
    )

    assert "mcp__demo__ping" not in app.current_session.session.tool_registry.names()
    assert "mcp_tool_search" not in app.current_session.session.tool_registry.names()


def test_app_unmount_closes_mcp_manager_once(tmp_path: Path) -> None:
    manager = FakeMcpManager()
    app = create_lanscoder_app(
        project_root=tmp_path,
        provider=FakeProvider([]),
        session_id="sess_test",
        tools=[],
        mcp_manager_factory=lambda configs: manager,
    )

    app.on_unmount()
    app.on_unmount()

    assert manager.close_calls == 1


def test_create_lanscoder_app_wires_session_commands_context_and_chat(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("项目规则", encoding="utf-8")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="收到")])

    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=provider,
        session_id="sess_test",
        tools=[],
    )

    assert isinstance(app.command_handler, CompositeCommandHandler)
    assert isinstance(app.chat_runner, AgentChatRunner)
    assert (tmp_path / ".lanscoder" / "sessions" / "sess_test.jsonl").exists()
    assert "Session: sess_test" in app.command_handler.handle("/context").output
    assert "Sessions:" in app.command_handler.handle("/sessions").output
    assert "/resume" in app.command_handler.handle("/help").output
    response = app.chat_runner.run_user_turn("你好")
    assert response.content == "收到"
    assert "项目规则" in provider.requests[0].messages[0].content


def test_create_lanscoder_app_wires_new_fork_and_skill_commands(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".lanscoder" / "skills"
    skills_dir.mkdir(parents=True)
    brief_dir = skills_dir / "brief"
    brief_dir.mkdir()
    (brief_dir / "SKILL.md").write_text("# Brief\n", encoding="utf-8")
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
        tools=[],
    )

    new_result = app.command_handler.handle("/new 新会话")
    assert new_result.output.startswith("New session: sess_")
    new_session_id = app.current_session.session.session_id
    assert new_session_id != "sess_test"

    fork_result = app.command_handler.handle("/fork 分支")
    assert fork_result.output.startswith(f"Forked session: {new_session_id} -> sess_")
    assert app.current_session.session.session_id != new_session_id

    skills_result = app.command_handler.handle("/skills")
    assert "brief project .lanscoder/skills/brief/SKILL.md" in skills_result.output
    skill_result = app.command_handler.handle("/skill brief")
    assert "Skill: brief" in skill_result.output


def test_create_lanscoder_app_enables_streaming_for_capable_provider(tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="fake-model", content="ok")],
        capabilities=ProviderCapabilities(supports_streaming=True),
    )

    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=provider,
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.use_streaming is True


def test_create_lanscoder_app_requires_catalog_without_injected_provider(tmp_path: Path) -> None:
    config = AppConfig(env={}, project_config={})

    with pytest.raises(ValueError, match="模型目录为空"):
        create_lanscoder_app(
            project_root=tmp_path,
            data_root=tmp_path / ".lanscoder",
            app_config=config,
            session_id="sess_test",
            tools=[],
        )


def test_create_lanscoder_app_honors_streaming_disabled_config(tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="fake-model", content="ok")],
        capabilities=ProviderCapabilities(supports_streaming=True),
    )
    config = AppConfig(
        env={},
        project_config={"providers": {"fake": {"type": "openai-compatible", "streaming": False}}},
    )

    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=provider,
        session_id="sess_test",
        tools=[],
        app_config=config,
    )

    assert app.chat_runner.use_streaming is False


def _catalog_config(*, default_model: str | None = "yuren/main") -> AppConfig:
    project = {
        "providers": {
            "yuren": {
                "type": "openai-compatible",
                "base_url": "https://example.test/v1",
                "api_key": "test-key",
            },
            "mimo": {
                "type": "openai-compatible",
                "base_url": "https://mimo.example/v1",
                "api_key": "mimo-key",
            },
        },
        "models": {
            "yuren/main": {"context_window": 128_000, "request": {"temperature": 0.2}},
            "mimo/pro": {"context_window": 200_000},
        },
    }
    if default_model is not None:
        project["default_model"] = default_model
    return AppConfig(
        env={},
        project_config=project,
    )


def test_factory_catalog_startup_honors_model_spec_over_default(tmp_path: Path) -> None:
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        app_config=_catalog_config(),
        model_spec="mimo/pro",
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.provider.name == "mimo"
    assert app.chat_runner.provider.model == "pro"
    assert app.chat_runner.context_window == 200_000


def test_factory_catalog_startup_honors_default_over_saved_state(tmp_path: Path) -> None:
    data_root = tmp_path / ".lanscoder"
    ModelStateStore(data_root / "model_state.json").record_selection("mimo/pro")
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=data_root,
        app_config=_catalog_config(default_model="yuren/main"),
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.provider.name == "yuren"
    assert app.chat_runner.provider.model == "main"
    assert app.chat_runner.context_window == 128_000


def test_factory_catalog_startup_falls_back_from_stale_saved_state(tmp_path: Path) -> None:
    data_root = tmp_path / ".lanscoder"
    ModelStateStore(data_root / "model_state.json").record_selection("gone/model")
    config = _catalog_config(default_model=None)
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=data_root,
        app_config=config,
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.provider.name == "mimo"
    assert app.chat_runner.provider.model == "pro"


def test_catalog_model_switch_records_selection_and_request_options(tmp_path: Path) -> None:
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        app_config=_catalog_config(),
        session_id="sess_test",
        tools=[],
    )

    result = app.command_handler.handle("/model mimo/pro")

    assert result.output == "Model switched: mimo/pro"
    assert app.chat_runner.request_options.temperature is None
    assert app.chat_runner.context_window == 200_000
    assert ModelStateStore(tmp_path / ".lanscoder" / "model_state.json").load().last_selected == "mimo/pro"


def test_catalog_model_switch_rejects_unconfigured_short_name(tmp_path: Path) -> None:
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        app_config=_catalog_config(),
        session_id="sess_test",
        tools=[],
    )

    result = app.command_handler.handle("/model pro")

    assert result.output == "Model switch failed: 模型目录模式需要使用 <provider>/<model>"


def test_catalog_picker_can_switch_mixed_case_provider_ref(tmp_path: Path) -> None:
    config = AppConfig(
        env={},
        project_config={
            "default_model": "Yuren/main",
            "providers": {
                "Yuren": {
                    "type": "openai-compatible",
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                }
            },
            "models": {
                "Yuren/main": {},
                "Yuren/pro": {},
            },
        },
    )
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        app_config=config,
        session_id="sess_test",
        tools=[],
    )

    picker = app.command_handler.handle("/models")
    selected = picker.action["models"][1]
    result = app.command_handler.handle(f"/model {selected['provider']}/{selected['model']}")

    assert selected == {"provider": "Yuren", "model": "pro"}
    assert result.output == "Model switched: Yuren/pro"
    assert app.chat_runner.provider.name == "Yuren"
    assert ModelStateStore(tmp_path / ".lanscoder" / "model_state.json").load().last_selected == "Yuren/pro"


def test_catalog_anthropic_alias_is_current_model_and_picker_selection(tmp_path: Path) -> None:
    config = AppConfig(
        env={},
        project_config={
            "default_model": "claude/sonnet",
            "providers": {
                "claude": {
                    "type": "anthropic",
                    "api_key": "test-key",
                }
            },
            "models": {
                "claude/sonnet": {},
                "claude/opus": {},
            },
        },
    )
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        app_config=config,
        session_id="sess_test",
        tools=[],
    )

    picker = app.command_handler.handle("/models")

    assert app.chat_runner.provider.name == "claude"
    assert "Current model: claude/sonnet" in picker.output
    assert picker.action["models"] == [
        {"provider": "claude", "model": "opus"},
        {"provider": "claude", "model": "sonnet"},
    ]
    assert picker.action["selected_index"] == 1


def test_app_factory_configures_default_loop_limits(tmp_path: Path) -> None:
    app = create_lanscoder_app(project_root=tmp_path, provider=FakeProvider([]), tools=[])

    assert app.chat_runner.limits == AgentLoopLimits.default()


def test_create_lanscoder_app_keeps_streaming_disabled_without_capability(tmp_path: Path) -> None:
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.use_streaming is False


def test_create_lanscoder_app_uses_consistent_data_root_for_share(tmp_path: Path) -> None:
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
        tools=[],
    )

    result = app.command_handler.handle("/share sess_test")

    assert "Share exported:" in result.output
    assert (tmp_path / ".lanscoder" / "shares" / "sess_test.md").exists()
    assert JsonlSessionStore(tmp_path / ".lanscoder").rebuild_session_view("sess_test").session_id == "sess_test"


def test_create_lanscoder_app_can_use_default_builtin_tools(tmp_path: Path) -> None:
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
    )

    assert app.chat_runner.tools
    names = [tool.name for tool in app.chat_runner.tools or []]
    assert "write" in names
    assert "edit" in names
    assert "apply_patch" in names
    assert "shell" in names
    assert "fetch" in names
    assert "web_search" in names


def test_factory_background_controls_remain_session_scoped(tmp_path: Path) -> None:
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=FakeProvider([]),
        session_id="sess_factory_a",
    )
    manager = app.chat_runner.background_manager
    assert manager is not None
    store = JsonlSessionStore(tmp_path / ".lanscoder")
    session_a = app.current_session.session
    session_b = AgentSession.create(
        store=store,
        session_id="sess_factory_b",
        tools=list(app.chat_runner.tools or []),
    )
    loop_a = create_agent_loop(session=session_a, provider=FakeProvider([]), background_manager=manager)
    loop_b = create_agent_loop(session=session_b, provider=FakeProvider([]), background_manager=manager)
    started = threading.Event()
    release = threading.Event()
    try:
        manager.start(
            lambda: (started.set(), release.wait(5), make_text_result("shell", "done"))[2],
            session_id=session_a.session_id,
            tool_name="shell",
        )
        assert started.wait(timeout=5) is True

        assert session_b.tool_registry.execute("background_status", {}).data["jobs"] == []
        assert session_b.tool_registry.execute("background_cancel", {"job_id": "bg_0001"}).ok is False

        release.set()
        assert manager.wait(timeout=5) is True
        loop_b._append_background_notifications()
        assert not any(message.role == "notification" and "<task_notification>" in message.parts[0].content for message in session_b.rebuild_view().messages)

        loop_a._append_background_notifications()
        assert sum(1 for message in session_a.rebuild_view().messages if message.role == "notification" and "<task_notification>" in message.parts[0].content) == 1
    finally:
        release.set()
        manager.shutdown()


def test_create_lanscoder_app_wires_l3_service_for_default_context_manager(tmp_path: Path) -> None:
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
        tools=[],
    )

    assert isinstance(app.chat_runner.context_manager.l3_service, LlmCompactService)


def test_create_lanscoder_app_persists_permission_grants(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_write",
                        name="write",
                        arguments={"path": "README.md", "content": "hello"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="done"),
        ]
    )
    app = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=provider,
        session_id="sess_test",
        tools=[create_write_tool(tmp_path)],
    )

    waiting = app.chat_runner.run_user_turn("写 README")
    assert waiting.finish_reason == "waiting_for_user_input"
    assert app.chat_runner.last_pending_input is not None
    app.chat_runner.resume_with_user_input(app.chat_runner.last_pending_input.id, "allow_always_same_scope")

    assert (tmp_path / ".lanscoder" / "permissions.json").exists()

    second = create_lanscoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".lanscoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_second",
        tools=[create_write_tool(tmp_path)],
    )
    result = second.chat_runner.current_session.session.execute_tool_call(ToolCall(id="call_write_again", name="write", arguments={"path": "README.md", "content": "again"}))

    assert result.ok is True
    assert result.data.get("request_type") != "permission_confirmation"
