"""A small, explicit batch runner for direct ``fresh_model`` canaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval_harness.live import run_fresh_model_case_path


CANARY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CanaryConfig:
    schema_version: int
    identifier: str
    cases: tuple[str, ...]
    repetitions: int = 1
    model: str | None = None
    max_tool_rounds: int = 120
    max_provider_calls: int = 120
    max_turn_seconds: float = 3600


@dataclass(frozen=True, slots=True)
class CanaryResult:
    summary_path: Path
    summary: dict[str, Any]


def load_canary_config(path: str | Path) -> CanaryConfig:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read canary config {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("canary config must be a JSON object")
    if raw.get("schema_version") != CANARY_SCHEMA_VERSION:
        raise ValueError(f"unsupported canary schema_version; expected {CANARY_SCHEMA_VERSION}")
    identifier = _required_string(raw, "id")
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases or not all(_safe_relative_path(item) for item in cases):
        raise ValueError("canary cases must be a non-empty list of safe relative paths")
    repetitions = _positive_int(raw.get("repetitions", 1), "repetitions")
    if repetitions > 100:
        raise ValueError("repetitions cannot exceed 100")
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("model must be a non-blank string or null")
    max_tool_rounds = _positive_int(raw.get("max_tool_rounds", 120), "max_tool_rounds")
    max_provider_calls = _positive_int(raw.get("max_provider_calls", 120), "max_provider_calls")
    max_turn_seconds = raw.get("max_turn_seconds", 3600)
    if isinstance(max_turn_seconds, bool) or not isinstance(max_turn_seconds, (int, float)) or max_turn_seconds <= 0:
        raise ValueError("max_turn_seconds must be positive")
    return CanaryConfig(
        schema_version=CANARY_SCHEMA_VERSION,
        identifier=identifier,
        cases=tuple(cases),
        repetitions=repetitions,
        model=model,
        max_tool_rounds=max_tool_rounds,
        max_provider_calls=max_provider_calls,
        max_turn_seconds=max_turn_seconds,
    )


async def run_canary(
    config_path: str | Path,
    output_dir: str | Path,
    *,
    project_root: str | Path = ".",
    model_ref: str | None = None,
    provider: Any | None = None,
) -> CanaryResult:
    config_source = Path(config_path).resolve()
    config = load_canary_config(config_source)
    root = Path(output_dir)
    if root.exists():
        raise FileExistsError(f"canary output directory already exists: {root}")
    root.mkdir(parents=True)
    selected_model = model_ref or config.model
    if selected_model is None and provider is None:
        from eval_harness.live import resolve_model_profile

        selected_model = resolve_model_profile(project_root).ref
    runs: list[dict[str, Any]] = []
    for case_name in config.cases:
        case_path = (config_source.parent / case_name).resolve()
        if not case_path.is_file():
            raise ValueError(f"canary case does not exist: {case_path}")
        case_id = case_path.stem
        for repetition in range(1, config.repetitions + 1):
            run_dir = root / "runs" / case_id / f"repeat-{repetition:02d}"
            result = await run_fresh_model_case_path(
                case_path,
                run_dir,
                project_root=project_root,
                model_ref=selected_model,
                provider=provider,
                max_tool_rounds=config.max_tool_rounds,
                max_provider_calls=config.max_provider_calls,
                max_turn_seconds=config.max_turn_seconds,
            )
            runs.append(
                {
                    "case": case_id,
                    "repetition": repetition,
                    "passed": bool(result.scorecard.get("passed")),
                    "scorecard": str(result.scorecard_path),
                    "trace": str(result.trace_path),
                }
            )
    summary = {
        "schema_version": CANARY_SCHEMA_VERSION,
        "id": config.identifier,
        "model": selected_model,
        "repetitions": config.repetitions,
        "run_count": len(runs),
        "passed": bool(runs) and all(item["passed"] for item in runs),
        "runs": runs,
    }
    summary_path = root / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return CanaryResult(summary_path=summary_path, summary=summary)


def run_canary_sync(config_path: str | Path, output_dir: str | Path, **kwargs: Any) -> CanaryResult:
    return asyncio.run(run_canary(config_path, output_dir, **kwargs))


def _required_string(raw: dict[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"canary config {name} must be a non-blank string")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return False
    return ".." not in Path(value).parts


__all__ = ["CanaryConfig", "CanaryResult", "load_canary_config", "run_canary", "run_canary_sync"]
