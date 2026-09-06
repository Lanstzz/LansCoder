from pathlib import Path

import pytest

from lanscoder.context.events import SessionEvent
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.writer import SessionEventWriter
from lanscoder.session.access import project_id_for_path
from lanscoder.session.errors import SessionUnsupportedSchemaError
from lanscoder.session.fork import ForkSessionService
from lanscoder.storage import LansCoderPaths


@pytest.mark.parametrize(
    ("schema_payload", "actual_version"),
    [
        ({}, "missing"),
        ({"context_event_schema_version": "v1"}, "v1"),
        ({"context_event_schema_version": "future"}, "future"),
    ],
)
def test_fork_rejects_unsupported_schema_without_writing_or_copying(
    tmp_path: Path,
    schema_payload: dict[str, str] | None,
    actual_version: str,
) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    primary_metadata = {
        "project_id": project_id_for_path(tmp_path),
        "kind": "primary",
    }
    store.append_event(
        SessionEvent(
            id="evt_created",
            session_id="sess_legacy",
            type="session_created",
            payload={"session_id": "sess_legacy", **primary_metadata, **schema_payload},
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_created_later",
            session_id="sess_legacy",
            type="session_created",
            payload={"context_event_schema_version": "v2"},
        )
    )
    archive = paths.storage_root / "archives" / "sess_legacy" / "archive.json"
    archive.parent.mkdir(parents=True)
    archive.write_text("source archive", encoding="utf-8")
    before_files = {path.relative_to(store.root): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    tool_calls: list[str] = []
    service = ForkSessionService(
        store=store,
        project_root=tmp_path,
        paths=paths,
        tools_provider=lambda: tool_calls.append("tools_provider") or [],
    )

    with pytest.raises(SessionUnsupportedSchemaError) as caught:
        service.fork("sess_legacy")

    after_files = {path.relative_to(store.root): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    assert caught.value.session_id == "sess_legacy"
    assert caught.value.actual_version == actual_version
    assert caught.value.expected_version == "v2"
    assert before_files == after_files
    assert tool_calls == []


def test_fork_accepts_v2_session_and_copies_events_and_archives(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    writer = SessionEventWriter(store=store, session_id="sess_source")
    writer.append_session_created(
        title="Source",
        project_id=project_id_for_path(tmp_path),
        kind="primary",
    )
    writer.append_user_message("历史消息")
    archive = paths.storage_root / "archives" / "sess_source" / "archive.json"
    archive.parent.mkdir(parents=True)
    archive.write_text("source archive", encoding="utf-8")

    result = ForkSessionService(store=store, project_root=tmp_path, paths=paths).fork("sess_source", title="Forked")

    assert result.session.session_id != "sess_source"
    assert result.record.title == "Forked"
    assert result.record.metadata["project_id"] == project_id_for_path(tmp_path)
    assert result.record.metadata["kind"] == "primary"
    assert result.session.rebuild_view().messages[0].parts[0].content == "历史消息"
    copied_archive = paths.storage_root / "archives" / result.session.session_id / "archive.json"
    assert copied_archive.read_text(encoding="utf-8") == "source archive"


def test_fork_rejects_future_schema_before_parsing_later_events(tmp_path: Path) -> None:
    paths = LansCoderPaths(storage_root=tmp_path / "storage", project_root=tmp_path)
    store = JsonlSessionStore(paths.storage_root)
    path = store.sessions_dir / "sess_future.jsonl"
    path.write_text(
        '{"id":"evt_created","session_id":"sess_future","type":"session_created",' '"payload":{"context_event_schema_version":"v3","project_id":"' + project_id_for_path(tmp_path) + '","kind":"primary"}}\n' '{"future_event_shape":true}\n',
        encoding="utf-8",
    )
    before = path.read_bytes()

    with pytest.raises(SessionUnsupportedSchemaError) as caught:
        ForkSessionService(store=store, project_root=tmp_path, paths=paths).fork("sess_future")

    assert caught.value.actual_version == "v3"
    assert path.read_bytes() == before
    assert list(store.sessions_dir.glob("*.jsonl")) == [path]
