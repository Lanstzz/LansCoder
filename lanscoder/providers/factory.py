"""provider 构造入口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from lanscoder.config.models import ModelProfile
from lanscoder.providers.anthropic_provider import AnthropicProvider
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.openai_compatible import OpenAICompatibleProvider
from lanscoder.providers.presets import PROVIDER_PRESETS
from lanscoder.providers.types import ProviderCapabilities


class ProviderConfigError(ValueError):
    """provider 配置缺失或不合法时抛出的异常。"""


def create_provider_for_model(profile: ModelProfile) -> ChatProvider:
    """根据完整的模型 Profile 创建 provider。

    Profile 中的模型 ID、provider ID、endpoint 和能力覆盖属于当前模型选择。
    """

    provider_type = profile.provider.type
    if provider_type in {"openai-compatible", "custom"}:
        return _create_catalog_openai_compatible(profile)
    if provider_type in PROVIDER_PRESETS:
        return _create_catalog_preset(profile)
    raise ProviderConfigError(f"不支持的 provider 类型：{provider_type}")


def _catalog_api_key(profile: ModelProfile) -> str | None:
    return profile.provider.api_key


def _catalog_capabilities(base: ProviderCapabilities, profile: ModelProfile) -> ProviderCapabilities:
    overrides = {}
    if profile.provider.parallel_tool_calls is not None:
        overrides["supports_parallel_tool_calls"] = profile.provider.parallel_tool_calls
    if profile.provider.streaming is not None:
        overrides["supports_streaming"] = profile.provider.streaming
    return replace(base, **overrides) if overrides else base


def _create_catalog_openai_compatible(profile: ModelProfile) -> ChatProvider:
    api_key = _catalog_api_key(profile)
    if not api_key:
        raise ProviderConfigError(f"provider {profile.provider.id} 缺少 api_key，请在配置文件中设置")
    capabilities = _catalog_capabilities(ProviderCapabilities(supports_streaming=True), profile)
    return OpenAICompatibleProvider(
        name=profile.provider.id,
        model=profile.model_id,
        api_key=api_key,
        base_url=profile.provider.base_url,
        capabilities=capabilities,
    )


def _create_catalog_preset(profile: ModelProfile) -> ChatProvider:
    preset = PROVIDER_PRESETS[profile.provider.type]
    api_key = _catalog_api_key(profile)
    if not api_key and preset.name == "ollama":
        api_key = "ollama"
    if not api_key:
        raise ProviderConfigError(f"provider {profile.provider.id} 缺少 api_key，请在配置文件中设置")
    base_url = profile.provider.base_url or preset.default_base_url
    capabilities = _catalog_capabilities(preset.capabilities, profile)
    return _create_provider_instance(
        kind=preset.kind,
        name=profile.provider.id,
        model=profile.model_id,
        api_key=api_key,
        base_url=base_url,
        capabilities=capabilities,
        extra_headers=preset.extra_headers,
        extra_body=preset.extra_body,
    )


def _create_provider_instance(
    *,
    kind: str,
    name: str,
    model: str,
    api_key: str,
    base_url: str | None,
    capabilities: ProviderCapabilities,
    extra_headers: Mapping[str, str] | None = None,
    extra_body: Mapping[str, Any] | None = None,
) -> ChatProvider:
    provider_class = {
        "openai-compatible": OpenAICompatibleProvider,
        "anthropic": AnthropicProvider,
    }.get(kind)
    if provider_class is None:
        raise ProviderConfigError(f"provider 类型未实现：{kind}")
    return provider_class(
        name=name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        capabilities=capabilities,
        extra_headers=dict(extra_headers or {}),
        extra_body=dict(extra_body or {}),
    )
