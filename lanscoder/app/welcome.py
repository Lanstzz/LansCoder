from __future__ import annotations

from rich.align import Align
from rich.text import Text

WELCOME_LOGO_PALETTE = {
    "O": "#002630",
    "W": "#ffffff",
    "B": "#b0e0ff",
}

_GRADIENT_COLORS = ("#00d4ff", "#00bef6", "#00a7f0", "#0099ff")
_GRADIENT_BAND_WIDTH = 20

_LANSCODER_GLYPHS: dict[str, tuple[str, ...]] = {
    "L": (
        "▄▄      ",
        "██      ",
        "██      ",
        "██      ",
        "██      ",
        "██▄▄▄▄▄▄",
        "▀▀▀▀▀▀▀▀",
    ),
    "a": (
        "        ",
        "        ",
        " ▄█████▄",
        " ▀ ▄▄▄██",
        "▄██▀▀▀██",
        "██▄▄▄███",
        " ▀▀▀▀ ▀▀",
    ),
    "n": (
        "        ",
        "        ",
        "██▄████▄",
        "██▀   ██",
        "██    ██",
        "██    ██",
        "▀▀    ▀▀",
    ),
    "s": (
        "        ",
        "        ",
        "▄▄█████▄",
        "██▄▄▄▄ ▀",
        " ▀▀▀▀██▄",
        "█▄▄▄▄▄██",
        " ▀▀▀▀▀▀ ",
    ),
    "C": (
        "   ▄▄▄▄ ",
        " ██▀▀▀▀█",
        "██▀     ",
        "██      ",
        "██▄     ",
        " ██▄▄▄▄█",
        "   ▀▀▀▀ ",
    ),
    "o": (
        "        ",
        "        ",
        " ▄████▄ ",
        "██▀  ▀██",
        "██    ██",
        "▀██▄▄██▀",
        "  ▀▀▀▀  ",
    ),
    "d": (
        "      ▄▄",
        "      ██",
        " ▄███▄██",
        "██▀  ▀██",
        "██    ██",
        "▀██▄▄███",
        "  ▀▀▀ ▀▀",
    ),
    "e": (
        "        ",
        "        ",
        " ▄████▄ ",
        "██▄▄▄▄██",
        "██▀▀▀▀▀▀",
        "▀██▄▄▄▄█",
        "  ▀▀▀▀▀ ",
    ),
    "r": (
        "       ",
        "       ",
        "██▄████",
        "██▀    ",
        "██     ",
        "██     ",
        "▀▀     ",
    ),
}


def _build_lanscoder_pixels() -> tuple[str, ...]:

    banner_rows: list[str] = []
    for row_index in range(7):
        row = ""
        for letter_index, letter in enumerate("LansCoder"):
            if letter_index:
                row += " "
            row += _LANSCODER_GLYPHS[letter][row_index]
        banner_rows.append(row)

    width = max(len(row) for row in banner_rows)
    blank = " " * width
    return (blank,) * 3 + tuple(banner_rows) + (blank,) * 3


WELCOME_LOGO_PIXELS = _build_lanscoder_pixels()

WELCOME_PARTICLE_FRAMES = (
    ((0, 10, "W"), (12, 35, "B"), (2, 60, "W"), (10, 20, "B")),
    ((1, 42, "B"), (11, 8, "W"), (0, 65, "W"), (12, 55, "B")),
    ((0, 25, "W"), (10, 45, "B"), (2, 70, "B"), (11, 30, "W")),
    ((1, 15, "B"), (12, 48, "W"), (0, 40, "W"), (10, 68, "B")),
)


def _shade_color(column: int) -> str:
    return _GRADIENT_COLORS[min(3, column // _GRADIENT_BAND_WIDTH)]


def welcome_renderable(*, compact: bool = False, particle_frame: int = 0) -> Align:
    if compact:
        return Align.center(
            Text.assemble(
                ("lanscoder", "#00d4ff bold"),
                ("\nlocal agent harness", "#6e6d72"),
            )
        )
    rows = [list(row) for row in WELCOME_LOGO_PIXELS]
    frame = WELCOME_PARTICLE_FRAMES[particle_frame % len(WELCOME_PARTICLE_FRAMES)]
    for row_index, column_index, pixel in frame:
        if not 0 <= row_index < len(rows):
            continue
        row = rows[row_index]
        if column_index >= len(row):
            row.extend(" " for _ in range(column_index - len(row) + 1))
        if row[column_index] in (" ", "."):
            row[column_index] = pixel

    text = Text()
    for row_index, row in enumerate(rows):
        if row_index:
            text.append("\n")
        for column, pixel in enumerate(row):
            color = WELCOME_LOGO_PALETTE.get(pixel)
            if color is not None:
                text.append("█", style=color)
            elif pixel in (" ", "."):
                text.append(" ", style=None)
            else:
                text.append(pixel, style=_shade_color(column))
    return Align.center(text)
