"""请求构建器:把会话视图与工具定义组装为 provider 请求,并计算上下文预算与投影指纹。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from lanscoder.agent.task_plan_policy import render_current_task_plan_snapshot
from lanscoder.context.context_builder import ContextBuilder
from lanscoder.context.identity import new_request_id, stable_json_hash
from lanscoder.context.token_budget import ContextBudget, build_context_budget
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatMessage, ChatRequest, MainRequestOptions

if TYPE_CHECKING:
    from lanscoder.agent.session import AgentSession


@dataclass(frozen=True, slots=True)
class PreparedMainRequest:
    """一次已组装的主请求:请求对象、请求 id、投影指纹与涉及的 tool_result 部件 id。"""

    request: ChatRequest
    request_id: str
    projection_fingerprint: str
    tool_result_part_ids: tuple[str, ...]


class RequestBuilder:
    """组装发给 provider 的主请求,并估算上下文预算。"""

    def __init__(
        self,
        *,
        session: AgentSession,
        provider: ChatProvider,
        context_builder: ContextBuilder,
        request_options: MainRequestOptions,
        context_window: int | None,
    ) -> None:
        self.session = session
        self._provider = provider
        self.context_builder = context_builder
        self._request_options = request_options
        self.context_window = context_window

    def build(
        self,
        view,
        *,
        definitions,
        tool_choice="auto",
        runtime_instruction=None,
    ) -> PreparedMainRequest:
        """构建主请求:生成消息、工具定义与投影指纹。"""
        messages = self._request_messages(
            view=view,
            runtime_instruction=runtime_instruction,
        )
        request = self._main_chat_request(messages, definitions, tool_choice)
        return PreparedMainRequest(
            request=request,
            request_id=new_request_id(),
            projection_fingerprint=stable_json_hash(
                {
                    "messages": [asdict(message) for message in messages],
                    "tools": [asdict(definition) for definition in definitions],
                },
                length=24,
            ),
            tool_result_part_ids=self.context_builder.projected_tool_result_part_ids(view),
        )

    def context_budget_for_view(
        self,
        view,
        *,
        runtime_instruction: str | None,
        definitions,
    ) -> ContextBudget:
        """按视图与工具定义计算上下文预算。"""
        messages = self._request_messages(
            view=view,
            runtime_instruction=runtime_instruction,
        )
        return build_context_budget(
            messages=messages,
            tools=definitions,
            context_window=self.context_window,
            max_output_tokens=self._request_options.max_tokens,
        )

    def _request_messages(self, *, view, runtime_instruction: str | None = None):
        """生成完整消息列表:系统前缀 + 运行指令 + 任务计划快照 + 历史消息。"""
        system_prefix = self.session.build_system_prefix(
            provider_name=self._provider.name,
            provider_model=self._provider.model,
            provider_capabilities=getattr(self._provider, "capabilities", None),
        )
        if runtime_instruction:
            system_prefix = [
                *system_prefix,
                ChatMessage(role="system", content=runtime_instruction),
            ]
        if view.task_plan is not None:
            system_prefix = [
                *system_prefix,
                ChatMessage(
                    role="system",
                    content=render_current_task_plan_snapshot(view.task_plan),
                ),
            ]
        return self._build_provider_messages(
            view,
            system_prefix=system_prefix,
        )

    def _build_provider_messages(self, view, *, system_prefix):
        """委托 ContextBuilder 生成最终的 provider 消息列表。"""
        return self.context_builder.build_provider_messages(
            view,
            system_prefix=system_prefix,
            store_root=self.session.store.root,
        )

    def _main_chat_request(self, messages, definitions, tool_choice) -> ChatRequest:
        """组装 ChatRequest 并并入请求选项。"""
        return ChatRequest(
            messages=messages,
            tools=definitions,
            tool_choice=tool_choice,
            **self._request_options.as_chat_request_kwargs(),
        )
