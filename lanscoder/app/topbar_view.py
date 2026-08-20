from __future__ import annotations

from rich.markup import escape
from rich.text import Text

from lanscoder.app import model_topbar_themes, theme

PERMISSION_MODE_COLORS = {
    "standard": "#cfd1d6",
    "aggressive": "#f6b73c",
    "bypass": "#ff6b5f",
}


def _markup_width(markup: str) -> int:
    return Text.from_markup(markup).cell_len


def _truncate_markup(markup: str, width: int) -> str:
    text = Text.from_markup(markup)
    text.truncate(max(0, width), overflow="ellipsis", pad=False)
    return text.markup


def _metadata_markup(values: list[tuple[str | None, str, int | None]], *, separator: str) -> str:
    return separator.join(value if color is None else f"[{color}]{escape(value)}[/]" for color, value, _ in values)


def _provider_name_markup(provider: str, *, glow_frame: int = 0) -> str:
    return f"[{theme.ACCENT}]{escape(provider)}[/]"


def _provider_model_markup(provider: str, model: str, *, glow_frame: int = 0) -> str:
    themed = model_topbar_themes.provider_model_markup(
        provider,
        model,
        glow_frame=glow_frame,
    )
    if themed is not None:
        return themed
    return f"{_provider_name_markup(provider, glow_frame=glow_frame)}[#6e6d72]/{escape(model)}[/]"
