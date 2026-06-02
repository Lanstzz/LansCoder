"""Agent 会话运行时。

这一层连接 context store、runtime state、system prompt cache 和 session scoped tools。
它不负责调用模型，也不执行压缩；这些动作由更外层的 agent loop 或 context manager 编排。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from firstcoder.context.events import SessionEvent
from firstcoder.context.identity import new_event_id, new_message_id, new_part_id
from firstcoder.context.models import MessagePart
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.system_prompt import PromptPrefixCache, SystemPromptBuilder, SystemPromptInputs
from firstcoder.providers.types import ChatResponse, ToolCall, ToolDefinition
from firstcoder.tools.registry import ToolRegistry
from firstcoder.tools.task_boundary import create_task_boundary_tool
from firstcoder.tools.types import Tool, ToolResult


DEFAULT_BASE_RULES = "你是 FirstCoder，一个本地 AI coding agent。请遵守项目规则并优先保持上下文可恢复。"


@dataclass(slots=True)
class AgentSession:
    """单个会话的运行时容器。

    `SessionRuntimeState` 和 `PromptPrefixCache` 都是运行期对象，不写成自然语言消息。
    真正可 resume 的会话事实通过 `JsonlSessionStore` 追加事件保存。
    """

    session_id: str
    store: JsonlSessionStore
    runtime_state: SessionRuntimeState
    tool_registry: ToolRegistry
    agents_md: str = ""
    base_rules: str = DEFAULT_BASE_RULES
    prompt_cache: PromptPrefixCache = field(default_factory=PromptPrefixCache)
    prompt_builder: SystemPromptBuilder = field(default_factory=SystemPromptBuilder)
    provider_capabilities: dict[str, object] = field(
        default_factory=lambda: {"tool_calling": True, "parallel_tool_calls": False},
    )
    permission_policy: dict[str, object] = field(
        default_factory=lambda: {"read": "allow", "write": "confirm", "shell": "confirm"},
    )
    mode: str = "default"

    @classmethod
    def create(
        cls,
        *,
        store: JsonlSessionStore,
        session_id: str,
        agents_md: str = "",
        tools: list[Tool] | None = None,
    ) -> "AgentSession":
        runtime_state = SessionRuntimeState(session_id=session_id)
        registry = _build_session_tool_registry(runtime_state, tools=tools)
        session = cls(
            session_id=session_id,
            store=store,
            runtime_state=runtime_state,
            tool_registry=registry,
            agents_md=agents_md,
        )
        session.append_session_created()
        return session

    @classmethod
    def from_project(
        cls,
        *,
        store: JsonlSessionStore,
        session_id: str,
        project_root: str | Path,
        tools: list[Tool] | None = None,
    ) -> "AgentSession":
        agents_path = Path(project_root) / "AGENTS.md"
        agents_md = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
        return cls.create(store=store, session_id=session_id, agents_md=agents_md, tools=tools)

    def append_session_created(self) -> None:
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=self.session_id,
                type="session_created",
                payload={"session_id": self.session_id},
            )
        )

    def build_system_prefix(self, *, provider_name: str, tools: list[ToolDefinition]) -> list:
        inputs = SystemPromptInputs(
            base_rules=self.base_rules,
            agents_md=self.agents_md,
            tools=tools,
            provider_name=provider_name,
            provider_capabilities=self.provider_capabilities,
            permission_policy=self.permission_policy,
            mode=self.mode,
        )
        entry = self.prompt_cache.get_or_build(inputs, self.prompt_builder)
        self.runtime_state.system_prompt_fingerprint = entry.fingerprint
        return entry.messages

    def append_user_message(self, content: str) -> str:
        message_id = new_message_id()
        part = MessagePart(id=new_part_id(), message_id=message_id, kind="text", content=content)
        self._append_message_event("user_message", message_id=message_id, parts=[part])
        return message_id

    def append_assistant_response(self, response: ChatResponse) -> str:
        message_id = new_message_id()
        parts: list[MessagePart] = []
        if response.content:
            parts.append(MessagePart(id=new_part_id(), message_id=message_id, kind="text", content=response.content))
        for tool_call in response.tool_calls:
            parts.append(_tool_call_part(message_id=message_id, tool_call=tool_call))
        self._append_message_event(
            "assistant_message",
            message_id=message_id,
            parts=parts,
            metadata={
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
            },
        )
        return message_id

    def append_tool_result(self, *, tool_call: ToolCall, result: ToolResult) -> str:
        message_id = new_message_id()
        part = MessagePart(
            id=new_part_id(),
            message_id=message_id,
            kind="tool_result",
            content=result.content,
            metadata={
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "ok": result.ok,
                "data": result.data,
                "error": result.error,
            },
        )
        self._append_message_event("tool_result", message_id=message_id, parts=[part])
        return message_id

    def rebuild_view(self):
        return self.store.rebuild_session_view(self.session_id)

    def _append_message_event(
        self,
        event_type: str,
        *,
        message_id: str,
        parts: list[MessagePart],
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=self.session_id,
                type=event_type,
                payload={
                    "message_id": message_id,
                    "parts": [part.to_dict() for part in parts],
                    "metadata": metadata or {},
                },
            )
        )


def _build_session_tool_registry(runtime_state: SessionRuntimeState, *, tools: list[Tool] | None) -> ToolRegistry:
    registry = ToolRegistry(tools or [])
    if "task_boundary" not in registry.names():
        registry.register(create_task_boundary_tool(runtime_state))
    return registry


def _tool_call_part(*, message_id: str, tool_call: ToolCall) -> MessagePart:
    return MessagePart(
        id=new_part_id(),
        message_id=message_id,
        kind="tool_call",
        content="",
        metadata={
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "arguments": tool_call.arguments,
        },
    )
