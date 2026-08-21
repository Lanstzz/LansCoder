from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BlockKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    COMMAND = "command"
    ERROR = "error"


class ChildKind(StrEnum):
    THINKING = "thinking"
    TOOL = "tool"


@dataclass(slots=True)
class ChildItem:
    kind: ChildKind
    key: str
    label: str
    status: str | None = None
    body: str = ""
    expanded: bool = False


@dataclass(slots=True)
class TranscriptBlock:
    kind: BlockKind
    text: str = ""
    children: list[ChildItem] = field(default_factory=list)
    streaming: bool = False


class TranscriptModel:
    def __init__(self) -> None:
        self.blocks: list[TranscriptBlock] = []

    def clear(self) -> None:
        self.blocks = []

    def last_block(self) -> TranscriptBlock | None:
        return self.blocks[-1] if self.blocks else None

    def add_block(self, kind: BlockKind, text: str = "") -> TranscriptBlock:
        block = TranscriptBlock(kind=kind, text=text)
        self.blocks.append(block)
        return block

    def find_last_command_block(self) -> TranscriptBlock | None:
        for block in reversed(self.blocks):
            if block.kind == BlockKind.COMMAND:
                return block
        return None


class TuiEntryKind(StrEnum):
    SYSTEM = "system"
    COMMAND = "command"
    USER = "user"
    ASSISTANT = "assistant"
    REASONING = "reasoning"
    TOOL = "tool"
    PERMISSION = "permission"
    ERROR = "error"


_DEFAULT_LABELS = {
    TuiEntryKind.SYSTEM: "system",
    TuiEntryKind.COMMAND: "command",
    TuiEntryKind.USER: "you",
    TuiEntryKind.ASSISTANT: "LansCoder",
    TuiEntryKind.REASONING: "thinking",
    TuiEntryKind.TOOL: "tool",
    TuiEntryKind.PERMISSION: "permission",
    TuiEntryKind.ERROR: "error",
}


@dataclass(slots=True)
class TuiTranscriptEntry:
    id: int
    kind: TuiEntryKind
    body: str
    label: str
    status: str | None = None
    widget: Any | None = None


@dataclass(slots=True)
class TuiToolActivity:
    name: str
    status: str
    summary: str = ""


@dataclass(slots=True)
class TuiTaskPlanPanelState:

    last_rendered_revision: int | None = None


@dataclass(slots=True)
class TuiTranscript:
    entries: list[TuiTranscriptEntry] = field(default_factory=list)
    active_tool: TuiToolActivity | None = None
    recent_tools: list[TuiToolActivity] = field(default_factory=list)
    _next_id: int = 1

    def add(
        self,
        kind: TuiEntryKind,
        body: str,
        *,
        label: str | None = None,
        status: str | None = None,
    ) -> TuiTranscriptEntry:
        entry = TuiTranscriptEntry(
            id=self._next_id,
            kind=kind,
            body=body,
            label=label or _DEFAULT_LABELS[kind],
            status=status,
        )
        self._next_id += 1
        self.entries.append(entry)
        return entry

    def record_tool_activity(self, name: str, status: str, summary: str = "") -> TuiToolActivity:
        activity = TuiToolActivity(name=name, status=status, summary=summary)
        if status == "running":
            self.active_tool = activity
            return activity
        self.active_tool = None
        self.recent_tools.append(activity)
        return activity
