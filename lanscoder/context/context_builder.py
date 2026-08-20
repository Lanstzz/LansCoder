from __future__ import annotations

from pathlib import Path

from lanscoder.context.checkpoint import Checkpoint, CheckpointIndex, checkpoint_summary_content
from lanscoder.context.models import AgentMessage, MessagePart, SessionView, latest_user_message_id
from lanscoder.context.tool_sequence import validate_tool_call_sequence
from lanscoder.input.attachments import load_image_base64
from lanscoder.providers.types import ChatMessage, ContentPart, ToolCall


class InvalidCheckpointBoundaryError(ValueError):
    pass


class ContextBuilder:

    def build_provider_messages(
        self,
        view: SessionView,
        *,
        system_prefix: list[ChatMessage] | None = None,
        checkpoint: Checkpoint | None = None,
        store_root: Path | None = None,
    ) -> list[ChatMessage]:
        active_checkpoint = checkpoint or CheckpointIndex(view.checkpoints).latest()
        messages = list(system_prefix or [])
        if active_checkpoint is not None:
            messages.append(ChatMessage(role="user", content=checkpoint_summary_content(active_checkpoint)))

        tail_messages = self._tail_messages(view, checkpoint=active_checkpoint)
        tail_messages = _collapse_identical_adjacent_duplicate_tool_calls(tail_messages)
        validate_tool_call_sequence(tail_messages)
        if _has_trimmed_text(tail_messages):
            messages.append(ChatMessage(role="user", content="[Earlier dialogue trimmed]"))
        latest_user_id = latest_user_message_id(tail_messages)
        for message in tail_messages:
            projected = self._project_message(
                message,
                preserve_trimmed_text=message.id == latest_user_id,
                store_root=store_root,
            )
            messages.extend(projected)
        return messages

    def projected_tool_result_part_ids(self, view: SessionView) -> tuple[str, ...]:

        checkpoint = CheckpointIndex(view.checkpoints).latest()
        tail = _collapse_identical_adjacent_duplicate_tool_calls(self._tail_messages(view, checkpoint=checkpoint))
        validate_tool_call_sequence(tail)
        return tuple(part.id for message in tail if message.role == "tool" for part in message.parts if part.kind == "tool_result")

    def _tail_messages(
        self,
        view: SessionView,
        *,
        checkpoint: Checkpoint | None,
    ) -> list[AgentMessage]:
        if checkpoint is None:
            return view.messages

        for index, message in enumerate(view.messages):
            if message.id == checkpoint.tail_start_message_id:
                tail = view.messages[index:]
                _validate_tail_boundary(tail)
                return tail
        raise InvalidCheckpointBoundaryError(
            f"checkpoint tail_start_message_id not found: {checkpoint.tail_start_message_id}",
        )

    def _project_message(
        self,
        message: AgentMessage,
        *,
        preserve_trimmed_text: bool = False,
        store_root: Path | None = None,
    ) -> list[ChatMessage]:
        if message.role == "system_meta":
            return []

        if message.role == "tool":
            return [_project_tool_part(part) for part in message.parts if part.kind in {"tool_result", "archive_placeholder"}]

        if message.role == "assistant":
            projected = _project_assistant_message(
                message,
                preserve_trimmed_text=preserve_trimmed_text or any(part.kind == "tool_call" for part in message.parts),
            )
            return [projected] if projected.content or projected.tool_calls else []

        if message.role == "user":
            visible_content = _join_visible_text(message.parts, preserve_trimmed_text=preserve_trimmed_text)
            content = _with_basis_message_id(message.id, visible_content)
            content_parts = _project_user_content_parts(message.parts, content=content, store_root=store_root)
            if not visible_content and content_parts is None:
                return []
            return [ChatMessage(role="user", content=content, content_parts=content_parts)]

        if message.role == "notification":
            content = _join_visible_text(message.parts, preserve_trimmed_text=preserve_trimmed_text)
            if not content:
                return []
            return [ChatMessage(role="user", content=content)]

        return []


