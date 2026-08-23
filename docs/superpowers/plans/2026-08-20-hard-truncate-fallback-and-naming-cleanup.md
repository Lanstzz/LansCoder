# Hard-Truncate Fallback and Level-Naming Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic hard-truncate fallback that produces a readable placeholder checkpoint when L3 (LLM summary) and its fallbacks all fail, and align the codebase's level naming to the new scheme (L1 route, L2 archive, L3 LLM summary): rename `l4_*`/`L4*` identifiers to `l3_*`/`L3*`, and rename persisted string values in place (single-user, no migration).

**Architecture:** (1) `CompactFallbackPolicy` gains a terminal `hard_truncate` action replacing `fail`. (2) The manager's `_run_fallback` handles it by building a deterministic `Checkpoint` — placeholder summary, boundary from the shared recent-N selection — and committing it via the existing checkpoint path. (3) Two mechanical rename passes: code identifiers (`l4_*`→`l3_*`) and persisted string values (`l2_*`/`l3_*`→`l1_*`/`l2_*`).

**Tech Stack:** Python, pytest, dataclasses. No new dependencies.

**Spec:** [docs/superpowers/specs/2026-08-20-hard-truncate-fallback-and-naming-cleanup-design.md](../../../docs/superpowers/specs/2026-08-20-hard-truncate-fallback-and-naming-cleanup-design.md)

## Global Constraints

- Compaction triggers are only `AUTO` / `PROMPT_TOO_LONG` / `MANUAL`. No task-switch trigger.
- Levels: L1 = route (former L2), L2 = archive (former L3), L3 = LLM summary (former L4). `CompactionLevel = Literal["l1","l2"]` (programmatic); L3 is the manager-level LLM step.
- The hard-truncate fallback produces a **readable placeholder summary**, not empty text. Placeholder text: `"[Earlier dialogue truncated — recent N turns kept]"`.
- Hard-truncate boundary = recent-N window (`created_turn >= current_turn - recent_turn_window + 1`), same as L3. `recent_turn_window` from `self.config.recent_turn_window`.
- When L3 never ran (`l3_service is None`), behavior is unchanged (early `_final_l3_failure`). Hard-truncate only runs after an L3 attempt failed.
- Persisted string values are renamed **in place** — no migration. Old session JSONL carrying old values is accepted as-is by this single-user project; reader and writer change together so `is_already_compacted` stays consistent.
- Deliberately unchanged: `_L2Candidate`, `l2_result_target_tokens`, `LlmCompactService`/`LlmCompactEvent`/`LlmCompactRequest`/`LlmCompactSummarizer` (LLM-flavored names, not L4-flavored).
- Repo formatter is **black** (line-length 200), not `ruff format`. Run `ruff check` and `black --check` (or `black`) on changed files.
- Commit message format: `{feat,fix,docs}: <concise imperative>` with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- After each task: `git add <explicit paths>`, never `git add -A`.

---

### Task 1: Rename `l4_*`/`L4*` code identifiers to `l3_*`/`L3*`

Mechanical rename of the LLM-summary step's identifiers so the code matches the new level scheme. No behavior change.

**Files:**
- Modify: `lanscoder/context/manager.py`, `lanscoder/context/llm_compact.py`, `lanscoder/context/runtime_replay.py`, `lanscoder/context/fallback.py`, `lanscoder/context/retry_policy.py`, `lanscoder/context/provider_summarizer.py`, `lanscoder/context/compaction.py` (comment at :355 only)
- Test: files referencing these symbols (grep to find; see Step 3)

**Interfaces:**
- Consumes: current identifiers as-is.
- Produces: renamed identifiers; Task 3 writes against the new `l3_*` names.

- [ ] **Step 1: Rename manager.py symbols**

