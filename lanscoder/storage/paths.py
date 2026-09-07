"""Canonical paths for LansCoder's local runtime storage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


def project_id_for_path(project_root: str | Path) -> str:
    """Return the stable identity for a project path."""
    resolved = Path(project_root).expanduser().resolve(strict=False)
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LansCoderPaths:
    """All filesystem locations owned by a LansCoder runtime.

    Constructing this object is intentionally side-effect free.  Callers that
    write data create the particular parent directory they need.
    """

    storage_root: Path | str | None = None
    project_root: Path | str | None = None

    def __post_init__(self) -> None:
        root = Path.home() / ".lanscoder" if self.storage_root is None else Path(self.storage_root)
        project = Path.cwd() if self.project_root is None else Path(self.project_root)
        object.__setattr__(self, "storage_root", root.expanduser())
        object.__setattr__(self, "project_root", project.expanduser().resolve(strict=False))

    @property
    def project_id(self) -> str:
        return project_id_for_path(self.project_root)

    @property
    def sessions(self) -> Path:
        return self.storage_root / "sessions"

    @property
    def payloads(self) -> Path:
        return self.storage_root / "payloads"

    @property
    def indexes(self) -> Path:
        return self.storage_root / "indexes"

    @property
    def locks(self) -> Path:
        return self.storage_root / "locks"

    @property
    def recovery(self) -> Path:
        return self.storage_root / "recovery"

    @property
    def recovery_tails(self) -> Path:
        return self.recovery / "tails"

    @property
    def tmp(self) -> Path:
        return self.storage_root / "tmp"

    @property
    def clipboard_tmp(self) -> Path:
        return self.tmp / "clipboard"

    @property
    def projects(self) -> Path:
        return self.storage_root / "projects"

    @property
    def project_state(self) -> Path:
        return self.projects / self.project_id

    @property
    def permissions(self) -> Path:
        return self.project_state / "permissions.json"

    @property
    def model_state(self) -> Path:
        return self.project_state / "model_state.json"

    @property
    def project_memory(self) -> Path:
        return self.project_state / "memory"

    @property
    def memory(self) -> Path:
        return self.storage_root / "memory"

    @property
    def skills(self) -> Path:
        return self.storage_root / "skills"

    @property
    def archives(self) -> Path:
        return self.storage_root / "archives"

    @property
    def attachments(self) -> Path:
        return self.payloads

    def session(self, session_id: str) -> Path:
        return self.sessions / f"{session_id}.jsonl"

    def session_lock(self, session_id: str) -> Path:
        return self.locks / f"{session_id}.lock"

    @property
    def index_lock(self) -> Path:
        return self.locks / "index.lock"

    def payload(self, sha256: str) -> Path:
        return self.payloads / sha256
