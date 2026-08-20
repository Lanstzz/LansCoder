from __future__ import annotations

from pathlib import Path


def truncate(value: str, max_chars: int, *, suffix: str = "\n\n[输出已截断]") -> tuple[str, bool]:

    if len(value) <= max_chars:
        return value, False
    return value[:max_chars] + suffix, True


def safe_read_text(path: Path, *, encoding: str = "utf-8") -> str:

    return path.read_text(encoding=encoding)


def optional_str(value: object) -> str | None:

    if value in (None, ""):
        return None
    return str(value)


def display_value(value: object | None, *, empty: str = "-") -> str:

    if value in (None, ""):
        return empty
    return str(value)


def model_label(provider: str | None, model: str | None, *, empty: str = "-") -> str:

    if provider and model:
        return f"{provider}/{model}"
    return provider or model or empty


def ellipsis_truncate(text: str, max_chars: int, *, normalize_ws: bool = False) -> str:

    value = " ".join(text.split()) if normalize_ws else text
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    ellipsis = "..."
    if max_chars <= len(ellipsis):
        return ellipsis[:max_chars]
    return value[: max_chars - len(ellipsis)] + ellipsis
