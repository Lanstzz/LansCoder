from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from lanscoder.agent.background import BackgroundJobManager
from lanscoder.app.commands import CommandResult
from lanscoder.context.models import SessionView
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.journal.models import new_branch_id
from lanscoder.session.branch import build_branch_topology
from lanscoder.session.projection import active_projection
from lanscoder.session.resume import ResumeService


class SessionLike(Protocol):
    session_id: str
    runtime_state: SessionRuntimeState
    current_turn: int

    def rebuild_view(self) -> SessionView: ...


@dataclass(slots=True)
class RecallCommandHandler:

    session: SessionLike
    store: JsonlSessionStore
    bootstrap: object
    on_recall: Callable[[object], None]
    busy_check: Callable[[], bool] = lambda: False
    resume_service: ResumeService | None = None
    background_manager: BackgroundJobManager | None = None

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
        view = self.session.rebuild_view()
        user_messages = [m for m in view.messages if m.role == "user"]

        if not user_messages:
            return CommandResult(handled=True, output="No messages to recall")

        original_texts = self.store.original_user_message_texts(self.session.session_id) if self.store is not None else {}
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
            turns.append(
                {
                    "turn_number": turn_number,
                    "message_id": msg.id,
                    "summary": summary,
                }
            )

        return CommandResult(
            handled=True,
            output="Select a turn to recall to:",
            action={
                "type": "recall_picker",
                "turns": turns,
            },
        )

    def _handle_recall_to(self, command: str) -> CommandResult:
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
        session_id = self.session.session_id
        events = self.store.list_events(session_id)
        topology = build_branch_topology(events)
        projected = active_projection(events, topology=topology)
        target_index = next(
            (
                index
                for index, event in enumerate(projected)
                if event.kind == "message.appended"
                and event.data.get("role") == "user"
                and str(event.data.get("message_id") or "") == message_id
            ),
            None,
        )
        if target_index is None:
            raise ValueError(f"message_id not found on the active branch: {message_id}")
        if target_index == 0:
            raise ValueError("cannot recall before the session.created event")

        parent_branch_id = topology.active_branch_id or topology.root_branch_id
        branch_id = new_branch_id()
        self.store.append_journal_event(
            session_id=session_id,
            kind="session.recalled",
            branch_id=branch_id,
            data={
                "new_branch_id": branch_id,
                "parent_branch_id": parent_branch_id,
                "base_sequence": projected[target_index - 1].sequence,
                "excluded_target_message_id": message_id,
            },
        )

        new_session = self._resume_session(session_id)

        self.on_recall(new_session)

        return f"Recalled to before message {message_id}"

    def _resume_session(self, session_id: str):

        if self.resume_service is not None:
            return self.resume_service.resume(session_id).session
        return self.bootstrap.resume(session_id)

    def _text_for_message(self, message_id: str) -> str:

        for msg in self.session.rebuild_view().messages:
            if msg.id != message_id or msg.role != "user":
                continue
            text = "\n".join(part.content for part in msg.parts if part.kind == "text" and part.content)
            if text or self.store is None:
                return text
            return self.store.original_user_message_texts(self.session.session_id).get(message_id, "")
        return ""
