"""L3 create_agent_session: headless 唯一装配源。

把 provider + session + 工具 + context 管理器 + runner 装配成
``AgentSessionHandle``;模型选择由调用方(如 app/factory.py)完成,core 只接收
选好的 provider。core 自身不 import app。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lanscoder.agent.background import BackgroundJobManager
from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.agent.session import AgentSession
from lanscoder.context.llm_compact import LlmCompactService
from lanscoder.context.manager import CompactionStrategy, ContextWindowManager
from lanscoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from lanscoder.context.store import JsonlSessionStore
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import MainRequestOptions
from lanscoder.session.bootstrap import SessionBootstrap
from lanscoder.storage import LansCoderPaths
from lanscoder.tools.builtin import create_builtin_registry
from lanscoder.tools.types import Tool
from lanscoder.utils.sandbox_access import SandboxAccess
from lanscoder.observability.context import JournalTraceResumeLookup
from lanscoder.observability.protocol import TraceRecorder

from lanscoder.core.runtime import AgentChatRunner, CurrentSessionState


@dataclass(slots=True)
class AgentSessionHandle:
    """L3 返回的聚合对象(D2): session + runner,L2 Agent 保持独立(session-free)。"""

    session: AgentSession
    runner: AgentChatRunner


def create_agent_session(
    *,
    provider: ChatProvider,
    project_root: str | Path,
    storage_root: str | Path | None = None,
    tools: list[Tool] | None = None,
    session_id: str | None = None,
    resume: bool = False,
    limits: AgentLoopLimits | None = None,
    request_options: MainRequestOptions | None = None,
    context_window: int | None = None,
    background_manager: BackgroundJobManager | None = None,
    user_memory_root: str | Path | None = None,
    compaction_strategy: str = "l1_l2_l3",
    trace_recorder: TraceRecorder | None = None,
) -> AgentSessionHandle:
    """headless 唯一装配源;持久化、内置工具、权限、上下文压缩都在这里落地。

    ``compaction_strategy`` 取值 ``no_compact`` / ``l1_l2`` / ``l1_l2_l3``
    (默认 ``l1_l2_l3`` 保持现有全量压缩行为),供基准评估 A/B 使用。
    """

    return _create_agent_session(
        provider=provider,
        project_root=project_root,
        storage_root=storage_root,
        tools=tools,
        session_id=session_id,
        resume=resume,
        limits=limits,
        request_options=request_options,
        context_window=context_window,
        background_manager=background_manager,
        user_memory_root=user_memory_root,
        compaction_strategy=compaction_strategy,
        trace_recorder=trace_recorder,
    )


def create_provisional_primary_session(
    *,
    provider: ChatProvider,
    project_root: str | Path,
    storage_root: str | Path | None = None,
    tools: list[Tool] | None = None,
    session_id: str | None = None,
    limits: AgentLoopLimits | None = None,
    request_options: MainRequestOptions | None = None,
    context_window: int | None = None,
    background_manager: BackgroundJobManager | None = None,
    user_memory_root: str | Path | None = None,
    compaction_strategy: str = "l1_l2_l3",
    trace_recorder: TraceRecorder | None = None,
) -> AgentSessionHandle:
    """Assemble the CLI/TUI primary runtime without persisting an empty root."""

    return _create_agent_session(
        provider=provider,
        project_root=project_root,
        storage_root=storage_root,
        tools=tools,
        session_id=session_id,
        resume=False,
        limits=limits,
        request_options=request_options,
        context_window=context_window,
        background_manager=background_manager,
        user_memory_root=user_memory_root,
        compaction_strategy=compaction_strategy,
        trace_recorder=trace_recorder,
        provisional_primary=True,
    )


def _create_agent_session(
    *,
    provider: ChatProvider,
    project_root: str | Path,
    storage_root: str | Path | None,
    tools: list[Tool] | None,
    session_id: str | None,
    resume: bool,
    limits: AgentLoopLimits | None,
    request_options: MainRequestOptions | None,
    context_window: int | None,
    background_manager: BackgroundJobManager | None,
    user_memory_root: str | Path | None,
    compaction_strategy: str,
    trace_recorder: TraceRecorder | None,
    provisional_primary: bool = False,
) -> AgentSessionHandle:
    """Shared assembly for durable SDK and provisional CLI/TUI primary runtimes."""

    project_path = Path(project_root)
    paths = LansCoderPaths(storage_root=storage_root, project_root=project_path)
    store = JsonlSessionStore(paths.storage_root)
    sandbox_access = SandboxAccess()
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
    bootstrap = SessionBootstrap(
        store=store,
        project_root=project_path,
        paths=paths,
        tools=resolved_tools,
        sandbox_access=sandbox_access,
        user_memory_root=user_memory_root,
    )
    if resume and session_id is not None:
        session = bootstrap.resume(session_id)
        session.restore_pending_permission_execution()
    elif provisional_primary:
        session = bootstrap.create_provisional_primary(session_id=session_id)
    else:
        session = bootstrap.create(session_id=session_id)

    if trace_recorder is None:
        trace_recorder = bootstrap.create_trace_recorder()
    context_manager = ContextWindowManager(
        store=store,
        strategy=CompactionStrategy(compaction_strategy),
        trace_recorder=trace_recorder,
        l3_service=LlmCompactService(
            store=store,
            summarizer=ProviderLlmCompactSummarizer(provider, trace_recorder=trace_recorder),
        ),
    )
    current = CurrentSessionState(session)
    runner = AgentChatRunner(
        current_session=current,
        provider=provider,
        tools=resolved_tools,
        context_manager=context_manager,
        limits=limits,
        use_streaming=False,
        request_options=request_options or MainRequestOptions(),
        context_window=context_window,
        background_manager=background_manager,
        trace_recorder=trace_recorder,
        trace_resume_lookup=JournalTraceResumeLookup(store.journal),
    )
    return AgentSessionHandle(session=session, runner=runner)
