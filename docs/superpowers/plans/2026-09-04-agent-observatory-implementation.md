# Agent Observatory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, redaction-aware Observatory that durably captures one LansCoder root run and its permission recovery, child agents, and background work for evidence-driven diagnosis.

**Architecture:** Add an `lanscoder.observability` package with dataclass contracts, a lock-protected JSONL/file store, deterministic redaction, context propagation, recorder adapters, and a localhost Web server. Existing runtime boundaries receive narrow observation hooks and continue unchanged when observation fails. The TUI opens the Web workbench through `/observe [run-id]`; no evaluation, scoring, experiment, case, or promotion subsystem is introduced.

**Tech Stack:** Python 3.11, dataclasses, `contextvars`, `threading.Lock`, JSONL and atomic files, standard-library `http.server`/`webbrowser`, existing Textual command routing, pytest, and ruff. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-04-agent-observatory-design.md`

## Global Constraints

- Persist local raw evidence in plaintext under `<project>/.lanscoder/observatory`; SHA-256 provides integrity only.
- Use one in-process `threading.Lock` for sequence allocation and every store mutation; do not add SQLite, cloud services, queues, auth, DLP, or distributed tracing.
- Collection is observational: it must not alter provider requests, tools, permissions, retries, cancellations, or normal outcomes.
- A missing observation is `incomplete`, never zero, success, or no-risk.
- Default UI/API projections are redacted. Raw reveal is localhost-only, explicit click initiated, response-only with `Cache-Control: no-store`, and never prefetched or reused by export code.
- The current implementation contains only Observatory capabilities. Do not add interfaces or adapters for a future evaluation system.
- Run commands through `venv/bin/python`; run ruff on every changed Python file. Do not commit unless the user separately requests a commit.

## File Structure

| File | Responsibility |
| --- | --- |
| `lanscoder/observability/__init__.py` | Public Observatory exports. |
| `lanscoder/observability/models.py` | Trace, run, pending, and derived-fact dataclasses and status literals. |
| `lanscoder/observability/store.py` | Atomic raw/event writes, indexes, pending map, recovery, and the single lock. |
| `lanscoder/observability/redaction.py` | Deterministic redacted JSON projection and exportability result. |
| `lanscoder/observability/context.py` | Immutable `TraceContext`, contextvar scope, and root/resume/child registry. |
| `lanscoder/observability/recorder.py` | Converts runtime facts to trace events and isolates sink failures. |
| `lanscoder/observability/session_adapter.py` | Converts persisted session facts and context reports to events. |
| `lanscoder/observability/web.py`, `static/` | Local page, redacted APIs, and explicit raw reveal endpoint. |
| `lanscoder/context/events.py`, `writer.py` | Post-persist session-event observer protocol and dispatch. |
| `lanscoder/agent/observer.py`, `loop.py`, `permission_resume.py` | Root lifecycle, provider-request, tool, and waiting facts. |
| `lanscoder/agent/tool_execution.py`, `background.py`, `subagent_engine.py` | Child/background context propagation and terminal linkage. |
| `lanscoder/core/runtime.py`, `core/session.py`, `app/factory.py` | Compose Observatory and retain context through resume. |
| `lanscoder/app/observe_commands.py`, `help_commands.py` | `/observe [run-id]` command and help registration. |

### Task 1: Contracts, durable store, redaction, and context registry

**Files:**
- Create: `lanscoder/observability/__init__.py`, `models.py`, `store.py`, `redaction.py`, `context.py`
- Test: `tests/test_observability_store.py`, `tests/test_observability_redaction.py`, `tests/test_observability_context.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class TraceContext:
    run_id: str
    root_run_id: str
    parent_run_id: str | None
    parent_event_id: str | None
    session_id: str
    turn_id: str
    background_job_id: str | None = None

class TraceStore:
    def start_run(self, context: TraceContext, *, kind: str) -> None: ...
    def append(
        self,
        context: TraceContext,
        *,
        kind: str,
        raw_payload: Mapping[str, object],
        safe_summary: Mapping[str, object],
        parent_event_id: str | None = None,
    ) -> TraceEvent: ...
    def transition_run(
        self, run_id: str, status: RunStatus, *, incomplete_reason: str | None = None
    ) -> None: ...
    def bind_pending(self, request_id: str, run_id: str) -> None: ...
    def run_for_pending(self, request_id: str) -> str | None: ...
    def clear_pending(self, request_id: str, *, terminal_run_id: str) -> None: ...
    def run_for_background_job(self, job_id: str) -> RunRecord | None: ...

