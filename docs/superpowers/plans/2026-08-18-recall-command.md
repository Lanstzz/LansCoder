# /recall Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/recall` slash command that lets users interactively rewind the conversation to a previous turn by truncating the session JSONL and rebuilding the session.

**Architecture:** Add `truncate_before_message()` to `JsonlSessionStore` for destructive JSONL truncation. Add `RecallCommandHandler` following the existing handler pattern (like `SessionCommandHandler`). Wire a `recall_picker` action into the TUI's existing picker framework. After truncation, rebuild the session via `SessionBootstrap.resume()`.

**Tech Stack:** Python 3.12+, pytest, JSONL file I/O

**Spec:** `docs/superpowers/specs/2026-08-18-recall-command-design.md`

## Global Constraints

- Truncation is destructive and irreversible — truncated data is discarded
- Only user-message boundaries are valid recall targets (never inside a tool loop)
- The `session_created` event (first line) is always preserved
- Follow existing handler pattern: `handle(self, text: str) -> CommandResult`
- Follow existing picker pattern: `action["type"]` + `picker_specs` dict + `picker_command`

---

### Task 1: `JsonlSessionStore.truncate_before_message()`

**Files:**
- Modify: `lanscoder/context/store.py:28-193`
- Test: `tests/test_recall.py`

**Interfaces:**
- Produces: `JsonlSessionStore.truncate_before_message(self, session_id: str, message_id: str) -> int`
- Produces: `JsonlSessionStore._session_path(self, session_id: str) -> Path` (already exists, consumed by truncate)

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for /recall truncation and handler."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lanscoder.context.events import SessionEvent
from lanscoder.context.store import JsonlSessionStore


def _write_session_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _make_event(session_id: str, event_id: str, event_type: str, payload: dict) -> dict:
    return {
        "id": event_id,
        "session_id": session_id,
        "type": event_type,
        "payload": payload,
        "created_at": "2026-08-18T12:00:00Z",
    }


def _make_user_message_event(session_id: str, message_id: str, content: str, turn: int) -> dict:
    return _make_event(
        session_id,
        f"evt_{message_id}",
        "user_message",
        {
            "message_id": message_id,
            "parts": [
                {
                    "id": f"part_{message_id}",
                    "message_id": message_id,
                    "kind": "text",
                    "content": content,
                    "metadata": {"created_turn": turn, "turn_id": turn},
                }
            ],
            "metadata": {},
        },
    )


def _make_assistant_message_event(session_id: str, message_id: str, content: str) -> dict:
    return _make_event(
        session_id,
        f"evt_{message_id}",
        "assistant_message",
        {
            "message_id": message_id,
            "parts": [
                {
                    "id": f"part_{message_id}",
                    "message_id": message_id,
                    "kind": "text",
                    "content": content,
                    "metadata": {},
                }
            ],
            "metadata": {"provider": "test", "model": "test", "finish_reason": "stop"},
        },
    )


