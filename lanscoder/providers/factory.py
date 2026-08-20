"""Provider 工厂:按模型档案创建对应的 ChatProvider 实例。"""

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
    """Provider 配置错误。"""

    pass


def create_provider_for_model(profile: ModelProfile) -> ChatProvider:
    """按模型档案创建 provider,支持 openai-compatible/custom 与预置类型。"""

    provider_type = profile.provider.type
    if provider_type in {"openai-compatible", "custom"}:
        return _create_catalog_openai_compatible(profile)
    if provider_type in PROVIDER_PRESETS:
        return _create_catalog_preset(profile)
    raise ProviderConfigError(f"不支持的 provider 类型：{provider_type}")


def _catalog_api_key(profile: ModelProfile) -> str | None:
    return profile.provider.api_key


def _catalog_capabilities(base: ProviderCapabilities, profile: ModelProfile) -> ProviderCapabilities:
    """用档案的 provider 配置覆盖能力(并行/流式开关)。"""
    overrides = {}
    if profile.provider.parallel_tool_calls is not None:
        overrides["supports_parallel_tool_calls"] = profile.provider.parallel_tool_calls
    if profile.provider.streaming is not None:
        overrides["supports_streaming"] = profile.provider.streaming
    return replace(base, **overrides) if overrides else base


def _create_catalog_openai_compatible(profile: ModelProfile) -> ChatProvider:
    """构建 openai-compatible provider(先校验 api_key)。"""
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
    """按预置类型构建 provider(ollama 缺 key 时用占位)。"""
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
    """按 kind 实例化具体的 provider 类。"""
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
