"""Step 3 (P1) SC-6: ``lanscoder.core`` 契约固化。

- ``core.__all__`` 精确钉死:增删公开名必须同步改本测试。
- 关键签名以结构快照(参数名/顺序/kind/必填/默认值)钉死。
- ``__version__`` 暴露且跟随包版本(``pyproject.toml`` 的 ``[project].version``)。
- ``py.typed`` marker 存在(PEP 561,SDK 类型面对外可用)。
"""

from __future__ import annotations

import dataclasses
import inspect
import tomllib
from pathlib import Path
from typing import get_args

import lanscoder.core as core
from lanscoder.core import (
    Agent,
    AgentSessionHandle,
    LlmTransport,
    LoopConfig,
    LoopContext,
    LoopMessage,
    agent_loop,
    convert_to_llm,
    create_agent_session,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_ALL = [
    "__version__",
    "Agent",
    "AgentEndEvent",
    "AgentEvent",
    "AgentSessionHandle",
    "AgentStartEvent",
    "LoopConfig",
    "LoopContext",
    "LlmTransport",
    "LoopMessage",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "ToolExecutionEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "TurnEndEvent",
    "TurnStartEvent",
    "agent_loop",
    "convert_to_llm",
    "create_agent_session",
]

EVENT_NAMES = [
    "AgentStartEvent",
    "AgentEndEvent",
    "TurnStartEvent",
    "TurnEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "MessageEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "ToolExecutionEndEvent",
]


def _param_snapshot(func) -> tuple[tuple[str, str, bool, object], ...]:
    """参数结构快照:(name, kind, required, default) 元组序列。"""

    return tuple(
        (
            name,
            str(parameter.kind),
            parameter.default is inspect.Parameter.empty,
            "<required>" if parameter.default is inspect.Parameter.empty else parameter.default,
        )
        for name, parameter in inspect.signature(func).parameters.items()
    )


def _fields_snapshot(cls) -> tuple[tuple[str, object, bool], ...]:
    """dataclass 字段快照:(name, default, has_default_factory)。"""

    return tuple(
        (
            field.name,
            "<required>" if field.default is dataclasses.MISSING else field.default,
            field.default_factory is not dataclasses.MISSING,
        )
        for field in dataclasses.fields(cls)
    )


def test_py_typed_marker_exists() -> None:
    assert (PROJECT_ROOT / "lanscoder" / "py.typed").is_file()


def test_core_all_is_pinned() -> None:
    assert core.__all__ == EXPECTED_ALL


def test_every_export_resolves() -> None:
    for name in EXPECTED_ALL:
        assert getattr(core, name) is not None


def test_version_follows_package_version() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert core.__version__ == pyproject["project"]["version"]


def test_version_is_semver_shaped() -> None:
    parts = core.__version__.split(".")
    assert len(parts) >= 2
    assert all(part.isdigit() for part in parts[:2])


def test_core_requires_anyio_version_with_to_thread_support() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "packages" / "lanscoder-core" / "pyproject.toml").read_text(encoding="utf-8"))
    assert "anyio>=4,<4.15" in pyproject["project"]["dependencies"]


def test_agent_loop_signature_pinned() -> None:
    assert _param_snapshot(agent_loop) == (
        ("prompts", "POSITIONAL_OR_KEYWORD", True, "<required>"),
        ("context", "POSITIONAL_OR_KEYWORD", True, "<required>"),
        ("config", "POSITIONAL_OR_KEYWORD", True, "<required>"),
        ("signal", "POSITIONAL_OR_KEYWORD", False, None),
    )


