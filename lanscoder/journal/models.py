"""Schema v1 immutable journal models."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

JOURNAL_SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def new_event_id() -> str:
    return _new_id("evt")


def new_trace_id() -> str:
    return _new_id("trc")


def new_observation_id() -> str:
    return _new_id("obs")


def new_branch_id() -> str:
    return _new_id("brn")


@dataclass(frozen=True, slots=True)
class JournalEnvelope:
    schema_version: int
    sequence: int
    event_id: str
    occurred_at: str
    kind: str
    session_id: str
    trace_id: str | None = None
    observation_id: str | None = None
    parent_observation_id: str | None = None
    branch_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int) or self.schema_version != JOURNAL_SCHEMA_VERSION:
            raise ValueError(f"unsupported journal schema_version: {self.schema_version}")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        for name in ("event_id", "kind", "session_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.occurred_at, str) or not self.occurred_at:
            raise ValueError("occurred_at must be a non-empty string")
        if not isinstance(self.data, dict):
            raise ValueError("data must be a JSON object")
        if self.kind == "session.created":
            root_branch_id = self.data.get("root_branch_id")
            if not isinstance(root_branch_id, str) or root_branch_id != self.branch_id:
                raise ValueError("session.created must use branch_id as data.root_branch_id")
        try:
            json.dumps(self.data, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError("data must be JSON serializable") from error

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "kind": self.kind,
            "session_id": self.session_id,
            "data": self.data,
        }
        for key in ("trace_id", "observation_id", "parent_observation_id", "branch_id"):
            optional_value = getattr(self, key)
            if optional_value is not None:
                value[key] = optional_value
        return value

    @property
    def id(self) -> str:
        return self.event_id

    @property
    def type(self) -> str:
        if self.kind == "message.appended":
            role = self.data.get("role")
            return {
                "user": "user_message",
                "assistant": "assistant_message",
                "tool": "tool_result",
                "notification": "background_notification",
            }.get(role, "message_appended")
        return self.kind.replace(".", "_")

    @property
    def payload(self) -> dict[str, Any]:
        return self.data

    @property
    def created_at(self) -> str:
        return self.occurred_at

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "JournalEnvelope":
        if not isinstance(value, dict):
            raise ValueError("journal record must be a JSON object")
        return cls(
            schema_version=value["schema_version"],
            sequence=value["sequence"],
            event_id=value["event_id"],
            occurred_at=value["occurred_at"],
            kind=value["kind"],
            session_id=value["session_id"],
            trace_id=value.get("trace_id"),
            observation_id=value.get("observation_id"),
            parent_observation_id=value.get("parent_observation_id"),
            branch_id=value.get("branch_id"),
            data=value.get("data", {}),
        )

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        kind: str,
        session_id: str,
        data: dict[str, Any] | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
        trace_id: str | None = None,
        observation_id: str | None = None,
        parent_observation_id: str | None = None,
        branch_id: str | None = None,
    ) -> "JournalEnvelope":
        return cls(
            schema_version=JOURNAL_SCHEMA_VERSION,
            sequence=sequence,
            event_id=event_id or new_event_id(),
            occurred_at=occurred_at or utc_now_iso(),
            kind=kind,
            session_id=session_id,
            trace_id=trace_id,
            observation_id=observation_id,
            parent_observation_id=parent_observation_id,
            branch_id=branch_id,
            data=dict(data or {}),
        )


JournalEvent = JournalEnvelope
