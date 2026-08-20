from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RetryAction = Literal["stronger_compaction", "backoff", "retry", "fail"]


@dataclass(frozen=True, slots=True)
class CompactRetryDecision:
    should_retry: bool
    action: RetryAction
    reason: str


@dataclass(frozen=True, slots=True)
class CompactRetryPolicy:

    max_prompt_too_long_retries: int = 1
    max_timeout_retries: int = 2
    max_no_summary_retries: int = 1

    def decide(self, reason: str, *, attempt: int) -> CompactRetryDecision:
        if reason == "prompt_too_long":
            return CompactRetryDecision(
                should_retry=attempt <= self.max_prompt_too_long_retries,
                action="stronger_compaction" if attempt <= self.max_prompt_too_long_retries else "fail",
                reason=reason,
            )

        if reason == "timeout":
            return CompactRetryDecision(
                should_retry=attempt <= self.max_timeout_retries,
                action="backoff" if attempt <= self.max_timeout_retries else "fail",
                reason=reason,
            )

        if reason == "no_summary":
            return CompactRetryDecision(
                should_retry=attempt <= self.max_no_summary_retries,
                action="retry" if attempt <= self.max_no_summary_retries else "fail",
                reason=reason,
            )

        return CompactRetryDecision(should_retry=False, action="fail", reason=reason)
