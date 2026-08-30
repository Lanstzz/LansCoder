from lanscoder.agent.loop_limits import AgentLoopLimits, AgentLoopStopReason


def test_default_limits_match_tui_goal_profile() -> None:
    limits = AgentLoopLimits.default()

    assert limits.max_tool_rounds == 200
    assert limits.max_provider_calls == 400
    assert limits.max_turn_seconds == 3600


def test_benchmark_limits_match_goal_profile() -> None:
    limits = AgentLoopLimits.benchmark()

    assert limits.max_tool_rounds == 120
    assert limits.max_provider_calls == 120
    assert limits.max_turn_seconds == 3600


def test_custom_max_tool_rounds_override() -> None:
    limits = AgentLoopLimits.default().with_max_tool_rounds(4)

    assert limits.max_tool_rounds == 4


def test_stop_reason_values_are_finish_reasons() -> None:
    assert AgentLoopStopReason.PROVIDER_CALL_LIMIT.value == "provider_call_limit"
    assert AgentLoopStopReason.TURN_TIMEOUT.value == "turn_timeout"
    assert AgentLoopStopReason.TOOL_ROUND_LIMIT.value == "tool_round_limit"