In `lanscoder/context/manager.py`:
- `class L4Compactor(Protocol)` → `class L3Compactor(Protocol)` (line 43).
- `l4_service: L4Compactor | None = None` → `l3_service: L3Compactor | None = None` (line 93).
- `ContextCompactResult.l4_event: LlmCompactEvent | None` → `l3_event` (line 74).
- All `self.l4_service` references → `self.l3_service` (lines 185, 250, 296).
- All `l4_request=` / `l4_request.` local vars → `l3_request` (lines 211, 247, 260, 261, 264, 278, 279, 292).
- `_record_l4_event` → `_record_l3_event` (definition + call sites at 226, 362, 407, 475).
- `_final_l4_failure` → `_final_l3_failure` (definition + call sites at 192, 445, 450).
- `failure_reason="l4_service_missing"` → `"l3_service_missing"` (lines 198, 200).
- `result.l4_event` field accesses in `compact_if_needed` (lines 240, 376, 429, 494) → `result.l3_event`.
- Docstring line 89 "编排 L1-L4" → "编排 L1-L3".

Use find-replace across the file; there should be no remaining `l4`/`L4` after.

- [ ] **Step 2: Rename llm_compact, replay, fallback, retry, provider, compaction symbols**

- `lanscoder/context/llm_compact.py`: module docstring "L4 LLM compact" → "L3"; `class L4Source` → `class L3Source` (line 219); `_build_l4_source` → `_build_l3_source` (line 235 + call sites); `L4Source(` constructions → `L3Source(`; error messages "current L4 source" → "L3 source" (line 129), "only successful L4 candidates" → "L3 candidates" (line 204), "within current L4 input tail" → "L3 input tail" (lines 287, 291); docstrings "L4 summarizer" → "L3" (line 52), "L4 boundary" → "L3 boundary" (line 56), "L4 摘要" → "L3 摘要" (line 226), "stable L4 summary contract" → "L3" (line 427).
- `lanscoder/context/runtime_replay.py`: `_apply_l4_compaction` → `_apply_l3_compaction` (definition + call site at line 53).
- `lanscoder/context/fallback.py`: `FallbackAction` value `"retry_l4_stronger_summary"` → `"retry_l3_stronger_summary"` (line 8); `action_for` body uses it (line 35); module docstring "L4 失败" → "L3 失败".
- `lanscoder/context/retry_policy.py`: docstring "L4 compact" → "L3 compact" (line 1).
- `lanscoder/context/provider_summarizer.py`: docstrings "L4 checkpoint" → "L3" (lines 1, 24, 26), "L4 summarizer 协议" → "L3" (line 24).
- `lanscoder/context/compaction.py:355`: comment "ContextBuilder/L4" → "ContextBuilder/L3".

- [ ] **Step 3: Rename test symbols**

Run `grep -rn "l4_\|L4\|retry_l4" tests/ --include="*.py"` and rename every hit:
- `FakeL4`, `WritingFakeL4` → `FakeL3`, `WritingFakeL3` (test_context_window_manager.py).
- `l4_service=FakeL4(...)` / `l4_service=l4` / `manager.l4_service` → `l3_service=...` etc.
- `result.l4_event` → `result.l3_event`.
- `L4Source` references → `L3Source` (test_context_llm_compact.py, test_context_provider_summarizer.py).
- `_apply_l4_compaction` in test_context_runtime_replay.py → `_apply_l3_compaction`.
- `test_manager_runs_l4_only_after_l1_l3_fail_target` → `test_manager_runs_l3_only_after_l1_l2_fail_target`.

- [ ] **Step 4: Verify no stragglers**

