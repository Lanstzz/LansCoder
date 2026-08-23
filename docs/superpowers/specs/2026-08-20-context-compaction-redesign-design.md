# Context Compaction Redesign: Budget-Driven Dialogue Summarization

Status: draft
Date: 2026-08-20
Author: Lanster (with Claude)

## 1. Problem

`TASK_HASH_CHANGED` is the only compaction path that forces L1 to blank old-task
dialogue regardless of budget. The task-boundary classifier (a hidden LLM call per
user message) treats ordinary Q&A as repeated task switches, so each confirmed switch
wipes the previous task's dialogue to empty string. `/recall` then shows `(empty message)`
for those turns. Real evidence: a session with 4 user messages (~9K tokens vs a
454K target) had turns 1-3 blanked right after a confirmed switch.

The display side is already fixed (`fix: show original text for compacted turns in /recall`).
This spec addresses the underlying aggressiveness: compaction should be budget-driven,
matching how Claude Code and Codex operate.

## 2. Design goals

- Compaction is pure context-window management: triggered only by budget pressure,
  provider prompt-too-long, or explicit user action. Task switches never trigger it.
- Old-task dialogue is not blanked. Old *turn* dialogue is summarized by the LLM when
  the budget demands it; the most recent N turns are preserved verbatim.
- Free deterministic layers handle tool results; the paid LLM layer handles dialogue.
- Remove the task-hash / task-boundary classifier system entirely.

## 3. Trigger surface

`ContextWindowTrigger` is reduced to:

- `AUTO` — after each user message, when input tokens cross the high watermark
  (unchanged: high = 90% of input capacity, low = 72%, `token_budget.py:60-61`).
- `PROMPT_TOO_LONG` — provider reports prompt too long.
- `MANUAL` — user-invoked compaction.

`TASK_HASH_CHANGED` is removed. All downstream special-casing is deleted:

- `manager.py:141` `required_levels=("l2","l3")` on task switch.
- `manager.py:153` and `manager.py:340` `force_old_task_compaction=True`.
- `manager.py:568-569` `_target_tokens` TASK_HASH_CHANGED branch (target = low×2/3).
- `loop.py` four `_compact_after_task_hash_changed()` call sites (621/634/999/1059)
  and the `_compact_after_task_hash_changed` method itself.
- `tool_execution.py:94` `task_hash_changed` field and its trigger at `:476-478`.
- `task_boundary_classifier.py:138-139` `_compact_if_needed(TASK_HASH_CHANGED)`.

`force_route_current_text` behavior for `MANUAL`/`PROMPT_TOO_LONG` is unchanged.

## 4. New level definitions

The pipeline becomes three levels. Ordering stays cheap-first, paid-last.

| Level | Former | Target | Product | Cost |
|---|---|---|---|---|
| L1 | L2 | Consumed DERIVED tool results | Route-compressed preview | Free, deterministic |
| L2 | L3 | STALE/SUPERSEDED/DUPLICATE / over-target DERIVED tool results | Archived placeholder | Free, deterministic |
| L3 | L4 | Old-turn dialogue (outside recent-N window) | LLM summary | Paid |

Former L1 (old-task dialogue blanking via `compact_old_task_part` → `content=""`) is
deleted. Its machinery is removed:

- `compaction.py` `_apply_l1`, `_is_cold_old_task_part`, `is_old_task_part`
  (`detector.py`), `compact_old_task_part` (`compressors.py`).
- `CompactionPipeline.cold_turn_distance` (and its config default `triggers.py:22`).

L2/L3 application logic is moved verbatim (rename only); L4 logic moves into L3 with
the recent-N constraint added (section 5) and the dialogue summary prompt (section 6).

## 5. Recent-N preservation

Align with Claude Code's `KEEP_RECENT` semantics (10 messages); here it is 10 *turns*,
where one turn = one user message plus all agent activity until the next user message.
Turn numbers already exist on every part as `created_turn`/`turn_id` (written by
`context/writer.py`, incremented only in `append_user_message`).

