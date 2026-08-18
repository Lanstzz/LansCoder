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