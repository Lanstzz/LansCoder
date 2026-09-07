from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from lanscoder.session.access import (
    ChildSessionFactory,
    SessionAccessDescriptor,
    SessionAccessError,
    SessionAccessPolicy,
    project_id_for_path,
)
from lanscoder.session.branch import build_branch_topology
from lanscoder.session.index import SessionIndex
from lanscoder.session.projection import active_projection, project_branch, visible_user_checkpoints


@dataclass(frozen=True)
class FakeEvent:
    sequence: int
    kind: str
    session_id: str = "sess_demo"
    branch_id: str | None = None
    data: dict[str, object] | None = None

    @property
    def payload(self) -> dict[str, object]:
        return self.data or {}


def event(sequence: int, event_type: str, *, branch_id: str | None = None, **data: object) -> FakeEvent:
    return FakeEvent(sequence=sequence, kind=event_type, branch_id=branch_id, data=data)


def test_project_id_uses_resolved_path_and_does_not_require_existing_directory(tmp_path: Path) -> None:
    project = tmp_path / "missing" / ".." / "project"

    assert project_id_for_path(project) == project_id_for_path(project.resolve(strict=False))
    assert len(project_id_for_path(project)) == 64


def test_access_policy_allows_only_same_project_primary_sessions(tmp_path: Path) -> None:
    project_id = project_id_for_path(tmp_path)
    journal = {
        "sess_primary": SessionAccessDescriptor("sess_primary", project_id),
        "sess_other": SessionAccessDescriptor("sess_other", "other-project"),
        "sess_child": SessionAccessDescriptor("sess_child", project_id, kind="subagent"),
    }
    policy = SessionAccessPolicy(tmp_path, journal=journal)

    assert policy.open_primary("sess_primary").kind == "primary"
    with pytest.raises(SessionAccessError, match="another project"):
        policy.open_primary("sess_other")
    with pytest.raises(SessionAccessError, match="not a primary"):
        policy.open_primary("sess_child")
    with pytest.raises(SessionAccessError, match="already exists"):
        policy.create_primary("sess_primary")


def test_child_factory_requires_parent_trace_and_inherits_project_identity(tmp_path: Path) -> None:
    project_id = project_id_for_path(tmp_path)
    journal = {"sess_parent": SessionAccessDescriptor("sess_parent", project_id)}
    policy = SessionAccessPolicy(tmp_path, journal=journal)
    factory = ChildSessionFactory(policy)

    child = factory.create_child(
        parent_session_id="sess_parent",
        parent_trace_id="trace_parent",
        project_id=project_id,
        worktree_metadata={"path": str(tmp_path / "worktree")},
        session_id="sess_child",
    )
    assert child.kind == "subagent"
    assert child.project_id == project_id
    assert child.parent_session_id == "sess_parent"
    assert child.worktree_metadata["path"].endswith("worktree")

    with pytest.raises(SessionAccessError, match="project_id"):
        factory.create_child(
            parent_session_id="sess_parent",
            parent_trace_id="trace_parent",
            project_id="wrong-project",
            worktree_metadata={},
        )


def test_active_projection_uses_parent_cutoff_and_excludes_siblings() -> None:
    events = [
        event(1, "session.created", branch_id="root", root_branch_id="root", kind="primary"),
        event(2, "message.appended", branch_id="root", role="user", message_id="u1"),
        event(3, "message.appended", branch_id="root", role="assistant", message_id="a1"),
        event(
            4,
            "session.recalled",
            branch_id="branch_a",
            new_branch_id="branch_a",
            parent_branch_id="root",
            base_sequence=2,
            excluded_target_message_id="u2",
        ),
        event(5, "message.appended", branch_id="branch_a", role="user", message_id="u_a"),
        event(
            6,
            "session.recalled",
            branch_id="branch_b",
            new_branch_id="branch_b",
            parent_branch_id="root",
            base_sequence=2,
            excluded_target_message_id="u2",
        ),
        event(7, "message.appended", branch_id="branch_b", role="user", message_id="u_b"),
    ]

    topology = build_branch_topology(events)
    projected = active_projection(events, topology=topology)
    projected_ids = [item.data.get("message_id") for item in projected if item.kind == "message.appended"]

    assert topology.active_branch_id == "branch_b"
    assert projected_ids == ["u1", "u_b"]
    assert "u_a" not in projected_ids
    assert all(item.kind != "session.recalled" for item in projected)
    assert [item.data.get("message_id") for item in project_branch(events, topology, "branch_a") if item.kind == "message.appended"] == ["u1", "u_a"]


def test_recall_only_switches_branch_and_visible_checkpoints_follow_active_path() -> None:
    events = [
        event(1, "session.created", branch_id="root", root_branch_id="root"),
        event(2, "user_message", branch_id="root", message_id="u1"),
        event(3, "user_message", branch_id="root", message_id="u2"),
        event(4, "session.recalled", branch_id="child", new_branch_id="child", parent_branch_id="root", base_sequence=2),
        event(5, "user_message", branch_id="child", message_id="u3"),
    ]

    assert visible_user_checkpoints(events) == ["u1", "u3"]


def test_session_index_rejects_legacy_projection_records(tmp_path: Path) -> None:
    project_id = project_id_for_path(tmp_path)
    journal = {
        "sess_primary": [
            event(1, "session.created", branch_id="root", root_branch_id="root", project_id=project_id, kind="primary", title="Primary"),
            event(2, "message.appended", branch_id="root", role="user", message_id="u1", content="hello"),
        ],
        "sess_other": [event(1, "session.created", branch_id="root", root_branch_id="root", project_id="other", kind="primary")],
        "sess_subagent": [event(1, "session.created", branch_id="root", root_branch_id="root", project_id=project_id, kind="subagent")],
    }
    index = SessionIndex(tmp_path, journal=journal, project_id=project_id)

    records = index.list_records()

    assert [record.session_id for record in records] == []
    assert index.list_records(kind=None) == []
