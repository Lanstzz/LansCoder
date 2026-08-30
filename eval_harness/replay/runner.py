"""Offline interaction replay through LansCoder's public L1 runtime API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lanscoder.core import LoopConfig, LoopContext, LoopMessage, agent_loop, create_agent_session
from lanscoder.agent.tool_execution import ToolExecutionEvent
from lanscoder.core.events import AgentEndEvent, MessageEndEvent, ToolExecutionEndEvent, ToolExecutionStartEvent
from lanscoder.core.events import ToolExecutionUpdateEvent
from lanscoder.utils.cancellation import CancellationToken, current_cancellation_token
from lanscoder.providers.types import MainRequestOptions, ToolDefinition
from lanscoder.tools.types import Tool, ToolResult, make_error_result, make_text_result

from eval_harness.replay.provider import ScriptedProvider
from eval_harness.schema.models import CaseManifest, load_case_manifest
from eval_harness.trace.recorder import TraceRecorder
from eval_harness.trace.redaction import Redactor
from eval_harness.verify.checks import build_scorecard, compare_scorecards, verify_run


@dataclass(frozen=True, slots=True)
class RunResult:
    """Paths and machine-readable outcome from one fresh offline run."""

    trace_path: Path
    scorecard_path: Path
    artifacts_path: Path
    scorecard: dict[str, Any]


class NetworkDisabled:
    """Reject process-level socket connections while an offline case is executing."""

    def __init__(self) -> None:
        self.attempts = 0
        self._original_create_connection = None
        self._original_connect = None
        self._original_connect_ex = None

    def __enter__(self) -> "NetworkDisabled":
        self._original_create_connection = socket.create_connection
        self._original_connect = socket.socket.connect
        self._original_connect_ex = socket.socket.connect_ex

        def blocked_connection(*args: object, **kwargs: object) -> None:
            del args, kwargs
            self.attempts += 1
            raise RuntimeError("network access is disabled for offline evaluation")

        socket.create_connection = blocked_connection
        socket.socket.connect = blocked_connection
        socket.socket.connect_ex = blocked_connection
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        assert self._original_create_connection is not None
        assert self._original_connect is not None
        assert self._original_connect_ex is not None
        socket.create_connection = self._original_create_connection
        socket.socket.connect = self._original_connect
        socket.socket.connect_ex = self._original_connect_ex


async def run_offline_case_path(
    case_path: str | Path,
    output_dir: str | Path,
    *,
    baseline_scorecard: dict[str, Any] | None = None,
    capsule_path: str | Path | None = None,
    capsule_passphrase: str | None = None,
) -> RunResult:
    """Load a portable JSON case and produce one fresh run directory."""

    resolved_case_path = Path(case_path)
    return await run_offline_case(
        load_case_manifest(resolved_case_path, capsule_path=capsule_path, capsule_passphrase=capsule_passphrase),
        output_dir,
        case_path=resolved_case_path,
        baseline_scorecard=baseline_scorecard,
    )


async def run_offline_case(
    manifest: CaseManifest,
    output_dir: str | Path,
    *,
    case_path: str | Path | None = None,
    baseline_scorecard: dict[str, Any] | None = None,
) -> RunResult:
    """Replay one interaction tape without network access or a real model provider."""

    if manifest.capsule_required:
        raise ValueError("extracted replay case requires capsule_path and capsule_passphrase")
    if manifest.mode != "interaction_replay":
        raise ValueError("offline runner supports only interaction_replay cases")
    run_dir = Path(output_dir)
    if run_dir.exists():
        raise FileExistsError(f"fresh run directory already exists: {run_dir}")
    artifacts_path = run_dir / "artifacts"
    _prepare_artifacts(manifest, case_path=case_path, artifacts_path=artifacts_path)

    redactor = Redactor(sensitive_values=manifest.private_values, paths=[artifacts_path])
    recorder = TraceRecorder(run_dir, redactor=redactor)
    provider = ScriptedProvider(manifest.provider_tape, on_interaction=recorder.record)
    cancellation = CancellationToken()
    tool_faults = {
        str(call["arguments"].get("path")): manifest.tool_faults[call["id"]]
        for entry in manifest.provider_tape
        for call in entry.tool_calls
        if call["id"] in (manifest.tool_faults or {})
        and isinstance(call.get("arguments"), dict)
        and isinstance(call["arguments"].get("path"), str)
    }
    tool_result_sizes = {
        str(call["arguments"].get("path")): manifest.tool_result_sizes[call["id"]]
        for entry in manifest.provider_tape
        for call in entry.tool_calls
        if call["id"] in manifest.tool_result_sizes
        and isinstance(call.get("arguments"), dict)
        and isinstance(call["arguments"].get("path"), str)
    }
    baseline_artifacts = _snapshot_artifacts(artifacts_path)
    final_delivery = ""
    failure: str | None = None
    started_at = time.monotonic()

    recorder.record(
        "run_started",
        {
            "case": manifest.identity(),
            "network_policy": "disabled",
            "runtime": {
                "api": "lanscoder.core.create_agent_session" if manifest.runtime == "session" else "lanscoder.core.agent_loop",
                "mode": manifest.runtime,
                "session_id": f"eval-{manifest.identifier}",
            },
        },
    )
    recorder.record(
        "user_input",
        {
            "content": manifest.prompt,
            "sha256": _digest(manifest.prompt),
        },
    )
    if manifest.enable_compaction and manifest.runtime == "l1":
        _record_compaction_probe(recorder)

    try:
        with NetworkDisabled() as network_guard:
            if manifest.runtime == "session":
                final_delivery, failure = await _run_session_runtime(
                    manifest,
                    artifacts_path=artifacts_path,
                    recorder=recorder,
                    provider=provider,
                    tool_faults=tool_faults,
                    tool_result_sizes=tool_result_sizes,
                )
            else:
                async for event in agent_loop(
                    [LoopMessage.user(manifest.prompt)],
                    LoopContext(
                        tools=_evaluation_tools(
                            manifest,
                            artifacts_path,
                            tool_faults=tool_faults,
                            tool_result_sizes=tool_result_sizes,
                        )
                    ),
                    LoopConfig(
                        provider=provider,
                        session_id=f"eval-{manifest.identifier}",
                        use_streaming=False,
                    ),
                    signal=cancellation,
                ):
                    recorder.record(event.type, _agent_event_payload(event))
                    if isinstance(event, ToolExecutionStartEvent) and _should_interrupt(manifest, event, recorder.events):
                        cancellation.cancel()
                        recorder.record("interrupt_requested", {"after_tool_calls": manifest.interrupt_after_tool_calls})
                    if isinstance(event, MessageEndEvent) and event.message is not None and event.message.role == "assistant":
                        final_delivery = event.message.content
                    if isinstance(event, AgentEndEvent) and not final_delivery:
                        final_delivery = _last_assistant_content(event)
        recorder.record("network_guard", {"attempts": network_guard.attempts})
    except Exception as exc:  # noqa: BLE001 - failures must be recorded for verifier evidence.
        failure = f"{type(exc).__name__}: {exc}"
        recorder.record("runtime_exception", {"kind": type(exc).__name__, "message": str(exc)})
        if "network_guard" not in {str(event.get("type")) for event in recorder.events}:
            recorder.record("network_guard", {"attempts": network_guard.attempts})

    artifacts_after = _snapshot_artifacts(artifacts_path)
    recorder.record("artifacts", _artifact_diff(baseline_artifacts, artifacts_after))
    recorder.record("final_delivery", {"content": final_delivery, "completed": failure is None})
    recorder.record(
        "run_completed",
        {
            "provider_calls": provider.calls,
            "tool_calls": sum(event.get("type") == "tool_execution_start" for event in recorder.events),
            "elapsed_ms": round((time.monotonic() - started_at) * 1000, 3),
            "compaction_events": sum(
                event.get("type") in {"context_compaction", "context_compaction_l3"} for event in recorder.events
            ),
            "recovery_events": _recovery_summary(recorder.events),
            "failure": failure,
        },
    )
    trace_path = recorder.write()
    verification = verify_run(manifest, trace_path, artifacts_path)
    comparison = compare_scorecards(baseline_scorecard, build_scorecard(verification, trace_path)) if baseline_scorecard is not None else None
    scorecard = build_scorecard(verification, trace_path, comparison=comparison)
    scorecard_path = run_dir / "scorecard.json"
    scorecard_path.write_text(_json(scorecard), encoding="utf-8")
    return RunResult(
        trace_path=trace_path,
        scorecard_path=scorecard_path,
        artifacts_path=artifacts_path,
        scorecard=scorecard,
    )


async def _run_session_runtime(
    manifest: CaseManifest,
    *,
    artifacts_path: Path,
    recorder: TraceRecorder,
    provider: ScriptedProvider,
    tool_faults: dict[str, str],
    tool_result_sizes: dict[str, int],
) -> tuple[str, str | None]:
    """Run a persistent L3 session, optionally interrupting and reopening it."""

    from lanscoder.context.store import JsonlSessionStore

    session_id = f"eval-{manifest.identifier}"
    runtime_root = recorder.output_dir / ".runtime"
    tools = _evaluation_tools(
        manifest,
        artifacts_path,
        tool_faults=tool_faults,
        tool_result_sizes=tool_result_sizes,
    )
    options = MainRequestOptions(max_tokens=manifest.max_output_tokens)
    seen_session_events: set[str] = set()
    interrupt_requested = False
    active_runner = None
    final_delivery = ""

    def on_tool_event(event: ToolExecutionEvent) -> None:
        nonlocal interrupt_requested
        if event.kind == "started":
            recorder.record(
                "tool_execution_start",
                {
                    "tool_call_id": event.tool_call.id,
                    "tool_name": event.tool_call.name,
                    "arguments_sha256": _digest_json(event.tool_call.arguments),
                    "argument_keys": sorted(event.tool_call.arguments) if isinstance(event.tool_call.arguments, dict) else [],
                },
            )
            if not interrupt_requested and _should_interrupt(
                manifest,
                ToolExecutionStartEvent(
                    tool_call_id=event.tool_call.id,
                    tool_name=event.tool_call.name,
                    args=event.tool_call.arguments,
                ),
                recorder.events,
            ):
                interrupt_requested = True
                recorder.record("interrupt_requested", {"after_tool_calls": manifest.interrupt_after_tool_calls})
                if active_runner is not None:
                    active_runner.cancel_current_turn()
            return
        if event.kind == "finished":
            recorder.record(
                "tool_execution_end",
                {
                    "tool_call_id": event.tool_call.id,
                    "tool_name": event.tool_call.name,
                    "result": _tool_result_payload(event.result),
                    "is_error": event.result is not None and not event.result.ok,
                },
            )
            return
        recorder.record(
            "tool_execution_update",
            {
                "tool_call_id": event.tool_call.id,
                "tool_name": event.tool_call.name,
                "lifecycle": event.kind,
                "result": _tool_result_payload(event.result) if event.result is not None else None,
            },
        )

    try:
        handle = create_agent_session(
            provider=provider,
            project_root=artifacts_path,
            data_root=runtime_root,
            tools=tools,
            session_id=session_id,
            limits=None,
            request_options=options,
            context_window=manifest.context_window,
            compaction_strategy=manifest.compaction_strategy,
        )
        active_runner = handle.runner
        active_runner.tool_event_handler = on_tool_event
        store = JsonlSessionStore(runtime_root)

        for prompt in manifest.warmup_prompts:
            await active_runner.arun_user_turn(prompt)
            _record_session_compaction_events(store, session_id, recorder, seen_session_events)

        first_response = await active_runner.arun_user_turn(manifest.prompt)
        final_delivery = first_response.content
        _record_session_compaction_events(store, session_id, recorder, seen_session_events)

        if manifest.resume_after_interrupt:
            recorder.record(
                "session_resumed",
                {
                    "session_id": session_id,
                    "interrupted": interrupt_requested,
                    "history_messages": len(handle.session.rebuild_view().messages),
                },
            )
            resumed = create_agent_session(
                provider=provider,
                project_root=artifacts_path,
                data_root=runtime_root,
                tools=tools,
                session_id=session_id,
                resume=True,
                limits=None,
                request_options=options,
                context_window=manifest.context_window,
                compaction_strategy=manifest.compaction_strategy,
            )
            active_runner = resumed.runner
            active_runner.tool_event_handler = on_tool_event
            resumed_response = await active_runner.arun_user_turn(manifest.resume_prompt or "继续完成当前任务。")
            final_delivery = resumed_response.content
            _record_session_compaction_events(store, session_id, recorder, seen_session_events)
        return final_delivery, None
    except Exception as exc:  # noqa: BLE001 - the trace must preserve runtime failures.
        failure = f"{type(exc).__name__}: {exc}"
        recorder.record("runtime_exception", {"kind": type(exc).__name__, "message": str(exc)})
        return final_delivery, failure
    finally:
        shutil.rmtree(runtime_root, ignore_errors=True)


def _record_session_compaction_events(
    store: Any,
    session_id: str,
    recorder: TraceRecorder,
    seen_event_ids: set[str],
) -> None:
    """Project persisted compaction events into portable, body-free trace facts."""

    for event in store.list_events(session_id):
        if event.id in seen_event_ids:
            continue
        seen_event_ids.add(event.id)
        if event.type not in {"compaction_completed", "llm_compaction_completed"}:
            continue
        nested = event.payload.get("event")
        nested = nested if isinstance(nested, dict) else {}
        is_programmatic = event.type == "compaction_completed"
        recorder.record(
            "context_compaction" if is_programmatic else "context_compaction_l3",
            {
                "trigger": event.payload.get("trigger"),
                "status": event.payload.get("status") or nested.get("status"),
                "reason": event.payload.get("reason") or nested.get("failure_reason"),
                "before_tokens": event.payload.get("before_tokens") if is_programmatic else None,
                "after_tokens": event.payload.get("after_tokens") if is_programmatic else None,
                "changed_parts": nested.get("changed_parts") if is_programmatic else None,
                "levels_attempted": nested.get("levels_attempted", []) if is_programmatic else [],
                "stopped_at": nested.get("stopped_at") if is_programmatic else None,
                "noop": nested.get("noop") if is_programmatic else None,
                "deduped": nested.get("deduped") if is_programmatic else None,
                "checkpoint_created": bool(event.payload.get("checkpoint_id") or nested.get("checkpoint_id")),
                "actual_runtime_event": True,
            },
        )


def run_case_sync(
    case_path: str | Path,
    output_dir: str | Path,
    *,
    baseline_scorecard: dict[str, Any] | None = None,
    capsule_path: str | Path | None = None,
    capsule_passphrase: str | None = None,
) -> RunResult:
    """Small synchronous convenience wrapper for the command-line interface."""

    return asyncio.run(
        run_offline_case_path(
            case_path,
            output_dir,
            baseline_scorecard=baseline_scorecard,
            capsule_path=capsule_path,
            capsule_passphrase=capsule_passphrase,
        )
    )


def _prepare_artifacts(manifest: CaseManifest, *, case_path: str | Path | None, artifacts_path: Path) -> None:
    if manifest.fixture is None:
        artifacts_path.mkdir(parents=True)
        return
    if case_path is None:
        raise ValueError("case_path is required when a manifest references a fixture")
    fixture_path = (Path(case_path).parent / manifest.fixture).resolve()
    if not fixture_path.is_dir():
        raise ValueError(f"fixture directory does not exist: {fixture_path}")
    shutil.copytree(fixture_path, artifacts_path)


def _write_file_tool(
    artifacts_path: Path,
    *,
    tool_faults: dict[str, str] | None = None,
    tool_result_sizes: dict[str, int] | None = None,
) -> Tool:
    root = artifacts_path.resolve()
    configured_faults = tool_faults or {}
    configured_result_sizes = tool_result_sizes or {}

    def write_file(path: str, content: str) -> ToolResult:
        if not isinstance(path, str) or not isinstance(content, str):
            return make_error_result("write_file", "path and content must be strings")
        target = (root / path).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            return make_error_result("write_file", "path escapes the fixture workspace")
        fault = configured_faults.get(path)
        if fault == "timeout":
            return make_error_result("write_file", "tool timeout injected", fault="timeout", timed_out=True)
        if fault == "failure":
            return make_error_result("write_file", "tool failure injected", fault="failure")
        if fault == "interrupt":
            token = current_cancellation_token()
            if token is not None:
                token.cancel()
                token.raise_if_cancelled()
            return make_error_result("write_file", "tool interruption probe has no cancellation context", fault="interrupt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        result_size = configured_result_sizes.get(path)
        result_content = f"wrote {relative.as_posix()}" if result_size is None else "x" * result_size
        return make_text_result("write_file", result_content, path=relative.as_posix(), bytes=len(content.encode("utf-8")))

    return Tool(
        definition=ToolDefinition(
            name="write_file",
            description="Write a UTF-8 text file inside the fixture workspace.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
        executor=write_file,
    )


def _evaluation_tools(
    manifest: CaseManifest,
    artifacts_path: Path,
    *,
    tool_faults: dict[str, str],
    tool_result_sizes: dict[str, int],
) -> list[Tool]:
    """Build only the mutation tools explicitly exercised by a case."""

    tools = [
        _write_file_tool(
            artifacts_path,
            tool_faults=tool_faults,
            tool_result_sizes=tool_result_sizes,
        )
    ]
    if any(call["name"] == "delete_file" for entry in manifest.provider_tape for call in entry.tool_calls):
        tools.append(_delete_file_tool(artifacts_path, tool_faults=tool_faults))
    return tools


def _delete_file_tool(artifacts_path: Path, *, tool_faults: dict[str, str]) -> Tool:
    root = artifacts_path.resolve()

    def delete_file(path: str) -> ToolResult:
        if not isinstance(path, str):
            return make_error_result("delete_file", "path must be a string")
        target = (root / path).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            return make_error_result("delete_file", "path escapes the fixture workspace")
        fault = tool_faults.get(path)
        if fault == "timeout":
            return make_error_result("delete_file", "tool timeout injected", fault="timeout", timed_out=True)
        if fault == "failure":
            return make_error_result("delete_file", "tool failure injected", fault="failure")
        if fault == "interrupt":
            token = current_cancellation_token()
            if token is not None:
                token.cancel()
                token.raise_if_cancelled()
            return make_error_result("delete_file", "tool interruption probe has no cancellation context", fault="interrupt")
        if not target.is_file():
            return make_error_result("delete_file", f"file does not exist: {relative.as_posix()}")
        target.unlink()
        return make_text_result("delete_file", f"deleted {relative.as_posix()}", path=relative.as_posix())

    return Tool(
        definition=ToolDefinition(
            name="delete_file",
            description="Delete a file inside the fixture workspace.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        executor=delete_file,
    )


def _agent_event_payload(event: object) -> dict[str, Any]:
    if isinstance(event, ToolExecutionStartEvent):
        return {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "arguments_sha256": _digest_json(event.args),
            "argument_keys": sorted(event.args) if isinstance(event.args, dict) else [],
        }
    if isinstance(event, ToolExecutionEndEvent):
        return {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "result": _tool_result_payload(event.result),
            "is_error": event.is_error,
        }
    if isinstance(event, ToolExecutionUpdateEvent):
        return {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "lifecycle": str(event.partial_result),
        }
    message = getattr(event, "message", None)
    if message is not None:
        return {"message": {"role": message.role, "content": message.content, "metadata": message.metadata}}
    if isinstance(event, AgentEndEvent):
        return {"message_count": len(event.messages)}
    return {}


def _last_assistant_content(event: AgentEndEvent) -> str:
    for message in reversed(event.messages):
        if message.role == "assistant":
            return message.content
    return ""


def _snapshot_artifacts(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _digest(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _artifact_diff(before: dict[str, str], after: dict[str, str]) -> dict[str, object]:
    return {
        "created": sorted(path for path in after if path not in before),
        "modified": sorted(path for path in after if path in before and after[path] != before[path]),
        "deleted": sorted(path for path in before if path not in after),
        "files": after,
    }


def _digest(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def _digest_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return _digest(payload)


def _tool_result_payload(result: object) -> dict[str, object]:
    if not isinstance(result, ToolResult):
        return {"present": result is not None}
    return {
        "name": result.name,
        "ok": result.ok,
        "content_sha256": _digest(result.content),
        "has_error": result.error is not None,
        "data_keys": sorted(result.data),
        "fault": result.data.get("fault"),
        "timed_out": result.data.get("timed_out", False),
    }


def _record_compaction_probe(recorder: TraceRecorder) -> None:
    """Record an explicit no-op probe without changing the runtime API."""

    recorder.record(
        "context_compaction",
        {
            "trigger": "evaluation_probe",
            "status": "success",
            "reason": "evaluation_probe",
            "before_tokens": 0,
            "after_tokens": 0,
            "changed_parts": 0,
            "noop": True,
        },
    )


def _should_interrupt(manifest: CaseManifest, event: ToolExecutionStartEvent, events: list[dict[str, object]]) -> bool:
    threshold = manifest.interrupt_after_tool_calls
    if threshold is None:
        return False
    starts = sum(item.get("type") == "tool_execution_start" for item in events)
    return starts >= threshold and not any(item.get("type") == "interrupt_requested" for item in events)


def _recovery_summary(events: list[dict[str, object]]) -> list[dict[str, object]]:
    summary: list[dict[str, object]] = []
    for event in events:
        event_type = event.get("type")
        data = event.get("data")
        if event_type == "provider_error" and isinstance(data, dict):
            summary.append({"kind": "provider", "error": data.get("kind"), "provider_error_kind": data.get("provider_error_kind")})
        elif event_type == "tool_execution_end" and isinstance(data, dict) and data.get("is_error"):
            summary.append({"kind": "tool", "tool_name": data.get("tool_name"), "fault": (data.get("result") or {}).get("fault")})
        elif event_type == "tool_execution_update" and isinstance(data, dict) and data.get("lifecycle") == "interrupted":
            summary.append({"kind": "interrupt", "tool_call_id": data.get("tool_call_id")})
        elif event_type == "context_compaction":
            summary.append({"kind": "compaction", "status": data.get("status") if isinstance(data, dict) else None})
        elif event_type == "context_compaction_l3":
            summary.append({"kind": "compaction_l3", "status": data.get("status") if isinstance(data, dict) else None})
        elif event_type == "session_resumed":
            summary.append({"kind": "session_resume", "interrupted": data.get("interrupted") if isinstance(data, dict) else None})
    return summary


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
