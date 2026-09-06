"""Branch topology primitives for append-only session journals."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SessionBranchContext:
    """Explicit branch identity carried by context-affecting writes."""

    session_id: str
    branch_id: str
    root_branch_id: str | None = None
    branch_head_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class BranchNode:
    branch_id: str
    parent_branch_id: str | None = None
    base_sequence: int | None = None


@dataclass(slots=True)
class BranchTopology:
    root_branch_id: str
    branches: dict[str, BranchNode] = field(default_factory=dict)
    active_branch_id: str | None = None

    def __post_init__(self) -> None:
        self.branches.setdefault(self.root_branch_id, BranchNode(self.root_branch_id))
        if self.active_branch_id is None:
            self.active_branch_id = self.root_branch_id

    def path_to_root(self, branch_id: str) -> tuple[BranchNode, ...]:
        path: list[BranchNode] = []
        seen: set[str] = set()
        current = branch_id
        while current:
            if current in seen:
                raise ValueError(f"branch topology contains a cycle at {current}")
            seen.add(current)
            node = self.branches.get(current)
            if node is None:
                raise ValueError(f"unknown branch: {current}")
            path.append(node)
            current = node.parent_branch_id or ""
        return tuple(path)


def build_branch_topology(events: Sequence[Any], *, active_branch_id: str | None = None) -> BranchTopology:
    """Build the immutable parent/base graph from the complete journal.

    A recall event is topology only.  It is never treated as a replayable
    message by the projection functions.
    """

    ordered = sorted(enumerate(events), key=lambda item: event_sequence(item[1], item[0] + 1))
    root_branch_id: str | None = None
    recalls: list[tuple[int, str]] = []
    branches: dict[str, BranchNode] = {}

    for fallback_sequence, event in ordered:
        kind = event_kind(event)
        data = event_data(event)
        branch_id = event_branch_id(event)
        if kind in {"session.created", "session_created"}:
            root_branch_id = str(data.get("root_branch_id") or branch_id or root_branch_id or "root")
            branches.setdefault(root_branch_id, BranchNode(root_branch_id))
        if kind != "session.recalled":
            continue
        new_branch_id = str(data.get("new_branch_id") or branch_id or "")
        parent_branch_id = str(data.get("parent_branch_id") or "")
        base_sequence = data.get("base_sequence")
        if not new_branch_id or not parent_branch_id or not _is_non_negative_int(base_sequence):
            raise ValueError("session.recalled must define new_branch_id, parent_branch_id and base_sequence")
        if root_branch_id is None:
            root_branch_id = parent_branch_id
            branches.setdefault(root_branch_id, BranchNode(root_branch_id))
        existing = branches.get(new_branch_id)
        node = BranchNode(new_branch_id, parent_branch_id, int(base_sequence))
        if existing is not None and existing != node:
            raise ValueError(f"branch {new_branch_id} has conflicting topology")
        branches[new_branch_id] = node
        recalls.append((event_sequence(event, fallback_sequence), new_branch_id))

    if root_branch_id is None:
        root_branch_id = next((event_branch_id(event) for _, event in ordered if event_branch_id(event)), "root")
        branches.setdefault(root_branch_id, BranchNode(root_branch_id))
    latest_active = active_branch_id or (max(recalls)[1] if recalls else root_branch_id)
    topology = BranchTopology(root_branch_id=root_branch_id, branches=branches, active_branch_id=latest_active)
    topology.path_to_root(latest_active)
    for node in branches.values():
        if node.parent_branch_id:
            topology.path_to_root(node.branch_id)
    return topology


def event_sequence(event: Any, fallback: int) -> int:
    value = _field(event, "sequence")
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid event sequence: {value!r}")
    return value


def event_kind(event: Any) -> str:
    return str(_field(event, "kind", "type") or "")


def event_branch_id(event: Any) -> str | None:
    value = _field(event, "branch_id")
    return str(value) if value is not None else None


def event_data(event: Any) -> dict[str, Any]:
    value = _field(event, "data", "payload")
    return dict(value) if isinstance(value, Mapping) else {}


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


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = [
    "BranchNode",
    "BranchTopology",
    "SessionBranchContext",
    "build_branch_topology",
    "event_branch_id",
    "event_data",
    "event_kind",
    "event_sequence",
]
