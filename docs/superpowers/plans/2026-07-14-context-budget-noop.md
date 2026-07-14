# Context Budget and No-Op Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop repeated no-op auto-compaction and assess compaction pressure from the actual provider request.

**Architecture:** Add a provider-request token estimator at the context boundary, pass it into the manager's trigger decision, and make no-effect compaction a non-persisted skipped result. Re-check the effective context after an L4 checkpoint before reporting success.

**Tech Stack:** Python, pytest, existing FirstCoder context and provider types.

---

### Task 1: Provider request budget

**Files:**
- Modify: `firstcoder/context/token_budget.py`
- Modify: `firstcoder/context/triggers.py`
- Test: `tests/test_context_triggers.py`

- [ ] **Step 1: Write failing tests** for system messages, provider messages, tool definitions, and output reserve contributing to estimated request tokens.
- [ ] **Step 2: Run the focused trigger tests** and confirm the new assertions fail.
- [ ] **Step 3: Implement the request estimator** and optional trigger estimate override.
- [ ] **Step 4: Run the focused trigger tests** and confirm they pass.

### Task 2: No-effect compaction result

**Files:**
- Modify: `firstcoder/context/manager.py`
- Test: `tests/test_context_window_manager.py`

- [ ] **Step 1: Write a failing test** that calls auto compaction twice against an unchanged no-op fingerprint and expects `skipped_no_effect` with only one persisted completion event.
- [ ] **Step 2: Run the focused manager test** and confirm it fails.
- [ ] **Step 3: Implement the no-effect return path** without recording a successful automatic compaction.
- [ ] **Step 4: Run the focused manager test** and confirm it passes.

### Task 3: L4 post-check

**Files:**
- Modify: `firstcoder/context/manager.py`
- Test: `tests/test_context_window_manager.py`

- [ ] **Step 1: Write a failing test** where checkpoint creation succeeds but rebuilt effective tokens remain above the target.
- [ ] **Step 2: Run the focused manager test** and confirm it fails.
- [ ] **Step 3: Return `still_over_budget` instead of success** in that case.
- [ ] **Step 4: Run focused context tests and the full test suite.**
