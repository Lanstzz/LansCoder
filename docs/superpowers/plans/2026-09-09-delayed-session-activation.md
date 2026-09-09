# Delayed Primary Session Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent an untouched LansCoder primary session from creating durable session, catalog, or trace records until the first meaningful operation requires persistence.

**Architecture:** Keep low-level `AgentSession.create()` immediate for direct SDK and child-session callers, and add an explicit provisional-primary path at the CLI/TUI composition boundary. The provisional runtime fully owns tools, permissions, memory, branch metadata, and pending command state in memory; `activate()` is the sole idempotent operation that writes `session.created`. The first user turn activates before creating its trace or writing its user message. `/new` and `/rename` update provisional metadata without persisting an empty session; `/resume`, `/fork`, and `/observe` continue to operate only on durable sessions or global observatory state.

**Tech Stack:** Python 3.11, dataclasses, existing JSONL journal/session writer, pytest, Ruff; no new dependencies.

**Spec:** `handoff.md` delayed primary-session decision and the session lifecycle rules in `docs/superpowers/specs/2026-09-06-local-observatory-design.md`.

## Global Constraints

- Only primary sessions created through the CLI/TUI composition path use delayed activation; `AgentSession.create()` remains immediate for direct SDK and authorized child-session semantics.
- An untouched provisional runtime creates no `sessions/<id>.jsonl`, no primary catalog record, no trace, and no journal event on close.
- `activate()` is the only root materialization entry point and is idempotent.
- The first durable event remains `session.created` at sequence 1, with `branch_id == root_branch_id`.
- The first user turn activates before `_trace_scope()`, trace start, context rebuild that requires a persisted branch, or message persistence; its durable order is `session.created` → `trace.started` → user message.
- `/new <title>` and `/rename <title>` only stage title metadata while the current runtime is provisional; title is included in the eventual `session.created` metadata.
- Resume and fork accept only existing current-project primary sessions. A provisional ID is not treated as an existing session and cannot be materialized by resume/fork.
- `/observe` for a provisional runtime opens the global Explorer without a nonexistent-session filter; a durable runtime keeps the existing active/recent/session-filter deep-link priority.
- Session/context required journal writes remain fail-closed; recorder, payload, index, and Web failures remain fail-open.
- Preserve child/subagent immediate roots, background trace behavior, branch context invariants, permission pause/resume, and existing direct SDK behavior.
- Do not add dependencies, change journal schema, change recorder semantics, submit commits, push, or create a PR.

### Task 1: Establish delayed-activation RED contracts

**Files:**
- Create or modify: `tests/test_delayed_session_activation.py`
- Modify only if needed for shared test fixtures: existing test utility files, without changing production code

**Interfaces:**
- Consumes the current `create_agent_session`, `SessionBootstrap`, `AgentSession`, command handler, and observatory APIs.
- Produces executable failing behavior contracts for Tasks 2 and 3; no production API is prescribed beyond the observable lifecycle behavior.

- [ ] **Step 1: Add the untouched-runtime contract.** Build a temporary storage root and fake provider, create the CLI/TUI composition runtime, assert that no session journal exists before input and after clean shutdown, and assert that the catalog and trace index expose nothing.
- [ ] **Step 2: Add the first-turn ordering contract.** Run one fake-provider user turn and assert exactly one root `session.created`, sequence one/root branch identity, and the persisted ordering `session.created` before `trace.started` before the user `message.appended` event.
- [ ] **Step 3: Add provisional command contracts.** Exercise `/new <title>` and `/rename <title>` on a provisional runtime; assert no journal/catalog record is created until the first user turn, then assert the title is present in the eventual root metadata and no standalone empty-session metadata event was written.
- [ ] **Step 4: Add access and observatory contracts.** Assert `/resume <provisional-id>` and `/fork` from a provisional runtime do not materialize it, and assert `/observe` builds a global Explorer URL without `session_id` when the runtime has no durable root.
- [ ] **Step 5: Add lifecycle edge contracts.** Cover activation idempotence, activation failure cleanup, permission pause/restart, child-session immediate persistence, and background dispatch branch context at the observable boundary. Keep tests fake-provider/local-only.
- [ ] **Step 6: Run the focused tests and record RED evidence.** Run `venv/bin/python -m pytest -q tests/test_delayed_session_activation.py`; expected result is failure in the new assertions because current construction writes `session.created` immediately. Fix only test setup errors until the failures are feature failures, then write the RED command/output to the task report.

### Task 2: Implement provisional primary runtime and first-turn materialization

**Files:**
- Modify: `lanscoder/session/bootstrap.py`
- Modify: `lanscoder/agent/session.py`
- Modify: `lanscoder/core/session.py`
- Modify: `lanscoder/core/runtime.py`
- Modify: `lanscoder/app/factory.py`
- Test: `tests/test_delayed_session_activation.py` and focused existing core/runtime tests

