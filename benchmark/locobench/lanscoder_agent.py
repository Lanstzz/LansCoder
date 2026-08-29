"""LansCoderAgent(BaseAgent):用 LansCoder ``create_agent_session`` 驱动 LoCoBench 场景。

被测对象是 LansCoder 的 ``ContextWindowManager`` 三层压缩(L1 路由压缩 /
L2 归档占位 / L3 LLM 摘要)。每个 LoCoBench turn 对应一次
``runner.arun_user_turn``;LansCoder 在内部自持会话历史并独占管理上下文
(harness 侧 ``--context-management none`` 时不干预)。

压缩行为通过读取 session store 的 ``compaction_completed`` /
``llm_compaction_completed`` / ``compaction_skipped`` 事件采集,与 harness
的 per-turn ``context_tokens`` 启发式、provider 真实 usage 分开标注。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from locobench.agents.base_agent import (
    AgentMessage,
    AgentResponse,
    BaseAgent,
    MessageRole,
    ToolCall,
)

from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.context.store import JsonlSessionStore
from lanscoder.core.session import create_agent_session
from lanscoder.providers.types import MainRequestOptions

from benchmark.locobench.tool_mapping import map_locobench_tools

logger = logging.getLogger(__name__)

COMPACTION_EVENT_TYPES = frozenset(
    {"compaction_completed", "llm_compaction_completed", "compaction_skipped"}
)


def _as_dict(value: Any) -> dict[str, Any]:
    """把 LLM 返回的工具参数(json 字符串或 dict)归一化为 dict。"""

    if isinstance(value, dict):
        return value
    return {}


def _normalize_tool_name(name: str) -> str:
    """去掉 harness 工具副本后缀:_copy_<id> → 干净的工具名。

    例: ``file_system_copy_21601233936_get_current_directory``
        → ``file_system_get_current_directory``。
    """

    parts = str(name).split("_")
    out: list[str] = []
    index = 0
    while index < len(parts):
        if parts[index] == "copy" and index + 1 < len(parts) and parts[index + 1].isdigit():
            index += 2
            continue
        out.append(parts[index])
        index += 1
    return "_".join(out)


class LansCoderAgent(BaseAgent):
    """LoCoBench BaseAgent 的 LansCoder 进程内实现。"""

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        super().__init__(name, config or {})
        self.provider = self.config["provider"]
        self.data_root = Path(self.config["data_root"])
        self.project_root = Path(self.config.get("project_root") or ".")
        self.context_window: int | None = self.config.get("context_window")
        self.max_tool_rounds = int(self.config.get("max_tool_rounds", 60))

        self._handle: Any = None
        self._store: JsonlSessionStore | None = None
        self._last_event_count = 0

        # 每次 scenario 重置的采集状态
        self.compaction_events: list[dict[str, Any]] = []
        self.turn_stats: list[dict[str, Any]] = []
        self._scenario_started_at: datetime | None = None

    # -- BaseAgent 接口 ---------------------------------------------------

    async def initialize_session(
        self,
        scenario_context: dict[str, Any],
        available_tools: list[Any] | None = None,
    ) -> bool:
        """装配 LansCoder 会话:LoCoBench 工具映射 + create_agent_session。"""

        mapped_tools = map_locobench_tools(available_tools)
        logger.info("LansCoderAgent: mapped %d LoCoBench functions -> %d LansCoder tools", len(available_tools or []), len(mapped_tools))

        handle = create_agent_session(
            provider=self.provider,
            project_root=self.project_root,
            data_root=self.data_root,
            tools=mapped_tools,
            limits=AgentLoopLimits(
                max_tool_rounds=self.max_tool_rounds,
                max_provider_calls=self.max_tool_rounds * 4,
                max_turn_seconds=3600,
            ),
            request_options=MainRequestOptions(),
            context_window=self.context_window,
        )
        # benchmark 非交互:工具直接执行,不弹权限确认
        handle.runner.current_session.set_permission_mode("bypass")

        self._handle = handle
        self._store = JsonlSessionStore(self.data_root)
        self._last_event_count = 0
        self.compaction_events = []
        self.turn_stats = []
        self._scenario_started_at = datetime.now()
        return True

    async def process_turn(
        self,
        message: str,
        available_tools: list[Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> AgentResponse:
        """执行一次 LoCoBench turn = 一次 LansCoder 用户回合。"""

        if self._handle is None:
            raise RuntimeError("LansCoderAgent.initialize_session 必须先调用")

        started = time.monotonic()
        before_count = self._last_event_count
        response = await self._handle.runner.arun_user_turn(message)
        elapsed = time.monotonic() - started

        usage = response.usage
        tokens_used = usage.total_tokens if usage and usage.total_tokens else 0

        # LansCoder 内部 loop 会执行多轮工具调用;最终 response.tool_calls 只含
        # 最后一条消息。从 session store 事件里采集本 turn 的全部工具调用。
        event_tool_calls, new_events = self._drain_turn_events(before_count)
        for event in new_events:
            self.compaction_events.append(event)

        by_id: dict[str, ToolCall] = {tc.call_id: tc for tc in event_tool_calls}
        for tool_call in response.tool_calls:
            by_id.setdefault(
                tool_call.id,
                ToolCall(
                    call_id=tool_call.id,
                    tool_name=_normalize_tool_name(tool_call.name).split("_", 1)[0],
                    function_name=_normalize_tool_name(tool_call.name),
                    parameters=_as_dict(tool_call.arguments),
                ),
            )
        tool_calls = list(by_id.values())

        message_obj = AgentMessage(
            role=MessageRole.ASSISTANT,
            content=response.content or "",
            tool_calls=tool_calls or None,
            context_tokens=int(len((response.content or "").split()) * 1.3),
            metadata={
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
            },
        )
        self.add_message_to_history(message_obj)

        self.turn_stats.append(
            {
                "turn": len(self.turn_stats) + 1,
                "user_message": message,
                "assistant_content": response.content or "",
                "tool_calls": [tc.to_dict() for tc in tool_calls],
                "tokens_used": tokens_used,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "elapsed_seconds": round(elapsed, 3),
                "finish_reason": response.finish_reason,
                "compaction_events": [event["summary"] for event in new_events],
            }
        )

        return AgentResponse(
            message=message_obj,
            tool_calls=tool_calls,
            processing_time=elapsed,
            tokens_used=tokens_used,
            metadata={
                "compaction_events": len(new_events),
                "provider_usage": {
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                    "total_tokens": tokens_used or None,
                },
            },
        )

    async def finalize_session(self) -> dict[str, Any]:
        """结束评估会话,返回统计与压缩行为。"""

        stats = self.get_session_statistics()
        stats["compaction_events"] = self.compaction_events
        stats["turn_stats"] = self.turn_stats
        stats["session_id"] = self._handle.session.session_id if self._handle else None
        return stats

    def clear_history(self) -> None:
        """重置会话历史与 per-scenario 采集状态(harness 在场景间调用)。

        先把已完成的场景统计落盘到 ``data_root/scenario_stats.jsonl``,
        再重置,保证每个场景的 turn/压缩数据都不丢。
        """

        self.flush_scenario_stats()
        super().clear_history()
        self.compaction_events = []
        self.turn_stats = []
        self._last_event_count = 0
        self._scenario_started_at = None

    def flush_scenario_stats(self) -> None:
        """把当前场景的 turn/compaction 统计追加到 sidecar JSONL(可重复调用)。"""

        if self._scenario_started_at is None or not self.turn_stats:
            return
        record = {
            "scenario_started_at": self._scenario_started_at.isoformat(),
            "session_id": self._handle.session.session_id if self._handle else None,
            "turn_stats": self.turn_stats,
            "compaction_events": self.compaction_events,
        }
        sidecar = Path(self.data_root) / "scenario_stats.jsonl"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with sidecar.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False))
            file.write("\n")

    # -- 事件采集 ---------------------------------------------------------

    def _drain_turn_events(self, before_count: int) -> tuple[list[ToolCall], list[dict[str, Any]]]:
        """读取本 turn 新增的 session events,提取工具调用与压缩事件。

        返回 (tool_calls, compaction_events);推进 ``_last_event_count``。
        """

        if self._store is None or self._handle is None:
            return [], []
        try:
            events = self._store.list_events(self._handle.session.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 session events 失败: %s", exc)
            return [], []

        tool_calls: list[ToolCall] = []
        compaction_events: list[dict[str, Any]] = []
        for event in events[before_count:]:
            payload = event.payload or {}
            if event.type == "assistant_message":
                for part in payload.get("parts") or []:
                    if part.get("kind") != "tool_call":
                        continue
                    metadata = part.get("metadata") or {}
                    raw_name = metadata.get("tool_name", "")
                    tool_calls.append(
                        ToolCall(
                            call_id=metadata.get("tool_call_id", ""),
                            tool_name=_normalize_tool_name(raw_name).split("_", 1)[0],
                            function_name=_normalize_tool_name(raw_name),
                            parameters=_as_dict(metadata.get("arguments")),
                        )
                    )
            elif event.type in COMPACTION_EVENT_TYPES:
                compaction_events.append(
                    {
                        "type": event.type,
                        "trigger": payload.get("trigger"),
                        "reason": payload.get("reason"),
                        "status": payload.get("status"),
                        "before_tokens": payload.get("before_tokens"),
                        "after_tokens": payload.get("after_tokens"),
                        "target_tokens": payload.get("target_tokens"),
                        "checkpoint_id": payload.get("checkpoint_id"),
                        "summary": {
                            "type": event.type,
                            "trigger": payload.get("trigger"),
                            "reason": payload.get("reason"),
                            "before_tokens": payload.get("before_tokens"),
                            "after_tokens": payload.get("after_tokens"),
                        },
                    }
                )

        self._last_event_count = len(events)
        return tool_calls, compaction_events
