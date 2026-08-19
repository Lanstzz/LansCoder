"""Recall slash command — rewind conversation to a previous turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from lanscoder.agent.background import BackgroundJobManager
from lanscoder.app.commands import CommandResult
from lanscoder.context.models import SessionView
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.session.resume import ResumeService


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
    busy_check: Callable[[], bool] = lambda: False  # True while a turn is in-flight / paused
    resume_service: ResumeService | None = None  # preferred resume path (B4)
    background_manager: BackgroundJobManager | None = None  # orphaned-job cancel (B3)

    def commands(self) -> list[tuple[str, str]]:
        return [("/recall", "Rewind conversation to a previous turn.")]

    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if command == "/recall":
            if self.busy_check():
                return self._busy_result()
            return self._handle_list()
        elif command.startswith("/recall "):
            if self.busy_check():
                return self._busy_result()
            return self._handle_recall_to(command)
        return CommandResult(handled=False)

    def _busy_result(self) -> CommandResult:
        return CommandResult(
            handled=True,
            output="当前 turn 尚未结束，请先等待或按 Esc 中断后再 recall。",
        )

    def _handle_list(self) -> CommandResult:
        """Handle bare /recall — show the turn picker."""
        view = self.session.rebuild_view()
        user_messages = [m for m in view.messages if m.role == "user"]

        if not user_messages:
            return CommandResult(handled=True, output="No messages to recall")

        # 压缩会把旧任务的文本 part 在有效视图里清空，但原始 user_message
        # 事件从未被改写；回退到原文，picker 才不会显示 (empty message)。
        original_texts = (
            self.store.original_user_message_texts(self.session.session_id)
            if self.store is not None
            else {}
        )
        turns = []
        for msg in user_messages:
            text_content = ""
            for part in msg.parts:
                if part.kind == "text" and part.content:
                    text_content = part.content
                    break
            if not text_content:
                text_content = original_texts.get(msg.id, "")
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

    def _handle_recall_to(self, command: str) -> CommandResult:
        """Handle /recall <message_id> — delegate to recall_to."""
        parts = command.split()
        if len(parts) != 2:
            return CommandResult(
                handled=True,
                output="Usage: /recall <message_id>",
            )
        message_id = parts[1]
        recalled_text = self._text_for_message(message_id)
        output = self.recall_to(message_id)
        action = {"type": "replay_session"}
        if recalled_text:
            action["recalled_text"] = recalled_text
        return CommandResult(
            handled=True,
            output=output,
            action=action,
        )

    def recall_to(self, message_id: str) -> str:
        """Truncate, rebuild, and swap session. Returns status message."""
        session_id = self.session.session_id
        target_turn = self._turn_for_message(message_id)
        self.store.truncate_before_message(session_id, message_id)

        from lanscoder.session.index import SessionIndex
        SessionIndex(self.store.root).rebuild_session(session_id)

        new_session = self._resume_session(session_id)

        if self.background_manager is not None and target_turn is not None:
            self.background_manager.abandon_since(session_id, min_dispatch_turn=target_turn)

        self.on_recall(new_session)

        return f"Recalled to before message {message_id}"

    def _resume_session(self, session_id: str):
        """Resume the truncated session through ResumeService when available.

        Reusing ResumeService keeps /recall consistent with /resume (schema
        validation + pending-permission restore). Falls back to raw bootstrap
        resume for callers that only wire bootstrap (e.g. tests).
        """

        if self.resume_service is not None:
            return self.resume_service.resume(session_id).session
        return self.bootstrap.resume(session_id)

    def _turn_for_message(self, message_id: str) -> int | None:
        """Return the turn number of a user message, or None if not found."""

        for msg in self.session.rebuild_view().messages:
            if msg.id != message_id or msg.role != "user":
                continue
            for part in msg.parts:
                turn = part.metadata.get("created_turn") or part.metadata.get("turn_id")
                if isinstance(turn, int) and turn > 0:
                    return turn
            return None
        return None

    def _text_for_message(self, message_id: str) -> str:
        """Return the text content of a user message, or "" if not found.

        Falls back to the store's original event text so recalling a turn whose
        text was compacted away still backfills what was actually said.
        """

        for msg in self.session.rebuild_view().messages:
            if msg.id != message_id or msg.role != "user":
                continue
            text = "\n".join(
                part.content for part in msg.parts if part.kind == "text" and part.content
            )
            if text or self.store is None:
                return text
            return self.store.original_user_message_texts(self.session.session_id).get(
                message_id, ""
            )
        return ""