**Interfaces:**
- Consumes Task 1 RED contracts.
- Produces an explicit provisional primary construction path with an idempotent activation operation, while preserving immediate `AgentSession.create()` for direct/child callers.

- [ ] **Step 1: Add the smallest provisional construction seam.** Refactor only the composition path so it can assemble an `AgentSession` runtime without calling the root-writing operation; retain all existing tool, permission, memory, writer, and branch dependencies.
- [ ] **Step 2: Add the activation operation.** Implement one idempotent activation method that writes the root metadata exactly once, establishes the root branch context, and fails closed without leaving a partially attached branch context if root persistence fails.
- [ ] **Step 3: Activate at the first durable turn boundary.** In `AgentChatRunner`, activate before obtaining the branch-dependent trace scope and before starting the root trace; ensure the user message is appended only after activation and that an already durable/resumed session is unaffected.
- [ ] **Step 4: Run the focused RED tests to GREEN.** Run `venv/bin/python -m pytest -q tests/test_delayed_session_activation.py tests/test_core_session.py`; adjust implementation, not assertions, until all relevant tests pass.
- [ ] **Step 5: Run style and regression checks for touched Python files.** Run `venv/bin/python -m ruff check` on touched files, `venv/bin/python -m ruff format --check` on touched files, and the relevant runtime/session test modules. Record exact output in the report.

### Task 3: Wire command and observatory provisional boundaries

**Files:**
- Modify: `lanscoder/app/session_commands.py`
- Modify: `lanscoder/session/new.py`
- Modify: `lanscoder/app/observe_commands.py`
- Modify: `lanscoder/app/memory_commands.py` only if its current writer access requires a durable root
- Modify: `lanscoder/app/commands.py` only if context commands need a provisional-safe view
- Test: `tests/test_app_session_commands.py`, `tests/test_app_factory.py`, `tests/test_observability_runtime_lifecycle.py`, and the Task 1 contract file

**Interfaces:**
- Consumes Task 2 provisional runtime and activation interfaces.
- Produces command behavior where staging metadata or opening observation does not accidentally create a root; durable operations still use `SessionAccessPolicy`.

- [ ] **Step 1: Make `/new` replace the current runtime provisionally.** Do not query the catalog for an unactivated result; return an in-memory session result/action and preserve staged title.
- [ ] **Step 2: Make `/rename` provisional-safe.** Update staged metadata in memory when no root exists; retain the existing journal metadata update for durable sessions.
- [ ] **Step 3: Make `/observe` provisional-safe.** Detect the absence of a persisted root and omit the session filter; retain active/recent trace deep links for durable sessions.
- [ ] **Step 4: Audit context, memory, share, resume, fork, and shutdown paths.** Ensure read-only operations do not materialize a provisional runtime, while operations that genuinely require persistence activate through the single seam or return the existing clear error.
- [ ] **Step 5: Run focused command/observatory tests and style checks.** Run the named test modules, Ruff check, format check, compileall for touched modules, and `git diff --check`; append evidence to the report.

### Task 4: Full lifecycle regression and final review gate

**Files:**
- Modify only tests or production files required by review findings from Tasks 2–3
- Test: all delayed-session tests plus affected session, runtime, CLI, background, fork, resume, and observability suites

**Interfaces:**
- Consumes the completed provisional activation and command boundary behavior.
- Produces evidence that the refactor preserves existing durable-session, child-session, branch, permission, background, and observatory semantics.

- [ ] **Step 1: Run the narrow cross-layer suite.** Run the delayed-session contract file together with `tests/test_session_resume_service.py`, `tests/test_session_fork.py`, `tests/test_app_session_commands.py`, `tests/test_app_factory.py`, and the observability runtime lifecycle/integration modules.
- [ ] **Step 2: Run repository quality checks.** Run the full pytest suite, Ruff check, format check for all modified Python files, compileall, and `git diff --check`. If loopback restrictions affect Web tests, record the exact environmental limitation and rerun only with the permitted local configuration.
- [ ] **Step 3: Dispatch the final whole-branch reviewer.** Supply the complete branch diff, ledger, contracts, and quality evidence. Resolve every Critical/Important finding through the prescribed implementer/re-review loop; do not fix findings in the orchestrator.
- [ ] **Step 4: Verify repository scope.** Confirm `git status --short` contains only this task's intended files and that no `.superpowers/` artifacts, runtime data, virtual environment files, secrets, commits, pushes, or PRs were added.

## Review and TDD Gates

- Task 1 is test-only and must demonstrate expected RED failures before any production edit.
- Each implementation task is reviewed by an independent read-only subagent before the next task proceeds.
- Critical/Important findings enter the five-round maximum fix/re-review loop; Minor findings are recorded in the ledger for final triage.
- The orchestrator does not implement production fixes and no subagent may create or dispatch another task/thread.
- Every dispatched subagent uses `gpt-5.6-terra` with `high` reasoning and receives an explicit no-subagents/no-commit-or-push boundary.