Run: `grep -rn "l4_\|L4\|retry_l4" lanscoder tests --include="*.py" | grep -v __pycache__`
Expected: zero matches. (Exception: `LlmCompact*` names must remain — they contain "L" + "m", not "L4"; verify the grep pattern only hits `l4`/`L4`.)

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_context_window_manager.py tests/test_context_llm_compact.py tests/test_context_provider_summarizer.py tests/test_context_runtime_replay.py tests/test_context_circuit_breaker.py -q`
Then `pytest -q`. Baseline: 10 pre-existing environmental failures (trio/anthropic/mcp). Expected: no NEW failures beyond baseline.

- [ ] **Step 6: Commit**

```bash
git add lanscoder/context/manager.py lanscoder/context/llm_compact.py lanscoder/context/runtime_replay.py lanscoder/context/fallback.py lanscoder/context/retry_policy.py lanscoder/context/provider_summarizer.py lanscoder/context/compaction.py <changed test files>
git commit -m "refactor: rename LLM-summary identifiers l4 to l3"
```

---

### Task 2: Rename persisted string values to match the new level scheme

Changes the `compaction_state`/`compacted_by` metadata strings written into session JSONL. In-place rename; no migration.

**Files:**
- Modify: `lanscoder/context/compaction.py`, `lanscoder/context/content/detector.py`, `lanscoder/context/archive.py`, `lanscoder/context/content/build.py`, `lanscoder/context/content/search.py`, `lanscoder/context/content/json.py`, `lanscoder/context/content/html.py`, `lanscoder/context/content/compressors.py`, `lanscoder/context/content/code.py`, `lanscoder/context/content/diff.py`
- Test: `tests/test_context_content_router.py`, `tests/test_context_compaction_pipeline.py`, `tests/test_context_store.py`, `tests/test_context_archive.py`, `tests/test_context_content_detector.py`

**Interfaces:**
- Consumes: nothing new (string literals only).
- Produces: new string values; tests assert the new values.

- [ ] **Step 1: Rename the string values**

Apply this mapping across `lanscoder/context/` (and only there):

| Old | New |
|---|---|
| `"l2_route_compacted"` | `"l1_route_compacted"` |
| `"l3_archive"` | `"l2_archive"` |
| `"l2_build_output"` | `"l1_build_output"` |
| `"l2_search_results"` | `"l1_search_results"` |
| `"l2_json_array"` | `"l1_json_array"` |
| `"l2_json_object"` | `"l1_json_object"` |
| `"l2_html"` | `"l1_html"` |
| `"l2_current_task_cold"` | `"l1_current_task_cold"` |
| `"l2_source_code"` | `"l1_source_code"` |
| `"l2_git_diff"` | `"l1_git_diff"` |
| `"l2_route"` (default in `_l2_compacted_by`) | `"l1_route"` |

Exact locations:
- `compaction.py:269` (`compaction_state: "l2_route_compacted"`), `:432` (`value or "l2_route"`), `:642` (`state not in {"raw", "l2_route_compacted"}`).
- `content/detector.py:12` (`"l2_route_compacted"` in `COMPACTED_STATES`).
- `archive.py:148` (`compacted_by: "l3_archive"`).
- `content/build.py:61`, `content/search.py:62`, `content/json.py:67,97`, `content/html.py:64`, `content/compressors.py:45`, `content/code.py:62`, `content/diff.py:92`.

- [ ] **Step 2: Rename `_l2_compacted_by`**

`lanscoder/context/compaction.py:426`: `def _l2_compacted_by(value: object) -> str` → `def _l1_compacted_by(value: object) -> str`, its call site at line 270, and its docstring ("L2 ownership boundary" → "L1 ownership boundary"). Body: `label.startswith("l3_")` → `label.startswith("l2_")` and `return f"l2_{label[3:]}"` → `return f"l1_{label[3:]}"` (the function now translates the archive level's legacy labels into the route level's L1 labels — verify the direction against the call site and adjust if the semantics differ).

- [ ] **Step 3: Update tests**

Run `grep -rn "l2_route_compacted\|l3_archive\|l2_build_output\|l2_search_results\|l2_json\|l2_html\|l2_current_task_cold\|l2_source_code\|l2_git_diff\|_l2_compacted_by\|l2_route\"" tests/ --include="*.py"` and update every assertion to the new value.

- [ ] **Step 4: Verify no stragglers**

Run: `grep -rn "l3_archive\|l2_route_compacted\|_l2_compacted_by" lanscoder tests --include="*.py" | grep -v __pycache__`
Expected: zero matches. (Verify `l2_route_compacted` appears nowhere; the `l2_archive`/`l1_*` values are the new ones.)

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_context_content_router.py tests/test_context_compaction_pipeline.py tests/test_context_store.py tests/test_context_archive.py tests/test_context_content_detector.py -q`
Then `pytest -q`. Expected: no NEW failures beyond the 10 baseline.

- [ ] **Step 6: Commit**

