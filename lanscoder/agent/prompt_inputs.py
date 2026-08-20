from __future__ import annotations

from pathlib import Path
from typing import Any

from lanscoder.context.system_prompt import SystemPromptInputs
from lanscoder.providers.types import ProviderCapabilities

DEFAULT_PERMISSION_POLICY: dict[str, Any] = {
    "path_access": "project_root_only",
    "read": "allow",
    "write": "confirm",
    "delete": "confirm",
    "shell": "confirm",
    "network": "confirm",
    "mcp_tools": "confirm",
    "env_secrets": "redact",
}


def read_agents_md(project_root: str | Path) -> str:

    agents_path = Path(project_root) / "AGENTS.md"
    if not agents_path.exists():
        return ""
    return agents_path.read_text(encoding="utf-8")


def provider_capabilities_for(provider_name: str, *, provider_model: str = "") -> dict[str, Any]:

    normalized = provider_name.lower()
    base: dict[str, Any] = {
        "tool_calling": True,
        "parallel_tool_calls": False,
        "system_prompt": "message",
        "tool_result_role": "tool",
    }
    if normalized == "anthropic":
        base.update(
            {
                "system_prompt": "separate_field",
                "tool_schema": "anthropic_messages",
                "tool_result_role": "user_tool_result_block",
            }
        )
    else:
        base.update({"tool_schema": "openai_compatible_tools"})

    if provider_model:
        base["model"] = provider_model
    return base


def provider_capabilities_from_instance(
    capabilities: ProviderCapabilities | None,
    *,
    provider_name: str,
    provider_model: str = "",
) -> dict[str, Any]:

    base = provider_capabilities_for(provider_name, provider_model=provider_model)
    if capabilities is None:
        return base
    base.update(
        {
            "tool_calling": capabilities.supports_tools,
            "parallel_tool_calls": capabilities.supports_parallel_tool_calls,
            "streaming": capabilities.supports_streaming,
            "json_mode": capabilities.supports_json_mode,
            "vision": capabilities.supports_vision,
            "reasoning": capabilities.supports_reasoning,
            "token_param": capabilities.token_param,
        }
    )
    return base


def build_system_prompt_inputs(
    *,
    base_rules: str,
    agents_md: str,
    skill_protocol: str = "",
    skill_catalog_summary: str = "",
    benchmark_task: str = "",
    provider_name: str,
    provider_model: str = "",
    provider_capabilities: ProviderCapabilities | None = None,
    provider_capability_overrides: dict[str, Any] | None = None,
    permission_policy: dict[str, Any] | None = None,
    mode: str = "default",
    memory_index: str = "",
) -> SystemPromptInputs:

    capabilities = provider_capabilities_from_instance(
        provider_capabilities,
        provider_name=provider_name,
        provider_model=provider_model,
    )
    capabilities.update(provider_capability_overrides or {})

    resolved_permission_policy = dict(DEFAULT_PERMISSION_POLICY)
    resolved_permission_policy.update(permission_policy or {})

    return SystemPromptInputs(
        base_rules=base_rules,
        agents_md=agents_md,
        skill_protocol=skill_protocol,
        skill_catalog_summary=skill_catalog_summary,
        benchmark_task=benchmark_task,
        provider_name=provider_name,
        provider_capabilities=capabilities,
        permission_policy=resolved_permission_policy,
        mode=mode,
        memory_index=memory_index,
    )
