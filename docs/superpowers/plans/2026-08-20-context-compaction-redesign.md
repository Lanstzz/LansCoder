# Context Compaction Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make compaction budget-driven only: remove the task-hash classification system and the forced L1 dialogue blanking, renumber the deterministic tool-result levels to L1/L2, and make the LLM summary (L3) summarize only old turns while keeping the recent N turns verbatim.

**Architecture:** Two removal phases (task-hash trigger, old L1 blanking), then a pipeline renumber (route→L1, archive→L2), then an LLM-summary enhancement (recent-N window + dialogue prompt). The LLM summary stays a separate manager-level step (`l4_service` internal name retained) with a new recent-turn constraint threaded through `LlmCompactRequest`.

**Tech Stack:** Python, pytest, dataclasses. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-08-20-context-compaction-redesign-design.md](../../../docs/superpowers/specs/2026-08-20-context-compaction-redesign-design.md) — the plan argues from the spec, so the spec travels with it; executors read both.

## Global Constraints

- Compaction triggers are only `AUTO` (90% high watermark), `PROMPT_TOO_LONG`, `MANUAL`. No task-switch trigger exists.
- The LLM summary step is conceptually level L3 but keeps its internal identifiers (`l4_service`, `LlmCompactService`, `LlmCompactEvent`, `L4Source`, `_apply_l4_compaction`) to limit churn. Do NOT rename those symbols.
- Programmatic pipeline levels are `"l1"` (route, former L2) and `"l2"` (archive, former L3). `CompactionLevel = Literal["l1", "l2"]`.
- `task_hash` / `active_task_hash` / `candidate_task_hash` are removed everywhere. `created_turn` / `turn_id` stay (they drive the recent-N window).
- `recent_turn_window: int = 10` in `ContextCompactionConfig` (`lanscoder/context/triggers.py`); `cold_turn_distance` removed.
- No old-data migration. Legacy `compaction_state="trimmed"` parts may still appear in old sessions; `context_builder.py` keeps its trimmed handling (read-only) — do not delete `_has_trimmed_text` / `_is_visible_text_part`.
- Commit message format: `{feat,fix,docs}: <concise imperative>` with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- After each task: `ruff check` and `ruff format` on changed files; run the task's tests; then commit only the files you changed this task (`git add <paths>`, never `git add -A`).

---

### Task 1: Remove the task-hash compaction trigger

Removes the `TASK_HASH_CHANGED` trigger end-to-end so no code path asks the manager to compact on a task switch. The pipeline fields (`active_task_hash`, `force_old_task_compaction`, `required_levels`) are left in place until Task 2 removes old L1.

**Files:**
- Modify: `lanscoder/context/manager.py:25-29` (enum), `:130-155` (main compact path), `:330-341` (fallback path), `:565-570` (`_target_tokens`)
- Modify: `lanscoder/agent/loop.py:1277-1278` (method), `:621-622`, `:634-635`, `:999-1000`, `:1059-1060` (call sites)
- Modify: `lanscoder/agent/tool_execution.py:94`, `:476-478`
- Modify: `lanscoder/agent/task_boundary_classifier.py:63-75` (drop `compact_if_needed`), `:138-139`
- Test: `tests/test_context_window_manager.py` (`test_manager_runs_pipeline_when_task_hash_changed`, `test_manager_forces_old_task_compaction*`)

**Interfaces:**
- Consumes: `ContextWindowTrigger` enum, `ContextCompactRequest` (unchanged).
- Produces: `ContextWindowTrigger` without `TASK_HASH_CHANGED`; `ContextWindowManager.compact_if_needed` no longer reads `required_levels`/`force_old_task_compaction` for any trigger.

- [ ] **Step 1: Remove the trigger from the enum and manager**

In `lanscoder/context/manager.py`, delete the `TASK_HASH_CHANGED = "task_hash_changed"` line from `ContextWindowTrigger` (line 27). Then in the main `compact_if_needed` path, replace lines 130-155 so the programmatic `CompactionRequest` never sets task-switch-specific fields:

```python
        target_tokens = _target_tokens(request, trigger)
        if request.budget.fixed_tokens >= request.budget.low_watermark:
            return ContextCompactResult(
                status="failed",
                reason="fixed_context_over_budget",
                view=request.view,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                final_failure_reason="fixed_context_over_budget",
            )

        programmatic = self.pipeline.compact(
            CompactionRequest(
                view=request.view,
                active_task_hash=request.runtime_state.active_task_hash,
                target_tokens=target_tokens,
                current_turn=request.current_turn,
                estimate_tokens=lambda candidate: request.estimate_budget(candidate).input_tokens,
                consumed_tool_result_part_ids=frozenset(request.runtime_state.consumed_tool_result_part_ids),
                l2_result_target_tokens=self.config.l2_result_target_tokens,
                force_route_current_text=_force_route_current_text_for_trigger(trigger),
            )
        )
```

This removes `required_levels=...` and `force_old_task_compaction=...` (they now default). Note: `active_task_hash` stays for now — removed in Task 2.

In the `_run_fallback` path, replace the `CompactionRequest(...)` at lines 330-341 to drop `required_levels` and `force_old_task_compaction`:

```python
            current_programmatic = self.pipeline.compact(
                CompactionRequest(
                    view=programmatic.view,
                    active_task_hash=request.runtime_state.active_task_hash,
                    target_tokens=target_tokens,
                    current_turn=request.current_turn,
                    estimate_tokens=lambda candidate: request.estimate_budget(candidate).input_tokens,
                    consumed_tool_result_part_ids=frozenset(request.runtime_state.consumed_tool_result_part_ids),
                    enabled_levels=("l1", "l2", "l3"),
                    l2_result_target_tokens=self.config.l2_result_target_tokens,
                    force_route_current_text=_force_route_current_text_for_trigger(trigger),
                )
            )
```

In `_target_tokens` (lines 565-570), remove the `TASK_HASH_CHANGED` branch:

```python
def _target_tokens(request: ContextCompactRequest, trigger: ContextWindowTrigger) -> int:
    if request.target_tokens is not None:
        return request.target_tokens
    return request.budget.low_watermark
```

- [ ] **Step 2: Remove loop call sites**

In `lanscoder/agent/loop.py`, delete the method `_compact_after_task_hash_changed` (lines 1277-1278). Delete the four call sites:

```python
        if execution.task_hash_changed:
            self._compact_after_task_hash_changed()
```

at lines 621-622, 634-635, 999-1000, and 1059-1060.

- [ ] **Step 3: Remove tool_execution task_hash_changed**

In `lanscoder/agent/tool_execution.py`, remove the `task_hash_changed: bool = False` field (line 94). At lines 476-478, keep the tagging call but drop the flag set:

```python
        if tool_call.name == "task_boundary" and result.ok and result.data.get("should_trigger_compaction"):
            self._tag_task_boundary_messages(result.data)
        return None
```

(Remove `state.task_hash_changed = True`.)

- [ ] **Step 4: Remove classifier compact trigger**

In `lanscoder/agent/task_boundary_classifier.py`, remove the `compact_if_needed` constructor parameter (declaration at line 63 and assignment at line 71), and remove the block at lines 138-139:

```python
        if result.data.get("should_trigger_compaction"):
            self._compact_if_needed(trigger=ContextWindowTrigger.TASK_HASH_CHANGED)
```