def _project_assistant_message(
    message: AgentMessage,
    *,
    preserve_trimmed_text: bool = False,
) -> ChatMessage:

    text_parts = [part.content for part in message.parts if part.kind == "text" and (preserve_trimmed_text or _is_visible_text_part(part)) and part.content]
    tool_calls = [
        ToolCall(
            id=str(part.metadata["tool_call_id"]),
            name=str(part.metadata["tool_name"]),
            arguments=part.metadata.get("arguments", {}),
        )
        for part in message.parts
        if part.kind == "tool_call"
    ]
    return ChatMessage(role="assistant", content="\n".join(text_parts), tool_calls=tool_calls)


def _project_tool_part(part: MessagePart) -> ChatMessage:

    return ChatMessage(
        role="tool",
        content=part.content,
        name=str(part.metadata.get("tool_name")) if part.metadata.get("tool_name") else None,
        tool_call_id=str(part.metadata["tool_call_id"]),
    )


def _validate_tail_boundary(messages: list[AgentMessage]) -> None:
    if not messages:
        return
    first = messages[0]
    if first.role == "tool":
        raise InvalidCheckpointBoundaryError(
            "checkpoint tail starts with orphan tool result; move tail_start_message_id " "to the assistant tool_call before this tool result",
        )


def _join_visible_text(parts: list[MessagePart], *, preserve_trimmed_text: bool = False) -> str:
    return "\n".join(part.content for part in parts if part.kind in {"text", "file", "archive_placeholder"} and (preserve_trimmed_text or _is_visible_text_part(part)) and part.content)


def _has_trimmed_text(messages: list[AgentMessage]) -> bool:
    return any(part.kind == "text" and part.metadata.get("compaction_state") == "trimmed" for message in messages for part in message.parts)


def _is_visible_text_part(part: MessagePart) -> bool:
    return part.metadata.get("compaction_state") != "trimmed"


def _collapse_identical_adjacent_duplicate_tool_calls(messages: list[AgentMessage]) -> list[AgentMessage]:

    collapsed: list[AgentMessage] = []
    for message in messages:
        signature = _duplicate_tool_call_signature(message)
        if signature is not None and collapsed and signature == _duplicate_tool_call_signature(collapsed[-1]):
            continue
        collapsed.append(message)
    return collapsed


def _duplicate_tool_call_signature(message: AgentMessage) -> tuple[tuple[str, ...], tuple[tuple[str, str, object], ...]] | None:
    if message.role != "assistant":
        return None
    text = tuple(part.content for part in message.parts if part.kind == "text")
    tool_calls = tuple(
        (
            str(part.metadata.get("tool_call_id") or ""),
            str(part.metadata.get("tool_name") or ""),
            part.metadata.get("arguments", {}),
        )
        for part in message.parts
        if part.kind == "tool_call"
    )
    if not tool_calls or any(not call_id or not name for call_id, name, _ in tool_calls):
        return None
    if any(part.kind not in {"text", "tool_call"} for part in message.parts):
        return None
    return text, tool_calls


def _with_basis_message_id(message_id: str, content: str) -> str:
    return f"[context: basis_message_id={message_id}]\n{content}"


def _project_user_content_parts(
    parts: list[MessagePart],
    *,
    content: str,
    store_root: Path | None,
) -> list[ContentPart] | None:

    content_parts = [ContentPart(type="text", text=content)]
    for part in parts:
        if part.kind != "image" or not _is_visible_text_part(part):
            continue
        image_path = _attachment_path(part, store_root=store_root)
        media_type = part.metadata.get("media_type")
        if image_path is None or not isinstance(media_type, str):
            continue
        try:
            data_base64 = load_image_base64(image_path)
        except OSError:
            continue
        content_parts.append(
            ContentPart(
                type="image",
                media_type=media_type,
                data_base64=data_base64,
                filename=str(part.metadata.get("filename") or image_path.name),
            )
        )
    return content_parts if len(content_parts) > 1 else None


def _attachment_path(part: MessagePart, *, store_root: Path | None) -> Path | None:
    relative_path = part.metadata.get("path")
    if store_root is None or not isinstance(relative_path, str):
        return None
    root = store_root.resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None
