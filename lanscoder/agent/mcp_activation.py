from __future__ import annotations

from lanscoder.mcp.search import MCP_TOOL_SEARCH_NAME
from lanscoder.providers.types import ToolCall
from lanscoder.tools.types import ToolResult, make_error_result
from lanscoder.observability.models import ObservationType, TraceScope
from lanscoder.observability.protocol import TraceRecorder


class McpActivationTracker:

    def __init__(
        self,
        mcp_tool_names: frozenset[str],
        *,
        trace_recorder: TraceRecorder | None = None,
        trace_id: str | None = None,
        trace_scope: TraceScope | None = None,
    ) -> None:
        self._mcp_tool_names = mcp_tool_names
        self._active: set[str] = set()
        self._trace_recorder = trace_recorder
        self._trace_id = trace_id
        self._trace_scope = trace_scope

    def clear(self) -> None:
        self._active.clear()

    def set_trace_context(
        self,
        recorder: TraceRecorder | None,
        trace_id: str | None,
        scope: TraceScope | None,
    ) -> None:
        self._trace_recorder = recorder
        self._trace_id = trace_id
        self._trace_scope = scope

    def validate(self, tool_call: ToolCall) -> ToolResult | None:
        if tool_call.name not in self._mcp_tool_names:
            return None
        if tool_call.name in self._active:
            return None
        return make_error_result(
            tool_call.name,
            "MCP tool is not active for this user turn. Call mcp_tool_search first.",
            mcp_activation_required=True,
        )

    def observe(self, tool_call: ToolCall, result: ToolResult) -> None:
        if tool_call.name != MCP_TOOL_SEARCH_NAME or not result.ok:
            return
        payload = result.data.get("mcp_tool_search")
        if not isinstance(payload, dict):
            return
        activated = payload.get("activated_tools")
        if not isinstance(activated, list):
            return
        self._active.update(name for name in activated if isinstance(name, str) and name in self._mcp_tool_names)
        if self._trace_recorder is not None and self._trace_id is not None:
            try:
                observation_id = self._trace_recorder.start_observation(
                    self._trace_id,
                    ObservationType.EVENT,
                    scope=self._trace_scope,
                    data={
                        "event": "mcp_activation",
                        "tool_call_id": tool_call.id,
                        "activated_tools": sorted(self._active),
                    },
                )
                self._trace_recorder.end_observation(observation_id, outcome="succeeded")
            except Exception:
                pass

    @property
    def active_names(self) -> frozenset[str]:
        return frozenset(self._active)

    @property
    def mcp_tool_names(self) -> frozenset[str]:
        return self._mcp_tool_names