```bash
git add lanscoder/context/compaction.py lanscoder/context/content/detector.py lanscoder/context/archive.py lanscoder/context/content/build.py lanscoder/context/content/search.py lanscoder/context/content/json.py lanscoder/context/content/html.py lanscoder/context/content/compressors.py lanscoder/context/content/code.py lanscoder/context/content/diff.py <changed test files>
git commit -m "refactor: align persisted compaction labels with l1/l2 levels"
```

---

### Task 3: Add the hard-truncate fallback

When L3 fails and all fallbacks fail, commit a deterministic placeholder checkpoint keeping the recent N turns verbatim.

**Files:**
- Modify: `lanscoder/context/fallback.py` (action), `lanscoder/context/provider_summarizer.py` (public boundary selector), `lanscoder/context/manager.py` (`_run_fallback` hard-truncate branch + `_hard_truncate`)
- Test: `tests/test_context_window_manager.py`, `tests/test_context_fallback*.py` (if present; else `test_context_window_manager.py`), plus a new test

**Interfaces:**
- Consumes: `recent_turn_window` from `ContextCompactionConfig` (exists), `_tail_boundary` + `_recent_turn_max_tail_start_index` + `_message_created_turn` from provider_summarizer (exist), `Checkpoint` + `CheckpointIndex` + `checkpoint_summary_content` from checkpoint.py, manager's existing checkpoint-commit path (`self.store.append_event` + `runtime_state.latest_checkpoint_id`).
- Produces: `CompactFallbackPolicy.action_for` returns `"hard_truncate"` as terminal; `provider_summarizer.select_compaction_boundary(messages, *, current_turn, recent_turn_window) -> tuple[str, str] | None` (covered_until, tail_start) or None when nothing is outside the window; `ContextWindowManager._hard_truncate(...)` returning `ContextCompactResult`.

- [ ] **Step 1: Add the terminal action**

In `lanscoder/context/fallback.py`:

```python
FallbackAction = Literal["stronger_programmatic", "retry_l3_stronger_summary", "hard_truncate"]
```

Change `action_for` (lines 31-36) so the default returns `hard_truncate` instead of `fail`:

```python
    def action_for(self, reason: str | None) -> FallbackAction:
        if reason == "prompt_too_long":
            return "stronger_programmatic"
        if reason in {"timeout", "no_summary"}:
            return "retry_l3_stronger_summary"
        return "hard_truncate"
```

Update the module docstring ("有限兜底策略" gains a note that `hard_truncate` is the deterministic last resort).

- [ ] **Step 2: Expose the boundary selector**

In `lanscoder/context/provider_summarizer.py`, add a public function wrapping the existing private boundary logic (place near `_tail_boundary`):

```python
def select_compaction_boundary(
    messages: list[AgentMessage],
    *,
    current_turn: int,
    recent_turn_window: int,
) -> tuple[str, str] | None:
    """Return (covered_until_message_id, tail_start_message_id) for hard truncation.

    Mirrors `_tail_boundary`: respects the recent-N window and tool-call sequence
    validity. Returns None when the whole conversation is inside the window (nothing
    to drop) or no valid boundary exists — the manager then falls through to failure.
    """

    try:
        boundary = _tail_boundary(
            messages,
            current_turn=current_turn,
            recent_turn_window=recent_turn_window,
        )
    except NoSummaryError:
        return None
    return boundary.covered_until_message_id, boundary.tail_start_message_id
```

- [ ] **Step 3: Add the manager hard-truncate branch**

In `lanscoder/context/manager.py`, add a method (near `_generate_validate_commit`). It reuses the existing `self.l3_service.commit_candidate(...)` path (which writes `checkpoint_created` + updates runtime state) and the established rebuild-from-store pattern, exactly like `_generate_validate_commit` does after a real L3 commit:

