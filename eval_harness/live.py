"""Direct real-provider execution for ``fresh_model`` cases."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.config import load_config
from lanscoder.config.models import ModelProfile
from lanscoder.providers import create_provider_for_model
from lanscoder.providers.factory import ProviderConfigError
from lanscoder.providers.presets import PROVIDER_PRESETS
from lanscoder.providers.types import MainRequestOptions

from eval_harness.replay.provider import RecordingProvider
from eval_harness.replay.runner import RunResult, _run_case_with_provider
from eval_harness.schema.models import CaseManifest, load_case_manifest


def resolve_model_profile(project_root: str | Path, model_ref: str | None = None) -> ModelProfile:
    """Resolve a configured model without exposing its API key to the harness."""

    config = load_config(project_root=project_root)
    catalog = config.model_catalog()
    selected_ref = model_ref or catalog.default_ref or (catalog.profiles[0].ref if catalog.profiles else None)
    if selected_ref is None:
        raise ProviderConfigError("model catalog is empty; configure a model before running fresh_model")
    profile = catalog.require(selected_ref)
    provider = profile.provider
    api_key_env = provider.api_key_env
    if api_key_env is None and provider.type in PROVIDER_PRESETS:
        api_key_env = PROVIDER_PRESETS[provider.type].api_key_env
    api_key = provider.api_key or (os.environ.get(api_key_env) if api_key_env else None)
    if not api_key:
        source = api_key_env or f"providers.{provider.id}.api_key"
        raise ProviderConfigError(f"provider {provider.id} is missing an API key; configure {source}")
    return replace(profile, provider=replace(provider, api_key=api_key))


async def run_fresh_model_case_path(
    case_path: str | Path,
    output_dir: str | Path,
    *,
    project_root: str | Path = ".",
    model_ref: str | None = None,
    provider: Any | None = None,
    baseline_scorecard: dict[str, Any] | None = None,
    max_tool_rounds: int = 120,
    max_provider_calls: int = 120,
    max_turn_seconds: float = 3600,
) -> RunResult:
    """Run a ``fresh_model`` case through a configured real provider."""

    resolved_case_path = Path(case_path)
    manifest = load_case_manifest(resolved_case_path)
    if manifest.mode != "fresh_model":
        raise ValueError("fresh model runner supports only fresh_model cases")
    profile = None if provider is not None else resolve_model_profile(project_root, model_ref)
    selected_provider = provider or create_provider_for_model(profile)
    request_options = _request_options(manifest, profile)
    context_window = manifest.context_window or (profile.context_window if profile is not None else None)
    limits = AgentLoopLimits(
        max_tool_rounds=_positive_number(max_tool_rounds, "max_tool_rounds"),
        max_provider_calls=_positive_number(max_provider_calls, "max_provider_calls"),
        max_turn_seconds=_positive_number(max_turn_seconds, "max_turn_seconds"),
    )
    return await _run_case_with_provider(
        manifest,
        output_dir,
        case_path=resolved_case_path,
        baseline_scorecard=baseline_scorecard,
        provider_factory=lambda recorder: RecordingProvider(selected_provider, on_interaction=recorder.record),
        network_policy="enabled",
        limits=limits,
        request_options=request_options,
        context_window=context_window,
        provider_ref=model_ref or (profile.ref if profile is not None else None),
    )


def run_fresh_model_case_sync(
    case_path: str | Path,
    output_dir: str | Path,
    **kwargs: Any,
) -> RunResult:
    """Synchronous convenience wrapper for one direct live-model run."""

    import asyncio

    return asyncio.run(run_fresh_model_case_path(case_path, output_dir, **kwargs))


def _request_options(manifest: CaseManifest, profile: ModelProfile | None) -> MainRequestOptions:
    if profile is None:
        return MainRequestOptions(max_tokens=manifest.max_output_tokens)
    return MainRequestOptions(
        temperature=profile.request.temperature,
        max_tokens=manifest.max_output_tokens or profile.request.max_tokens,
        extra_body=profile.request.extra_body,
    )


def _positive_number(value: int | float, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


__all__ = [
    "resolve_model_profile",
    "run_fresh_model_case_path",
    "run_fresh_model_case_sync",
]
