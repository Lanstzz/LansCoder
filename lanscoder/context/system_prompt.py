from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from lanscoder.context.identity import content_fingerprint, stable_json_hash
from lanscoder.context.versions import SYSTEM_PROMPT_VERSION
from lanscoder.providers.types import ChatMessage

MEMORY_PROTOCOL = (
    "Persistent memory is available as named files. The index below lists project-level "
    "and user-level memories. Read a full memory with read_memory before acting on it. "
    "Use remember to save durable facts: project scope for repo-specific facts, user scope "
    "for cross-project preferences."
)


@dataclass(frozen=True, slots=True)
class SystemPromptInputs:

    base_rules: str
    agents_md: str
    provider_name: str
    provider_capabilities: dict[str, Any]
    permission_policy: dict[str, Any]
    skill_protocol: str = ""
    skill_catalog_summary: str = ""
    benchmark_task: str = ""
    mode: str = "default"
    memory_index: str = ""
    prompt_version: str = SYSTEM_PROMPT_VERSION


@dataclass(frozen=True, slots=True)
class PromptPrefixCacheEntry:
    fingerprint: str
    messages: list[ChatMessage]


class SystemPromptBuilder:

    def fingerprint(self, inputs: SystemPromptInputs) -> str:
        value = {
            "prompt_version": inputs.prompt_version,
            "base_rules_hash": content_fingerprint(inputs.base_rules),
            "agents_md_hash": content_fingerprint(inputs.agents_md),
            "skill_protocol_hash": content_fingerprint(inputs.skill_protocol),
            "skill_catalog_summary_hash": content_fingerprint(inputs.skill_catalog_summary),
            "benchmark_mode": bool(inputs.benchmark_task.strip()),
            "provider_name": inputs.provider_name,
            "provider_capabilities": inputs.provider_capabilities,
            "permission_policy": inputs.permission_policy,
            "mode": inputs.mode,
            "memory_index_hash": content_fingerprint(inputs.memory_index),
        }
        return stable_json_hash(value)

    def build(self, inputs: SystemPromptInputs) -> PromptPrefixCacheEntry:
        fingerprint = self.fingerprint(inputs)
        content = "\n\n".join(
            section
            for section in [
                inputs.base_rules.strip(),
                _agent_instructions(inputs.benchmark_task),
                _format_section("Project instructions", inputs.agents_md),
                _format_section("Project skill protocol", inputs.skill_protocol),
                _format_section("Available skills", inputs.skill_catalog_summary),
                _format_section("Provider", _format_provider(inputs)),
                _format_section("Permission policy", _format_json(inputs.permission_policy)),
                _format_memory_section(inputs),
            ]
            if section
        )
        message = ChatMessage(role="system", content=content)
        return PromptPrefixCacheEntry(
            fingerprint=fingerprint,
            messages=[message],
        )


class PromptPrefixCache:

    def __init__(self) -> None:
        self._entry: PromptPrefixCacheEntry | None = None

    def get_or_build(
        self,
        inputs: SystemPromptInputs,
        builder: SystemPromptBuilder | None = None,
    ) -> PromptPrefixCacheEntry:
        builder = builder or SystemPromptBuilder()
        fingerprint = builder.fingerprint(inputs)
        if self._entry is not None and self._entry.fingerprint == fingerprint:
            return self._entry

        self._entry = builder.build(inputs)
        return self._entry

    @property
    def entry(self) -> PromptPrefixCacheEntry | None:
        return self._entry


def _format_section(title: str, content: str) -> str:
    content = content.strip()
    if not content:
        return ""
    return f"{title}:\n{content}"


def _format_memory_section(inputs: SystemPromptInputs) -> str:
    index = inputs.memory_index.strip()
    if not index:
        return ""
    return f"Memory:\n{MEMORY_PROTOCOL}\n\n{index}"


def _agent_instructions(benchmark_task: str = "") -> str:
    filename = "benchmark_agent_instructions.md" if benchmark_task.strip() else "agent_instructions.md"
    path = Path(__file__).with_name("prompts") / filename
    content = path.read_text(encoding="utf-8").strip()
    return content


def _format_provider(inputs: SystemPromptInputs) -> str:
    return "\n".join(
        [
            f"name={inputs.provider_name}",
            f"capabilities={_format_json(inputs.provider_capabilities)}",
            f"mode={inputs.mode}",
            f"prompt_version={inputs.prompt_version}",
        ]
    )


def _format_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