- New config: `recent_turn_window: int = 10` (default 10, configurable).
- Summary boundary constraint (enforced in `_validate_summary_boundary` or the L3
  caller):
  - Keeping N recent turns means the retained window is turns `[T - N + 1, T]`
    (T = `current_turn`), so `tail_start_message_id` must be the first message whose
    `created_turn >= T - N + 1`. This starts the tail at the beginning of turn
    `T - N + 1`.
  - If the boundary turn contains an unconsumed tool transaction, `tail_start` shifts
    to just after it (reuse `_earliest_unconsumed_transaction_index`; retained turns then
    exceed N, which is acceptable).
- Covered region = messages from `covered_until_message_id` up to but not including
  `tail_start`.

## 6. Dialogue summary prompt

New dialogue-level prompt, distinct from the existing task-level checkpoint prompt.
The L3 summary must preserve: gist of user requests, conclusions already given,
unfinished/open items, key constraints and preferences. It must drop: tool-call
detail, intermediate reasoning, redundant content. Exact wording is drafted during
implementation with a fixture-backed test.

## 7. Removal of the task-hash system

Delete the entire task-hash / task-boundary subsystem; it exists only to drive the
removed compaction path and to tag metadata that nothing consumes after this change.

- `agent/task_boundary_classifier.py` — whole file.
- `tools/task_boundary.py` — tool; remove registration from
  `tools/session_registry.py` and hidden-tool handling.
- `context/task_boundary.py` — hash generation + observation state machine.
- `context/runtime_state.py` — `candidate_task_hash`, `candidate_task_basis_message_id`,
  `task_hash_stable_count`, `observe_task_hash_candidate`.
- `context/runtime_replay.py:64-77` — replay branch for `task_boundary_observed`.
- `context/inspector.py` + `app/commands.py:123-124` — `active_task_hash` /
  `candidate_task_hash` display fields.
- `session.py` — `_current_context_metadata` task_hash tagging, `_append_task_boundary_observation_if_present`.
- `loop.py` — `_initialize_active_task_if_missing`, `_classify_task_boundary(_async)`,
  `_tag_message_parts_with_task_hash`, `_tag_task_boundary_messages_with_active_hash`.
- `agent/subagent.py:100/381/444` — `parent_task_hash` field.
- `tools/delegate.py` — `parent_task_hash` parameter.
- `context/writer.py` — `append_task_boundary_observation` (if unused elsewhere).

Task plan subsystem (`task_create`/`task_update`/`task_list`/`task_revise`) is
independent of task hash and is untouched.

Benefit beyond simplification: removes one hidden LLM call per user message.

## 8. Versioning and compatibility

- Bump `COMPACTION_STRATEGY_VERSION` (level semantics changed).
- No migration of old session data (single-user project). Old events whose
  `levels_attempted` carry former layer names must render without crashing; map to new
  semantics or display verbatim.

## 9. Testing

Existing tests to update:

- `test_context_compaction_pipeline.py` — L1 blanking cases (`test_l1_*`,
  `test_l1_task_switch_immediately_trims_old_dialogue*`), `cold_turn_distance` config.
- `test_context_task_boundary.py`, `test_task_boundary_tool.py` — removed subsystem.
- `test_context_runtime_replay.py` — removed replay branch.
- `test_agent_tool_flow.py` — `task_hash_changed` trigger paths.

New tests:

- Recent-N window constraint: tail_start lands at `created_turn == T - N`; unconsumed
  tool transaction shifts tail_start past it.
- AUTO trigger does not blank old-task dialogue (regression for the original bug).
- L3 dialogue summary uses the dialogue prompt and produces readable output.
- Trigger reduction: TASK_HASH_CHANGED no longer exists; MANUAL/PROMPT_TOO_LONG
  unaffected.

## 10. Open items

- Exact wording of the dialogue summary prompt (section 6) — drafted at implementation.
- `recent_turn_window` lands alongside the other compaction tunables in
  `context/triggers.py` (`CompactionConfig`).
