"""Conservative redaction for portable trace and scorecard values."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b(\s*[:=]\s*)([^\s,;]+)")
_BEARER_TOKEN = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
_KNOWN_SECRET = re.compile(r"\b(?:sk|rk|AIza)[-_A-Za-z0-9]{8,}\b")
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+")


class Redactor:
    """Replace registered secrets and absolute paths with stable placeholders."""

    def __init__(self, *, sensitive_values: Sequence[str] = (), paths: Sequence[str | Path] = ()) -> None:
        self._sensitive_values = tuple(sorted((value for value in sensitive_values if value), key=len, reverse=True))
        self._paths = tuple(sorted((str(Path(path).resolve()) for path in paths), key=len, reverse=True))

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self.redact(item) for item in value]
        return value

    def redact_text(self, text: str) -> str:
        result = text
        result = _SECRET_ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{_placeholder('REDACTED', match.group(3))}",
            result,
        )
        result = _BEARER_TOKEN.sub(lambda match: f"{match.group(1)}{_placeholder('REDACTED', match.group(2))}", result)
        result = _KNOWN_SECRET.sub(lambda match: _placeholder("REDACTED", match.group(0)), result)
        for secret in self._sensitive_values:
            result = result.replace(secret, _placeholder("REDACTED", secret))
        for path in self._paths:
            result = result.replace(path, _placeholder("PATH", path))
        return _ABSOLUTE_PATH.sub(lambda match: _placeholder("PATH", match.group(0)), result)


def _placeholder(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"<{kind}:{digest}>"