class TestTruncateBeforeMessage:
    def test_truncate_to_specific_message(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi there"),
            _make_user_message_event(sid, "msg_03", "do something", 2),
            _make_assistant_message_event(sid, "msg_04", "ok done"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        retained = store.truncate_before_message(sid, "msg_03")

        assert retained == 3  # session_created + msg_01 + msg_02
        remaining = store.list_events(sid)
        assert len(remaining) == 3
        assert remaining[0].type == "session_created"
        assert remaining[1].type == "user_message"
        assert remaining[1].payload["message_id"] == "msg_01"
        assert remaining[2].type == "assistant_message"

    def test_truncate_to_first_turn(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi there"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        retained = store.truncate_before_message(sid, "msg_01")

        assert retained == 1  # only session_created
        remaining = store.list_events(sid)
        assert len(remaining) == 1
        assert remaining[0].type == "session_created"

    def test_truncate_preserves_session_created(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "turn 1", 1),
            _make_assistant_message_event(sid, "msg_02", "response 1"),
            _make_user_message_event(sid, "msg_03", "turn 2", 2),
            _make_assistant_message_event(sid, "msg_04", "response 2"),
            _make_user_message_event(sid, "msg_05", "turn 3", 3),
            _make_assistant_message_event(sid, "msg_06", "response 3"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        retained = store.truncate_before_message(sid, "msg_05")

        remaining = store.list_events(sid)
        assert remaining[0].type == "session_created"
        assert remaining[0].payload["session_id"] == sid
        assert remaining[-1].type == "assistant_message"
        assert remaining[-1].payload["message_id"] == "msg_04"

    def test_truncate_atomic_on_error(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        original_content = store._session_path(sid).read_text()

        with pytest.raises(ValueError, match="message_id not found"):
            store.truncate_before_message(sid, "msg_nonexistent")

        assert store._session_path(sid).read_text() == original_content

    def test_truncate_nonexistent_session(self, tmp_path):
        store = JsonlSessionStore(tmp_path)

        with pytest.raises(FileNotFoundError):
            store.truncate_before_message("sess_nonexistent", "msg_01")

    def test_truncate_rejects_non_user_message(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        with pytest.raises(ValueError, match="is not a user_message"):
            store.truncate_before_message(sid, "msg_02")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recall.py::TestTruncateBeforeMessage -v`
Expected: 6 tests FAIL with "has no attribute 'truncate_before_message'"

- [ ] **Step 3: Write `truncate_before_message()` implementation**

Add to `JsonlSessionStore` class in `lanscoder/context/store.py`:

```python
import os
import tempfile

def truncate_before_message(self, session_id: str, message_id: str) -> int:
    """Truncate the session JSONL to exclude the given message and everything after it.

    The target message_id must belong to a user_message event. The file is
    truncated to the line immediately before that event. The session_created
    event (first line) is always preserved.

    Returns:
        The number of lines retained after truncation.

    Raises:
        FileNotFoundError: if the session file does not exist.
        ValueError: if message_id is not found or does not belong to a user_message.
    """
    path = self._session_path(session_id)
    if not path.exists():
        raise FileNotFoundError(f"Session file not found: {path}")

    events = self.list_events(session_id)
    if not events:
        raise ValueError(f"Session {session_id} has no events")

    # Find the target line index (1-based)
    target_line: int | None = None
    for index, event in enumerate(events):
        if event.type == "user_message" and str(event.payload.get("message_id") or "") == message_id:
            target_line = index
            break

    if target_line is None:
        # Check if the message_id exists at all (but with wrong type)
        for index, event in enumerate(events):
            if str(event.payload.get("message_id") or "") == message_id:
                raise ValueError(
                    f"message_id {message_id} is not a user_message event (type={event.type}); "
                    f"can only recall to user message boundaries"
                )
        raise ValueError(f"message_id {message_id} not found in session {session_id}")

    # Read all lines from the file
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    # Validate: first line must be session_created
    if target_line == 0:
        raise ValueError("Cannot truncate before the session_created event")

    retained_lines = lines[:target_line]

    # Atomic write via temp file
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{session_id}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.writelines(retained_lines)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return len(retained_lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recall.py::TestTruncateBeforeMessage -v`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add lanscoder/context/store.py tests/test_recall.py
git commit -m "feat: add truncate_before_message to JsonlSessionStore"
```

---

### Task 2: `SessionIndex` single-session rebuild

**Files:**
- Modify: `lanscoder/session/index.py:50-61`
- Test: `tests/test_recall.py`

**Interfaces:**
- Consumes: `SessionIndex.rebuild()` (already exists, rebuilds all)
- Produces: `SessionIndex.rebuild_session(self, session_id: str) -> None` — rebuilds a single session's index entry

- [ ] **Step 1: Write the failing test**

Append to `tests/test_recall.py`:

```python
from lanscoder.session.index import SessionIndex


class TestSessionIndexRebuildSession:
    def test_rebuild_session_updates_index(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_test123456"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "hello", 1),
            _make_assistant_message_event(sid, "msg_02", "hi there"),
            _make_user_message_event(sid, "msg_03", "do something", 2),
            _make_assistant_message_event(sid, "msg_04", "ok done"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        index = SessionIndex(tmp_path)
        index.rebuild()
        records_before = index.list_records()
        assert len(records_before) == 1
        assert records_before[0].user_turn_count == 2

        # Truncate to turn 1
        store.truncate_before_message(sid, "msg_03")
        # Rebuild just this session's index entry
        index.rebuild_session(sid)

        records_after = index.list_records()
        assert len(records_after) == 1
        assert records_after[0].user_turn_count == 1
        assert records_after[0].message_count == 2  # msg_01 + msg_02
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recall.py::TestSessionIndexRebuildSession -v`
Expected: FAIL with "has no attribute 'rebuild_session'"

- [ ] **Step 3: Write `rebuild_session()` implementation**

Add to `SessionIndex` class in `lanscoder/session/index.py`:

```python
def rebuild_session(self, session_id: str) -> None:
    """Rebuild the index entry for a single session from its JSONL file."""
    from lanscoder.session.catalog import record_from_path

    path = self.root / "sessions" / f"{session_id}.jsonl"
    if not path.exists():
        with _INDEX_LOCK:
            data = self._load_data()
            data["sessions"].pop(session_id, None)
            self._write_data(data)
        return

    record = record_from_path(path)
    with _INDEX_LOCK:
        data = self._load_data()
        data["sessions"][session_id] = _record_to_dict(record)
        self._write_data(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recall.py::TestSessionIndexRebuildSession -v`
Expected: 1 test PASS

- [ ] **Step 5: Commit**

```bash
git add lanscoder/session/index.py tests/test_recall.py
git commit -m "feat: add rebuild_session to SessionIndex for single-session index rebuild"
```

---

### Task 3: `RecallCommandHandler`

**Files:**
- Create: `lanscoder/app/recall_commands.py`
- Test: `tests/test_recall.py`

**Interfaces:**
- Consumes: `JsonlSessionStore.truncate_before_message(session_id, message_id) -> int` (from Task 1)
- Consumes: `SessionIndex.rebuild_session(session_id) -> None` (from Task 2)
- Consumes: `SessionBootstrap.resume(session_id) -> AgentSession` (existing)
- Consumes: `CommandResult`, `CommandHandlerLike` protocol (existing)
- Produces: `RecallCommandHandler(session, store, bootstrap, on_recall)` — dataclass with `handle(text) -> CommandResult` and `recall_to(message_id) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recall.py`:

```python
from lanscoder.app.recall_commands import RecallCommandHandler
from lanscoder.app.commands import CommandResult
from lanscoder.agent.session import AgentSession
from lanscoder.session.bootstrap import SessionBootstrap


class _FakeSessionLike:
    """Minimal SessionLike for RecallCommandHandler tests."""
    def __init__(self, session_id: str, messages: list):
        self.session_id = session_id
        self._messages = messages

    def rebuild_view(self):
        from lanscoder.context.models import SessionView
        return SessionView(session_id=self.session_id, messages=list(self._messages))

    @property
    def runtime_state(self):
        from lanscoder.context.runtime_state import SessionRuntimeState
        return SessionRuntimeState()

    @property
    def current_turn(self):
        return max(
            (part.metadata.get("created_turn", 0) for msg in self._messages for part in msg.parts),
            default=0,
        )


def _make_msg(msg_id: str, role: str, content: str, turn: int = 1):
    from lanscoder.context.models import AgentMessage, MessagePart
    return AgentMessage(
        id=msg_id,
        session_id="sess_test",
        role=role,
        parts=[MessagePart(
            id=f"part_{msg_id}",
            message_id=msg_id,
            kind="text",
            content=content,
            metadata={"created_turn": turn, "turn_id": turn},
        )],
    )


class TestRecallCommandHandler:
    def test_handler_lists_user_messages(self):
        messages = [
            _make_msg("msg_01", "user", "hello world", 1),
            _make_msg("msg_02", "assistant", "hi there", 1),
            _make_msg("msg_03", "user", "do something please", 2),
            _make_msg("msg_04", "assistant", "ok", 2),
            _make_msg("msg_05", "user", "another thing", 3),
            _make_msg("msg_06", "assistant", "done", 3),
        ]
        session = _FakeSessionLike("sess_test", messages)
        handler = RecallCommandHandler(
            session=session,
            store=None,  # type: ignore[arg-type]
            bootstrap=None,  # type: ignore[arg-type]
            on_recall=None,  # type: ignore[arg-type]
        )

        result = handler.handle("/recall")

        assert result.handled is True
        assert result.action is not None
        assert result.action["type"] == "recall_picker"
        turns = result.action["turns"]
        assert len(turns) == 3
        assert turns[0]["turn_number"] == 1
        assert turns[0]["message_id"] == "msg_01"
        assert "hello world" in turns[0]["summary"]
        assert turns[1]["turn_number"] == 2
        assert turns[1]["message_id"] == "msg_03"
        assert turns[2]["turn_number"] == 3
        assert turns[2]["message_id"] == "msg_05"

    def test_handler_ignores_non_recall_commands(self):
        handler = RecallCommandHandler(
            session=_FakeSessionLike("sess_test", []),
            store=None,  # type: ignore[arg-type]
            bootstrap=None,  # type: ignore[arg-type]
            on_recall=None,  # type: ignore[arg-type]
        )

        result = handler.handle("/help")

        assert result.handled is False

    def test_handler_empty_session(self):
        handler = RecallCommandHandler(
            session=_FakeSessionLike("sess_test", []),
            store=None,  # type: ignore[arg-type]
            bootstrap=None,  # type: ignore[arg-type]
            on_recall=None,  # type: ignore[arg-type]
        )

        result = handler.handle("/recall")

        assert result.handled is True
        assert "No messages" in result.output

    def test_handler_single_turn(self):
        messages = [
            _make_msg("msg_01", "user", "hello", 1),
            _make_msg("msg_02", "assistant", "hi", 1),
        ]
        session = _FakeSessionLike("sess_test", messages)
        handler = RecallCommandHandler(
            session=session,
            store=None,  # type: ignore[arg-type]
            bootstrap=None,  # type: ignore[arg-type]
            on_recall=None,  # type: ignore[arg-type]
        )

        result = handler.handle("/recall")

        assert result.handled is True
        assert "Nothing to recall" in result.output
        assert result.action is None

    def test_recall_to_truncates_and_swaps(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_recall_test"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "turn 1", 1),
            _make_assistant_message_event(sid, "msg_02", "response 1"),
            _make_user_message_event(sid, "msg_03", "turn 2", 2),
            _make_assistant_message_event(sid, "msg_04", "response 2"),
            _make_user_message_event(sid, "msg_05", "turn 3", 3),
            _make_assistant_message_event(sid, "msg_06", "response 3"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        # Build a real AgentSession so we can resume it
        bootstrap = SessionBootstrap(
            store=store,
            project_root=tmp_path,
            data_root=tmp_path,
        )
        session = bootstrap.create(session_id=sid)
        # Rebuild the session from the pre-written events
        session = bootstrap.resume(sid)

        swapped_session = None

        def on_recall(new_session):
            nonlocal swapped_session
            swapped_session = new_session

        handler = RecallCommandHandler(
            session=session,
            store=store,
            bootstrap=bootstrap,
            on_recall=on_recall,
        )

        output = handler.recall_to("msg_03")

        assert "Recalled" in output
        assert "turn 2" not in output  # removed
        assert swapped_session is not None
        assert swapped_session.session_id == sid

        # Verify the truncated session has correct messages
        view = swapped_session.rebuild_view()
        user_messages = [m for m in view.messages if m.role == "user"]
        assert len(user_messages) == 1
        assert user_messages[0].id == "msg_01"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recall.py::TestRecallCommandHandler -v`
Expected: FAIL with "No module named 'lanscoder.app.recall_commands'"

- [ ] **Step 3: Write `RecallCommandHandler` implementation**

Create `lanscoder/app/recall_commands.py`:

```python
"""Recall slash command — rewind conversation to a previous turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from lanscoder.app.commands import CommandResult
from lanscoder.context.models import SessionView
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore


class SessionLike(Protocol):
    session_id: str
    runtime_state: SessionRuntimeState
    current_turn: int

    def rebuild_view(self) -> SessionView: ...


@dataclass(slots=True)
class RecallCommandHandler:
    """Handle /recall — interactive conversation rewind."""

    session: SessionLike
    store: JsonlSessionStore
    bootstrap: object  # SessionBootstrap, imported lazily to avoid circular imports
    on_recall: Callable[[object], None]  # callback to swap session in runner

    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if command != "/recall":
            return CommandResult(handled=False)

        view = self.session.rebuild_view()
        user_messages = [m for m in view.messages if m.role == "user"]

        if not user_messages:
            return CommandResult(handled=True, output="No messages to recall")

        if len(user_messages) <= 1:
            return CommandResult(
                handled=True,
                output="Nothing to recall — only one turn in this session",
            )

        turns = []
        for msg in user_messages:
            text_content = ""
            for part in msg.parts:
                if part.kind == "text" and part.content:
                    text_content = part.content
                    break
            turn_number = 1
            for part in msg.parts:
                tn = part.metadata.get("created_turn") or part.metadata.get("turn_id")
                if isinstance(tn, int) and tn > 0:
                    turn_number = tn
                    break
            summary = text_content[:80] if text_content else "(empty message)"
            turns.append({
                "turn_number": turn_number,
                "message_id": msg.id,
                "summary": summary,
            })

        return CommandResult(
            handled=True,
            output="Select a turn to recall to:",
            action={
                "type": "recall_picker",
                "turns": turns,
            },
        )

    def recall_to(self, message_id: str) -> str:
        """Truncate, rebuild, and swap session. Returns status message."""
        session_id = self.session.session_id
        self.store.truncate_before_message(session_id, message_id)

        from lanscoder.session.index import SessionIndex
        SessionIndex(self.store.root).rebuild_session(session_id)

        new_session = self.bootstrap.resume(session_id)
        self.on_recall(new_session)

        return f"Recalled to before message {message_id}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recall.py::TestRecallCommandHandler -v`
Expected: 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add lanscoder/app/recall_commands.py tests/test_recall.py
git commit -m "feat: add RecallCommandHandler for /recall slash command"
```

---

### Task 4: Wire into TUI picker framework and factory

**Files:**
- Modify: `lanscoder/app/picker_adapters.py:1-58`
- Modify: `lanscoder/app/tui.py:546-602`
- Modify: `lanscoder/app/factory.py:335-346`

**Interfaces:**
- Consumes: `RecallCommandHandler` (from Task 3)
- Consumes: `recall_picker_item` adapter function (new)
- Consumes: `picker_command` `"recall"` branch (new)
- Produces: `recall_picker` action wired into TUI picker_specs

- [ ] **Step 1: Add `recall_picker_item` and `picker_command` recall branch**

In `lanscoder/app/picker_adapters.py`, add after `skill_picker_item`:

```python
def recall_picker_item(item: dict[str, object]) -> TuiPickerItem:
    message_id = str(item.get("message_id") or "")
    turn_number = item.get("turn_number")
    summary = str(item.get("summary") or "")
    label = f"Turn {turn_number}"
    return TuiPickerItem(
        id=message_id,
        label=label,
        detail=summary,
    )
```

In the same file, add to `picker_command` function after the `skill` branch:

```python
    if kind == "recall":
        return f"/recall {item.id}" if item.id else None
```

- [ ] **Step 2: Add `recall_picker` to TUI picker_specs**

In `lanscoder/app/tui.py`, add to the `picker_specs` dict in `_handle_command_action` after the `"skill_picker"` entry:

```python
            "recall_picker": (
                "recall",
                "Select a turn to recall to:",
                "turns",
                recall_picker_item,
                "No turns to recall.",
                "Use up/down and enter to recall, or type a number.",
                "turns",
            ),
```

Also add the import at the top of `tui.py`:

```python
from lanscoder.app.picker_adapters import (
    session_picker_item,
    model_picker_item,
    skill_picker_item,
    recall_picker_item,
    picker_command,
    render_picker_item,
)
```

- [ ] **Step 3: Register `RecallCommandHandler` in factory**

In `lanscoder/app/factory.py`, add the import:

```python
from lanscoder.app.recall_commands import RecallCommandHandler
```

Then add `RecallCommandHandler` to the `CompositeCommandHandler` list. Place it after `session_handler`:

```python
        recall_handler = RecallCommandHandler(
            session=current,
            store=store,
            bootstrap=bootstrap,
            on_recall=current.set_session,
        )
        command_handler = CompositeCommandHandler(
            [
                HelpCommandHandler(),
                McpCommandHandler(mcp_manager),
                ModelCommandHandler(model_switcher),
                session_handler,
                recall_handler,
                context_handler,
                permission_handler,
                skill_handler,
                memory_handler,
            ]
        )
```

- [ ] **Step 4: Run existing tests to verify nothing breaks**

Run: `pytest tests/ -q --ignore=tests/test_recall.py`
Expected: all existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add lanscoder/app/picker_adapters.py lanscoder/app/tui.py lanscoder/app/factory.py
git commit -m "feat: wire /recall command into TUI picker and factory"
```

---

### Task 5: Add `/recall` to help

**Files:**
- Modify: `lanscoder/app/help_commands.py:9-30`

**Interfaces:**
- Consumes: `HELP_COMMANDS` list (existing)

- [ ] **Step 1: Add the help entry**

In `lanscoder/app/help_commands.py`, add to the `HELP_COMMANDS` list:

```python
    ("/recall", "Rewind conversation to a previous turn."),
```

Place it after the `/fork` entry for logical grouping with session commands.

- [ ] **Step 2: Verify help renders correctly**

Run: `pytest tests/ -q -k "help"` (if help tests exist) or:
Run: `pytest tests/ -q`
Expected: all tests PASS

- [ ] **Step 3: Commit**

```bash
git add lanscoder/app/help_commands.py
git commit -m "feat: add /recall to help command list"
```

---

### Task 6: Integration tests

**Files:**
- Test: `tests/test_recall.py`

**Interfaces:**
- Consumes: `JsonlSessionStore.truncate_before_message()` (from Task 1)
- Consumes: `RecallCommandHandler` (from Task 3)
- Consumes: `SessionBootstrap` (existing)

- [ ] **Step 1: Write integration tests**

Append to `tests/test_recall.py`:

```python
class TestRecallIntegration:
    def test_recall_roundtrip(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_integration"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "first turn", 1),
            _make_assistant_message_event(sid, "msg_02", "first response"),
            _make_user_message_event(sid, "msg_03", "second turn", 2),
            _make_assistant_message_event(sid, "msg_04", "second response"),
            _make_user_message_event(sid, "msg_05", "third turn", 3),
            _make_assistant_message_event(sid, "msg_06", "third response"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        # Bootstrap and resume
        bootstrap = SessionBootstrap(store=store, project_root=tmp_path, data_root=tmp_path)
        session = bootstrap.resume(sid)

        # Verify all 3 turns are present
        view = session.rebuild_view()
        user_msgs = [m for m in view.messages if m.role == "user"]
        assert len(user_msgs) == 3

        swapped = None

        def on_recall(new_session):
            nonlocal swapped
            swapped = new_session

        handler = RecallCommandHandler(
            session=session,
            store=store,
            bootstrap=bootstrap,
            on_recall=on_recall,
        )

        # Recall to turn 2 (message msg_03)
        handler.recall_to("msg_03")

        assert swapped is not None
        view = swapped.rebuild_view()
        user_msgs = [m for m in view.messages if m.role == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0].id == "msg_01"

    def test_recall_then_continue(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_continue"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "first turn", 1),
            _make_assistant_message_event(sid, "msg_02", "first response"),
            _make_user_message_event(sid, "msg_03", "second turn", 2),
            _make_assistant_message_event(sid, "msg_04", "second response"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        bootstrap = SessionBootstrap(store=store, project_root=tmp_path, data_root=tmp_path)
        session = bootstrap.resume(sid)

        swapped = None

        def on_recall(new_session):
            nonlocal swapped
            swapped = new_session

        handler = RecallCommandHandler(
            session=session,
            store=store,
            bootstrap=bootstrap,
            on_recall=on_recall,
        )

        handler.recall_to("msg_03")

        # After recall, append a new user message
        swapped.append_user_message("new turn after recall")
        swapped.append_assistant_response(
            type("ChatResponse", (), {
                "provider": "test",
                "model": "test",
                "content": "new response",
                "finish_reason": "stop",
                "tool_calls": None,
            })()
        )

        # Verify the new messages are persisted
        view = swapped.rebuild_view()
        user_msgs = [m for m in view.messages if m.role == "user"]
        assert len(user_msgs) == 2
        assert user_msgs[0].id == "msg_01"
        # The new message should have a different ID
        assert user_msgs[1].id != "msg_03"

    def test_recall_updates_session_index(self, tmp_path):
        store = JsonlSessionStore(tmp_path)
        sid = "sess_index_test"
        events = [
            _make_event(sid, "evt_01", "session_created", {"session_id": sid, "context_event_schema_version": "v2"}),
            _make_user_message_event(sid, "msg_01", "turn 1", 1),
            _make_assistant_message_event(sid, "msg_02", "response 1"),
            _make_user_message_event(sid, "msg_03", "turn 2", 2),
            _make_assistant_message_event(sid, "msg_04", "response 2"),
            _make_user_message_event(sid, "msg_05", "turn 3", 3),
            _make_assistant_message_event(sid, "msg_06", "response 3"),
        ]
        _write_session_jsonl(store._session_path(sid), events)

        from lanscoder.session.index import SessionIndex
        index = SessionIndex(tmp_path)
        index.rebuild()

        records = index.list_records()
        assert len(records) == 1
        assert records[0].user_turn_count == 3

        store.truncate_before_message(sid, "msg_03")
        index.rebuild_session(sid)

        records = index.list_records()
        assert len(records) == 1
        assert records[0].user_turn_count == 1
        assert records[0].message_count == 2
```

- [ ] **Step 2: Run integration tests**

Run: `pytest tests/test_recall.py::TestRecallIntegration -v`
Expected: 3 tests PASS

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -q`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_recall.py
git commit -m "test: add integration tests for /recall command"
```