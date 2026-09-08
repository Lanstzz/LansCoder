"""Read-only Git metadata captured with a root trace."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    head: str | None = None
    branch: str | None = None
    dirty: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {"git_head": self.head, "git_branch": self.branch, "git_dirty": self.dirty}


def snapshot_git(path: str | Path) -> GitSnapshot:
    root = Path(path)
    head = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=all", allow_empty=True)
    if head is None and branch is None and status is None:
        return GitSnapshot()
    return GitSnapshot(head=head, branch=branch, dirty=None if status is None else bool(status))


def get_git_snapshot(path: str | Path) -> GitSnapshot:
    return snapshot_git(path)


def _git(path: Path, *arguments: str, allow_empty: bool = False) -> str | None:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", *arguments],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if value or allow_empty else None


__all__ = ["GitSnapshot", "get_git_snapshot", "snapshot_git"]
