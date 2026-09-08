"""Session access boundaries for primary and internal subagent sessions.

The journal writer is intentionally not imported here.  Stage 1 does not yet
provide a stable writer protocol, so this module validates access and returns
data objects that a writer can persist without allowing callers to bypass the
policy.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lanscoder.session.errors import SessionNotFoundError
from lanscoder.storage.paths import project_id_for_path as _project_id_for_path


PRIMARY_KIND = "primary"
SUBAGENT_KIND = "subagent"


class SessionAccessError(ValueError):
    """Raised when a session is outside the caller's access boundary."""


def project_id_for_path(path: str | os.PathLike[str]) -> str:
    """Return the canonical storage project identity for a project path."""

    return _project_id_for_path(path)


@dataclass(frozen=True, slots=True)
class SessionAccessDescriptor:
    """Journal metadata required to make an access decision."""

    session_id: str
    project_id: str
    kind: str = PRIMARY_KIND
    parent_session_id: str | None = None
    parent_trace_id: str | None = None
    worktree_metadata: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


class SessionAccessPolicy:
    """Enforce the project and session-kind boundary for user entry points."""

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        journal: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.project_id = project_id or project_id_for_path(self.project_root)
        self.journal = journal

    def create_primary(
        self,
        session_id: str | None = None,
        *,
        metadata: Mapping[str, Any] | None = None,
        **extra_metadata: Any,
    ) -> SessionAccessDescriptor:
        """Authorize creation of a new primary session.

        Persistence is intentionally left to the journal writer.  If a journal
        reader is supplied, an existing id is rejected before any write can be
        attempted.
        """

        resolved_id = session_id or _new_session_id()
        self._validate_session_id(resolved_id)
        if self._lookup(resolved_id) is not None:
            raise SessionAccessError(f"session already exists: {resolved_id}")
        values = dict(metadata or {})
        values.update(extra_metadata)
        values.update(
            {
                "project_id": self.project_id,
                "project_root": str(self.project_root.resolve(strict=False)),
                "kind": PRIMARY_KIND,
            }
        )
        return SessionAccessDescriptor(
            session_id=resolved_id,
            project_id=self.project_id,
            metadata=values,
        )

    def open_primary(
        self,
        session_id: str,
        *,
        session: Any | None = None,
    ) -> SessionAccessDescriptor:
        """Authorize opening an existing primary session in this project."""

        self._validate_session_id(session_id)
        descriptor = self._coerce_descriptor(session_id, session if session is not None else self._lookup(session_id))
        if descriptor is None:
            raise SessionAccessError(f"session not found: {session_id}")
        if descriptor.kind != PRIMARY_KIND:
            raise SessionAccessError(f"session is not a primary session: {session_id}")
        if descriptor.project_id != self.project_id:
            raise SessionAccessError(f"session belongs to another project: {session_id}")
        return descriptor

    def fork_primary(
        self,
        source_session_id: str,
        *,
        session_id: str | None = None,
    ) -> SessionAccessDescriptor:
        """Authorize a fork and describe its new primary session."""

        source = self.open_primary(source_session_id)
        target_id = session_id or _new_session_id()
        self._validate_session_id(target_id)
        if self._lookup(target_id) is not None:
            raise SessionAccessError(f"session already exists: {target_id}")
        metadata = dict(source.metadata)
        metadata.update(
            {
                "project_id": self.project_id,
                "kind": PRIMARY_KIND,
                "forked_from": source.session_id,
            }
        )
        return SessionAccessDescriptor(
            session_id=target_id,
            project_id=self.project_id,
            metadata=metadata,
        )

    def _lookup(self, session_id: str) -> Any | None:
        if self.journal is None:
            return None
        if isinstance(self.journal, Mapping):
            return self.journal.get(session_id)
        for name in ("get_session", "read_session", "session_metadata", "lookup"):
            method = getattr(self.journal, name, None)
            if method is None:
                continue
            try:
                value = method(session_id)
                return value if value else None
            except (KeyError, LookupError, SessionNotFoundError):
                return None
        read_events = getattr(self.journal, "read_events", None)
        if read_events is not None:
            try:
                value = read_events(session_id)
            except (KeyError, LookupError, ValueError):
                return None
            return value if value else None
        return None

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not session_id or "/" in session_id or "\\" in session_id or session_id in {".", ".."}:
            raise SessionAccessError(f"invalid session id: {session_id!r}")

    @staticmethod
    def _coerce_descriptor(session_id: str, value: Any) -> SessionAccessDescriptor | None:
        if value is None:
            return None
        if isinstance(value, SessionAccessDescriptor):
            return value
        if isinstance(value, (list, tuple)):
            for item in value:
                kind = _field(item, "kind", "type")
                if kind in {"session.created", "session_created"}:
                    value = item
                    break
            else:
                return None

        metadata = _mapping(_field(value, "metadata"))
        data = _mapping(_field(value, "data"))
        payload = _mapping(_field(value, "payload"))
        merged = {**payload, **data, **metadata}
        resolved_id = str(_field(value, "session_id") or merged.get("session_id") or session_id)
        resolved_project = _field(value, "project_id") or merged.get("project_id")
        event_kind = _field(value, "kind")
        kind = merged.get("kind") or (None if event_kind in {"session.created", "session_created"} else event_kind) or PRIMARY_KIND
        if not resolved_project:
            return None
        return SessionAccessDescriptor(
            session_id=resolved_id,
            project_id=str(resolved_project),
            kind=str(kind),
            parent_session_id=_optional_text(_field(value, "parent_session_id") or merged.get("parent_session_id")),
            parent_trace_id=_optional_text(_field(value, "parent_trace_id") or merged.get("parent_trace_id")),
            worktree_metadata=_mapping(_field(value, "worktree_metadata") or merged.get("worktree_metadata")),
            metadata=merged,
        )