```python
    def _hard_truncate(
        self,
        *,
        request: ContextCompactRequest,
        view: SessionView,
        target_tokens: int,
        before_tokens: int,
        before_failure_count: int,
        trigger: ContextWindowTrigger,
        mode: ContextCompactMode,
    ) -> ContextCompactResult | None:
        """L3 与 fallback 全失败后的确定性兜底：近 N 轮保留，其余替换为占位摘要。

        Returns None when there is nothing outside the recent window (nothing to drop),
        so the caller falls through to the existing failure path.
        """

        messages = [message for message in view.messages if message.role != "system_meta"]
        boundary = select_compaction_boundary(
            messages,
            current_turn=request.current_turn,
            recent_turn_window=self.config.recent_turn_window,
        )
        if boundary is None:
            return None
        covered_until_message_id, tail_start_message_id = boundary

        checkpoint = Checkpoint(
            id="",
            session_id=view.session_id,
            summary="[Earlier dialogue truncated — recent N turns kept]",
            tail_start_message_id=tail_start_message_id,
            covered_until_message_id=covered_until_message_id,
            source_fingerprint=session_view_fingerprint(view),
            sequence=max((existing.sequence for existing in view.checkpoints), default=0) + 1,
            metadata={
                "created_by": "hard_truncate",
                "recent_turn_window": self.config.recent_turn_window,
            },
        )
        candidate = LlmCompactCandidate(
            checkpoint=checkpoint,
            event=LlmCompactEvent(
                status="success",
                source_fingerprint=checkpoint.source_fingerprint,
                checkpoint_id=checkpoint.id,
            ),
        )
        self.l3_service.commit_candidate(candidate, runtime_state=request.runtime_state)
        rebuilt_view = self.store.rebuild_session_view(request.view.session_id)
        after_tokens = request.estimate_budget(rebuilt_view).input_tokens
        if after_tokens >= target_tokens:
            return None
        self._record_l3_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=candidate.event,
        )
        self._record_auto_success_if_needed(request=request, mode=mode)
        return ContextCompactResult(
            status="success",
            reason="hard_truncate",
            view=rebuilt_view,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            programmatic_event=None,
            l3_event=candidate.event,
        )
```

This mirrors `_generate_validate_commit`'s commit pattern (`commit_candidate` → `rebuild_session_view` → `estimate_budget`), so it needs no new imports beyond `select_compaction_boundary` from `lanscoder.context.provider_summarizer` (add to the import block). `LlmCompactCandidate`, `LlmCompactEvent`, `Checkpoint`, `session_view_fingerprint`, `ContextWindowTrigger`, `ContextCompactMode` are already imported in manager.py.

Call it from `_run_fallback` (Step 4) passing the local already-converted `trigger` (a `ContextWindowTrigger`, converted at the top of `compact_if_needed`) and `mode`.

- [ ] **Step 4: Wire into `_run_fallback` — both failure sites**

`_run_fallback` has **two** bare-failure return sites that must attempt hard-truncate first, because the spec's "when L3 and its fallbacks all fail" covers both:
1. the `retry_l3_stronger_summary` block's failure return (around lines 422-432), and
2. the terminal block (around lines 445-457).

Add a small helper on `ContextWindowManager` that both sites call, so hard-truncate is the universal last resort:

```python
    def _final_l3_failure_or_hard_truncate(
        self,
        *,
        request: ContextCompactRequest,
        trigger: ContextWindowTrigger,
        mode: ContextCompactMode,
        target_tokens: int,
        view: SessionView,
        before_tokens: int,
        before_failure_count: int,
        programmatic: CompactionResult,
        after_tokens: int,
        event: LlmCompactEvent,
        reason: str,
        fallback_steps: list[dict[str, object]] | None = None,
    ) -> ContextCompactResult:
        """Try the deterministic hard truncate; if nothing is droppable, fail."""

        truncated = self._hard_truncate(
            request=request,
            view=view,
            target_tokens=target_tokens,
            before_tokens=before_tokens,
            before_failure_count=before_failure_count,
            trigger=trigger,
            mode=mode,
        )
        if truncated is not None:
            return truncated
        return self._final_l3_failure(
            request=request,
            trigger=trigger,
            mode=mode,
            target_tokens=target_tokens,
            programmatic=programmatic,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            before_failure_count=before_failure_count,
            event=event,
            reason=reason,
            fallback_steps=fallback_steps,
        )
```

Then:
- **Terminal block (site 2)**: replace the `return self._final_l3_failure(...)` at lines 445-457 with:

