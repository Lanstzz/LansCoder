"""Small cross-process advisory file locks."""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Self


class LockError(RuntimeError):
    """Raised when an advisory lock cannot be acquired."""


class AdvisoryLock(AbstractContextManager["AdvisoryLock"]):
    """An advisory lock backed by a persistent lock file."""

    def __init__(self, path: str | Path, *, shared: bool = False) -> None:
        self.path = Path(path)
        self.shared = shared
        self._handle: object | None = None

    def acquire(self) -> Self:
        if self._handle is not None:
            raise LockError(f"lock is already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            _lock_file(handle, shared=self.shared)
        except Exception as error:
            handle.close()
            raise LockError(f"could not acquire lock: {self.path}") from error
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            _unlock_file(handle)
        finally:
            handle.close()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def session_lock(paths: object, session_id: str, *, shared: bool = False) -> AdvisoryLock:
    """Build a lock for a ``LansCoderPaths``-like object."""
    return AdvisoryLock(paths.session_lock(session_id), shared=shared)  # type: ignore[attr-defined]


def _lock_file(handle: object, *, shared: bool) -> None:
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_RLCK if shared else msvcrt.LK_LOCK
        msvcrt.locking(handle.fileno(), mode, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    fcntl.flock(handle.fileno(), operation)  # type: ignore[attr-defined]


def _unlock_file(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)  # type: ignore[attr-defined]
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