def test_create_agent_session_signature_pinned() -> None:
    assert _param_snapshot(create_agent_session) == (
        ("provider", "KEYWORD_ONLY", True, "<required>"),
        ("project_root", "KEYWORD_ONLY", True, "<required>"),
        ("data_root", "KEYWORD_ONLY", False, None),
        ("tools", "KEYWORD_ONLY", False, None),
        ("session_id", "KEYWORD_ONLY", False, None),
        ("resume", "KEYWORD_ONLY", False, False),
        ("limits", "KEYWORD_ONLY", False, None),
        ("request_options", "KEYWORD_ONLY", False, None),
        ("context_window", "KEYWORD_ONLY", False, None),
        ("background_manager", "KEYWORD_ONLY", False, None),
        ("user_memory_root", "KEYWORD_ONLY", False, None),
        ("compaction_strategy", "KEYWORD_ONLY", False, "l1_l2_l3"),
    )


def test_convert_to_llm_signature_pinned() -> None:
    assert _param_snapshot(convert_to_llm) == (
        ("message", "POSITIONAL_OR_KEYWORD", True, "<required>"),
    )


def test_agent_public_methods_pinned() -> None:
    assert _param_snapshot(Agent.subscribe) == (
        ("self", "POSITIONAL_OR_KEYWORD", True, "<required>"),
        ("listener", "POSITIONAL_OR_KEYWORD", True, "<required>"),
    )
    assert _param_snapshot(Agent.prompt) == (
        ("self", "POSITIONAL_OR_KEYWORD", True, "<required>"),
        ("input_", "POSITIONAL_OR_KEYWORD", True, "<required>"),
    )
    assert _param_snapshot(Agent.steer) == (
        ("self", "POSITIONAL_OR_KEYWORD", True, "<required>"),
        ("message", "POSITIONAL_OR_KEYWORD", True, "<required>"),
    )
    assert _param_snapshot(Agent.follow_up) == (
        ("self", "POSITIONAL_OR_KEYWORD", True, "<required>"),
        ("message", "POSITIONAL_OR_KEYWORD", True, "<required>"),
    )
    assert _param_snapshot(Agent.abort) == (
        ("self", "POSITIONAL_OR_KEYWORD", True, "<required>"),
    )


def test_loop_config_fields_pinned() -> None:
    assert _fields_snapshot(LoopConfig) == (
        ("provider", "<required>", False),
        ("session_id", "", False),
        ("use_streaming", None, False),
        ("request_options", None, False),
        ("context_window", None, False),
        ("limits", None, False),
        ("background_manager", None, False),
        ("guidance_provider", None, False),
    )


def test_loop_context_fields_pinned() -> None:
    assert _fields_snapshot(LoopContext) == (
        ("system_prompt", "", False),
        ("messages", "<required>", True),
        ("tools", "<required>", True),
    )


def test_loop_message_fields_pinned() -> None:
    assert _fields_snapshot(LoopMessage) == (
        ("role", "<required>", False),
        ("content", "<required>", False),
        ("name", None, False),
        ("tool_call_id", None, False),
        ("tool_calls", "<required>", True),
        ("metadata", "<required>", True),
    )


def test_agent_session_handle_fields_pinned() -> None:
    """D2: handle 只留 session + runner,契约测试把该形态钉死。"""

    assert _fields_snapshot(AgentSessionHandle) == (
        ("session", "<required>", False),
        ("runner", "<required>", False),
    )


def test_llm_transport_protocol_pinned() -> None:
    """D3: 2 方法 + 3 属性,复用 providers 叶子类型。"""

    assert set(LlmTransport.__annotations__) == {"name", "model", "capabilities"}
    assert LlmTransport.__annotations__["name"] == "str"
    assert LlmTransport.__annotations__["model"] == "str"
    assert callable(getattr(LlmTransport, "complete"))
    assert callable(getattr(LlmTransport, "astream"))


def test_agent_events_are_frozen_slots_dataclasses() -> None:
    for name in EVENT_NAMES:
        cls = getattr(core, name)
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen is True
        # slots=True 自 3.10 可用,但 ``__dataclass_params__.slots`` 属性 3.13+ 才有;
        # 用 ``__slots__`` 的存在性做跨 3.11/3.12/3.13 一致的断言。
        assert hasattr(cls, "__slots__")


def test_agent_event_union_pinned() -> None:
    assert get_args(core.AgentEvent) == tuple(getattr(core, name) for name in EVENT_NAMES)
