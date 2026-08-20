from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta


def _utc_after(minutes: int) -> str:
    return (
        (datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=minutes))
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )


def _parse_utc_iso(value: str) -> datetime:

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("runtime timestamp must include a timezone")
    return parsed.astimezone(UTC)


def active_auto_compact_disabled_until(state: "SessionRuntimeState") -> str | None:

    if not state.auto_compact_disabled_until:
        return None

    disabled_until = _parse_utc_iso(state.auto_compact_disabled_until)
    if disabled_until > datetime.now(UTC):
        return state.auto_compact_disabled_until
    return None


def auto_compact_circuit_is_open(state: "SessionRuntimeState") -> bool:

    if active_auto_compact_disabled_until(state):
        return True

    if state.auto_compact_disabled_until:
        state.auto_compact_disabled_until = None
    return False


@dataclass(slots=True)
class CompactionHistoryEntry:

    event_type: str
    trigger: str
    target_tokens: int | None
    input_fingerprint: str | None
    status: str
    reason: str | None
    before_tokens: int | None
    after_tokens: int | None
    checkpoint_id: str | None
    created_at: str | None


@dataclass(slots=True)
class SessionRuntimeState:

    session_id: str
    latest_checkpoint_id: str | None = None
    auto_compact_failure_count: int = 0
    auto_compact_disabled_until: str | None = None
    last_auto_compact_failure_reason: str | None = None
    system_prompt_fingerprint: str | None = None
    last_compaction_input_fingerprint: str | None = None
    last_no_effect_compaction_fingerprint: str | None = None
    consumed_tool_result_part_ids: set[str] = field(default_factory=set)
    recent_compaction_events: list[CompactionHistoryEntry] = field(default_factory=list)

    def record_auto_compact_failure(
        self,
        reason: str,
        *,
        failure_limit: int = 3,
        disabled_minutes: int = 30,
    ) -> bool:

        self.auto_compact_failure_count += 1
        self.last_auto_compact_failure_reason = reason
        if self.auto_compact_failure_count < failure_limit:
            return False

        self.auto_compact_disabled_until = _utc_after(disabled_minutes)
        return True

    def record_auto_compact_success(self) -> None:
        self.auto_compact_failure_count = 0
        self.auto_compact_disabled_until = None
        self.last_auto_compact_failure_reason = None

    def record_compaction_event(self, entry: CompactionHistoryEntry, *, limit: int = 10) -> None:
        self.recent_compaction_events.append(entry)
        if len(self.recent_compaction_events) > limit:
            self.recent_compaction_events = self.recent_compaction_events[-limit:]
