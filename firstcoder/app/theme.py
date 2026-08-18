"""TUI theme accent colors.

Centralize the interface tints so the whole chrome can be rethemed in one
place. The tui.tcss stylesheet mirrors these values (static CSS cannot import
Python constants) — keep both in sync when a color changes.
"""

ACCENT = "#4f8cff"  # primary brand/accent blue (was brand green #7bba55)
ACCENT_DARK = "#2b5fd9"  # muted status accent, activity line (was green #527c3b)
USER_ACCENT = "#ffffff"  # user-message left rail (was cyan #50b7c2)
