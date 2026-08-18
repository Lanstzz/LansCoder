"""Welcome screen renderables."""

from __future__ import annotations

from rich.align import Align
from rich.text import Text

# 粒子颜色（白 / 浅蓝）与保留的深色背景风格。"O" 不参与渲染（透明格渲染为空格，
# 背景由 TUI 主题提供），仅保留配色文档用途。
WELCOME_LOGO_PALETTE = {
    "O": "#002630",
    "W": "#ffffff",
    "B": "#b0e0ff",
}

# 前景渐变色（左亮 #00d4ff → 右深 #0099ff），按列号取值。
_GRADIENT_COLORS = ("#00d4ff", "#00bef6", "#00a7f0", "#0099ff")
_GRADIENT_BAND_WIDTH = 20  # 79 列分 4 档渐变，每档约 20 列

# "LansCoder" 方块体 banner（▄/▀/█ 三阶盒型笔画）。这是手调的 9 个字母位图，
# 拼接时字母间用 1 列间隔（原始排版间隔 2-3 列，压缩到 1 列以适配 ≤80 列的
# 全尺寸显示阈值）。每个字母 7 行，宽 8 列（r 为 7 列）。
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
    """把 9 个字母拼成 banner 画布：字母间 1 列间隔，上下各留 3 行给粒子闪烁。"""

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

# 粒子只落在透明格上（welcome_renderable 的覆盖规则保证），因此坐标都取在
# 上下留白（画布 13 行：上 3 / 字 7 / 下 3），不会盖住 "LansCoder" 本身。
WELCOME_PARTICLE_FRAMES = (
    ((0, 10, "W"), (12, 35, "B"), (2, 60, "W"), (10, 20, "B")),
    ((1, 42, "B"), (11, 8, "W"), (0, 65, "W"), (12, 55, "B")),
    ((0, 25, "W"), (10, 45, "B"), (2, 70, "B"), (11, 30, "W")),
    ((1, 15, "B"), (12, 48, "W"), (0, 40, "W"), (10, 68, "B")),
)


def _shade_color(column: int) -> str:
    return _GRADIENT_COLORS[min(3, column // _GRADIENT_BAND_WIDTH)]


def welcome_renderable(*, compact: bool = False, particle_frame: int = 0) -> Align:
    """Render the animated logo, or a small-screen wordmark when space is tight."""
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
