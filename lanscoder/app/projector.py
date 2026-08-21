from __future__ import annotations

import time

from lanscoder.app.activity_view import compact_tool_arguments
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

    def append_assistant_text(self, chunk: str) -> bool:
        """追加一段回答文本;若此前有未结算的 thinking,一并结算。返回是否发生了结算。"""
        block = self._ensure_assistant()
        finalized = self._finish_thinking()
        block.text += chunk
        return finalized

    def _finalize_thinking(self, child: ChildItem) -> None:
        child.finished = True
        if child.started_at is not None:
            child.duration_seconds = max(0.0, time.monotonic() - child.started_at)

    def _finish_thinking(self) -> bool:
        """结算当前回合最近的未结算 thinking(任一 TOOL 子项之上的那个)。"""
        block = self._current
        if block is None:
            return False
        for child in reversed(block.children):
            if child.kind != ChildKind.THINKING or child.finished:
                continue
            self._finalize_thinking(child)
            return True
        return False

    def append_thinking(self, chunk: str, *, track_duration: bool = True) -> None:
        block = self._ensure_assistant()
        if block.children and block.children[-1].kind == ChildKind.THINKING:
            block.children[-1].body += chunk
            return
        block.children.append(
            ChildItem(
                ChildKind.THINKING,
                f"t{len(block.children)}",
                "Thinking…",
                body=chunk,
                started_at=time.monotonic() if track_duration else None,
            )
        )

    def tool_event(
        self,
        tool_call_id: str,
        name: str,
        kind: str,
        *,
        arguments: object = "",
        ok: bool | None = None,
        result_body: str = "",
    ) -> bool:
        """记录一次工具事件,返回是否同时结算了 thinking。

        ``arguments``/``result_body`` 为完整调用与结果原文;折叠预览在
        ``label`` 内用 compact 助手截断,展开时从 ``name``/``arguments``/``body`` 取全文。
        """
        finalized = self._finish_thinking()
        block = self._ensure_assistant()
        full_arguments = str(arguments) if arguments else ""
        if kind == "started":
            label = f"tool {name}"
            if full_arguments:
                label += f" {compact_tool_arguments(full_arguments)}"
            for child in block.children:
                if child.kind == ChildKind.TOOL and child.key == tool_call_id:
                    child.label = label
                    child.status = "running"
                    child.name = name
                    child.arguments = full_arguments
                    return finalized
            block.children.append(
                ChildItem(
                    ChildKind.TOOL,
                    tool_call_id,
                    label,
                    name=name,
                    arguments=full_arguments,
                    status="running",
                )
            )
            return finalized
        child = next((c for c in block.children if c.kind == ChildKind.TOOL and c.key == tool_call_id), None)
        if child is None:
            label = f"tool {name}"
            child = ChildItem(ChildKind.TOOL, tool_call_id, label, name=name, status="running")
            block.children.append(child)
        if kind == "finished":
            child.status = "success" if ok else "error"
            if result_body:
                child.body = result_body
        elif kind == "denied":
            child.status = "denied"
        else:
            child.status = "error"
        return finalized

    def end_turn(self) -> None:
        if self._current is not None:
            for child in self._current.children:
                if child.kind == ChildKind.TOOL and child.status == "running":
                    child.status = "error"
                elif child.kind == ChildKind.THINKING and not child.finished:
                    self._finalize_thinking(child)
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
            # 投影顺序决定显示顺序:thinking 先于同消息的 tool_call 与文本
            reasoning = _reasoning_from_message(message)
            if reasoning:
                projector.append_thinking(reasoning, track_duration=False)
            for part in parts:
                if part.kind == "text" and getattr(part, "content", None):
                    projector.append_assistant_text(part.content)
                elif part.kind == "tool_call":
                    meta = getattr(part, "metadata", None) or {}
                    projector.tool_event(
                        str(meta.get("tool_call_id") or getattr(part, "id", "") or ""),
                        str(meta.get("tool_name") or "tool"),
                        "started",
                        arguments=meta.get("arguments"),
                    )
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
                    result_body=str(getattr(part, "content", "") or ""),
                )
        elif role == "notification":
            text = "\n".join(p.content for p in parts if p.kind == "text" and getattr(p, "content", None))
            if text:
                projector.flat_block(BlockKind.SYSTEM, text)
    projector.end_turn()
