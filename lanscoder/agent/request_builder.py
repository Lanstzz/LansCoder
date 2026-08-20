"""纯请求构造：把 SessionView 投影成 provider 请求。

从 AgentLoop 抽出的纯变换层：输入 view + definitions，输出 PreparedMainRequest
或 ContextBudget。它不触碰 loop 状态（调用计数、事件流、工具执行），因此可以脱离
AgentLoop 单独构造与测试。Provider 侧的信息（name/model/capabilities）通过构造依赖
注入，避免在请求构造时再依赖运行期 loop 字段。
"""

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


# ============================================================================
# PreparedMainRequest — 一次 provider 请求的准备结果
# ============================================================================
# 把"构造 provider 请求"和"真正调用 provider"拆开，便于在调用前后做追踪/去重/诊断。
# - request: 最终交给 provider.complete() 的 ChatRequest
# - request_id: 用于追踪这一次调用，写入 session 的 provider_projection_consumed 记录
# - projection_fingerprint: 消息+工具定义的哈希，用于诊断"同一份投影被调用了几次"
# - tool_result_part_ids: 本次投影消费了哪些 tool_result part，便于后续 compact 判断哪些
#   tool_result 已经"被模型看过了"，可以安全压缩
# ============================================================================
@dataclass(frozen=True, slots=True)
class PreparedMainRequest:
    request: ChatRequest
    request_id: str
    projection_fingerprint: str
    tool_result_part_ids: tuple[str, ...]


class RequestBuilder:
    """把 SessionView 投影成一次主请求或上下文预算。

    所有方法都是纯变换：给定 view（外加 definitions / tool_choice /
    runtime_instruction），返回新对象，不改 session、不触发 compact。loop 与 runtime
    各自持有自己的 RequestBuilder 实例，构造逻辑只此一份。
    """

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
        """一次完整的主请求构造：投影 + 组装 ChatRequest + 追踪指纹。"""
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
        return self.context_builder.build_provider_messages(
            view,
            system_prefix=system_prefix,
            store_root=self.session.store.root,
        )

    def _main_chat_request(self, messages, definitions, tool_choice) -> ChatRequest:
        return ChatRequest(
            messages=messages,
            tools=definitions,
            tool_choice=tool_choice,
            **self._request_options.as_chat_request_kwargs(),
        )
