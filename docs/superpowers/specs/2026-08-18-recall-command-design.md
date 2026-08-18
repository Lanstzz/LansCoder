# /recall Command Design

## Overview

Add a `/recall` slash command that lets users rewind the conversation to a
previous turn, similar to Claude Code's `/rewind`. The command presents an
interactive picker of conversation turns; selecting one truncates the
session JSONL at that boundary and rebuilds the session from the truncated
file.

## Motivation

Users sometimes want to undo recent turns — the model went down the wrong
path, a tool call produced unwanted side effects, or they simply want to
try a different approach from an earlier point. Today the only option is
`/fork` (which creates a copy) or `/resume` (which loads a different
session). Neither allows in-place rollback.

## Design

### Interaction Flow

```
User types /recall
  → RecallCommandHandler lists all user-message turns
  → TUI renders a picker (turn number + first 80 chars of message)
  → User selects turn N or presses Esc
  → If Esc: no-op
  → If selected:
      1. JsonlSessionStore.truncate_before_message(session_id, message_id)
      2. SessionIndex.rebuild(session_id)
      3. SessionBootstrap.resume(session_id) → new AgentSession
      4. TUI clears output, replays truncated messages
      5. User can continue from the recall point
```

### Safety Boundary: Only User-Message Boundaries

The `/recall` command only allows rewinding to **user message boundaries**
(the start of a turn). This guarantees the JSONL is never truncated in the
middle of a tool_call/tool_result sequence, which would produce an illegal
message sequence for providers.

The picker only displays `role="user"` messages. Each represents the start
of a turn. The tool loop (assistant → tool_calls → tool_results → … →
final assistant) is an atomic unit within that turn.

### Truncation Semantics

Truncation is **destructive and irreversible**. Truncated data is
discarded. The JSONL file is rewritten with only the events up to (but not
including) the target user message. The `session_created` event is always
preserved.

### Recovery After Recall

After truncation, the session is rebuilt from scratch via the existing
`SessionBootstrap.resume()` path. This reuses the same battle-tested logic
that `/resume` uses, including runtime state replay, checkpoint
restoration, and tool registry re-indexing.

## Files

| File | Action | Responsibility |
|---|---|---|
| `lanscoder/context/store.py` | Modify | Add `truncate_before_message()` method |
| `lanscoder/session/index.py` | Modify | Add `rebuild()` method for single session |
| `lanscoder/app/recall_commands.py` | **Create** | `RecallCommandHandler` class |
| `lanscoder/app/factory.py` | Modify | Register `RecallCommandHandler` |
| `lanscoder/app/help_commands.py` | Modify | Add `/recall` to help list |
| `tests/test_recall.py` | **Create** | Tests for truncation and handler |

## Key Interfaces

### JsonlSessionStore.truncate_before_message

```python
def truncate_before_message(self, session_id: str, message_id: str) -> int:
    """Truncate the session JSONL to exclude the given message and everything after it.

    The target message_id must belong to a user_message event. The file is
    truncated to the line immediately before that event. The session_created
    event (first line) is always preserved.

    Returns:
        The number of lines retained after truncation.

    Raises:
        FileNotFoundError: if the session file does not exist.
        ValueError: if message_id is not found in the session.
    """
```

### RecallCommandHandler

```python
@dataclass(slots=True)
class RecallCommandHandler:
    """Handle /recall — interactive conversation rewind."""

    session: SessionLike
    store: JsonlSessionStore
    bootstrap: SessionBootstrap
    on_recall: Callable[[AgentSession], None]  # callback to swap session in runner

    def handle(self, text: str) -> CommandResult:
        """If /recall, return a picker action; otherwise return handled=False."""

    def recall_to(self, message_id: str) -> str:
        """Truncate, rebuild, and swap session. Returns status message."""
```

### CommandResult.action

```python
# When /recall is typed:
CommandResult(
    handled=True,
    output="Select a turn to recall to:",
    action={
        "type": "recall_picker",
        "turns": [
            {"turn_number": 1, "message_id": "msg_...", "summary": "..."},
            ...
        ],
    },
)
```

## Error Handling

| Scenario | Behavior |
|---|---|
| Only 1 turn in session | "Nothing to recall — only one turn in this session" |
| No messages in session | "No messages to recall" |
| Picker cancelled (Esc) | No-op, empty output |
| JSONL file missing or corrupt | Error message, no truncation |
| Target message_id not found | ValueError, no truncation |
| Pending permission confirmation | Allowed; truncation clears pending state; rebuild recovers cleanly |
| Target is not a user_message | Rejected (internal guard; picker only shows user messages) |

## Testing Strategy

### Unit Tests (`tests/test_recall.py`)

- `test_truncate_to_specific_message` — truncate to a middle turn, verify retained lines
- `test_truncate_to_first_turn` — truncate to the first user message, only session_created remains
- `test_truncate_preserves_session_created` — session_created event always survives
- `test_truncate_atomic_on_error` — original file intact when message_id not found
- `test_truncate_nonexistent_session` — FileNotFoundError for missing session
- `test_handler_lists_user_messages` — handler correctly extracts user messages from view
- `test_handler_empty_session` — graceful handling of session with no messages
- `test_handler_single_turn` — "Nothing to recall" message for single-turn session

### Integration Tests

- `test_recall_roundtrip` — create session, write 3 turns, recall to turn 2, verify rebuilt view
- `test_recall_then_continue` — after recall, verify new messages can be appended and the session works
- `test_recall_updates_session_index` — verify SessionIndex is updated after truncation

## Non-Goals

- Undoing a recall (re-truncation is not reversible)
- Recalling into the middle of a tool loop
- Selective message deletion (only whole-turn truncation from the end)
- A `/recall last` or `/recall N` shorthand (can be added later; interactive picker is the MVP)