from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BlockKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    COMMAND = "command"
    ERROR = "error"


class ChildKind(StrEnum):
    THINKING = "thinking"
    TOOL = "tool"
    TEXT_RUN = "text_run"


@dataclass(slots=True)
class ChildItem:
    kind: ChildKind
    key: str
    label: str
    status: str | None = None
    body: str = ""
    expanded: bool = False
    # THINKING: 流式计时与非流式结算标记
    started_at: float | None = None
    finished: bool = False
    duration_seconds: float | None = None
    # TOOL: 完整调用原文(折叠行只用 label 预览)
    name: str = ""
    arguments: str = ""


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


@dataclass(slots=True)
class TuiTaskPlanPanelState:
    last_rendered_revision: int | None = None
