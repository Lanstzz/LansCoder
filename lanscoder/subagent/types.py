from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

SubagentRole = Literal["researcher", "reviewer", "tester", "coder"]

READ_ONLY_TOOL_NAMES = frozenset(
    {
        "ls",
        "view",
        "grep",
        "glob",
        "tree",
        "read_multi",
        "git_status",
        "git_diff",
        "git_log",
        "diagnostics",
        "think",
        "retrieve_archive",
        "web_search",
        "fetch",
    }
)
REVIEWER_TOOL_NAMES = frozenset({"view", "grep", "git_status", "git_diff", "git_log", "read_multi", "think", "retrieve_archive"})
TESTER_TOOL_NAMES = READ_ONLY_TOOL_NAMES | frozenset({"shell", "python_exec"})
CODER_TOOL_NAMES = TESTER_TOOL_NAMES | frozenset({"write", "edit", "delete", "apply_patch"})


@dataclass(frozen=True, slots=True)
class SubagentProfile:
    role: SubagentRole
    description: str
    allowed_tool_names: frozenset[str]
    allow_background: bool = True
    requires_worktree: bool = False


SUBAGENT_PROFILES: dict[str, SubagentProfile] = {
    "researcher": SubagentProfile(
        role="researcher",
        description="Read-only codebase exploration and evidence collection.",
        allowed_tool_names=READ_ONLY_TOOL_NAMES,
    ),
    "reviewer": SubagentProfile(
        role="reviewer",
        description="Read-only review of diffs, call sites, and risks.",
        allowed_tool_names=REVIEWER_TOOL_NAMES,
    ),
    "tester": SubagentProfile(
        role="tester",
        description="Validation-focused investigation with diagnostics and approved execution tools.",
        allowed_tool_names=TESTER_TOOL_NAMES,
    ),
    "coder": SubagentProfile(
        role="coder",
        description="Implementation work. Background coding runs inside an isolated git worktree so it can never mutate the parent working tree.",
        allowed_tool_names=CODER_TOOL_NAMES,
        allow_background=True,
        requires_worktree=True,
    ),
}


@dataclass(slots=True)
class SubagentRequest:
    role: SubagentRole
    task: str
    parent_session_id: str
    parent_summary: str | None = None
    path_hints: list[str] = field(default_factory=list)
    run_in_background: bool = False
    isolate_worktree: bool = False


@dataclass(slots=True)
class SubagentResult:
    ok: bool
    role: SubagentRole
    child_session_id: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    error: str | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    diff_summary: str | None = None
    total_tokens: int | None = None
    provider_calls: int | None = None
    elapsed_seconds: float | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "child_session_id": self.child_session_id,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "files_changed": list(self.files_changed),
            "error": self.error,
            "worktree_path": self.worktree_path,
            "worktree_branch": self.worktree_branch,
            "diff_summary": self.diff_summary,
            "total_tokens": self.total_tokens,
            "provider_calls": self.provider_calls,
            "elapsed_seconds": self.elapsed_seconds,
        }


def role_allows_background(role: str) -> bool:
    profile = SUBAGENT_PROFILES.get(str(role).strip())
    return bool(profile and profile.allow_background)


def role_requires_worktree(role: str) -> bool:
    profile = SUBAGENT_PROFILES.get(str(role).strip())
    return bool(profile and profile.requires_worktree)


@runtime_checkable
class SubagentRunner(Protocol):

    foreground_progress: dict[str, Any] | None

    def profile(self, role: str) -> SubagentProfile | None: ...

    def run(self, request: SubagentRequest) -> SubagentResult: ...
