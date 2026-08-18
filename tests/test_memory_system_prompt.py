from lanscoder.agent.prompt_inputs import build_system_prompt_inputs
from lanscoder.context.system_prompt import SystemPromptBuilder, SystemPromptInputs


def _inputs(**overrides: object) -> SystemPromptInputs:
    values = {
        "base_rules": "你是 LansCoder。",
        "agents_md": "",
        "provider_name": "openai-compatible",
        "provider_capabilities": {"tool_calling": True, "parallel_tool_calls": False},
        "permission_policy": {},
        "mode": "default",
    }
    values.update(overrides)
    return SystemPromptInputs(**values)


def test_memory_index_change_invalidates_fingerprint() -> None:
    builder = SystemPromptBuilder()
    before = builder.fingerprint(_inputs(memory_index="Project memory:\n- [a](a.md) — aaa"))
    after = builder.fingerprint(_inputs(memory_index="Project memory:\n- [b](b.md) — bbb"))
    assert before != after


def test_memory_index_stable_for_same_input() -> None:
    builder = SystemPromptBuilder()
    inputs = _inputs(memory_index="Project memory:\n- [a](a.md) — aaa")
    assert builder.fingerprint(inputs) == builder.fingerprint(inputs)


def test_empty_memory_index_omits_section() -> None:
    entry = SystemPromptBuilder().build(_inputs())
    assert "Memory:" not in entry.messages[0].content


def test_memory_section_rendered_in_prefix() -> None:
    builder = SystemPromptBuilder()
    entry = builder.build(_inputs(memory_index="Project memory:\n- [build-commands](build-commands.md) — How to build"))
    content = entry.messages[0].content
    assert "Memory:" in content
    assert "build-commands" in content
    assert "read_memory" in content  # 协议文本提示模型按需读全文


def test_build_system_prompt_inputs_passes_memory_index() -> None:
    inputs = build_system_prompt_inputs(
        base_rules="r",
        agents_md="",
        memory_index="User memory:\n- [x](x.md) — xx",
        provider_name="openai-compatible",
    )
    assert inputs.memory_index == "User memory:\n- [x](x.md) — xx"
