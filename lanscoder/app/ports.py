from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from lanscoder.context.manager import ContextCompactRequest, ContextCompactResult

if TYPE_CHECKING:
    from lanscoder.app.commands import CommandResult
    from lanscoder.input.attachments import UserAttachment


class CommandHandlerLike(Protocol):
    def handle(self, text: str) -> CommandResult: ...
    def commands(self) -> list[tuple[str, str]]: ...


class ChatRunnerLike(Protocol):
    last_pending_input: object | None

    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> Any: ...

    async def aresume_with_user_input(self, request_id: str, answer: str) -> Any: ...


class CurrentSessionLike(Protocol):
    session_id: str

    def rebuild_view(self) -> object: ...


class ContextManagerLike(Protocol):
    def compact_if_needed(self, request: ContextCompactRequest) -> ContextCompactResult: ...
