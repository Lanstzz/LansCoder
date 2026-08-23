# Hard-Truncate Fallback and Level-Naming Cleanup

Status: draft
Date: 2026-08-20
Author: Lanster (with Claude)

## 1. Background

The context-compaction redesign (2026-08-20-context-compaction-redesign-design.md) is
implemented and merged to `main` in 8 commits. Two follow-ups remain:

1. **Hard-truncate fallback.** The final whole-branch review flagged a design tension:
   when L3 (the LLM summary step) fails on a session whose conversation is entirely
   inside the recent window, the manager returns `status="failed"` with no checkpoint.
   For `MANUAL`/`PROMPT_TOO_LONG` this is a user-visible refusal with no recovery path.
   The user decided: when L3 and its fallbacks all fail, fall back to a deterministic
   hard truncation — a readable placeholder checkpoint that keeps the recent N turns
   verbatim and replaces older dialogue with a placeholder summary.

2. **Level-naming cleanup.** The redesign deliberately kept legacy identifiers (`l4_*`,
   `LlmCompact*` internal names) and legacy persisted string values (`l2_route_compacted`,
   `l3_archive`, `l2_search_results`, …) to limit churn. The user wants these aligned to
   the new level scheme (L1 route, L2 archive, L3 LLM summary). Single-user project, so
   persisted string values are changed in place with no migration.

## 2. Design A: Hard-truncate fallback

### 2.1 Behavior

When the programmatic pipeline (L1 route, L2 archive) runs, L3 (LLM summary) fails, and
the fallback chain (`stronger_programmatic`, `retry_l3_stronger_summary`) also fails, the
manager must not return a bare failure. Instead it constructs a deterministic checkpoint:

- `tail_start_message_id` = the first message whose `created_turn >= current_turn - N + 1`
  (reuse the recent-N window logic from `_recent_turn_max_tail_start_index` /
  `_message_created_turn` in `provider_summarizer.py`).
- `covered_until_message_id` = the message immediately before `tail_start`.
- `summary` = `"[Earlier dialogue truncated — recent N turns kept]"` (readable placeholder).
- Written as a `checkpoint_created` event via the existing checkpoint commit path.

`context_builder.py` already renders a checkpoint as one `user` message + the verbatim
tail, so the truncated view is indistinguishable in shape from an LLM-summarized view.
No new rendering branch is needed.

### 2.2 Fallback chain

`CompactFallbackPolicy.action_for` (lanscoder/context/fallback.py) gains a terminal action:

```python
FallbackAction = Literal["stronger_programmatic", "retry_l3_stronger_summary", "hard_truncate"]

    def action_for(self, reason: str | None) -> FallbackAction:
        if reason == "prompt_too_long":
            return "stronger_programmatic"
        if reason in {"timeout", "no_summary"}:
            return "retry_l3_stronger_summary"
        return "hard_truncate"
```

The final `fail` action is replaced by `hard_truncate`. The manager's `_run_fallback`
handles `hard_truncate` by building the deterministic checkpoint and committing it via
the existing L3 commit path, returning `status="success"` when the truncated view is under
target. If even the truncated view (recent N turns only) is over budget — possible in
pathological sessions where N turns alone exceed the window — the manager returns the
existing `_final_l3_failure` with reason `still_over_budget_after_hard_truncate`.

### 2.3 Implementation shape

- `lanscoder/context/fallback.py`: add the `hard_truncate` action; default `action_for`
  returns it.
- `lanscoder/context/manager.py`: in `_run_fallback`, add a `hard_truncate` branch that
  builds the deterministic checkpoint and commits it. A small helper
  `_build_hard_truncate_checkpoint(view, *, current_turn, recent_turn_window)` constructs
  the `Checkpoint` (tail/covered/summary); it reuses the boundary helpers.
- The `recent_turn_window` value comes from `self.config.recent_turn_window` (exists).

### 2.4 Edge cases

- Session entirely inside the recent window (nothing to truncate): the boundary helper
  returns no covered region; the manager falls through to `_final_l3_failure`. This is
  correct — there is genuinely nothing old enough to drop. (This preserves the
  plan-intentional guard against paying LLM cost on short sessions; hard truncate is the
  deterministic last resort only when L3 already failed.)
