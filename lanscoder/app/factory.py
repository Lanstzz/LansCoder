"""LansCoder TUI 组装工厂。"""

# ============================================================================
# 阅读路径导航 (Reading Path Guide)
# ============================================================================
# 这是 LansCoder 的组装工厂 (composition root)。
# create_lanscoder_app() 把所有子系统拼成一个可运行的 LansCoderApp：
#   provider ← providers.factory.create_provider_for_model()
#   tools    ← tools.builtin.create_builtin_registry() + MCP tools
#   session  ← session.bootstrap.SessionBootstrap (唯一合法的 session 组装路径)
#   runner   ← app.runtime.AgentChatRunner (用户消息到 AgentLoop 的桥梁)
#   commands ← CompositeCommandHandler (slash commands)
# → 上一步阅读：lanscoder/cli.py (main, create_cli_app)
# → 下一步阅读：lanscoder/app/runtime.py (AgentChatRunner)
# ============================================================================

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
from lanscoder.app.router import CompositeCommandHandler
from lanscoder.app.runtime import AgentChatRunner, CurrentSessionState
from lanscoder.app.session_commands import SessionCommandHandler
from lanscoder.app.skill_commands import SkillCommandHandler
from lanscoder.app.tui import LansCoderApp, LansCoderTuiConfig
from lanscoder.config.models import ModelCatalog, ModelProfile
from lanscoder.config.settings import AppConfig, load_config
from lanscoder.context.llm_compact import LlmCompactService
from lanscoder.context.manager import ContextWindowManager
from lanscoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from lanscoder.context.store import JsonlSessionStore
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
from lanscoder.skills.discovery import discover_all_skills
from lanscoder.tools.builtin import create_builtin_registry
from lanscoder.agent.background import BackgroundJobManager
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess


class McpManagerLike(Protocol):
    """Factory-level MCP lifecycle and discovery boundary."""

    def connect_all(self) -> None: ...

    def connect_all_in_background(self) -> None: ...

    def tools(self) -> tuple[tuple[str, McpToolDescription], ...]: ...

    def statuses(self) -> tuple[McpServerStatus, ...]: ...

    def doctor(self, name: str) -> McpServerStatus | None: ...

    def reconnect(self, name: str | None = None) -> bool: ...

    def close(self) -> None: ...


# ----------------------------------------------------------------------------
# McpToolProvider — 动态工具列表 (Dynamic tool list)
# ----------------------------------------------------------------------------
# 每次 __call__() 都重新合并 builtin tools + 当前 MCP 工具，而不是构造时一次性固化。
# 为什么是动态的：MCP 服务器可能在运行中连接/断开/重连，工具列表会变化。
# 异常时优雅降级：MCP 查询失败就只返回 base tools，不阻断主流程。
# adapt_mcp_tool() 来自 lanscoder/mcp/adapter.py，负责把 MCP 协议的工具描述
# 适配成 LansCoder 内部的 Tool 接口。
# ----------------------------------------------------------------------------
class McpToolProvider:
    """Merge a stable base tool set with the manager's current MCP catalog."""

    def __init__(self, base_tools: list[Tool], manager: McpManagerLike, *, include_mcp: bool) -> None:
        self._base_tools = list(base_tools)
        self._manager = manager
        self._include_mcp = include_mcp

    def __call__(self) -> list[Tool]:
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


