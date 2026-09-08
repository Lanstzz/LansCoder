"""Content-addressed payload storage."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


class PayloadIntegrityError(ValueError):
    """Raised when a payload does not match its content address."""


@dataclass(frozen=True, slots=True)
class PayloadRef:
    sha256: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or any(character not in "0123456789abcdef" for character in self.sha256):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        if not self.media_type or not isinstance(self.media_type, str):
            raise ValueError("media_type must be a non-empty string")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        return {"sha256": self.sha256, "media_type": self.media_type, "size_bytes": self.size_bytes}

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "PayloadRef":
        return cls(
            sha256=str(value["sha256"]),
            media_type=str(value["media_type"]),
            size_bytes=int(value["size_bytes"]),
        )


class PayloadStore:
    """Atomically write and verify payloads below a storage root."""

    def __init__(self, root: str | Path | object) -> None:
        self.root = Path(root.payloads if hasattr(root, "payloads") else root)  # type: ignore[attr-defined]

    def put(self, content: bytes | bytearray | memoryview | str, *, media_type: str) -> PayloadRef:
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        digest = hashlib.sha256(raw).hexdigest()
        destination = self.root / digest
        self.root.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self._verify_file(destination, digest, len(raw))
            return PayloadRef(digest, media_type, len(raw))

        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            _fsync_directory(self.root)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return PayloadRef(digest, media_type, len(raw))

    def put_json(self, value: object, *, media_type: str = "application/json") -> PayloadRef:
        return self.put(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"), media_type=media_type)

    def read(self, reference: PayloadRef | str) -> bytes:
        digest = reference.sha256 if isinstance(reference, PayloadRef) else reference
        path = self.root / digest
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise PayloadIntegrityError(f"payload {digest} does not match its SHA-256")
        if isinstance(reference, PayloadRef) and len(raw) != reference.size_bytes:
            raise PayloadIntegrityError(f"payload {digest} has an unexpected size")
        return raw

    def open(self, reference: PayloadRef | str) -> BinaryIO:
        """Open a verified payload as a binary stream."""
        raw = self.read(reference)
        from io import BytesIO

        return BytesIO(raw)

    @staticmethod
    def _verify_file(path: Path, digest: str, expected_size: int) -> None:
        raw = path.read_bytes()
        if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != digest:
            raise PayloadIntegrityError(f"existing payload {digest} is not content addressed")


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
