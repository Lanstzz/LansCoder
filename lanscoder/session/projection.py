"""Deterministic session projections over an append-only branch journal."""

from __future__ import annotations

from collections.abc import Sequence
from math import inf
from typing import Any

from lanscoder.session.branch import (
    BranchTopology,
    build_branch_topology,
    event_branch_id,
    event_kind,
    event_sequence,
)


def project_branch(
    events: Sequence[Any],
    topology: BranchTopology,
    branch_id: str,
    *,
    cutoff: int | float = inf,
) -> list[Any]:
    """Return ``Project(branch, cutoff)`` exactly as defined by the SDD."""

    node = topology.branches.get(branch_id)
    if node is None:
        raise ValueError(f"unknown branch: {branch_id}")
    local_cutoff = cutoff
    if node.parent_branch_id is not None:
        parent_cutoff = min(cutoff, node.base_sequence if node.base_sequence is not None else -1)
        projected = project_branch(events, topology, node.parent_branch_id, cutoff=parent_cutoff)
    else:
        projected = []

    local = [
        event
        for fallback, event in enumerate(events, start=1)
        if event_kind(event) != "session.recalled"
        and event_branch_id(event) == branch_id
        and event_sequence(event, fallback) <= local_cutoff
    ]
    projected.extend(local)
    return sorted(projected, key=lambda item: _sort_key(events, item))


def active_projection(
    events: Sequence[Any],
    *,
    topology: BranchTopology | None = None,
    active_branch_id: str | None = None,
    cutoff: int | float = inf,
) -> list[Any]:
    """Project the latest active branch, excluding topology-only recall events."""

    resolved_topology = topology or build_branch_topology(events, active_branch_id=active_branch_id)
    branch_id = active_branch_id or resolved_topology.active_branch_id
    if branch_id is None:
        return []
    return project_branch(events, resolved_topology, branch_id, cutoff=cutoff)


def visible_user_checkpoints(events: Sequence[Any], *, active_branch_id: str | None = None) -> list[str]:
    """Return user message ids visible on the active path, in replay order."""

    result: list[str] = []
    for event in active_projection(events, active_branch_id=active_branch_id):
        kind = event_kind(event)
        data = _event_data(event)
        is_user_message = kind == "user_message" or (
            kind == "message.appended" and str(data.get("role") or "") == "user"
        )
        if is_user_message:
            message_id = data.get("message_id")
            if message_id:
                result.append(str(message_id))
    return result


def _event_data(event: Any) -> dict[str, Any]:
    value = event.data if hasattr(event, "data") else event.payload if hasattr(event, "payload") else event.get("data", event.get("payload", {}))
    return dict(value) if isinstance(value, dict) else {}


def _sort_key(events: Sequence[Any], target: Any) -> int:
    for fallback, event in enumerate(events, start=1):
        if event is target:
            return event_sequence(event, fallback)
    return 0


__all__ = ["active_projection", "project_branch", "visible_user_checkpoints"]
