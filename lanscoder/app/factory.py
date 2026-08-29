"""应用组合根:按配置装配 provider、会话、工具、MCP、命令处理器与 TUI 应用,返回可运行实例。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import Protocol

from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.app.commands import ContextCommandHandler
from lanscoder.app.help_commands import HelpCommandHandler
from lanscoder.app.mcp_commands import McpCommandHandler
from lanscoder.app.memory_commands import MemoryCommandHandler
from lanscoder.app.model_commands import ModelCommandHandler, ModelState
from lanscoder.app.model_state import ModelSelectionState, ModelStateStore
from lanscoder.app.permission_commands import PermissionCommandHandler
from lanscoder.app.recall_commands import RecallCommandHandler
from lanscoder.app.router import CompositeCommandHandler
from lanscoder.app.session_commands import SessionCommandHandler
from lanscoder.app.skill_commands import SkillCommandHandler
from lanscoder.app.tui import LansCoderApp, LansCoderTuiConfig
from lanscoder.config.models import ModelCatalog, ModelProfile
from lanscoder.config.settings import AppConfig, load_config
from lanscoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from lanscoder.context.triggers import ContextCompactionConfig
from lanscoder.core.runtime import AgentChatRunner
from lanscoder.core.session import create_agent_session
from lanscoder.mcp.adapter import adapt_mcp_tool
from lanscoder.mcp.config import load_mcp_configs
from lanscoder.mcp.manager import McpManager
from lanscoder.mcp.models import McpServerStatus, McpToolDescription
from lanscoder.mcp.search import McpSearchEntry, create_mcp_tool_search
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.factory import (
    ProviderConfigError,
    create_provider_for_model,
)
from lanscoder.providers.types import MainRequestOptions
from lanscoder.session.bootstrap import SessionBootstrap
from lanscoder.session.catalog import SessionCatalog
from lanscoder.session.fork import ForkSessionService
from lanscoder.session.new import NewSessionService
from lanscoder.session.resume import ResumeService
from lanscoder.session.share import SessionShareService
from lanscoder.tools.builtin import create_builtin_registry
from lanscoder.agent.background import BackgroundJobManager
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess


class McpManagerLike(Protocol):
    """McpManager 的最小接口,便于在测试中替换真实实现。"""

    def connect_all(self) -> None: ...

    def connect_all_in_background(self) -> None: ...

    def tools(self) -> tuple[tuple[str, McpToolDescription], ...]: ...

    def statuses(self) -> tuple[McpServerStatus, ...]: ...

    def doctor(self, name: str) -> McpServerStatus | None: ...

    def reconnect(self, name: str | None = None) -> bool: ...

    def close(self) -> None: ...


class McpToolProvider:
    """把基础工具与 MCP 发现到的工具合并为最终工具集;查询延迟到运行时以便热更新。"""

    def __init__(self, base_tools: list[Tool], manager: McpManagerLike, *, include_mcp: bool) -> None:
        self._base_tools = list(base_tools)
        self._manager = manager
        self._include_mcp = include_mcp

    def __call__(self) -> list[Tool]:
        """返回合并后的工具列表;MCP 工具逐个适配去重,并附上工具搜索入口。"""
        tools = list(self._base_tools)
        if not self._include_mcp:
            return tools
        names = {tool.name for tool in tools}
        entries: list[McpSearchEntry] = []
        try:
            catalog = self._manager.tools()
        except Exception:
            return tools
        for server, discovered_tool in catalog:
            try:
                tool = adapt_mcp_tool(self._manager, server, discovered_tool, existing_names=names)
            except ValueError:
                continue
            tools.append(tool)
            names.add(tool.name)
            entries.append(
                McpSearchEntry(
                    server=server,
                    tool=discovered_tool.name,
                    definition=tool.definition,
                )
            )
        if entries and "mcp_tool_search" not in names:
            tools.append(create_mcp_tool_search(tuple(entries)))
        return tools


def create_lanscoder_app(
    *,
    project_root: str | Path = ".",
    data_root: str | Path | None = None,
    provider: ChatProvider | None = None,
    session_id: str | None = None,
    resume_session: bool = False,
    tools: list[Tool] | None = None,
    config: LansCoderTuiConfig | None = None,
    app_config: AppConfig | None = None,
    mcp_manager_factory: Callable[[tuple], McpManagerLike] | None = None,
    model_spec: str | None = None,
    compact_config: ContextCompactionConfig | None = None,
    context_window: int | None = None,
    compaction_strategy: str | None = None,
) -> LansCoderApp:
    """应用工厂:解析配置、装配全部组件并返回可运行的 LansCoderApp。"""

    project_path = Path(project_root)
    resolved_data_root = Path(data_root) if data_root is not None else project_path / ".lanscoder"
    resolved_app_config = app_config or load_config(project_root=project_path)
    model_state_store = ModelStateStore(resolved_data_root / "model_state.json")
    model_catalog = resolved_app_config.model_catalog()
    selected_profile: ModelProfile | None = None
    if provider is None:
        if not model_catalog.profiles:
            raise ValueError("模型目录为空；请配置 default_model、[providers] 和 [models]")
        selected_profile = _initial_model_profile(
            model_catalog,
            model_spec=model_spec,
            state=model_state_store.load(),
        )
        try:
            provider = create_provider_for_model(selected_profile)
        except ProviderConfigError as error:
            raise ValueError(str(error)) from error
    sandbox_access = SandboxAccess()
    background_manager = BackgroundJobManager()
    resolved_tools = (
        tools
        if tools is not None
        else create_builtin_registry(
            project_path,
            include_mutation_tools=True,
            include_execution_tools=True,
            include_network_tools=True,
            access=sandbox_access,
        ).tools()
    )
    mcp_manager = (mcp_manager_factory or McpManager)(load_mcp_configs(resolved_app_config))
    try:
        mcp_manager.connect_all_in_background()
    except Exception:
        pass
    tool_provider = McpToolProvider(resolved_tools, mcp_manager, include_mcp=tools is None)
    current_tools = tool_provider()
    resolved_provider = provider
    handle = create_agent_session(
        provider=resolved_provider,
        project_root=project_path,
        data_root=resolved_data_root,
        tools=current_tools,
        session_id=session_id,
        resume=resume_session,
        limits=AgentLoopLimits.default(),
        request_options=_main_request_options(selected_profile),
        context_window=(
            context_window
            if context_window is not None
            else (selected_profile.context_window if selected_profile is not None else None)
        ),
        compaction_strategy=compaction_strategy or "l1_l2_l3",
        background_manager=background_manager,
    )
    chat_runner = handle.runner
    # TUI 专属接线:core 是 headless 装配源;流式开关 / MCP 热更新工具集 / 压缩配置
    # 属应用层行为,在 handle 之上补齐,不改 core 装配。
    chat_runner.tools_provider = tool_provider
    chat_runner.use_streaming = _should_use_streaming(resolved_provider, resolved_app_config)
    context_manager = chat_runner.context_manager
    if compact_config is not None:
        context_manager.config = compact_config
    current = chat_runner.current_session
    session = current.session
    store = session.store
    bootstrap = SessionBootstrap(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools=current_tools,
        sandbox_access=sandbox_access,
    )
    compact_summarizer = context_manager.l3_service.summarizer
    catalog = SessionCatalog(resolved_data_root)
    from lanscoder.session.index import SessionIndex

    SessionIndex(resolved_data_root).prune_empty(exclude={session.session_id})
    resume_service = ResumeService(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools_provider=tool_provider,
        sandbox_access=sandbox_access,
        catalog=catalog,
    )
    new_service = NewSessionService(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools_provider=tool_provider,
        sandbox_access=sandbox_access,
    )
    fork_service = ForkSessionService(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools_provider=tool_provider,
        sandbox_access=sandbox_access,
        catalog=catalog,
    )
    session_handler = SessionCommandHandler(
        catalog=catalog,
        current_session=current.session,
        new_service=new_service,
        fork_service=fork_service,
        resume_service=resume_service,
        share_service=SessionShareService(store),
        store=store,
        on_resume=current.set_session,
    )
    permission_handler = PermissionCommandHandler(session=current)

    def skill_catalog_provider():
        return current.session.skill_catalog

    def skill_refresher():
        return current.session.refresh_skills(project_path)

    skill_handler = SkillCommandHandler(
        catalog_provider=skill_catalog_provider,
        skill_refresher=skill_refresher,
    )
    memory_handler = MemoryCommandHandler(
        memory_provider=lambda: current.session.memory_manager,
        writer_provider=lambda: current.session.writer,
    )
    context_handler = ContextCommandHandler(
        session=current,
        context_manager=context_manager,
        budget_provider=chat_runner.context_budget,
    )
    model_switcher = RuntimeModelSwitcher(
        app_config=resolved_app_config,
        chat_runner=chat_runner,
        compact_summarizer=compact_summarizer,
        catalog=model_catalog,
        state_store=model_state_store,
    )
    recall_handler = RecallCommandHandler(
        session=current,
        store=store,
        bootstrap=bootstrap,
        on_recall=current.set_session,
        resume_service=resume_service,
        background_manager=background_manager,
    )
    command_handler = CompositeCommandHandler(
        [
            McpCommandHandler(mcp_manager),
            ModelCommandHandler(model_switcher),
            session_handler,
            recall_handler,
            context_handler,
            permission_handler,
            skill_handler,
            memory_handler,
        ]
    )
    help_handler = HelpCommandHandler(command_handler=command_handler)
    command_handler.handlers.insert(0, help_handler)

    def _close_session_and_mcp() -> None:
        try:
            chat_runner.flush_background_notifications()
        finally:
            mcp_manager.close()

    app = LansCoderApp(
        command_handler=command_handler,
        chat_runner=chat_runner,
        current_session=current,
        config=config
        or LansCoderTuiConfig(
            provider_name=resolved_provider.name,
            provider_model=resolved_provider.model,
            project_name=project_path.resolve().name,
        ),
        on_shutdown=_close_session_and_mcp,
    )
    recall_handler.busy_check = lambda: app.is_turn_active()
    session_handler.busy_check = lambda: app.is_turn_active()
    app.set_slash_commands(command_handler.all_commands())
    return app


def _should_use_streaming(provider: ChatProvider, config: AppConfig) -> bool:
    """按 provider 能力与配置决定是否启用流式响应。"""
    if not bool(getattr(getattr(provider, "capabilities", None), "supports_streaming", False)):
        return False
    configured = config.get_provider_bool("streaming", env="LANSCODER_STREAMING", provider_name=provider.name)
    if configured is None:
        return True
    return configured


class RuntimeModelSwitcher:
    """运行时模型切换:按模型规格重建 provider 并热替换到 chat_runner。"""

    def __init__(
        self,
        *,
        app_config: AppConfig,
        chat_runner: AgentChatRunner,
        compact_summarizer: ProviderLlmCompactSummarizer,
        catalog: ModelCatalog | None = None,
        state_store: ModelStateStore | None = None,
    ) -> None:
        self._app_config = app_config
        self._chat_runner = chat_runner
        self._compact_summarizer = compact_summarizer
        self._catalog = catalog or app_config.model_catalog()
        self._state_store = state_store

    def current_model(self) -> ModelState:
        """返回当前生效的 provider/model。"""
        provider = self._chat_runner.provider
        return ModelState(provider=provider.name, model=provider.model)

    def model_choices(self) -> list[ModelState]:
        """返回目录中全部模型去重后的选择列表。"""
        return _unique_model_states([ModelState(provider=profile.provider_id, model=profile.model_id) for profile in self._catalog.list()])

    def switch_model(self, spec: str) -> ModelState:
        """按规格切换模型并持久化选择,返回新模型状态。"""
        selected_provider, model = _parse_model_spec(spec)
        if selected_provider is None:
            raise ValueError("模型目录模式需要使用 <provider>/<model>")
        ref = f"{selected_provider}/{model}"
        profile = self._catalog.get(ref)
        if profile is None:
            raise ValueError(f"未配置模型：{ref}。请在 [models] 中添加它。")
        return self._apply_profile(profile, persist=True)

    def _apply_profile(self, profile: ModelProfile, *, persist: bool) -> ModelState:
        """落地模型档案:重建 provider、更新 chat_runner 与压缩器,按需记录选择。"""
        try:
            provider = create_provider_for_model(profile)
        except ProviderConfigError as error:
            raise ValueError(str(error)) from error
        self._chat_runner.set_model(
            provider,
            request_options=_main_request_options(profile),
            context_window=profile.context_window,
            use_streaming=_should_use_streaming(provider, self._app_config),
        )
        self._compact_summarizer.provider = provider
        if persist and self._state_store is not None:
            self._state_store.record_selection(profile.ref)
        return ModelState(provider=provider.name, model=provider.model)


def _main_request_options(profile: ModelProfile | None) -> MainRequestOptions:
    """从模型档案构建请求选项,无档案时用默认值。"""
    if profile is None:
        return MainRequestOptions()
    request = profile.request
    return MainRequestOptions(
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        extra_body=request.extra_body,
    )


def _initial_model_profile(
    catalog: ModelCatalog,
    *,
    model_spec: str | None,
    state: ModelSelectionState,
) -> ModelProfile:
    """确定初始模型档案:按 model_spec → 默认 → 上次选择 → 目录首个 依次回退。"""
    for ref in (model_spec, catalog.default_ref, state.last_selected):
        if ref and catalog.get(ref):
            return catalog.require(ref)
    profiles = catalog.list()
    if not profiles:
        raise ValueError("模型目录为空")
    return profiles[0]


def _parse_model_spec(spec: str) -> tuple[str | None, str]:
    """解析模型规格为 (provider, model),provider 可缺省。"""
    value = spec.strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("usage: /model <model> or /model <provider>/<model>")
    provider, model = value.split("/", 1) if "/" in value else (None, value)
    provider = provider.strip() if provider else None
    model = model.strip()
    if not model:
        raise ValueError("model name is required")
    return provider, model


def _unique_model_states(states: list[ModelState]) -> list[ModelState]:
    """按 (provider, model) 去重模型状态列表。"""
    unique: list[ModelState] = []
    seen: set[tuple[str, str]] = set()
    for state in states:
        key = (state.provider, state.model)
        if key in seen:
            continue
        seen.add(key)
        unique.append(state)
    return unique
