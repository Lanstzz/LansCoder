from __future__ import annotations

from lanscoder.app.activity_view import compact_tool_arguments, compact_tool_content
from lanscoder.app.tui_state import BlockKind, ChildItem, ChildKind, TranscriptBlock, TranscriptModel


class TranscriptProjector:
    def __init__(self, model: TranscriptModel) -> None:
        self.model = model
        self._current: TranscriptBlock | None = None

    def _close_current(self) -> None:
        self.end_turn()

    def _ensure_assistant(self) -> TranscriptBlock:
        if self._current is None or self._current.kind != BlockKind.ASSISTANT:
            block = self.model.add_block(BlockKind.ASSISTANT)
            self._current = block
        return self._current

    def start_user(self, text: str) -> None:
        self._close_current()
        self.model.add_block(BlockKind.USER, text)

    def flat_block(self, kind: BlockKind, text: str) -> None:
        self._close_current()
        self.model.add_block(kind, text)

    def start_assistant(self) -> None:
        self._ensure_assistant()

    def append_assistant_text(self, chunk: str) -> None:
        block = self._ensure_assistant()
        block.text += chunk

    def append_thinking(self, chunk: str) -> None:
        block = self._ensure_assistant()
        if block.children and block.children[-1].kind == ChildKind.THINKING:
            block.children[-1].body += chunk
            return
        block.children.append(ChildItem(ChildKind.THINKING, f"t{len(block.children)}", "Thinking…", body=chunk))

    def tool_event(
        self,
        tool_call_id: str,
        name: str,
        kind: str,
        *,
        arguments: str = "",
        ok: bool | None = None,
        result_body: str = "",
    ) -> None:
        block = self._ensure_assistant()
        if kind == "started":
            label = f"tool {name}"
            if arguments:
                label += f" {arguments}"
            for child in block.children:
                if child.kind == ChildKind.TOOL and child.key == tool_call_id:
                    child.label = label
                    child.status = "running"
                    return
            block.children.append(ChildItem(ChildKind.TOOL, tool_call_id, label, status="running"))
            return
        child = next((c for c in block.children if c.kind == ChildKind.TOOL and c.key == tool_call_id), None)
        if child is None:
            label = f"tool {name}"
            child = ChildItem(ChildKind.TOOL, tool_call_id, label, status="running")
            block.children.append(child)
        if kind == "finished":
            child.status = "success" if ok else "error"
            if result_body:
                child.body = result_body
        elif kind == "denied":
            child.status = "denied"
        else:
            child.status = "error"

    def end_turn(self) -> None:
        if self._current is not None:
            for child in self._current.children:
                if child.kind == ChildKind.TOOL and child.status == "running":
                    child.status = "error"
        self._current = None


def _reasoning_from_message(message) -> str:
    metadata = getattr(message, "metadata", None) or {}
    diagnostics = metadata.get("diagnostics") or {}
    if not isinstance(diagnostics, dict):
        return ""
    return str(diagnostics.get("reasoning") or "")


def replay_messages(projector: TranscriptProjector, messages) -> None:
    for message in messages:
        role = getattr(message, "role", "")
        parts = list(getattr(message, "parts", []) or [])
        if role == "user":
            text = "\n".join(p.content for p in parts if p.kind == "text" and getattr(p, "content", None))
            if text:
                projector.start_user(text)
        elif role == "assistant":
            projector.start_assistant()
            for part in parts:
                if part.kind == "text" and getattr(part, "content", None):
                    projector.append_assistant_text(part.content)
                elif part.kind == "tool_call":
                    meta = getattr(part, "metadata", None) or {}
                    projector.tool_event(
                        str(meta.get("tool_call_id") or getattr(part, "id", "") or ""),
                        str(meta.get("tool_name") or "tool"),
                        "started",
                        arguments=compact_tool_arguments(meta.get("arguments")),
                    )
            reasoning = _reasoning_from_message(message)
            if reasoning:
                projector.append_thinking(reasoning)
        elif role == "tool":
            for part in parts:
                if part.kind != "tool_result":
                    continue
                meta = getattr(part, "metadata", None) or {}
                projector.tool_event(
                    str(meta.get("tool_call_id") or ""),
                    str(meta.get("tool_name") or "tool"),
                    "finished",
                    ok=bool(meta.get("ok", True)),
                    result_body=compact_tool_content(str(getattr(part, "content", "") or "")),
                )
        elif role == "notification":
            text = "\n".join(p.content for p in parts if p.kind == "text" and getattr(p, "content", None))
            if text:
                projector.flat_block(BlockKind.SYSTEM, text)
    projector.end_turn()