# ============================================================================
# create_lanscoder_app() — 整个 TUI 的组装入口 (composition root)
# ============================================================================
# 参数说明：
#   project_root       — 项目根目录，用于解析配置、决定 sandbox 工作目录
#   data_root          — 数据根目录（session/context 持久化），默认 <project_root>/.lanscoder
#   provider           — 可选注入的 ChatProvider；为 None 时从 config + model_spec 自动创建
#   session_id         — 要恢复/绑定的 session ID
#   resume_session     — True 表示走 resume 路径，否则走 from_project 新建路径
#   tools              — 可选注入的工具列表；为 None 时使用 create_builtin_registry()
#   config             — TUI 级配置（provider_name/model 显示用），可自动推导
#   app_config         — 应用级配置（AppConfig），为 None 时调用 load_config() 从项目加载
#   mcp_manager_factory— 可注入的 MCP manager 工厂，便于测试替换；默认用 McpManager
#   model_spec         — "<provider>/<model>" 格式的模型指定，优先于 default 和 last_selected
# 返回值：LansCoderApp — 可直接交给 Textual 运行的 TUI 实例
# ============================================================================
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
) -> LansCoderApp:
    """组装可运行的 LansCoder TUI。

    `data_root` 默认是 `<project_root>/.lanscoder`，并传给 context/session 各组件作为
    统一数据根。
    """

    # ------------------------------------------------------------------
    # 阶段 1: 配置解析 (Configuration resolution)
    # ------------------------------------------------------------------
    # 解析项目目录、数据根目录、应用配置。data_root 是 session/context
    # 各组件的统一持久化根目录。
    project_path = Path(project_root)  # 解析目录路径和配置
    resolved_data_root = Path(data_root) if data_root is not None else project_path / ".lanscoder"
    resolved_app_config = app_config or load_config(project_root=project_path)
    # ------------------------------------------------------------------
    # 阶段 2: 模型选择 (Model selection & provider creation)
    # ------------------------------------------------------------------
    # 优先级：model_spec 参数 > state.last_selected > catalog.default_ref > 第一个 profile
    # create_provider_for_model() 来自 lanscoder/providers/factory.py，
    # 根据 AppConfig + ModelProfile 实例化对应的 ChatProvider（Anthropic/OpenAI/...）。
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
            provider = create_provider_for_model(resolved_app_config, selected_profile)
        except ProviderConfigError as error:
            raise ValueError(str(error)) from error
    # ------------------------------------------------------------------
    # 阶段 3: 工具创建 (Tools creation & MCP integration)
    # ------------------------------------------------------------------
    # create_builtin_registry() 来自 lanscoder/tools/builtin.py，注册所有内置工具
    # （文件读写、bash 执行、搜索等）。SandboxAccess 控制工具的执行权限边界。
    # MCP manager 在后台异步连接所有配置的 MCP 服务器，失败不阻断启动。
    # McpToolProvider 把 builtin + MCP 工具动态合并（见上方 McpToolProvider 注释）。
    store = JsonlSessionStore(resolved_data_root)
    sandbox_access = SandboxAccess()
    background_manager = BackgroundJobManager()
    resolved_tools = (  # 创建内置工具注册表
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
    mcp_manager = (mcp_manager_factory or McpManager)(load_mcp_configs(resolved_app_config))  # MCP 管理
    try:
        mcp_manager.connect_all_in_background()  # 后台异步连接，失败不阻断
    except Exception:
        pass
    tool_provider = McpToolProvider(resolved_tools, mcp_manager, include_mcp=tools is None)
    current_tools = tool_provider()  # 首次调用，获取当前可用的完整工具列表
    resolved_provider = provider
    # ------------------------------------------------------------------
    # 阶段 4: Session 组装 (Session bootstrap) — 唯一合法的 session 组装路径
    # ------------------------------------------------------------------
    # SessionBootstrap 来自 lanscoder/session/bootstrap.py，封装了 session 创建/恢复
    # 的全部逻辑。这里只决定走 resume 还是 from_project 分支。
    # → 见 lanscoder/session/bootstrap.py 了解完整的 session 生命周期。
    bootstrap = SessionBootstrap(  # SessionBootstrap 组装和 session 创建/恢复 唯一合法的session组装路径
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools=current_tools,
        sandbox_access=sandbox_access,
    )
    session = (
        bootstrap.resume(session_id)  # 恢复已有 session：加载历史 context 继续对话
        if resume_session and session_id is not None
        else bootstrap.from_project(session_id=session_id)  # 新建或绑定到项目的 session
    )
    current = CurrentSessionState(session)  # 包装为 TUI 可观察的当前 session 状态
    # ------------------------------------------------------------------
    # 阶段 5: 上下文管理 (Context window management & compaction)
    # ------------------------------------------------------------------
    # ContextWindowManager 来自 lanscoder/context/manager.py，负责在 token 预算
    # 内管理对话历史。L4 压缩使用 LLM 把过长的 context 摘要成更短的版本。
    # ProviderLlmCompactSummarizer 让压缩服务复用当前 provider 来做摘要。
    compact_summarizer = ProviderLlmCompactSummarizer(resolved_provider)
    context_manager = ContextWindowManager(  # 上下文压缩管理 → lanscoder/context/manager.py
        store=store,
        l4_service=LlmCompactService(
            store=store,
            summarizer=compact_summarizer,
        ),
    )
    catalog = SessionCatalog(resolved_data_root)
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
        return discover_all_skills(project_path)

    skill_handler = SkillCommandHandler(catalog_provider=skill_catalog_provider)
    memory_handler = MemoryCommandHandler(
        memory_provider=lambda: current.session.memory_manager,
        writer_provider=lambda: current.session.writer,
    )
    # ------------------------------------------------------------------
    # 阶段 6: Runner 创建 (AgentChatRunner)
    # ------------------------------------------------------------------
    # AgentChatRunner 来自 lanscoder/app/runtime.py，是用户消息到 AgentLoop 执行的桥梁。
    # 它持有 provider、tools、context_manager，负责编排一次完整的 agent 对话循环。
    chat_runner = AgentChatRunner(  # 用户发消息 到 AgentLoop 执行的桥梁 → lanscoder/app/runtime.py
        current_session=current,
        provider=resolved_provider,
        tools=current_tools,
        tools_provider=tool_provider,
        context_manager=context_manager,
        limits=AgentLoopLimits.default(),
        use_streaming=_should_use_streaming(resolved_provider, resolved_app_config),
        request_options=_main_request_options(selected_profile),
        context_window=selected_profile.context_window if selected_profile is not None else None,
        background_manager=background_manager,
    )
    context_handler = ContextCommandHandler(
        session=current,
        context_manager=context_manager,
        budget_provider=chat_runner.context_budget,
    )
    # ------------------------------------------------------------------
    # 阶段 7: 命令处理 (Composite command handler for slash commands)
    # ------------------------------------------------------------------
    # 把各个领域的命令 handler 组合成 CompositeCommandHandler，
    # 由 LansCoderApp 在用户输入 "/" 前缀时路由到对应的 handler。
    # RuntimeModelSwitcher 支持运行时切换模型（/model 命令）。
    model_switcher = RuntimeModelSwitcher(
        app_config=resolved_app_config,
        chat_runner=chat_runner,
        compact_summarizer=compact_summarizer,
        catalog=model_catalog,
        state_store=model_state_store,
    )
    command_handler = CompositeCommandHandler(  # 路由所有 slash commands → lanscoder/app/router.py
        [
            HelpCommandHandler(),
            McpCommandHandler(mcp_manager),
            ModelCommandHandler(model_switcher),
            session_handler,
            context_handler,
            permission_handler,
            skill_handler,
            memory_handler,
        ]
    )
    return LansCoderApp(
        command_handler=command_handler,
        chat_runner=chat_runner,
        current_session=current,
        config=config
        or LansCoderTuiConfig(
            provider_name=resolved_provider.name,
            provider_model=resolved_provider.model,
            project_name=project_path.resolve().name,
        ),
        on_shutdown=mcp_manager.close,
    )


# streaming 需要双重确认：provider 能力（capabilities.supports_streaming）+
# 配置开关（config 中的 streaming 字段或 LANSCODER_STREAMING 环境变量）。
# 配置为 None 时默认开启 streaming。
def _should_use_streaming(provider: ChatProvider, config: AppConfig) -> bool:
    if not bool(getattr(getattr(provider, "capabilities", None), "supports_streaming", False)):
        return False
    configured = config.get_provider_bool("streaming", env="LANSCODER_STREAMING", provider_name=provider.name)
    if configured is None:
        return True
    return configured


class RuntimeModelSwitcher:
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
        provider = self._chat_runner.provider
        return ModelState(provider=provider.name, model=provider.model)

    def model_choices(self) -> list[ModelState]:
        return _unique_model_states([ModelState(provider=profile.provider_id, model=profile.model_id) for profile in self._catalog.list()])

    def switch_model(self, spec: str) -> ModelState:
        selected_provider, model = _parse_model_spec(spec)
        if selected_provider is None:
            raise ValueError("模型目录模式需要使用 <provider>/<model>")
        ref = f"{selected_provider}/{model}"
        profile = self._catalog.get(ref)
        if profile is None:
            raise ValueError(f"未配置模型：{ref}。请在 [models] 中添加它。")
        return self._apply_profile(profile, persist=True)

    def _apply_profile(self, profile: ModelProfile, *, persist: bool) -> ModelState:
        try:
            provider = create_provider_for_model(self._app_config, profile)
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
    for ref in (model_spec, catalog.default_ref, state.last_selected):
        if ref and catalog.get(ref):
            return catalog.require(ref)
    profiles = catalog.list()
    if not profiles:
        raise ValueError("模型目录为空")
    return profiles[0]


def _parse_model_spec(spec: str) -> tuple[str | None, str]:
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
    unique: list[ModelState] = []
    seen: set[tuple[str, str]] = set()
    for state in states:
        key = (state.provider, state.model)
        if key in seen:
            continue
        seen.add(key)
        unique.append(state)
    return unique
