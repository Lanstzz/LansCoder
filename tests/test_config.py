"""配置加载和 provider factory 的基础测试。"""

from __future__ import annotations

import pytest

from lanscoder.config import AppConfig, load_config
from lanscoder.config.models import ModelCatalogError
from lanscoder.config.settings import default_global_config_path, render_default_config
from lanscoder.providers.anthropic_provider import AnthropicProvider
from lanscoder.providers.factory import (
    ProviderConfigError,
    create_provider_for_model,
)
from lanscoder.providers.openai_compatible import OpenAICompatibleProvider
from lanscoder.providers.presets import PROVIDER_PRESETS


def test_load_config_has_no_implicit_provider_without_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    config = load_config(env={})

    assert config.model_catalog().profiles == ()


def test_load_config_reads_project_lanscoder_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("LANSCODER_PROVIDER", raising=False)
    (tmp_path / "lanscoder.toml").write_text(
        "\n".join(
            [
                'default_model = "custom/custom-model"',
                "[providers.custom]",
                'type = "openai-compatible"',
                'base_url = "https://example.com/v1"',
                'api_key = "test-key"',
                '[models."custom/custom-model"]',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(project_root=tmp_path)

    assert config.model_catalog().require("custom/custom-model").provider.id == "custom"
    assert config.get_config_value("default_model") == "custom/custom-model"
    assert config.model_catalog().require("custom/custom-model").provider.base_url == "https://example.com/v1"
    assert config.project_config_path == tmp_path / "lanscoder.toml"


def test_legacy_environment_provider_does_not_override_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("LANSCODER_PROVIDER", "deepseek")
    (tmp_path / "lanscoder.toml").write_text(
        "\n".join(['default_model = "custom/model"', "[providers.custom]", 'type = "openai-compatible"', '[models."custom/model"]']),
        encoding="utf-8",
    )

    config = load_config(project_root=tmp_path)

    assert config.model_catalog().default_ref == "custom/model"


def test_render_default_config_uses_api_key_not_plain_secret():
    content = render_default_config()

    assert "api_key " in content
    assert "api_key_env" not in content
    assert "parallel_tool_calls = true" in content


def test_model_catalog_reads_context_window() -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {"custom": {"type": "openai-compatible"}},
            "models": {
                "custom/model": {
                    "context_window": 128_000,
                    "request": {"max_tokens": 8_192},
                }
            },
        },
    )

    profile = config.model_catalog().require("custom/model")

    assert profile.context_window == 128_000
    assert profile.request.max_tokens == 8_192


@pytest.mark.parametrize("value", [0, -1, True, "128000"])
def test_model_catalog_rejects_invalid_context_window(value) -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {"custom": {"type": "openai-compatible"}},
            "models": {"custom/model": {"context_window": value}},
        },
    )

    with pytest.raises(ModelCatalogError, match="context_window"):
        config.model_catalog()


def test_model_catalog_rejects_output_reserve_that_exhausts_window() -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {"custom": {"type": "openai-compatible"}},
            "models": {
                "custom/model": {
                    "context_window": 1_000,
                    "request": {"max_tokens": 950},
                }
            },
        },
    )

    with pytest.raises(ModelCatalogError, match="max_tokens.*context_window"):
        config.model_catalog()


def test_default_global_config_path_respects_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert default_global_config_path() == tmp_path / "lanscoder" / "config.toml"


def test_mcp_config_merges_servers_without_using_provider_accessors():
    config = AppConfig(
        env={},
        global_config={"mcp": {"global": {"type": "local", "command": ["global"]}}},
        project_config={"mcp": {"project": {"type": "remote", "url": "https://example.test/mcp"}}},
    )

    assert config.mcp_config() == {
        "global": {"type": "local", "command": ["global"]},
        "project": {"type": "remote", "url": "https://example.test/mcp"},
    }


def test_openai_compatible_presets_have_constructable_metadata():
    expected = {
        "openai",
        "deepseek",
        "qwen",
        "moonshot",
        "zhipu",
        "openrouter",
        "ollama",
    }

    for name in expected:
        preset = PROVIDER_PRESETS[name]
        assert preset.kind == "openai-compatible"
        assert preset.name == name
        assert preset.api_key_env
        assert preset.model_env
        assert preset.default_model
        assert preset.capabilities.supports_tools is True


def test_catalog_preset_builds_openrouter_provider_without_extra_headers():
    config = AppConfig(
        env={},
        project_config={
            "providers": {"openrouter": {"type": "openrouter", "api_key": "test-key"}},
            "models": {"openrouter/custom": {}},
        },
    )

    provider = create_provider_for_model(config.model_catalog().require("openrouter/custom"))

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert not provider.extra_headers


def test_model_catalog_deep_merges_global_and_project_entries() -> None:
    config = AppConfig(
        env={},
        global_config={
            "providers": {
                "yuren": {
                    "type": "openai-compatible",
                    "base_url": "https://global.example/v1",
                    "api_key_env": "YUREN_API_KEY",
                }
            },
            "models": {
                "yuren/gpt-main": {
                    "label": "Global label",
                    "request": {
                        "temperature": 0.2,
                        "extra_body": {"reasoning_effort": "medium", "reasoning_summary": "auto"},
                    },
                },
                "yuren/gpt-cheap": {},
            },
        },
        project_config={
            "default_model": "yuren/gpt-main",
            "providers": {"yuren": {"base_url": "https://project.example/v1"}},
            "models": {
                "yuren/gpt-main": {
                    "label": "Project label",
                    "request": {"max_tokens": 8192, "extra_body": {"reasoning_effort": "high"}},
                }
            },
        },
    )

    catalog = config.model_catalog()

    assert catalog.default_ref == "yuren/gpt-main"
    assert [item.ref for item in catalog.list()] == ["yuren/gpt-cheap", "yuren/gpt-main"]
    main = catalog.require("yuren/gpt-main")
    assert main.label == "Project label"
    assert main.provider.base_url == "https://project.example/v1"
    assert main.request.temperature == 0.2
    assert main.request.max_tokens == 8192
    assert main.request.extra_body == {"reasoning_effort": "high", "reasoning_summary": "auto"}


