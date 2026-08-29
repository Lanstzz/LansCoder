"""CompactionEvent 归一化采集(纯函数,不依赖 locobench)。

从 LansCoder session store 的原始事件里提取压缩行为记录,统一成分析友好的
dict。L1/L2(``compaction_completed``)与 L3(``llm_compaction_completed``)
分开标注;硬截断通过 ``fallback_steps[].action == "hard_truncate"`` 识别。
"""

from __future__ import annotations

from typing import Any

COMPACTION_EVENT_TYPES = frozenset(
    {"compaction_completed", "llm_compaction_completed", "compaction_skipped"}
)


def _event_inner(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("event")
    return inner if isinstance(inner, dict) else {}


def _is_hard_truncate(inner: dict[str, Any]) -> bool:
    """判定一次 llm_compaction_completed 事件是否为硬截断兜底。"""

    if inner.get("failure_reason") == "hard_truncate" or inner.get("final_failure_reason") == "hard_truncate":
        return True
    steps = inner.get("fallback_steps")
    if isinstance(steps, list):
        return any(isinstance(step, dict) and step.get("action") == "hard_truncate" for step in steps)
    return False


def normalize_compaction_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把一个压缩类 session 事件归一化为分析友好 dict。"""

    record: dict[str, Any] = {
        "type": event_type,
        "trigger": payload.get("trigger"),
        "created_at": payload.get("created_at"),
        "event_version": payload.get("event_version"),
    }
    if event_type == "compaction_completed":
        inner = _event_inner(payload)
        record.update(
            {
                "status": payload.get("status"),
                "reason": payload.get("reason"),
                "before_tokens": payload.get("before_tokens"),
                "after_tokens": payload.get("after_tokens"),
                "target_tokens": payload.get("target_tokens"),
                "checkpoint_id": payload.get("checkpoint_id"),
                "input_fingerprint": payload.get("input_fingerprint"),
                # L1/L2 行为(来自 CompactionEvent)
                "levels_attempted": inner.get("levels_attempted"),
                "stopped_at": inner.get("stopped_at"),
                "changed_parts": inner.get("changed_parts"),
                "noop": inner.get("noop"),
                "deduped": inner.get("deduped"),
                "llm_used": inner.get("llm_used"),
                "level_metrics": inner.get("level_metrics"),
                "lifecycle_counts": inner.get("lifecycle_counts"),
                "archive_ids": inner.get("archive_ids"),
            }
        )
    elif event_type == "llm_compaction_completed":
        inner = _event_inner(payload)
        record.update(
            {
                "status": inner.get("status") or payload.get("status"),
                "reason": payload.get("reason"),
                "failure_reason": inner.get("failure_reason"),
                "final_failure_reason": inner.get("final_failure_reason"),
                "checkpoint_id": inner.get("checkpoint_id") or payload.get("checkpoint_id"),
                "retry_count": inner.get("retry_count"),
                "fallback_steps": inner.get("fallback_steps"),
                "hard_truncate": _is_hard_truncate(inner),
            }
        )
    elif event_type == "compaction_skipped":
        record.update(
            {
                "status": "skipped",
                "reason": payload.get("reason"),
                "input_fingerprint": payload.get("input_fingerprint"),
            }
        )
    return record