class TraceContextRegistry:
    def start_root(self, *, session_id: str, turn_id: str, kind: str) -> TraceContext: ...
    def resume(self, *, session_id: str, request_id: str) -> TraceContext | None: ...
    def start_child(
        self,
        *,
        parent: TraceContext,
        session_id: str,
        parent_event_id: str,
        kind: str,
        background_job_id: str | None = None,
    ) -> TraceContext: ...
```

- [ ] **Step 1: Write failing storage and context tests.**

  Cover pending mapping persistence across a new `TraceStore` instance; unique monotonic sequences for concurrent appends; `clear_pending` rejecting a non-matching terminal run; child records preserving immutable root/parent/background IDs; and `resume` rejecting a request mapped to another session.

- [ ] **Step 2: Write failing redaction and recovery tests.**

  Assert raw payload retains a secret while `safe_summary` masks it; nested keys, free text, tool output, attachments, token strings, connection strings, and private-key blocks are redacted; a redaction failure returns `exportable=False`; recovery retains orphan payload/event evidence and marks the associated run incomplete; a persisted running background child marks both child and root incomplete with `process_restarted_while_background_running`.

- [ ] **Step 3: Run the focused tests and confirm they fail.**

  Run: `venv/bin/python -m pytest tests/test_observability_store.py tests/test_observability_redaction.py tests/test_observability_context.py -q`

  Expected: collection errors because the package and contracts do not exist.

- [ ] **Step 4: Implement the minimal contracts and store.**

  Store events in `events/<run_id>.jsonl`, raw payloads in `payloads/<event_id>.json`, run metadata in `runs.json`, and pending mappings in `pending.json`. Under the single lock, write canonical JSON UTF-8 to a same-directory temporary file, `fsync`, calculate SHA-256, atomically rename, append and `fsync` the event JSONL, then atomically replace indexes. `clear_pending` raises `ValueError` unless the mapping points to `terminal_run_id`. Recovery removes only unreferenced temporary files and preserves orphan evidence while adding an incomplete reason.

- [ ] **Step 5: Implement deterministic redaction and context registry.**

  Define `RedactionPolicy(version=1)` with key-name and token/connection-string/private-key patterns. Return a redacted projection plus `exportable`; never mutate the raw payload. `TraceContextRegistry.resume` accepts only an existing run with the requested session ID and reports `None` for missing or conflicting mappings.

- [ ] **Step 6: Verify the task.**

  Run: `venv/bin/python -m pytest tests/test_observability_store.py tests/test_observability_redaction.py tests/test_observability_context.py -q && venv/bin/python -m ruff check lanscoder/observability tests/test_observability_store.py tests/test_observability_redaction.py tests/test_observability_context.py`

  Expected: all focused tests pass and ruff reports no findings.

### Task 2: Persisted session, plan, compaction, and context adapters

**Files:**
- Modify: `lanscoder/context/events.py`, `lanscoder/context/writer.py`, `lanscoder/agent/session.py`, `lanscoder/agent/loop.py`
- Create: `lanscoder/observability/session_adapter.py`
- Test: `tests/test_observability_session_adapter.py`, `tests/test_observability_context_capture.py`

**Interfaces:**

```python
class SessionEventObserver(Protocol):
    def on_session_event(self, event: SessionEvent) -> None: ...

class ObservatorySessionAdapter:
    def on_session_event(self, event: SessionEvent) -> None: ...
    def on_context_inspection(
        self,
        context: TraceContext,
        report: ContextInspectionReport,
        prepared: PreparedMainRequest,
    ) -> None: ...