def test_model_catalog_rejects_model_without_declared_provider() -> None:
    config = AppConfig(env={}, project_config={"models": {"missing/model": {}}})

    with pytest.raises(ModelCatalogError, match="missing/model.*missing"):
        config.model_catalog()


def test_model_catalog_rejects_legacy_single_provider_config() -> None:
    config = AppConfig(
        env={"YUREN_API_KEY": "test-key"},
        project_config={
            "model": "yurenapi/gpt-legacy",
            "provider": {
                "type": "openai-compatible",
                "name": "yurenapi",
                "base_url": "https://example.test/v1",
                "api_key_env": "YUREN_API_KEY",
            },
        },
    )

    with pytest.raises(ModelCatalogError, match="旧的 model.*不受支持"):
        config.model_catalog()


def test_create_provider_for_model_uses_profile_provider_and_model_options() -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {
                "yuren": {
                    "type": "openai-compatible",
                    "base_url": "https://example.test/v1",
                    "api_key": "test-key",
                    "parallel_tool_calls": True,
                    "streaming": False,
                }
            },
            "models": {"yuren/gpt-test": {}},
        },
    )

    provider = create_provider_for_model(config.model_catalog().require("yuren/gpt-test"))

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "yuren"
    assert provider.model == "gpt-test"
    assert provider.base_url == "https://example.test/v1"
    assert provider.capabilities.supports_parallel_tool_calls is True
    assert provider.capabilities.supports_streaming is False


def test_create_provider_for_model_supports_anthropic_profile() -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {"claude": {"type": "anthropic", "api_key": "test-key"}},
            "models": {"claude/claude-test": {}},
        },
    )

    provider = create_provider_for_model(config.model_catalog().require("claude/claude-test"))

    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "claude"
    assert provider.model == "claude-test"


def test_create_provider_for_model_reports_missing_api_key() -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {
                "yuren": {
                    "type": "openai-compatible",
                }
            },
            "models": {"yuren/gpt-test": {}},
        },
    )

    with pytest.raises(ProviderConfigError, match="缺少 api_key"):
        create_provider_for_model(config.model_catalog().require("yuren/gpt-test"))


def test_create_provider_for_model_supports_preset_and_profile_model() -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {"openai": {"type": "openai", "api_key": "test-key"}},
            "models": {"openai/custom-gpt": {}},
        },
    )

    provider = create_provider_for_model(config.model_catalog().require("openai/custom-gpt"))

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "custom-gpt"


def test_create_provider_for_model_reports_missing_preset_api_key() -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {"openai": {"type": "openai"}},
            "models": {"openai/custom-gpt": {}},
        },
    )

    with pytest.raises(ProviderConfigError, match="缺少 api_key"):
        create_provider_for_model(config.model_catalog().require("openai/custom-gpt"))


def test_model_catalog_validates_request_options_and_reserved_extra_body() -> None:
    base = {"providers": {"p": {"type": "openai-compatible"}}, "models": {"p/m": {}}}
    config = AppConfig(env={}, project_config={**base, "models": {"p/m": {"request": {"max_tokens": 0}}}})
    with pytest.raises(ModelCatalogError, match="max_tokens"):
        config.model_catalog()

    config = AppConfig(
        env={},
        project_config={
            **base,
            "models": {"p/m": {"request": {"extra_body": {"messages": []}}}},
        },
    )
    with pytest.raises(ModelCatalogError, match="extra_body"):
        config.model_catalog()


@pytest.mark.parametrize("reasoning_effort", ["xhigh", "minimal"])
def test_model_catalog_passes_provider_specific_reasoning_effort_through(reasoning_effort: str) -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {"p": {"type": "openai-compatible"}},
            "models": {"p/m": {"request": {"reasoning_effort": reasoning_effort}}},
        },
    )

    request = config.model_catalog().require("p/m").request

    assert request.reasoning_effort == reasoning_effort
    assert request.extra_body == {"reasoning_effort": reasoning_effort}


@pytest.mark.parametrize("reasoning_effort", [123, "", "   "])
def test_model_catalog_rejects_non_string_or_blank_reasoning_effort(reasoning_effort: object) -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {"p": {"type": "openai-compatible"}},
            "models": {"p/m": {"request": {"reasoning_effort": reasoning_effort}}},
        },
    )

    with pytest.raises(ModelCatalogError, match="reasoning_effort.*非空字符串"):
        config.model_catalog()


def test_model_catalog_rejects_duplicate_reasoning_effort_in_extra_body() -> None:
    config = AppConfig(
        env={},
        project_config={
            "providers": {"p": {"type": "openai-compatible"}},
            "models": {
                "p/m": {
                    "request": {
                        "reasoning_effort": "xhigh",
                        "extra_body": {"reasoning_effort": "minimal"},
                    }
                }
            },
        },
    )

    with pytest.raises(ModelCatalogError, match="reasoning_effort.*冲突"):
        config.model_catalog()