```python
        return self._final_l3_failure_or_hard_truncate(
            request=request,
            trigger=trigger,
            mode=mode,
            target_tokens=target_tokens,
            view=current_programmatic.view,
            before_tokens=before_tokens,
            before_failure_count=before_failure_count,
            programmatic=programmatic,
            after_tokens=outcome.input_tokens,
            event=_with_fallback(outcome.event, fallback_steps=steps, final_failure_reason=reason),
            reason=reason,
            fallback_steps=steps,
        )
```

- **Retry block (site 1)**: in the `if action in {"stronger_programmatic", "retry_l3_stronger_summary"}` branch, on the failure path (around lines 410-432, where `retry.event.status != "success"`), replace the inline failed `ContextCompactResult` return with the same helper:

```python
            if retry.event.status != "success":
                return self._final_l3_failure_or_hard_truncate(
                    request=request,
                    trigger=trigger,
                    mode=mode,
                    target_tokens=target_tokens,
                    view=current_programmatic.view,
                    before_tokens=before_tokens,
                    before_failure_count=before_failure_count,
                    programmatic=programmatic,
                    after_tokens=retry.input_tokens,
                    event=_with_fallback(retry.event, fallback_steps=steps, final_failure_reason=final_reason),
                    reason=final_reason or "failed",
                    fallback_steps=steps,
                )
```

Note `current_programmatic` is `programmatic` unless a stronger programmatic pass already ran (it is reassigned at line 355), so `view=current_programmatic.view` is correct in both sites.

**FallbackStep for truncation**: `_hard_truncate` returns the `ContextCompactResult` directly. To record the truncation in the event trail, in `_hard_truncate` (Step 3), after computing `after_tokens`, append a `FallbackStep` with `action="hard_truncate"`, `status="success"` to `candidate.event.fallback_steps` — see Step 5 note; alternatively record it via `_record_l3_event`'s event. The exact placement is the implementer's call; the requirement is the event trail shows `hard_truncate` succeeded.

- [ ] **Step 5: Add tests**

In `tests/test_context_window_manager.py`:
- New test: `test_fallback_policy_returns_hard_truncate_for_unknown_reason` — construct `CompactFallbackPolicy()` and assert `action_for("provider_error") == "hard_truncate"`, `action_for("prompt_too_long") == "stronger_programmatic"`, `action_for("no_summary") == "retry_l3_stronger_summary"`.
- New test: `test_manager_hard_truncates_when_l3_and_fallbacks_fail` — build a manager whose `l3_service` returns a failed candidate and whose fallback can't recover; assert the result `reason == "hard_truncate"`, `status == "success"`, the truncated view keeps recent-N messages and the checkpoint summary equals the placeholder text, and a `checkpoint_created` event was written.
- New test: `test_manager_hard_truncate_returns_none_when_all_recent` — a session fully inside the recent window; assert `_hard_truncate` returns None (or the fallback returns the existing failure, not hard_truncate).

Add a test in `tests/test_context_provider_summarizer.py` for `select_compaction_boundary` returning the same boundary as `_tail_boundary` and returning None for a fully-recent conversation.

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_context_window_manager.py tests/test_context_provider_summarizer.py -q`
Then `pytest -q`. Expected: no NEW failures beyond the 10 baseline.

- [ ] **Step 7: Commit**

```bash
git add lanscoder/context/fallback.py lanscoder/context/provider_summarizer.py lanscoder/context/manager.py tests/test_context_window_manager.py tests/test_context_provider_summarizer.py
git commit -m "feat: hard-truncate fallback with placeholder checkpoint"
```

---

### Task 4: Bump compaction strategy version and final sweep

Reflects the persisted-string change and new fallback action.

**Files:**
- Modify: `lanscoder/context/versions.py`

**Interfaces:**
- Consumes: all prior tasks.

- [ ] **Step 1: Bump version**

In `lanscoder/context/versions.py`, change `COMPACTION_STRATEGY_VERSION = "v3"` to `"v4"`.

- [ ] **Step 2: Run full suite + linter**

Run: `pytest -q`
Run: `ruff check lanscoder tests && black --check lanscoder tests`
Expected: all PASS (10 pre-existing environmental failures only).

- [ ] **Step 3: Commit**

```bash
git add lanscoder/context/versions.py
git commit -m "chore: bump compaction strategy version to v4"
```

(Note: `docs/` is gitignored; no spec/plan file is committed.)