- Tool-call/tool-result sequences: the hard-truncate boundary must respect
  `validate_tool_call_sequence` on the tail, exactly like the LLM path. Reuse the same
  guard: the boundary is only valid if `tail_messages[tail_start:]` passes
  `validate_tool_call_sequence`; otherwise slide `tail_start` earlier (same logic as
  `_tail_boundary`'s downward loop).

## 3. Design B: Level-naming cleanup

### 3.1 B1 — code identifiers `l4_*`/`L4*` → `l3_*`/`L3*`

Mechanical rename; no behavior change. Files and symbols:

- `lanscoder/context/manager.py`: `L4Compactor`→`L3Compactor`, `l4_service`→`l3_service`,
  `l4_event`→`l3_event` (in `ContextCompactResult`), `l4_request`→`l3_request`,
  `_record_l4_event`→`_record_l3_event`, `_final_l4_failure`→`_final_l3_failure`,
  `l4_service_missing` failure reason → `l3_service_missing`, docstring "编排 L1-L4"→"L1-L3".
- `lanscoder/context/llm_compact.py`: module docstring "L4 LLM compact"→"L3",
  `L4Source`→`L3Source`, `_build_l4_source`→`_build_l3_source`, `LlmCompactRequest`'s
  `LlmCompactService` (unchanged name — it is already LLM-flavored, not L4-flavored),
  error messages "current L4 source"→"L3", "only successful L4 candidates"→"L3".
- `lanscoder/context/runtime_replay.py`: `_apply_l4_compaction`→`_apply_l3_compaction`.
- `lanscoder/context/fallback.py`: `retry_l4_stronger_summary`→`retry_l3_stronger_summary`
  (action value and docstring), module docstring "L4 失败"→"L3 失败".
- `lanscoder/context/retry_policy.py`: docstring "L4 compact"→"L3".
- `lanscoder/context/provider_summarizer.py`: docstrings "L4 checkpoint"→"L3",
  "L4 summarizer 协议"→"L3".
- `lanscoder/context/compaction.py:355`: comment "ContextBuilder/L4"→"ContextBuilder/L3".

Tests referencing these symbols (`FakeL4`, `l4_service=`, `l4_event`, `L4Source`,
`test_manager_runs_l4_only_after_l1_l3_fail_target`, etc.) are renamed to match.

### 3.2 B2 — persisted string values (no migration, single-user)

| Now | New | File:line |
|---|---|---|
| `l2_route_compacted` | `l1_route_compacted` | compaction.py:269 (write), :642 (state check), content/detector.py:12 (COMPACTED_STATES) |
| `l3_archive` | `l2_archive` | archive.py:148 |
| `l2_build_output` | `l1_build_output` | content/build.py:61 |
| `l2_search_results` | `l1_search_results` | content/search.py:62 |
| `l2_json_array` | `l1_json_array` | content/json.py:67 |
| `l2_json_object` | `l1_json_object` | content/json.py:97 |
| `l2_html` | `l1_html` | content/html.py:64 |
| `l2_current_task_cold` | `l1_current_task_cold` | content/compressors.py:45 |
| `l2_source_code` | `l1_source_code` | content/code.py:62 |
| `l2_git_diff` | `l1_git_diff` | content/diff.py:92 |
| `l2_route` (default) | `l1_route` | compaction.py:432 |

Function rename: `_l2_compacted_by` → `_l1_compacted_by` (compaction.py:426) — it labels
route output, which is now L1.

Deliberately unchanged: `_L2Candidate` (archive is now L2 — name already correct),
`l2_result_target_tokens` field (means "new L2 per-result target" — correct),
`l2_route_compacted`→`l1_route_compacted` in COMPACTED_STATES keeps `is_already_compacted`
working (both reader and writer change together).

Tests asserting these strings (test_context_content_router.py, test_context_compaction_pipeline.py,
test_context_store.py, test_context_archive.py, test_context_content_detector.py) are updated.

## 4. Versioning

`COMPACTION_STRATEGY_VERSION` `v3` → `v4` (persisted string values changed; strategy
gains a new fallback action). `SYSTEM_PROMPT_VERSION` unchanged (no prompt-text change).

## 5. Testing

- Hard truncate:
  - `CompactFallbackPolicy.action_for` returns `hard_truncate` for unknown/fail reasons,
    `stronger_programmatic` for prompt_too_long, `retry_l3_stronger_summary` for timeout/no_summary.
  - Manager: when L3 + fallbacks fail, `_run_fallback` commits a deterministic checkpoint
    with the placeholder summary, keeps recent N turns, returns `status="success"` when under target.
  - Boundary respects tool sequence: a hard-truncate tail that would split a
    tool_call/tool_result pair slides earlier.
  - Session entirely inside the recent window → still `_final_l3_failure` (nothing to truncate).
- Naming: existing tests updated to new identifiers/string values; grep confirms zero
  `l4_`/`L4*`/`l2_route_compacted`/`l3_archive`/`l2_*_compacted_by` references remain.

## 6. Open items

- Exact placeholder summary text (`"[Earlier dialogue truncated — recent N turns kept]"`)
  — fixed at implementation if reviewer/user prefers different wording.
- Whether `hard_truncate` should also apply when L3 never ran (e.g. `l3_service is None`)
  — currently only in the fallback chain after a failed L3 attempt; decide at implementation.
