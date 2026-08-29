"""benchmark.locobench.compaction_capture 纯函数单测(不依赖 locobench)。"""

from __future__ import annotations

from benchmark.locobench.compaction_capture import normalize_compaction_event


def test_normalize_compaction_completed_carries_l1_l2_metrics() -> None:
    payload = {
        "event_version": "1",
        "trigger": "auto",
        "target_tokens": 60,
        "status": "success",
        "reason": "l1",
        "before_tokens": 1000,
        "after_tokens": 400,
        "checkpoint_id": None,
        "input_fingerprint": "fp",
        "event": {
            "levels_attempted": ["l1", "l2"],
            "stopped_at": "l1",
            "changed_parts": 3,
            "noop": False,
            "deduped": False,
            "llm_used": False,
            "level_metrics": {
                "l1": {"before_tokens": 1000, "after_tokens": 600, "saved_tokens": 400, "changed_parts": 2},
                "l2": {"before_tokens": 600, "after_tokens": 400, "saved_tokens": 200, "changed_parts": 1},
            },
            "lifecycle_counts": {"hot": 5, "cold": 2},
            "archive_ids": ["a1"],
        },
    }
    record = normalize_compaction_event("compaction_completed", payload)
    assert record["type"] == "compaction_completed"
    assert record["status"] == "success"
    assert record["before_tokens"] == 1000
    assert record["after_tokens"] == 400
    assert record["levels_attempted"] == ["l1", "l2"]
    assert record["stopped_at"] == "l1"
    assert record["level_metrics"]["l1"]["changed_parts"] == 2
    assert record["level_metrics"]["l2"]["changed_parts"] == 1
    assert record["archive_ids"] == ["a1"]


def test_normalize_llm_compaction_detects_hard_truncate() -> None:
    payload = {
        "trigger": "auto",
        "target_tokens": 60,
        "status": "success",
        "reason": "hard_truncate",
        "checkpoint_id": "ckpt",
        "event": {
            "status": "success",
            "source_fingerprint": "fp",
            "retry_count": 0,
            "failure_reason": None,
            "checkpoint_id": "ckpt",
            "fallback_steps": [
                {
                    "step": 1,
                    "reason": "hard_truncate",
                    "action": "hard_truncate",
                    "before_tokens": 900,
                    "after_tokens": 200,
                    "status": "success",
                }
            ],
            "final_failure_reason": None,
        },
    }
    record = normalize_compaction_event("llm_compaction_completed", payload)
    assert record["type"] == "llm_compaction_completed"
    assert record["status"] == "success"
    assert record["hard_truncate"] is True
    assert record["fallback_steps"][0]["action"] == "hard_truncate"


def test_normalize_llm_compaction_success_without_hard_truncate() -> None:
    payload = {
        "trigger": "auto",
        "target_tokens": 60,
        "status": "success",
        "reason": "token_threshold",
        "checkpoint_id": "ckpt",
        "event": {
            "status": "success",
            "source_fingerprint": "fp",
            "retry_count": 0,
            "failure_reason": None,
            "checkpoint_id": "ckpt",
            "fallback_steps": None,
            "final_failure_reason": None,
        },
    }
    record = normalize_compaction_event("llm_compaction_completed", payload)
    assert record["status"] == "success"
    assert record["hard_truncate"] is False
    assert record["checkpoint_id"] == "ckpt"


def test_normalize_compaction_skipped() -> None:
    record = normalize_compaction_event(
        "compaction_skipped",
        {"trigger": "auto", "reason": "skipped_no_effect", "input_fingerprint": "fp"},
    )
    assert record["status"] == "skipped"
    assert record["reason"] == "skipped_no_effect"


def test_normalize_unknown_event_type_keeps_minimal_record() -> None:
    record = normalize_compaction_event("mystery", {"trigger": "auto"})
    assert record["type"] == "mystery"
    assert record["trigger"] == "auto"
