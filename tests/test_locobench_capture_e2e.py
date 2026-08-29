"""LansCoderAgent._drain_turn_events 端到端采集测试(需 locobench,否则跳过)。

在 LoCoBench venv(/tmp/LoCoBench-Agent/.venv,已安装 locobench + lanscoder)
下运行;仓库 dev venv 未安装 locobench 时自动跳过。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("locobench")

from lanscoder.context.events import SessionEvent
from lanscoder.context.store import JsonlSessionStore
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse


class _FakeProvider(ChatProvider):
    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("不应发起 provider 调用")


def _make_agent(tmp_path) -> tuple:
    from benchmark.locobench.lanscoder_agent import LansCoderAgent

    agent = LansCoderAgent(
        name="lanscoder-test",
        config={
            "provider": _FakeProvider(),
            "data_root": str(tmp_path),
            "locobench_root": str(tmp_path),
        },
    )
    agent._store = JsonlSessionStore(tmp_path)
    agent._handle = SimpleNamespace(session=SimpleNamespace(session_id="sess_test"))
    return agent, agent._store


def test_drain_turn_events_captures_tool_calls_and_compaction(tmp_path) -> None:
    from benchmark.locobench.compaction_capture import COMPACTION_EVENT_TYPES

    agent, store = _make_agent(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_1",
            session_id="sess_test",
            type="assistant_message",
            payload={
                "message_id": "msg_1",
                "parts": [
                    {
                        "kind": "tool_call",
                        "metadata": {
                            "tool_call_id": "call_1",
                            "tool_name": "file_system_copy_123_get_current_directory",
                            "arguments": '{"path": "."}',
                        },
                    }
                ],
            },
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_2",
            session_id="sess_test",
            type="compaction_completed",
            payload={
                "trigger": "auto",
                "target_tokens": 60,
                "status": "success",
                "reason": "l1",
                "before_tokens": 1000,
                "after_tokens": 400,
                "event": {
                    "levels_attempted": ["l1", "l2"],
                    "stopped_at": "l1",
                    "changed_parts": 2,
                    "level_metrics": {
                        "l1": {"before_tokens": 1000, "after_tokens": 600, "saved_tokens": 400, "changed_parts": 2},
                        "l2": {"before_tokens": 600, "after_tokens": 400, "saved_tokens": 200, "changed_parts": 0},
                    },
                },
            },
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_3",
            session_id="sess_test",
            type="llm_compaction_completed",
            payload={
                "trigger": "auto",
                "target_tokens": 60,
                "status": "success",
                "reason": "hard_truncate",
                "event": {
                    "status": "success",
                    "source_fingerprint": "fp",
                    "fallback_steps": [{"step": 1, "action": "hard_truncate", "status": "success"}],
                    "final_failure_reason": None,
                },
            },
        )
    )

    tool_calls, compaction_events = agent._drain_turn_events(0)

    assert [tc.function_name for tc in tool_calls] == ["file_system_get_current_directory"]
    assert len(compaction_events) == 2
    programmatic = compaction_events[0]
    assert programmatic["type"] == "compaction_completed"
    assert programmatic["before_tokens"] == 1000
    assert programmatic["after_tokens"] == 400
    assert programmatic["level_metrics"]["l1"]["changed_parts"] == 2
    l3 = compaction_events[1]
    assert l3["type"] == "llm_compaction_completed"
    assert l3["hard_truncate"] is True
    assert {e["type"] for e in compaction_events} <= set(COMPACTION_EVENT_TYPES)
    # 第二次 drain 不应重复采集
    assert agent._drain_turn_events(agent._last_event_count) == ([], [])