```

- [ ] **Step 1: Write failing adapter tests.**

  Assert observers run only after `SessionEventWriter.append_event` persists `task_plan_updated`, each compaction event type, metadata, and checkpoint facts. Add a throwing observer and assert the session event remains persisted. Assert a context inspection event contains the post-compaction report and the exact prepared request that will be sent.

- [ ] **Step 2: Run the focused tests and confirm they fail.**

  Run: `venv/bin/python -m pytest tests/test_observability_session_adapter.py tests/test_observability_context_capture.py -q`

  Expected: failures because the observer protocol and adapter are absent.

- [ ] **Step 3: Add post-persist observer dispatch.**

  Add an optional immutable observer tuple to `SessionEventWriter`. Call each observer only after append succeeds and catch each observer exception independently. Add a narrow registration method to `AgentSession` so new, resumed, and child sessions receive the same adapter.

- [ ] **Step 4: Capture the final context view.**

  After `_prepare_main_provider_request` completes compaction and builds the final request, call `ContextInspector.inspect(view, runtime_state, budget=budget)` and send its report plus the exact `PreparedMainRequest` to the adapter. The adapter observes persisted events and reports only; it never reads or mutates private session state.

- [ ] **Step 5: Verify the task.**

  Run: `venv/bin/python -m pytest tests/test_observability_session_adapter.py tests/test_observability_context_capture.py tests/test_context_store.py tests/test_context_inspector.py -q && venv/bin/python -m ruff check lanscoder/context/events.py lanscoder/context/writer.py lanscoder/agent/session.py lanscoder/agent/loop.py lanscoder/observability/session_adapter.py`

### Task 3: Root lifecycle, exact provider inputs, and restart-safe resume

**Files:**
- Modify: `lanscoder/agent/observer.py`, `lanscoder/agent/loop.py`, `lanscoder/agent/permission_resume.py`, `lanscoder/core/runtime.py`, `lanscoder/core/session.py`
- Create: `lanscoder/observability/recorder.py`
- Test: `tests/test_observability_recorder.py`, `tests/test_agent_observability_integration.py`

**Interfaces:**

```python
class RunTraceSink(Protocol):
    def on_run_started(self, context: TraceContext, *, kind: str) -> None: ...
    def on_provider_request(self, context: TraceContext, prepared: PreparedMainRequest) -> None: ...
    def on_provider_response(self, context: TraceContext, response: ChatResponse, elapsed_ms: int) -> None: ...
    def on_tool_event(self, context: TraceContext, event: ToolExecutionEvent) -> TraceEvent | None: ...
    def on_waiting_for_input(self, context: TraceContext, request_id: str) -> None: ...
    def on_run_finished(self, context: TraceContext, status: str) -> None: ...
    def on_incomplete(self, context: TraceContext, reason: str) -> None: ...
```

- [ ] **Step 1: Write failing root and resume tests.**

  Cover permission resume after runner recreation retaining the original root run; missing pending mapping creating an incomplete observation without breaking resume; provider request snapshots matching actual messages, tools, and options; provider error and cancellation terminals; and a failing sink preserving the scripted turn result.

- [ ] **Step 2: Run the focused tests and confirm they fail.**

  Run: `venv/bin/python -m pytest tests/test_observability_recorder.py tests/test_agent_observability_integration.py -q`

  Expected: failures because runtime callbacks and recorder context are absent.

- [ ] **Step 3: Implement recorder event conversion.**

  Write raw payloads and safe summaries through `TraceStore`, emit lifecycle/provider/tool/waiting/terminal events, measure synchronous and streaming provider durations, and wrap every sink call in `try/except Exception`. A recorder failure emits `observation.incomplete` where possible and never propagates into normal Agent control flow.

- [ ] **Step 4: Propagate root context and pending mappings.**

  Make `_start_turn` call `registry.start_root` and `_resume_turn` call `registry.resume` before reusing or creating a loop. If resume returns `None`, create a diagnostic context and emit `pending_run_mapping_missing` while preserving existing resume behavior. Pass context explicitly into `AgentLoop`, `TurnObserver`, and `ToolExecutor`.

- [ ] **Step 5: Record exact request and waiting boundaries.**

  Emit `provider.request` after `_prepare_main_provider_request` and before provider dispatch. `PermissionResumeHandler` emits waiting state and persists `pending.json` before exposing the prompt; terminal `on_run_finished` is the only place that clears the matching mapping.

- [ ] **Step 6: Verify the task.**

  Run: `venv/bin/python -m pytest tests/test_observability_recorder.py tests/test_agent_observability_integration.py tests/test_agent_context_loop.py -q && venv/bin/python -m ruff check lanscoder/agent/observer.py lanscoder/agent/loop.py lanscoder/agent/permission_resume.py lanscoder/core/runtime.py lanscoder/core/session.py lanscoder/observability/recorder.py`

### Task 4: Child-agent and background-run linkage

**Files:**
- Modify: `lanscoder/agent/tool_execution.py`, `lanscoder/agent/background.py`, `lanscoder/agent/subagent_engine.py`, `lanscoder/core/runtime.py`
- Test: `tests/test_observability_children.py`, `tests/test_observability_background.py`

**Interfaces:**

```python
class BackgroundJob:
    trace_context: TraceContext | None