Remove the now-unused `ContextWindowTrigger` import. (This file is deleted entirely in Task 3; this step just keeps the module importable so Task 1's tests pass.)

- [ ] **Step 5: Update manager trigger tests**

In `tests/test_context_window_manager.py`:
- Delete `test_manager_runs_pipeline_when_task_hash_changed` (around line 398) — asserts removed behavior.
- Delete the `test_*_task_hash_changed` / `force_old_task_compaction` assertions (around lines 421-426, 437-456). Keep any test that asserts AUTO behavior.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_context_window_manager.py -v`
Expected: PASS (removed tests gone, remaining pass). If `TaskBoundaryClassifier` import still resolves, that's expected — it is deleted in Task 3.

- [ ] **Step 7: Commit**

```bash
git add lanscoder/context/manager.py lanscoder/agent/loop.py lanscoder/agent/tool_execution.py lanscoder/agent/task_boundary_classifier.py tests/test_context_window_manager.py
git commit -m "refactor: remove task-hash compaction trigger"
```

---

### Task 2: Delete old L1 blanking and renumber levels (route→L1, archive→L2)

Removes the entire "old-task dialogue blanking" layer and renumbers the two deterministic tool-result levels.

**Files:**
- Modify: `lanscoder/context/compaction.py`
- Modify: `lanscoder/context/content/detector.py`
- Modify: `lanscoder/context/content/compressors.py`
- Modify: `lanscoder/context/triggers.py` (remove `cold_turn_distance`, add `recent_turn_window`)
- Modify: `lanscoder/context/manager.py` (`__post_init__`, `CompactionRequest` field removal)
- Test: `tests/test_context_compaction_pipeline.py`, `tests/test_context_content_detector.py`, `tests/test_context_content_compressors.py`

**Interfaces:**
- Consumes: `CompactionRequest` from Task 1 (still has `active_task_hash`, now unused — removed here).
- Produces: `CompactionPipeline` with levels `"l1"` (route) and `"l2"` (archive); `CompactionRequest` without `active_task_hash`, `required_levels`, `force_old_task_compaction`; `CompactionLevel = Literal["l1","l2"]`; `recent_turn_window` in config.

- [ ] **Step 1: Remove old L1 from compaction.py**

In `lanscoder/context/compaction.py`:
- `CompactionLevel` (line 34): change to `CompactionLevel = Literal["l1", "l2"]`.
- `CompactionRequest` (lines 37-49): remove `active_task_hash: str | None`, change `enabled_levels` default to `("l1", "l2")`, remove `required_levels: tuple[CompactionLevel, ...] = ()` and `force_old_task_compaction: bool = False`.
- `CompactionPipeline` (lines 86-91): remove `cold_turn_distance: int = 8` field and its constructor usage.
- Delete `_apply_l1` (lines 256-293) and `_is_cold_old_task_part` (lines 462-469) and `_replace_l1_trimmed` (lines 445-451).
- Rename the archive-level helpers so they describe the new level-2 (former L3): `_has_l3_mandatory_candidates` → `_has_l2_mandatory_candidates`, `_has_l3_per_result_pressure` → `_has_l2_per_result_pressure`, `_is_l3_mandatory` → `_is_l2_mandatory`, `_l3_priority` → `_l2_priority`, `_l3_candidates` → `_l2_candidates`, `_can_archive_l3_part` → `_can_archive_l2_part`, `_l3_backing_record` → `_l2_backing_record`. Update every call site (in `compact`, the local variables `has_l3_mandatory_candidates` / `has_l3_per_result_pressure` become `has_l2_*`).
- In `_apply_level` (lines 225-254), rename dispatch so `"l1"` → the former `_apply_l2` body and `"l2"` → the former `_apply_l3` body:

```python
    def _apply_level(
        self,
        view: SessionView,
        *,
        request: CompactionRequest,
        level: CompactionLevel,
        lifecycle_records: dict[tuple[str, str], ToolResultLifecycleRecord],
    ) -> list[dict[str, object]]:
        if level == "l1":
            return self._apply_l1(
                view,
                request=request,
                lifecycle_records=lifecycle_records,
            )
        if level == "l2":
            return self._apply_l2(
                view,
                request=request,
                lifecycle_records=lifecycle_records,
            )
        return []
```

- Rename the former `_apply_l2` method to `_apply_l1(view, *, request, lifecycle_records)`. It already uses `request.consumed_tool_result_part_ids`, `request.current_turn`, and `request.l2_result_target_tokens` via helpers — unchanged.
- Rename the former `_apply_l3` method to `_apply_l2`. Its current signature takes `active_task_hash` and does `del active_task_hash`; remove that parameter and the `del` line: `(self, view, *, request, lifecycle_records)`.
- Remove the import of `compact_old_task_part` and `is_old_task_part` (keep `is_already_compacted`).
- Rewrite the early-exit condition in `compact()` (lines 122-128). It currently guards on `force_old_task_compaction`, the `required_levels` local, and `"l3"` level strings. New version — the archive level is now `"l2"`, so mandatory archive cleanup is guarded on `"l2"`:

```python
        if (
            before_tokens <= request.target_tokens
            and not ("l2" in request.enabled_levels and has_l2_mandatory_candidates)
            and not ("l2" in request.enabled_levels and has_l2_per_result_pressure)
        ):
            deduped = input_fingerprint in self._seen_noop_fingerprints
            self._seen_noop_fingerprints.add(input_fingerprint)
            return CompactionResult(
                view=view,
                event=CompactionEvent(
                    input_fingerprint=input_fingerprint,
                    before_tokens=before_tokens,
                    after_tokens=before_tokens,
                    levels_attempted=[],
                    stopped_at="already_within_budget",
                    changed_parts=0,
                    reason="already_within_budget",
                    target_tokens=request.target_tokens,
                    noop=True,
                    deduped=deduped,
                    lifecycle_counts=lifecycle_counts,
                ),
            )
```

- Remove the `required_levels = set(request.required_levels).intersection(request.enabled_levels)` line (102) and the two `has_l3_*` local assignments (lines 108-120) — they become `has_l2_*` with the rename.
- Rewrite the stop condition in the level loop (lines 170-191). Remove the `required_levels.intersection(remaining_levels)` term and change `"l3"` to `"l2"`:

```python
            if (
                after_level_tokens <= request.target_tokens
                and not (
                    "l2" in remaining_levels
                    and (
                        _has_l2_mandatory_candidates(
                            _effective_tail_messages(view),
                            lifecycle_records=lifecycle_records,
                            current_turn=request.current_turn,
                            consumed_tool_result_part_ids=request.consumed_tool_result_part_ids,
                        )
                        or _has_l2_per_result_pressure(
                            _effective_tail_messages(view),
                            lifecycle_records=lifecycle_records,
                            current_turn=request.current_turn,
                            per_result_target=per_result_target,
                            consumed_tool_result_part_ids=request.consumed_tool_result_part_ids,
                        )
                    )
                )
            ):
                stopped_at = level
                break
```

- In the manager fallback path (`manager.py` ~line 336), change `enabled_levels=("l1", "l2", "l3")` to `enabled_levels=("l1", "l2")`.

**`active_task_hash` call sites:** `CompactionRequest` no longer has this field, so the two manager constructions that pass it (main path line 145, fallback path line 331) must drop `active_task_hash=request.runtime_state.active_task_hash`. Also update the pipeline-test helper `_request` in `tests/test_context_compaction_pipeline.py:116` (remove the `active_task_hash` keyword) and any `CompactionRequest(` construction in `tests/test_context_store.py:23` that passes it.

- [ ] **Step 2: Remove detector and compressor**

In `lanscoder/context/content/detector.py`, delete `is_old_task_part` (lines 22-28). Keep `is_already_compacted` and `COMPACTED_STATES`.

In `lanscoder/context/content/compressors.py`, delete `compact_old_task_part` (lines 22-42) and its now-unused imports if any (`MessagePart` still used elsewhere in the file — verify).

- [ ] **Step 3: Update config**

In `lanscoder/context/triggers.py` `ContextCompactionConfig` (lines 18-23), remove `cold_turn_distance: int = 8` and add `recent_turn_window: int = 10`:

```python
    l2_result_target_tokens: int = 800
    large_tool_result_tokens: int = 1_200
    max_turn_tool_result_tokens: int = 4_000
    max_tail_messages: int = 120
    recent_turn_window: int = 10
    cold_preview_chars: int = 160
```

In `lanscoder/context/manager.py` `ContextWindowManager.__post_init__` (lines 98-107), remove the `cold_turn_distance=self.config.cold_turn_distance` argument.

- [ ] **Step 4: Update pipeline tests**

In `tests/test_context_compaction_pipeline.py`:
- The `_request` helper passes `active_task_hash=...`; remove that keyword argument. The `_message` helper's `task_hash` param: remove it and its `part_metadata` entry (keep `created_turn`).
- Delete tests for old L1 blanking: `test_l1_skips_current_task_content`, `test_l1_task_switch_immediately_trims_old_dialogue_but_never_latest_user_or_tool_call_text`, `test_l1_auto_waits_for_old_task_cold_turn_distance`, `test_l1_l3_skip_checkpoint_covered_history`, and any test asserting `compacted_by == "l1_old_task_dialogue"` or `compaction_state == "trimmed"`.
- Tests that exercise former L2/L3 route/archive behavior now run under the `("l1", "l2")` enabled levels — update assertions that hard-code `levels_attempted[0] == "l1"` if the fixture previously forced old L1 first (verify each: route tests should still see `"l1"`; archive tests see `"l2"`).
- Update any `enabled_levels=("l1",)` fixture to `("l1",)` (route only) or `("l2",)` (archive only) as the test intends.

In `tests/test_context_content_detector.py`, delete all tests that import/use `is_old_task_part` (the whole file becomes empty — replace with a single smoke test of `is_already_compacted`).

In `tests/test_context_content_compressors.py`, delete tests for `compact_old_task_part`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_context_compaction_pipeline.py tests/test_context_content_detector.py tests/test_context_content_compressors.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lanscoder/context/compaction.py lanscoder/context/content/detector.py lanscoder/context/content/compressors.py lanscoder/context/triggers.py lanscoder/context/manager.py tests/test_context_compaction_pipeline.py tests/test_context_content_detector.py tests/test_context_content_compressors.py
git commit -m "refactor: drop old-task dialogue blanking, renumber route/archive levels"
```

---

### Task 3: Remove the task-hash subsystem

Deletes the classifier, the `task_boundary` tool, the hash state machine, and every consumer (runtime state, replay, inspector, session tagging, delegate, subagent, writer, registry, hidden tools).

**Files:**
- Delete: `lanscoder/agent/task_boundary_classifier.py`, `lanscoder/tools/task_boundary.py`, `lanscoder/context/task_boundary.py`
- Modify: `lanscoder/context/runtime_state.py`, `lanscoder/context/runtime_replay.py`, `lanscoder/context/inspector.py`, `lanscoder/app/commands.py`, `lanscoder/context/writer.py`, `lanscoder/context/versions.py`
- Modify: `lanscoder/agent/session.py`, `lanscoder/agent/loop.py`, `lanscoder/agent/tool_execution.py`, `lanscoder/agent/subagent.py`
- Modify: `lanscoder/tools/delegate.py`, `lanscoder/tools/session_registry.py`, `lanscoder/tools/__init__.py`, `lanscoder/tools/hidden.py`
- Test: `tests/test_context_task_boundary.py` (delete), `tests/test_task_boundary_tool.py` (delete), plus `tests/test_context_runtime_replay.py`, `tests/test_context_runtime_state.py`, `tests/test_context_writer.py`, `tests/test_context_inspector.py`, `tests/test_delegate_tool.py`, `tests/test_tools.py`, `tests/test_agent_tool_flow.py`, `tests/test_session_resume_service.py`, `tests/test_app_factory.py`, `tests/test_app_runtime.py`, `tests/test_background_jobs.py`, `tests/test_agent_context_loop.py`, `tests/test_agent_e2e.py`, `tests/test_model_request_options.py`, `tests/test_context_system_prompt.py`, `tests/test_context_store.py`, `tests/test_context_builder_new.py`

**Interfaces:**
- Consumes: nothing from prior tasks (this is a pure deletion).
- Produces: a codebase with no `task_hash`, `active_task_hash`, `candidate_task_hash`, `TaskBoundaryService`, `TaskBoundaryObservation`, `task_boundary` tool, or `task_boundary_observed` event handling.

- [ ] **Step 1: Delete the three module files**

```bash
rm lanscoder/agent/task_boundary_classifier.py lanscoder/tools/task_boundary.py lanscoder/context/task_boundary.py
```

- [ ] **Step 2: Purge runtime_state**

In `lanscoder/context/runtime_state.py` `SessionRuntimeState` (lines 72-89), remove `active_task_hash`, `candidate_task_hash`, `candidate_task_basis_message_id`, `task_hash_stable_count`. Delete `observe_task_hash_candidate` (lines 91-122).

- [ ] **Step 3: Purge replay**

In `lanscoder/context/runtime_replay.py`, remove the `task_boundary_observed` branch (lines 32-34) and the `_apply_task_boundary` function (lines 60-77). Update the module docstring line that mentions "active task hash".

- [ ] **Step 4: Purge inspector and commands**

In `lanscoder/context/inspector.py`, remove `active_task_hash` and `candidate_task_hash` from `ContextInspectionReport` (lines 24-25) and their assignments (lines 72-73).

In `lanscoder/app/commands.py`, remove the two display lines (123-124):

```python
        f"Active task hash: {display_value(report.active_task_hash)}",
        f"Candidate task hash: {display_value(report.candidate_task_hash)}",
```

- [ ] **Step 5: Purge writer**

In `lanscoder/context/writer.py`, remove the import of `TaskBoundaryObservation, TaskBoundaryService` (line 20) and `append_task_boundary_observation` (lines 285-288). Update the module docstring if it mentions task boundary.

- [ ] **Step 6: Purge versions**

In `lanscoder/context/versions.py`, remove `TASK_BOUNDARY_TOOL_VERSION = "v1"` (line 6).

- [ ] **Step 7: Purge session**

In `lanscoder/agent/session.py`:
- Remove import `from lanscoder.context.task_boundary import observation_from_tool_result_data` (line 26).
- Remove `_current_context_metadata` (lines 635-639) and `_attach_current_context_metadata` (lines 641-646) — they only add `task_hash`. Call sites of `_attach_current_context_metadata` (lines 454, 564) must drop those calls; the parts are then written without task metadata.
- Remove `_append_task_boundary_observation_if_present` (lines 628-633) and its call at line 571.
- Remove `_task_boundary_required_stable_count` (line 753) and the two `task_boundary_required_stable_count=...` arguments (lines 161, 257).
- Update the `from_jsonl` docstring (line 226-227) that mentions task_boundary.

- [ ] **Step 8: Purge loop**

In `lanscoder/agent/loop.py`:
- Remove imports of `TaskBoundaryClassifier` (line 37) and `TaskBoundaryService` (line 55).
- Remove classifier construction block (lines 173-185, "阶段 6").
- Remove `_initialize_active_task_if_missing` (lines 462-468), `_classify_task_boundary` / `_classify_task_boundary_async` (lines 470-474), `_tag_message_parts_with_task_hash` (lines 476-488), `_tag_task_boundary_messages_with_active_hash` (lines 490-498).
- In the sync user-turn path (lines 290-304), the classification call was the only thing in the `try` that could raise `_AgentLoopLimitReached` / `AgentCancelledError`. Replace the whole block from `self._begin_turn()` through the `try/except` with:

```python
        self._begin_turn()  # provider_call_count = 0, _tool_rounds_completed=0
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        message_id = self.session.append_user_message(content, attachments=attachments)  # 把用户消息写进jsonl

        # run_tool_loop_interactive 是核心循环的外壳, 它负责捕获异常
        return self._run_tool_loop_interactive(
            self._complete_once_with_recovery,
        )
```

Apply the same removal to the async path (lines 424-439): drop the `try:` / `except _AgentLoopLimitReached` / `except AgentCancelledError` block around the now-empty `_initialize_active_task_if_missing` call, leaving `message_id = ...` then the `await self._run_tool_loop_interactive_async(...)` return. If `_complete_turn` becomes unused after both removals, remove it too and verify no other caller remains.

- Remove `tag_task_boundary_messages=self._tag_task_boundary_messages_with_active_hash` from both `TaskBoundaryClassifier(...)` (deleted) and `ToolExecutor(...)` constructions (lines 184, 206). For `ToolExecutor`, that keyword must also be removed from its constructor (Step 9).
- Remove `parent_task_hash=self.session.runtime_state.active_task_hash` from `create_delegate_tool(...)` (line 1377). Since `active_task_hash` is gone from runtime state, pass nothing; verify `create_delegate_tool` default is `None` (it is, after Step 10).

- [ ] **Step 9: Purge tool_execution**

In `lanscoder/agent/tool_execution.py`, remove the `tag_task_boundary_messages` constructor parameter (lines 116, 128) and its call at line 477. Remove the `ToolExecutionState.task_hash_changed` field (already removed in Task 1 Step 3 — verify it's gone). The `task_boundary` special-case block (lines 476-478) is now fully removed.

- [ ] **Step 10: Purge delegate and subagent**

In `lanscoder/tools/delegate.py`, remove `parent_task_hash: str | None = None` (line 21) and its forwarding (line 54).

In `lanscoder/agent/subagent.py`, remove `parent_task_hash: str | None = None` (line 100), its docstring mention, and the two forwarding lines (381, 444).

- [ ] **Step 11: Purge registry, tools init, hidden**

In `lanscoder/tools/session_registry.py`:
- Remove imports of `TaskBoundaryPolicy, TaskBoundaryService` (line 12) and `create_task_boundary_tool` (line 22).
- Remove `single_observation_basis_message_ids` (line 50) and `task_boundary_required_stable_count` (line 51) parameters and the `boundary_service` construction (lines 68-72).
- Remove the registration block (lines 86-87) and the docstring line about `task_boundary`.

In `lanscoder/tools/__init__.py`, remove the `task_boundary` import (line 19) and the `"create_task_boundary_tool"` export (line 50).

In `lanscoder/tools/hidden.py`, change `HIDDEN_TOOL_STATUS_NAMES` to `frozenset()` (empty) or delete the constant if nothing else references it — check usages in loop.

- [ ] **Step 12: Delete task-boundary tests, purge the rest**

Delete `tests/test_context_task_boundary.py` and `tests/test_task_boundary_tool.py`.

Update these files by removing every test/assert that references the removed symbols. For each, run the file and remove the failing tests:
- `tests/test_context_runtime_replay.py` — `test_replay_restores_active_task_hash*`, `test_replay_*_candidate_task_hash*` tests.
- `tests/test_context_runtime_state.py` — `observe_task_hash_candidate` tests.
- `tests/test_context_writer.py` — `append_task_boundary_observation` tests.
- `tests/test_context_inspector.py` — `active_task_hash` display assertions.
- `tests/test_delegate_tool.py` — `parent_task_hash` assertions.
- `tests/test_tools.py` — `task_boundary` tool registration tests.
- `tests/test_agent_tool_flow.py`, `tests/test_agent_context_loop.py`, `tests/test_agent_e2e.py`, `tests/test_background_jobs.py` — `task_boundary` / classifier wiring tests.
- `tests/test_session_resume_service.py`, `tests/test_app_factory.py`, `tests/test_app_runtime.py`, `tests/test_model_request_options.py`, `tests/test_context_system_prompt.py`, `tests/test_context_store.py`, `tests/test_context_builder_new.py` — any `task_hash` / `task_boundary` / `active_task_hash` references.

- [ ] **Step 13: Grep for stragglers**

Run: `grep -rn "task_boundary\|task_hash\|active_task_hash\|TaskBoundary\|parent_task_hash" lanscoder tests --include="*.py" | grep -v __pycache__`
Expected: no matches (except maybe a harmless docstring — fix those too).

- [ ] **Step 14: Run the full suite**

Run: `pytest -q`
Expected: PASS. Fix any failures caused by leftover references.

- [ ] **Step 15: Commit**

```bash
git add -A lanscoder/agent lanscoder/tools lanscoder/context lanscoder/app tests
git commit -m "refactor: remove task-hash classification system"
```

(Staging `-A` here is scoped to the directories you changed in this task — verify with `git status` before committing that only this task's deletions/edits are staged.)

---

### Task 4: Thread recent-turn window into the LLM summary and enforce it

The LLM summary (conceptually L3, internally `l4_service`) must not summarize the most recent N turns. Adds `current_turn` and `recent_turn_window` to `LlmCompactRequest` and enforces the floor in the boundary selection.

**Files:**
- Modify: `lanscoder/context/llm_compact.py` (`LlmCompactRequest`, `_validate_summary_boundary`)
- Modify: `lanscoder/context/provider_summarizer.py` (`LlmCompactSummarizer.summarize` signature, `_tail_boundary`)
- Modify: `lanscoder/context/manager.py` (two `LlmCompactRequest(...)` constructions)
- Test: `tests/test_context_llm_compact.py`, `tests/test_context_provider_summarizer.py`, `tests/test_context_window_manager.py`

**Interfaces:**
- Consumes: `recent_turn_window` from `ContextCompactionConfig` (Task 2), `ContextCompactRequest.current_turn` (existing).
- Produces: `LlmCompactRequest` with `current_turn: int` and `recent_turn_window: int`; `LlmCompactSummarizer.summarize(messages, *, summary_mode, current_turn, recent_turn_window)`; boundary selection that keeps the recent N turns.

- [ ] **Step 1: Extend LlmCompactRequest**

In `lanscoder/context/llm_compact.py`, add fields to `LlmCompactRequest` (lines 73-80):

```python
@dataclass(slots=True)
class LlmCompactRequest:
    view: SessionView
    runtime_state: SessionRuntimeState
    consumed_tool_result_part_ids: frozenset[str]
    mode: CompactMode = "auto"
    expected_source_fingerprint: str | None = None
    summary_mode: str = "default"
    current_turn: int = 0
    recent_turn_window: int = 10
```

- [ ] **Step 2: Thread through the service**

In `LlmCompactService.generate_candidate` (line 107), pass the new fields into `_summarize`:

```python
                summary = _summarize(
                    self.summarizer,
                    source_messages,
                    summary_mode=request.summary_mode,
                    current_turn=request.current_turn,
                    recent_turn_window=request.recent_turn_window,
                )
```

Change `_summarize` (lines 387-398) to forward them:

```python
def _summarize(
    summarizer: LlmCompactSummarizer,
    messages: list[AgentMessage],
    *,
    summary_mode: str,
    current_turn: int,
    recent_turn_window: int,
) -> LlmCompactSummary:
    summary = summarizer.summarize(
        messages,
        summary_mode=summary_mode,
        current_turn=current_turn,
        recent_turn_window=recent_turn_window,
    )
    return LlmCompactSummary(
        summary=normalize_coding_handoff(summary.summary),
        tail_start_message_id=summary.tail_start_message_id,
        covered_until_message_id=summary.covered_until_message_id,
    )
```

- [ ] **Step 3: Enforce the floor in the summarizer boundary**

In `lanscoder/context/provider_summarizer.py`, update `LlmCompactSummarizer` protocol (in `llm_compact.py` — change `summarize` signature to accept `current_turn: int, recent_turn_window: int`) and `ProviderLlmCompactSummarizer.summarize` to accept and forward the new args. Then modify `_tail_boundary` to refuse a tail that keeps fewer than `recent_turn_window` turns:

```python
def _tail_boundary(
    messages: list[AgentMessage],
    *,
    current_turn: int,
    recent_turn_window: int,
) -> _TailBoundary:
    """选择保守 tail，同时保证保留最近 N 轮对话。"""

    candidates = _boundary_candidates(messages)
    if len(candidates) < 2:
        raise NoSummaryError("not enough messages to summarize")

    max_tail_start_index = _recent_turn_max_tail_start_index(
        candidates,
        current_turn=current_turn,
        recent_turn_window=recent_turn_window,
    )
    if max_tail_start_index <= 0:
        raise NoSummaryError("no dialogue outside the recent turn window")

    for index in range(max_tail_start_index, 0, -1):
        try:
            validate_tool_call_sequence(candidates[index:])
        except InvalidToolCallSequenceError:
            continue
        return _TailBoundary(
            tail_start_message_id=candidates[index].id,
            covered_until_message_id=candidates[index - 1].id,
        )
    raise NoSummaryError("could not find a valid checkpoint tail boundary")
```

Add the helper (place near `_boundary_candidates`):

```python
def _recent_turn_max_tail_start_index(
    candidates: list[AgentMessage],
    *,
    current_turn: int,
    recent_turn_window: int,
) -> int:
    """tail_start 允许的最大候选下标。

    保留最近 N 轮意味着 tail 必须包含 turn [T-N+1, T] 的全部消息，所以
    tail_start 最晚必须落在 turn T-N+1 开头的消息上。若 T-N+1 <= 1（全部
    都在窗口内）或历史不足，返回 0（无可摘要）。
    """

    min_created_turn = current_turn - recent_turn_window + 1
    if min_created_turn <= 1:
        return 0
    for index, message in enumerate(candidates):
        if _message_created_turn(message) >= min_created_turn:
            return index
    return 0


def _message_created_turn(message: AgentMessage) -> int | None:
    for part in message.parts:
        created_turn = part.metadata.get("created_turn")
        if isinstance(created_turn, int) and not isinstance(created_turn, bool):
            return created_turn
    return None
```

Semantics: `max_tail_start_index` is the index of the first candidate message whose turn is inside the recent window (>= `T - N + 1`). Allowed tail starts are `[0, max_tail_start_index]`; the downward loop keeps the original "latest valid boundary wins" behavior (smallest tail that satisfies the floor) while never starting past the window. The `validate_tool_call_sequence` check is unchanged from the original loop.

**Do not "fix" the NoSummaryError path.** When the entire conversation is inside the recent window (`max_tail_start_index <= 0`), the new `raise NoSummaryError("no dialogue outside the recent turn window")` intentionally makes L3 fail as `no_summary`, which flows through the existing `CompactRetryPolicy` (one retry, then fail → manager's `_final_l4_failure`). This mirrors the pre-existing `"not enough messages to summarize"` behavior and is the honest outcome when there is genuinely nothing old enough to summarize. It also naturally guards L3: a session short enough to fit entirely in the window should not be paying for an LLM summary call.

- [ ] **Step 4: Pass the fields from the manager**

In `lanscoder/context/manager.py`, both `LlmCompactRequest(...)` constructions (main path ~line 211 and fallback `retry_l4_stronger_summary` ~line 390) add:

```python
                current_turn=request.current_turn,
                recent_turn_window=self.config.recent_turn_window,
```

- [ ] **Step 5: Update tests**

- `tests/test_context_llm_compact.py` — update fake summarizers / `LlmCompactRequest` constructions to include `current_turn` and `recent_turn_window` (defaults make most passes; only where boundary selection is exercised do you need to set them).
- `tests/test_context_provider_summarizer.py` — update `_tail_boundary` / `summarize` calls with the new keyword args; add a test that a tail would otherwise include a recent turn now gets pushed to `created_turn == T - N + 1`.
- `tests/test_context_window_manager.py` — fake L4s that construct `LlmCompactRequest` need the new fields (defaults suffice).

Add a new test in `tests/test_context_provider_summarizer.py`. The existing `_message` helper lacks `created_turn`, so add a local helper and import the private `_tail_boundary`:

```python
from lanscoder.context.provider_summarizer import ProviderLlmCompactSummarizer, _tail_boundary


def _user_message_with_turn(message_id: str, *, created_turn: int) -> AgentMessage:
    message = _message(message_id, "user", "内容")
    message.parts[0].metadata["created_turn"] = created_turn
    return message


def test_tail_boundary_keeps_recent_turn_window() -> None:
    messages = [
        _user_message_with_turn("msg_1", created_turn=1),
        _user_message_with_turn("msg_2", created_turn=2),
        _user_message_with_turn("msg_3", created_turn=3),
        _user_message_with_turn("msg_4", created_turn=4),
    ]
    boundary = _tail_boundary(messages, current_turn=4, recent_turn_window=2)

    assert boundary.covered_until_message_id == "msg_2"
    assert boundary.tail_start_message_id == "msg_3"
```

With `recent_turn_window=2` and `current_turn=4`, the floor is `min_created_turn = 3`: tail must include turns 3-4, so `max_tail_start_index` points at `msg_3`. The downward loop finds `msg_3` valid (all-user tail), giving `tail_start=msg_3`, `covered_until=msg_2`.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_context_llm_compact.py tests/test_context_provider_summarizer.py tests/test_context_window_manager.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lanscoder/context/llm_compact.py lanscoder/context/provider_summarizer.py lanscoder/context/manager.py tests/test_context_llm_compact.py tests/test_context_provider_summarizer.py tests/test_context_window_manager.py
git commit -m "feat: keep recent N turns verbatim in LLM summary boundary"
```

---

### Task 5: Dialogue summary prompt for L3

Replaces the coding-handoff prompt used by the LLM summary step with a dialogue-oriented prompt. The step's boundary selection is unchanged (Task 4), only the prompt contract changes.

**Files:**
- Modify: `lanscoder/context/provider_summarizer.py`
- Modify: `lanscoder/context/llm_compact.py` (heading constant or normalization, if reused)
- Test: `tests/test_context_provider_summarizer.py`

**Interfaces:**
- Consumes: `LlmCompactSummarizer` protocol signature from Task 4.
- Produces: `_build_summary_prompt` that emits a dialogue summary with sections: 用户请求要点 / 已给出结论 / 未完成事项 / 关键约束与偏好. `summary_mode="stronger"` still tightens.

- [ ] **Step 1: Add the dialogue heading contract**

In `lanscoder/context/llm_compact.py`, add a dialogue-heading constant next to `CODING_HANDOFF_HEADINGS`:

```python
DIALOGUE_SUMMARY_HEADINGS: tuple[str, ...] = (
    "## 用户请求要点",
    "## 已给出的结论",
    "## 未完成事项",
    "## 关键约束与偏好",
)
```

- [ ] **Step 2: Build the dialogue prompt**

In `lanscoder/context/provider_summarizer.py`, add `_build_dialogue_summary_prompt` and route on `summary_mode` in `_build_summary_prompt`. The dialogue prompt:

```python
def _build_dialogue_summary_prompt(messages: list[AgentMessage], *, summary_mode: str) -> str:
    mode_hint = "更强压缩，优先保留事实与约束。" if summary_mode == "stronger" else "常规压缩。"
    headings = "\n".join(DIALOGUE_SUMMARY_HEADINGS)
    sections = [
        f"摘要模式：{mode_hint}",
        "",
        "以下会话的多轮对话需要压缩。只输出一次下面的每个标题；若该项没有证据，写“无”：",
        headings,
        "",
        "要求：保留用户请求的要点、已经给出的结论、尚未完成或悬而未决的事项、以及影响后续工作的关键约束与偏好。丢弃：工具调用细节、中间推理、重复信息。",
        "",
        "需要压缩的对话历史：",
    ]
    for message in messages:
        content = _message_text(message)
        if not content:
            continue
        sections.append(f"\n[{message.id}] role={message.role}\n{content}")
    return "\n".join(sections)
```

Wire it: in `ProviderLlmCompactSummarizer.summarize`, the system prompt currently says "输出简洁的 coding handoff...七个 Markdown 标题". Replace with a dialogue-flavored system line that references the four headings, and call `_build_dialogue_summary_prompt` instead of `_build_summary_prompt`. Generalize `normalize_coding_handoff` to accept headings:

```python
def normalize_coding_handoff(summary: str, headings: tuple[str, ...] = CODING_HANDOFF_HEADINGS) -> str:
    bodies: dict[str, list[str]] = {heading: [] for heading in headings}
    current: str | None = None
    preamble: list[str] = []
    for line in summary.strip().splitlines():
        heading = line.strip()
        if heading in bodies:
            current = heading
            continue
        if heading.startswith("##"):
            line = heading.lstrip("#").strip()
        if current is None:
            preamble.append(line)
        else:
            bodies[current].append(line)

    if preamble:
        bodies[headings[0]].extend(preamble)

    sections: list[str] = []
    for heading in headings:
        body = "\n".join(bodies[heading]).strip()
        sections.append(f"{heading}\n{body or '无'}")
    return "\n\n".join(sections)
```

(The only change from the current body is replacing the `CODING_HANDOFF_HEADINGS` references with the `headings` parameter. The default keeps existing callers and tests working.)

In `ProviderLlmCompactSummarizer.summarize`, after building the response, normalize with the dialogue headings:

```python
        return LlmCompactSummary(
            summary=normalize_coding_handoff(summary, headings=DIALOGUE_SUMMARY_HEADINGS),
            tail_start_message_id=tail.tail_start_message_id,
            covered_until_message_id=tail.covered_until_message_id,
        )
```

**Critical integration point — stop double-normalization in `_summarize`:** `lanscoder/context/llm_compact.py` `_summarize` (lines 387-398) currently re-runs `normalize_coding_handoff(summary.summary)` on the already-normalized provider output. With a dialogue-normalized summary, that second pass would mangle the headings. Change `_summarize` to trust the summarizer contract and pass the summary through unchanged:

```python
    summary = summarizer.summarize(
        messages,
        summary_mode=summary_mode,
        current_turn=current_turn,
        recent_turn_window=recent_turn_window,
    )
    return LlmCompactSummary(
        summary=summary.summary,
        tail_start_message_id=summary.tail_start_message_id,
        covered_until_message_id=summary.covered_until_message_id,
    )
```

Update the existing function signature and its callers in tests accordingly.

- [ ] **Step 3: Update tests**

In `tests/test_context_provider_summarizer.py`:
- Tests asserting the seven `CODING_HANDOFF_HEADINGS` sections for the dialogue mode now assert the four `DIALOGUE_SUMMARY_HEADINGS`.
- Add a test that the dialogue prompt omits tool-call lines from the emitted history (assert the prompt text contains only role/message text, not tool_call argument dumps — verify against `_message_text`).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_context_provider_summarizer.py tests/test_context_llm_compact.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lanscoder/context/llm_compact.py lanscoder/context/provider_summarizer.py tests/test_context_provider_summarizer.py tests/test_context_llm_compact.py
git commit -m "feat: dialogue summary prompt for compacted turns"
```

---

### Task 6: Bump compaction strategy version and full-suite sweep

Reflects the level-semantics change in `COMPACTION_STRATEGY_VERSION` and runs the whole suite to close out.

**Files:**
- Modify: `lanscoder/context/versions.py`
- Modify: `docs/superpowers/specs/2026-08-20-context-compaction-redesign-design.md` (mark status as implemented)

**Interfaces:**
- Consumes: all prior tasks.

- [ ] **Step 1: Bump version**

In `lanscoder/context/versions.py`, change `COMPACTION_STRATEGY_VERSION = "v2"` to `"v3"`.

- [ ] **Step 2: Run the full suite + linter**

Run: `pytest -q`
Run: `ruff check lanscoder tests && ruff format --check lanscoder tests`
Expected: all PASS. Fix any remaining issues.

- [ ] **Step 3: Update the spec status**

In the spec file, change the front-matter `Status: draft` to `Status: implemented` and add a one-line note at the top that the plan is `docs/superpowers/plans/2026-08-20-context-compaction-redesign.md`.

- [ ] **Step 4: Commit**

```bash
git add lanscoder/context/versions.py docs/superpowers/specs/2026-08-20-context-compaction-redesign-design.md
git commit -m "chore: bump compaction strategy version to v3"
```