@dataclass(slots=True)
class ChildSessionFactory:
    """The sole session-creation boundary for persistent subagent sessions."""

    policy: SessionAccessPolicy | None
    constructor: Callable[..., Any] | None = None
    allow_standalone_for_tests: bool = False

    @classmethod
    def standalone_for_tests(cls, constructor: Callable[..., Any]) -> "ChildSessionFactory":
        """Build the explicit test-only adapter for engines without project state."""
        return cls(policy=None, constructor=constructor, allow_standalone_for_tests=True)

    def create_child_session(
        self,
        *,
        parent_session_id: str,
        parent_trace_id: str | None,
        project_id: str | None,
        worktree_metadata: Mapping[str, Any],
        delegate_role: str | None = None,
        delegate_task: str | None = None,
        triggering_observation_id: str | None = None,
        session_id: str | None = None,
        session_arguments: Mapping[str, Any],
    ) -> Any:
        """Authorize and construct one child through the installed boundary."""
        descriptor = self._authorize_child(
            parent_session_id=parent_session_id,
            parent_trace_id=parent_trace_id,
            project_id=project_id,
            worktree_metadata=worktree_metadata,
            delegate_role=delegate_role,
            delegate_task=delegate_task,
            triggering_observation_id=triggering_observation_id,
            session_id=session_id,
        )
        if self.constructor is None:
            raise SessionAccessError("child session constructor is not installed")
        return self.constructor(descriptor, **dict(session_arguments))

    def _authorize_child(
        self,
        *,
        parent_session_id: str,
        parent_trace_id: str | None,
        project_id: str | None,
        worktree_metadata: Mapping[str, Any],
        delegate_role: str | None,
        delegate_task: str | None,
        triggering_observation_id: str | None,
        session_id: str | None,
    ) -> SessionAccessDescriptor:
        if self.policy is not None:
            return self.create_child(
                parent_session_id=parent_session_id,
                parent_trace_id=parent_trace_id,
                project_id=project_id,
                worktree_metadata=worktree_metadata,
                delegate_role=delegate_role,
                delegate_task=delegate_task,
                triggering_observation_id=triggering_observation_id,
                session_id=session_id,
            )
        if not self.allow_standalone_for_tests:
            raise SessionAccessError("subagent child session requires an access policy")
        child_id = session_id or _new_session_id()
        metadata: dict[str, Any] = {
            "kind": SUBAGENT_KIND,
            "parent_session_id": parent_session_id,
            "worktree_metadata": dict(worktree_metadata),
        }
        if project_id is not None:
            metadata["project_id"] = project_id
        if parent_trace_id is not None:
            metadata["parent_trace_id"] = parent_trace_id
        if delegate_role is not None:
            metadata["delegate_role"] = delegate_role
        if delegate_task is not None:
            metadata["delegate_task"] = delegate_task
        if triggering_observation_id is not None:
            metadata["parent_observation_id"] = triggering_observation_id
            metadata["triggering_observation_id"] = triggering_observation_id
        return SessionAccessDescriptor(
            session_id=child_id,
            project_id=project_id or "",
            kind=SUBAGENT_KIND,
            parent_session_id=parent_session_id,
            parent_trace_id=parent_trace_id,
            worktree_metadata=dict(worktree_metadata),
            metadata=metadata,
        )

    def create_child(
        self,
        *,
        parent_session_id: str,
        parent_trace_id: str,
        project_id: str | None,
        worktree_metadata: Mapping[str, Any],
        delegate_role: str | None = None,
        delegate_task: str | None = None,
        triggering_observation_id: str | None = None,
        session_id: str | None = None,
    ) -> SessionAccessDescriptor:
        if self.policy is None:
            raise SessionAccessError("subagent child session requires an access policy")
        if project_id != self.policy.project_id:
            raise SessionAccessError("subagent project_id must match its parent project")
        if not parent_trace_id:
            raise SessionAccessError("subagent parent_trace_id is required")
        if not isinstance(worktree_metadata, Mapping):
            raise SessionAccessError("subagent worktree_metadata must be a mapping")
        parent = self.policy.open_primary(parent_session_id)
        child_id = session_id or _new_session_id()
        self.policy._validate_session_id(child_id)
        if self.policy._lookup(child_id) is not None:
            raise SessionAccessError(f"session already exists: {child_id}")
        metadata = {
            "project_id": parent.project_id,
            "kind": SUBAGENT_KIND,
            "parent_session_id": parent.session_id,
            "parent_trace_id": parent_trace_id,
            "worktree_metadata": dict(worktree_metadata),
        }
        if delegate_role is not None:
            metadata["delegate_role"] = delegate_role
        if delegate_task is not None:
            metadata["delegate_task"] = delegate_task
        if triggering_observation_id is not None:
            metadata["parent_observation_id"] = triggering_observation_id
            metadata["triggering_observation_id"] = triggering_observation_id
        if worktree_metadata.get("path") is not None:
            metadata["worktree_path"] = str(worktree_metadata["path"])
        if worktree_metadata.get("branch") is not None:
            metadata["worktree_branch"] = str(worktree_metadata["branch"])
        return SessionAccessDescriptor(
            session_id=child_id,
            project_id=parent.project_id,
            kind=SUBAGENT_KIND,
            parent_session_id=parent.session_id,
            parent_trace_id=parent_trace_id,
            worktree_metadata=dict(worktree_metadata),
            metadata=metadata,
        )


def _field(value: Any, *names: str) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None