class SubagentEngine:
    def run(
        self,
        request: SubagentRequest,
        *,
        parent_trace: TraceContext | None = None,
        parent_event_id: str | None = None,
    ) -> SubagentResult: ...
```

- [ ] **Step 1: Write failing child/background tests.**

  Cover foreground delegation creating a completed child with matching root/parent IDs; background dispatch, completion, failure, cancellation-requested, and terminal cancellation events; copied context in the worker thread; restart while a persisted background child is running marking both runs incomplete; and recorder failure preserving `SubagentResult` and job status.

- [ ] **Step 2: Run the focused tests and confirm they fail.**

  Run: `venv/bin/python -m pytest tests/test_observability_children.py tests/test_observability_background.py -q`

  Expected: failures because execution paths do not provide child trace context.

- [ ] **Step 3: Link foreground child runs.**

  Make `ToolExecutor` emit `started` and place its event ID in `TraceContextScope` before entering `AgentSession.execute_tool_call*`. Make `create_delegate_tool` pass the scoped parent context and event ID to `SubagentEngine.run`. Create the child context from those immutable IDs, pass it to `child_runner_factory(..., trace_context=child_context)`, and finish it on response, paused input, error, or `AgentCancelledError`.

- [ ] **Step 4: Link background jobs.**

  Emit `background_dispatched` after validation and before `BackgroundJobManager.start`. Store the returned child context on `BackgroundJob`, submit `contextvars.copy_context().run`, and have `_finish` write the terminal event and finish that exact child before notification. `cancel` writes cancellation-requested immediately and finalizes only at terminal job state.

- [ ] **Step 5: Verify the task.**

  Run: `venv/bin/python -m pytest tests/test_observability_children.py tests/test_observability_background.py tests/test_delegate_tool.py tests/test_background_jobs.py -q && venv/bin/python -m ruff check lanscoder/agent/tool_execution.py lanscoder/agent/background.py lanscoder/agent/subagent_engine.py lanscoder/core/runtime.py`

### Task 5: Run-first localhost Web workbench and redacted APIs

**Files:**
- Create: `lanscoder/observability/web.py`, `lanscoder/observability/static/run.html`, `lanscoder/observability/static/run.js`
- Test: `tests/test_observability_web.py`

**Interfaces:**

```python
class ObservatoryServer:
    def start(self) -> str: ...
    def running(self) -> ContextManager[str]: ...
    def page_url(self, run_id: str | None = None) -> str: ...
    def api_url(self, path: str) -> str: ...
