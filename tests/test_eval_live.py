from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_harness.canary.runner import load_canary_config, run_canary
from eval_harness.live import resolve_model_profile, run_fresh_model_case_path
from lanscoder.providers.base import ChatProvider
from lanscoder.providers.types import ChatRequest, ChatResponse, ProviderCapabilities, TokenUsage


class FakeLiveProvider(ChatProvider):
    @property
    def name(self) -> str:
        return "fake-live"

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_streaming=False)

    def complete(self, request: ChatRequest) -> ChatResponse:
        del request
        return ChatResponse(
            provider=self.name,
            model=self.model,
            content="Live model canary completed.",
            finish_reason="stop",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )


def _write_case(path: Path, *, identifier: str = "live-case") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": identifier,
                "title": "Direct live provider case",
                "mode": "fresh_model",
                "prompt": "Say that the live canary completed.",
                "fixture": None,
                "provider_tape": [],
                "expected_artifacts": {},
                "expected_delivery_contains": "Live model canary completed.",
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_fresh_model_case_records_live_provider_and_usage(tmp_path: Path) -> None:
    case_path = tmp_path / "case.json"
    _write_case(case_path)

    result = await run_fresh_model_case_path(case_path, tmp_path / "run", provider=FakeLiveProvider())

    assert result.scorecard["passed"] is True
    assert result.scorecard["metrics"]["token_usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    trace = result.trace_path.read_text(encoding="utf-8")
    assert '"network_policy":"enabled"' in trace
    assert '"provider":"fake-live"' in trace
    assert '"model":"fake-model"' in trace


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_canary_runs_repetitions_without_harbor(tmp_path: Path) -> None:
    case_path = tmp_path / "case.json"
    _write_case(case_path)
    config_path = tmp_path / "canary.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "test-canary",
                "cases": ["case.json"],
                "repetitions": 2,
            }
        ),
        encoding="utf-8",
    )

    result = await run_canary(config_path, tmp_path / "output", provider=FakeLiveProvider(), model_ref="fake/fake-model")

    assert result.summary["passed"] is True
    assert result.summary["model"] == "fake/fake-model"
    assert result.summary["run_count"] == 2
    assert (tmp_path / "output" / "runs" / "case" / "repeat-01" / "trace.jsonl").is_file()
    assert (tmp_path / "output" / "runs" / "case" / "repeat-02" / "scorecard.json").is_file()


def test_canary_config_rejects_parent_case_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "canary.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "id": "bad", "cases": ["../case.json"]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="safe relative paths"):
        load_canary_config(config_path)


def test_live_profile_resolves_api_key_from_declared_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "lanscoder.toml").write_text(
        "\n".join(
            [
                'default_model = "custom/model"',
                "[providers.custom]",
                'type = "openai-compatible"',
                'api_key_env = "EVAL_TEST_API_KEY"',
                'base_url = "https://example.test/v1"',
                '[models."custom/model"]',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EVAL_TEST_API_KEY", "test-key")

    profile = resolve_model_profile(tmp_path)

    assert profile.ref == "custom/model"
    assert profile.provider.api_key == "test-key"
