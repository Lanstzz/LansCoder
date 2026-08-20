from __future__ import annotations

from lanscoder.utils.text import optional_str

from lanscoder.context.events import SessionEvent
from lanscoder.context.runtime_state import CompactionHistoryEntry, SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore


def replay_runtime_state(store: JsonlSessionStore, session_id: str) -> SessionRuntimeState:

    state = SessionRuntimeState(session_id=session_id)
    for event in store.list_events(session_id):
        _apply_event(state, event)
    return state


def _apply_event(state: SessionRuntimeState, event: SessionEvent) -> None:
    if event.type == "provider_projection_consumed":
        part_ids = event.payload.get("part_ids")
        if isinstance(part_ids, list):
            state.consumed_tool_result_part_ids.update(part_id for part_id in part_ids if isinstance(part_id, str) and part_id)
        return

    if event.type == "checkpoint_created":
        state.latest_checkpoint_id = str(event.payload.get("id") or "")
        source_fingerprint = event.payload.get("source_fingerprint")
        if source_fingerprint:
            state.last_compaction_input_fingerprint = str(source_fingerprint)
        return

    if event.type == "compaction_completed":
        compaction_event = _event_payload(event)
        input_fingerprint = compaction_event.get("input_fingerprint")
        if input_fingerprint:
            state.last_compaction_input_fingerprint = str(input_fingerprint)
        state.record_compaction_event(_compaction_history_entry(event, compaction_event))
        return

    if event.type == "compaction_skipped":
        if event.payload.get("reason") == "skipped_no_effect":
            state.last_no_effect_compaction_fingerprint = optional_str(event.payload.get("input_fingerprint"))
        return

    if event.type == "llm_compaction_completed":
        _apply_l3_compaction(state, event)


def _apply_l3_compaction(state: SessionRuntimeState, event: SessionEvent) -> None:
    payload = event.payload
    l3_event = _event_payload(event)
    source_fingerprint = l3_event.get("source_fingerprint")
    if source_fingerprint:
        state.last_compaction_input_fingerprint = str(source_fingerprint)
    state.record_compaction_event(_compaction_history_entry(event, l3_event))

    if l3_event.get("status") == "success":
        checkpoint_id = l3_event.get("checkpoint_id")
        if checkpoint_id:
            state.latest_checkpoint_id = str(checkpoint_id)
        state.record_auto_compact_success()
        return

    if payload.get("trigger") == "auto" and l3_event.get("status") == "failed":
        state.record_auto_compact_failure(str(l3_event.get("failure_reason") or "unknown"))


def _event_payload(event: SessionEvent) -> dict[str, object]:
    nested = event.payload.get("event")
    return dict(nested) if isinstance(nested, dict) else {}


def _compaction_history_entry(event: SessionEvent, nested_event: dict[str, object]) -> CompactionHistoryEntry:
    payload = event.payload
    input_fingerprint = payload.get("input_fingerprint") or nested_event.get("input_fingerprint")
    if input_fingerprint is None:
        input_fingerprint = nested_event.get("source_fingerprint")

    return CompactionHistoryEntry(
        event_type=event.type,
        trigger=str(payload.get("trigger") or ""),
        target_tokens=_optional_int(payload.get("target_tokens")),
        input_fingerprint=optional_str(input_fingerprint),
        status=str(payload.get("status") or nested_event.get("status") or _status_from_compaction(nested_event)),
        reason=optional_str(payload.get("reason") or nested_event.get("reason") or nested_event.get("failure_reason")),
        before_tokens=_optional_int(payload.get("before_tokens") or nested_event.get("before_tokens")),
        after_tokens=_optional_int(payload.get("after_tokens") or nested_event.get("after_tokens")),
        checkpoint_id=optional_str(payload.get("checkpoint_id") or nested_event.get("checkpoint_id")),
        created_at=optional_str(payload.get("created_at") or nested_event.get("created_at")),
    )


def _status_from_compaction(compaction_event: dict[str, object]) -> str:
    if "success" in compaction_event:
        return "success" if compaction_event.get("success") else "failed"
    return "success"


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
