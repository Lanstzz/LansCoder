from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

FallbackAction = Literal["stronger_programmatic", "retry_l3_stronger_summary", "hard_truncate"]


@dataclass(frozen=True, slots=True)
class FallbackStep:

    step: int
    reason: str
    action: FallbackAction
    before_tokens: int
    after_tokens: int
    status: Literal["success", "failed", "skipped"]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CompactFallbackPolicy:

    def action_for(self, reason: str | None) -> FallbackAction:
        if reason == "prompt_too_long":
            return "stronger_programmatic"
        if reason in {"timeout", "no_summary"}:
            return "retry_l3_stronger_summary"
        return "hard_truncate"
