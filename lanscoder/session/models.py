from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from lanscoder.agent.session import AgentSession


SessionStatus = Literal["ok", "empty", "corrupt"]
ArchiveMode = Literal["placeholder", "preview_only"]


@dataclass(slots=True)
class SessionRecord:

    session_id: str
    title: str
    created_at: str | None = None
    updated_at: str | None = None
    workspace: str | None = None
    provider: str | None = None
    model: str | None = None
    message_count: int = 0
    user_turn_count: int = 0
    checkpoint_count: int = 0
    archive_count: int = 0
    latest_user_input: str | None = None
    latest_assistant_output: str | None = None
    latest_checkpoint_id: str | None = None
    status: SessionStatus | str = "ok"
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RedactionOptions:

    redact_paths: bool = True
    redact_secrets: bool = True


@dataclass(slots=True)
class ShareOptions:

    include_event_ids: bool = False
    include_compaction_metadata: bool = False
    include_tool_calls: bool = True
    include_tool_results: bool = False
    max_tool_result_chars: int = 1200
    redact_paths: bool = True
    redact_secrets: bool = True
    archive_mode: ArchiveMode | str = "placeholder"


@dataclass(slots=True)
class TranscriptEntry:

    role: str
    title: str
    content: str
    message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Transcript:

    session: SessionRecord
    entries: list[TranscriptEntry] = field(default_factory=list)


@dataclass(slots=True)
class ResumeResult:

    session: AgentSession
    record: SessionRecord
