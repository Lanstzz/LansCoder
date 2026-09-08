from __future__ import annotations

from dataclasses import asdict
from collections.abc import Callable
from typing import Any, Mapping, Sequence

from lanscoder.context.compaction import CompactionEvent
from lanscoder.context.events import SessionEvent
from lanscoder.context.identity import new_event_id, new_message_id, new_part_id
from lanscoder.context.llm_compact import LlmCompactEvent
from lanscoder.context.metadata import metadata_without_reserved_keys
from lanscoder.context.models import MessagePart, SessionView, utc_now_iso
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.versions import CONTEXT_EVENT_SCHEMA_VERSION
from lanscoder.session.branch import SessionBranchContext, build_branch_topology
from lanscoder.session.projection import project_branch
from lanscoder.journal.models import new_branch_id
from lanscoder.input.attachments import PreparedAttachment
from lanscoder.planning.models import TaskPlan
from lanscoder.planning.validation import validate_plan
from lanscoder.providers.types import ChatResponse, ToolCall
from lanscoder.tools.types import ToolResult
from lanscoder.observability.models import ObservationType, TraceScope
from lanscoder.observability.protocol import TraceRecorder


class SessionEventWriter:
    def __init__(
        self,
        *,
        store: JsonlSessionStore,
        session_id: str,
        current_turn: int = 0,
        branch_context: SessionBranchContext | None = None,
        trace_recorder: TraceRecorder | None = None,
        trace_id: str | None = None,
        trace_scope: TraceScope | None = None,
        allow_inactive_branch: bool = False,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.current_turn = current_turn
        self.trace_recorder = trace_recorder
        self.trace_id = trace_id
        self.trace_scope = trace_scope
        self.allow_inactive_branch = allow_inactive_branch
        self.branch_context = None
        if branch_context is not None:
            self._validate_branch_context(branch_context)
            self.branch_context = branch_context
        elif hasattr(self.store, "journal"):
            self.branch_context = self._existing_branch_context()

    def append_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        branch_context: SessionBranchContext | None = None,
        allow_inactive_branch: bool = False,
    ) -> None:
        kind_map = {
            "session_created": "session.created",
            "session_metadata_updated": "session.metadata_updated",
            "user_message": "message.appended",
            "assistant_message": "message.appended",
            "tool_result": "message.appended",
            "background_notification": "message.appended",
        }
        kind = kind_map.get(event_type, event_type.replace("_", "."))
        if branch_context is not None and branch_context.session_id != self.session_id:
            raise ValueError("branch context session_id does not match writer session")
        if hasattr(self.store, "journal"):
            branch_context = branch_context or self.branch_context
            if kind == "session.created":
                root = str(payload.get("root_branch_id") or new_branch_id())
                payload = {**payload, "root_branch_id": root}
                branch_context = SessionBranchContext(self.session_id, root, root)
            elif branch_context is None:
                branch_context = self._existing_branch_context()
                if branch_context is None:
                    raise ValueError("session.created must be appended before context events")
            if kind == "message.appended":
                role = {"user_message": "user", "assistant_message": "assistant", "tool_result": "tool", "background_notification": "notification"}[event_type]
                payload = {**payload, "role": role}
            if kind != "session.created":
                topology = self._persisted_branch_topology()
                if topology is None or branch_context.root_branch_id != topology.root_branch_id or branch_context.branch_id not in topology.branches:
                    raise ValueError("branch context does not match the persisted root or active branch")
                if not (allow_inactive_branch or self.allow_inactive_branch) and branch_context.branch_id != topology.active_branch_id:
                    raise ValueError("branch context does not match the persisted root or active branch")
            self.store.append_journal_event(session_id=self.session_id, kind=kind, data=payload, branch_id=branch_context.branch_id)
            if branch_context is self.branch_context or self.branch_context is None:
                self.branch_context = branch_context
            self._record_event_observation(event_type, payload)
            return
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=self.session_id,
                type=event_type,
                payload=payload,
            )
        )
        self._record_event_observation(event_type, payload)

    def set_trace_context(
        self,
        recorder: TraceRecorder | None,
        trace_id: str | None,
        scope: TraceScope | None,
    ) -> None:
        self.trace_recorder = recorder
        self.trace_id = trace_id
        self.trace_scope = scope

    def _record_event_observation(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.trace_recorder is None or self.trace_id is None:
            return
        scope = self.trace_scope
        if scope is None and self.branch_context is not None:
            scope = TraceScope(self.session_id, self.branch_context.branch_id)
        if scope is None:
            return
        observation_id = None
        try:
            observation_id = self.trace_recorder.start_observation(
                self.trace_id,
                ObservationType.EVENT,
                scope=scope,
                data={"event": event_type, **{key: value for key, value in payload.items() if key in {"trigger", "operation", "skill_name", "server", "job_id", "status"}}},
            )
            self.trace_recorder.end_observation(
                observation_id,
                outcome="succeeded",
                data={"event": event_type},
            )
        except Exception:
            return

    def append_session_created(self, **metadata: Any) -> None:
        payload = {"session_id": self.session_id}
        payload.update(metadata_without_reserved_keys(metadata))
        payload["context_event_schema_version"] = CONTEXT_EVENT_SCHEMA_VERSION
        self.append_event("session_created", payload)

    def append_session_metadata_updated(self, **metadata: Any) -> None:
        self.append_event("session_metadata_updated", metadata_without_reserved_keys(metadata))

    def append_message_part_metadata_updated(self, *, message_id: str, part_id: str, metadata: dict[str, Any]) -> None:
        self.append_event(
            "message_part_metadata_updated",
            {
                "message_id": message_id,
                "part_id": part_id,
                "metadata": dict(metadata),
            },
        )

    def append_provider_projection_consumed(
        self,
        *,
        request_id: str,
        projection_fingerprint: str,
        part_ids: list[str],
        provider: str,
        model: str,
    ) -> None:
        normalized = sorted({part_id for part_id in part_ids if part_id})
        if not normalized:
            return
        self.append_event(
            "provider_projection_consumed",
            {
                "request_id": request_id,
                "projection_fingerprint": projection_fingerprint,
                "part_ids": normalized,
                "provider": provider,
                "model": model,
            },
        )

    def append_user_message(
        self,
        content: str,
        *,
        attachments: list[PreparedAttachment] | None = None,
        metadata: dict[str, Any] | None = None,
        part_metadata: dict[str, Any] | None = None,
    ) -> str:
        self.current_turn += 1
        message_id = new_message_id()
        parts = [
            MessagePart(
                id=new_part_id(),
                message_id=message_id,
                kind="text",
                content=content,
                metadata=self._part_metadata(part_metadata),
            )
        ]
        for attachment in attachments or []:
            attachment_metadata = dict(part_metadata or {})
            attachment_metadata.update(
                {
                    "filename": attachment.filename,
                    "media_type": attachment.media_type,
                    "path": attachment.relative_path,
                    "bytes": attachment.size_bytes,
                    "sha256": attachment.sha256,
                    "source": attachment.source,
                }
            )
            parts.append(
                MessagePart(
                    id=new_part_id(),
                    message_id=message_id,
                    kind=attachment.kind,
                    content=(f"[image: {attachment.filename}]" if attachment.kind == "image" else attachment.inline_text or f"[file: {attachment.filename}]"),
                    metadata=self._part_metadata(attachment_metadata),
                )
            )
        self._append_message_event(
            "user_message",
            message_id=message_id,
            parts=parts,
            metadata=metadata,
        )
        return message_id

    def append_assistant_response(self, response: ChatResponse) -> str:
        message_id = new_message_id()
        parts: list[MessagePart] = []
        if response.content:
            parts.append(
                MessagePart(
                    id=new_part_id(),
                    message_id=message_id,
                    kind="text",
                    content=response.content,
                    metadata=self._part_metadata(),
                )
            )
        for tool_call in response.tool_calls:
            parts.append(tool_call_to_part(message_id=message_id, tool_call=tool_call))
        self._attach_turn_metadata(parts)
        self._append_message_event(
            "assistant_message",
            message_id=message_id,
            parts=parts,
            metadata={
                "provider": response.provider,
                "model": response.model,
                "finish_reason": response.finish_reason,
            },
        )
        return message_id

    def append_assistant_parts(
        self,
        parts: list[MessagePart],
        *,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> str:
        message_id = message_id or new_message_id()
        self._attach_turn_metadata(parts)
        self._append_message_event(
            "assistant_message",
            message_id=message_id,
            parts=parts,
            metadata=metadata,
        )
        return message_id

    def append_tool_result(self, *, tool_call: ToolCall, result: ToolResult) -> str:
        message_id = new_message_id()
        part = MessagePart(
            id=new_part_id(),
            message_id=message_id,
            kind="tool_result",
            content=result.content,
            metadata={
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "ok": result.ok,
                "data": result.data,
                "error": result.error,
            },
        )
        self._attach_turn_metadata([part])
        self._append_message_event("tool_result", message_id=message_id, parts=[part])
        return message_id

    def append_tool_result_part(self, part: MessagePart, *, message_id: str | None = None) -> str:
        message_id = message_id or part.message_id
        self._attach_turn_metadata([part])
        self._append_message_event("tool_result", message_id=message_id, parts=[part])
        return message_id

    def append_compaction_completed(
        self,
        *,
        trigger: str,
        target_tokens: int,
        event: CompactionEvent,
    ) -> None:
        event_payload = asdict(event)
        self.append_event(
            "compaction_completed",
            {
                "event_version": CONTEXT_EVENT_SCHEMA_VERSION,
                "trigger": trigger,
                "target_tokens": target_tokens,
                "created_at": event.created_at,
                "input_fingerprint": event.input_fingerprint,
                "status": "success" if event.success else "failed",
                "reason": event.reason,
                "before_tokens": event.before_tokens,
                "after_tokens": event.after_tokens,
                "checkpoint_id": event.checkpoint_id,
                "event": event_payload,
            },
        )

    def append_llm_compaction_completed(
        self,
        *,
        trigger: str,
        target_tokens: int,
        event: LlmCompactEvent,
    ) -> None:
        event_payload = asdict(event)
        created_at = utc_now_iso()
        self.append_event(
            "llm_compaction_completed",
            {
                "event_version": CONTEXT_EVENT_SCHEMA_VERSION,
                "trigger": trigger,
                "target_tokens": target_tokens,
                "created_at": created_at,
                "input_fingerprint": event.source_fingerprint,
                "status": event.status,
                "reason": event.failure_reason or event.status,
                "before_tokens": None,
                "after_tokens": None,
                "checkpoint_id": event.checkpoint_id,
                "event": event_payload,
            },
        )

    def append_compaction_skipped(self, *, trigger: str, input_fingerprint: str, reason: str) -> None:
        self.append_event(
            "compaction_skipped",
            {
                "event_version": CONTEXT_EVENT_SCHEMA_VERSION,
                "trigger": trigger,
                "input_fingerprint": input_fingerprint,
                "reason": reason,
                "created_at": utc_now_iso(),
            },
        )

    def append_task_plan_updated(
        self,
        *,
        previous_revision: int,
        operation: str,
        changes: Sequence[Mapping[str, object]],
        snapshot: TaskPlan | Mapping[str, object],
        branch_context: SessionBranchContext | None = None,
    ) -> None:
        if isinstance(previous_revision, bool) or not isinstance(previous_revision, int) or previous_revision < 0:
            raise ValueError("previous_revision must be a non-negative integer")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation must be a non-blank string")

        plan = TaskPlan.from_dict(snapshot.to_dict() if isinstance(snapshot, TaskPlan) else snapshot)
        validate_plan(plan)
        if plan.revision != previous_revision + 1:
            raise ValueError("task plan revision must be exactly one greater than previous_revision")
        normalized_changes = [dict(change) for change in changes]
        self.append_event(
            "task_plan_updated",
            {
                "previous_revision": previous_revision,
                "revision": plan.revision,
                "operation": operation,
                "changes": normalized_changes,
                "snapshot": plan.to_dict(),
            },
            branch_context=branch_context,
            allow_inactive_branch=branch_context is not None,
        )

    def append_background_scheduled(
        self,
        *,
        job_id: str,
        tool_name: str,
        dispatch_branch_context: Mapping[str, object],
        parent_trace_id: str | None = None,
        parent_observation_id: str | None = None,
        branch_context: SessionBranchContext | None = None,
    ) -> None:
        self.append_event(
            "background_scheduled",
            {
                "job_id": job_id,
                "tool_name": tool_name,
                "dispatch_branch_context": dict(dispatch_branch_context),
                "parent_trace_id": parent_trace_id,
                "parent_observation_id": parent_observation_id,
                "status": "scheduled",
            },
            branch_context=branch_context,
            allow_inactive_branch=True,
        )

    def append_background_lifecycle(
        self,
        *,
        job_id: str,
        status: str,
        branch_context: SessionBranchContext | None = None,
        **data: object,
    ) -> None:
        self.append_event(
            "background_lifecycle",
            {"job_id": job_id, "status": status, **data},
            branch_context=branch_context,
            allow_inactive_branch=True,
        )

    def append_background_notification_evidence(
        self,
        *,
        job_id: str,
        status: str,
        branch_context: SessionBranchContext | None = None,
        detached_from_active_branch: bool = False,
    ) -> None:
        self.append_event(
            "background_notification_delivery",
            {
                "job_id": job_id,
                "status": status,
                "detached_from_active_branch": detached_from_active_branch,
            },
            branch_context=branch_context,
            allow_inactive_branch=True,
        )

    def mutate_task_plan(
        self,
        *,
        expected_revision: int,
        operation: str,
        reducer: Callable[[TaskPlan | None], Any],
        branch_context: SessionBranchContext | None = None,
    ) -> Any:
        """Apply one task-plan reducer against a branch under the session lock.

        The reducer is intentionally a task-plan-specific callback rather than a
        generic compare-and-swap mechanism.  It only validates and transforms a
        projection; all I/O happens here, while the journal write lock is held.
        """

        target = branch_context or self.branch_context
        if target is None:
            raise ValueError("task-plan mutation requires a persisted branch context")
        if target.session_id != self.session_id:
            raise ValueError("task-plan branch context session_id does not match writer session")

        envelope = None
        with self.store.journal.write_transaction(self.session_id) as (events, append_locked):
            if not any(
                event.kind == "session.created"
                and isinstance(event.data.get("root_branch_id"), str)
                and event.data["root_branch_id"]
                and isinstance(event.branch_id, str)
                and event.branch_id == event.data["root_branch_id"]
                for event in events
            ):
                raise ValueError("task-plan branch context requires a persisted session.created")
            topology = build_branch_topology(events)
            if target.root_branch_id != topology.root_branch_id or target.branch_id not in topology.branches:
                raise ValueError("task-plan branch context does not match the persisted branch topology")
            projected = project_branch(events, topology, target.branch_id)
            view = SessionView(session_id=self.session_id)
            for event in projected:
                self.store._apply_event(view, event, sequence=event.sequence)
            current_plan = view.task_plan
            result = reducer(current_plan)
            if result.changed:
                plan = TaskPlan.from_dict(result.plan.to_dict() if isinstance(result.plan, TaskPlan) else result.plan)
                validate_plan(plan)
                previous_revision = current_plan.revision if current_plan is not None else 0
                if plan.revision != previous_revision + 1:
                    raise ValueError("task plan revision must be exactly one greater than previous_revision")
                normalized_changes = [dict(change) for change in result.changes]
                envelope = append_locked(
                    "task.plan.updated",
                    {
                        "previous_revision": previous_revision,
                        "revision": plan.revision,
                        "operation": operation,
                        "changes": normalized_changes,
                        "snapshot": plan.to_dict(),
                    },
                    branch_id=target.branch_id,
                )

        if envelope is not None:
            from lanscoder.session.index import SessionIndex

            SessionIndex(self.store.root).update_event(envelope)
        return result

    def append_background_notification(
        self,
        *,
        content: str,
        job_id: str,
        tool_name: str,
        status: str,
        task_id: str | None = None,
        observed_revision: int | None = None,
        label: str | None = None,
        error: str | None = None,
        branch_context: SessionBranchContext | None = None,
    ) -> str:
        message_id = new_message_id()
        metadata = {
            "background_job_id": job_id,
            "background_tool_name": tool_name,
            "background_status": status,
        }
        if task_id is not None:
            metadata["background_task_id"] = task_id
        if observed_revision is not None:
            metadata["background_observed_revision"] = observed_revision
        if label is not None:
            metadata["background_label"] = label
        if error is not None:
            metadata["background_error"] = error
        part = MessagePart(
            id=new_part_id(),
            message_id=message_id,
            kind="text",
            content=content,
            metadata=self._part_metadata(metadata),
        )
        self._append_message_event(
            "background_notification",
            message_id=message_id,
            parts=[part],
            branch_context=branch_context,
            allow_inactive_branch=True,
        )
        return message_id

    def _append_message_event(
        self,
        event_type: str,
        *,
        message_id: str,
        parts: list[MessagePart],
        metadata: dict[str, Any] | None = None,
        branch_context: SessionBranchContext | None = None,
        allow_inactive_branch: bool = False,
    ) -> None:
        self.append_event(
            event_type,
            {
                "message_id": message_id,
                "parts": [part.to_dict() for part in parts],
                "metadata": metadata or {},
            },
            branch_context=branch_context,
            allow_inactive_branch=allow_inactive_branch,
        )

    def _part_metadata(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(metadata or {})
        merged.setdefault("created_turn", self.current_turn)
        merged.setdefault("turn_id", self.current_turn)
        return merged

    def _attach_turn_metadata(self, parts: list[MessagePart]) -> None:
        for part in parts:
            part.metadata = self._part_metadata(part.metadata)

    def _existing_branch_context(self) -> SessionBranchContext | None:
        topology = self._persisted_branch_topology()
        if topology is None:
            return None
        return SessionBranchContext(self.session_id, topology.active_branch_id or topology.root_branch_id, topology.root_branch_id)

    def _validate_branch_context(self, branch_context: SessionBranchContext) -> None:
        if branch_context.session_id != self.session_id:
            raise ValueError("branch context session_id does not match writer session")
        topology = self._persisted_branch_topology()
        if topology is None:
            raise ValueError("branch context requires a persisted session.created")
        if branch_context.root_branch_id != topology.root_branch_id or branch_context.branch_id not in topology.branches:
            raise ValueError("branch context does not match the persisted root or active branch")
        if not self.allow_inactive_branch and branch_context.branch_id != topology.active_branch_id:
            raise ValueError("branch context does not match the persisted root or active branch")

    def _persisted_branch_topology(self):
        events = self.store.list_events(self.session_id)
        if not any(
            event.kind == "session.created"
            and isinstance(event.data.get("root_branch_id"), str)
            and event.data["root_branch_id"]
            and isinstance(event.branch_id, str)
            and event.branch_id
            and event.data["root_branch_id"] == event.branch_id
            for event in events
        ):
            return None
        return build_branch_topology(events)


def tool_call_to_part(*, message_id: str, tool_call: ToolCall) -> MessagePart:
    return MessagePart(
        id=new_part_id(),
        message_id=message_id,
        kind="tool_call",
        content="",
        metadata={
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "arguments": tool_call.arguments,
        },
    )