```

The server exposes `GET /observe`, `GET /observe?run_id=<id>`, `GET /api/runs`, `GET /api/runs/<run_id>`, `GET /api/events/<event_id>`, and explicit `GET /api/events/<event_id>/raw`. It binds only to `127.0.0.1`; non-GET requests return 405.

- [ ] **Step 1: Write failing server tests.**

  Assert page and API URLs are separate content types; default run/event responses contain redacted summaries only; raw is absent unless the explicit endpoint is requested; raw responses include `Cache-Control: no-store`; unfinished runs remain `running`; only loopback binding is used; unknown IDs return 404; and child/background events are grouped into lanes beneath the selected root.

- [ ] **Step 2: Run the focused tests and confirm they fail.**

  Run: `venv/bin/python -m pytest tests/test_observability_web.py -q`

  Expected: collection failures because the server and static page do not exist.

- [ ] **Step 3: Implement read-only APIs and page.**

  Serialize run and event indexes from `TraceStore` using safe summaries only. Add an explicit raw handler that resolves one payload, sets `Cache-Control: no-store`, and never stores the response in a server-side cache. Render status, duration/token summaries, stable sequence, root/child lanes, and four result/process/efficiency/risk evidence entry points. The “显示原文” button performs a fetch only on click and resets on reload.

- [ ] **Step 4: Verify the task.**

  Run: `venv/bin/python -m pytest tests/test_observability_web.py -q && venv/bin/python -m ruff check lanscoder/observability/web.py`

### Task 6: Application composition and `/observe` deep link

**Files:**
- Create: `lanscoder/app/observe_commands.py`
- Modify: `lanscoder/app/factory.py`, `lanscoder/app/help_commands.py`
- Test: `tests/test_observe_commands.py`, `tests/test_app_factory.py`

- [ ] **Step 1: Write failing command and factory tests.**

  Cover `/observe` opening the latest root run for the current session, explicit IDs resolving globally, no captured run returning a clear result, unknown IDs returning a clear error, help registration, and one app-level store shared by root, child, and background runners.

- [ ] **Step 2: Run the focused tests and confirm they fail.**

  Run: `venv/bin/python -m pytest tests/test_observe_commands.py tests/test_app_factory.py -q`

  Expected: failures because command handling and Observatory wiring are absent.

- [ ] **Step 3: Compose the shared Observatory services.**

  Make `create_lanscoder_app` create `TraceStore(resolved_data_root / "observatory")`, the recorder, the context registry, and a lazy singleton `ObservatoryServer`. Inject the same services into the main runner and propagate them to children and background workers.

- [ ] **Step 4: Register and implement the command.**

  Add `/observe [run-id]` to `CompositeCommandHandler`, not `SessionCommandHandler`. Resolve the latest root run for the current session when no ID is supplied; resolve explicit IDs globally; open `server.page_url(run_id)` with `webbrowser`; and return a `CommandResult` without changing session state.

- [ ] **Step 5: Verify the task.**

  Run: `venv/bin/python -m pytest tests/test_observe_commands.py tests/test_app_factory.py tests/test_observability_children.py -q && venv/bin/python -m ruff check lanscoder/app/observe_commands.py lanscoder/app/factory.py lanscoder/app/help_commands.py`

### Task 7: End-to-end acceptance and regression verification

**Files:**
- Create: `tests/test_observability_acceptance.py`
- Test with: all Observatory-focused tests and existing delegate/background tests

- [ ] **Step 1: Write the acceptance scenarios.**

  Add an end-to-end fixture that starts a permissioned root run, dispatches a background child, recreates the app, resumes the pending request, waits for the child, and asserts the root is completed with linked child metadata. Add a redaction scenario asserting the default page/API exclude nested secrets while the explicit raw endpoint returns the original value only with `no-store`. Add an incomplete-recovery scenario asserting unknown background terminal state is not rendered as cancelled or completed.

- [ ] **Step 2: Run the acceptance tests.**

  Run: `venv/bin/python -m pytest tests/test_observability_acceptance.py -q`

  Expected: all acceptance scenarios pass.

- [ ] **Step 3: Run the complete relevant verification set.**

  Run:

  ```bash
  venv/bin/python -m pytest \
    tests/test_observability_store.py \
    tests/test_observability_redaction.py \
    tests/test_observability_context.py \
    tests/test_observability_session_adapter.py \
    tests/test_observability_context_capture.py \
    tests/test_observability_recorder.py \
    tests/test_agent_observability_integration.py \
    tests/test_observability_children.py \
    tests/test_observability_background.py \
    tests/test_observability_web.py \
    tests/test_observe_commands.py \
    tests/test_observability_acceptance.py \
    tests/test_delegate_tool.py \
    tests/test_background_jobs.py -q
  venv/bin/python -m ruff check \
    lanscoder/observability \
    lanscoder/context/events.py lanscoder/context/writer.py \
    lanscoder/agent/session.py lanscoder/agent/observer.py \
    lanscoder/agent/loop.py lanscoder/agent/permission_resume.py \
    lanscoder/agent/tool_execution.py lanscoder/agent/background.py \
    lanscoder/agent/subagent_engine.py lanscoder/core/runtime.py \
    lanscoder/core/session.py lanscoder/app/observe_commands.py \
    lanscoder/app/factory.py lanscoder/app/help_commands.py
  ```

  Expected: all targeted tests pass and ruff reports no findings. Do not claim broader repository tests pass unless they are also run.

## Plan Self-Review

- Spec coverage: Tasks 1–2 cover contracts, atomic storage, redaction, recovery, session events, plans, compaction, and context capture; Tasks 3–4 cover root/resume/provider/tool/permission/child/background lineage; Tasks 5–6 cover the localhost UI, APIs, TUI deep link, and shared app composition; Task 7 covers end-to-end recovery and redaction.
- Scope check: no task creates evaluation objects, scorecards, experiments, task packages, cases, or promotion APIs. Cross-run features are limited to filtering, sorting, and diagnostic viewing.
- Integrity check: raw payloads, event envelopes, safe summaries, indexes, pending mappings, incomplete states, and loopback raw reveal all have explicit implementation and test steps.
- Placeholder scan: the plan contains no unresolved placeholders or unspecified implementation steps.
- Type consistency: `TraceContext`, `TraceStore`, `TraceContextRegistry`, `RunTraceSink`, `ObservatorySessionAdapter`, and `ObservatoryServer` signatures are reused consistently across tasks.
