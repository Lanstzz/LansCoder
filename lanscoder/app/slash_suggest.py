"""Slash-command suggestion dropdown for the composer input."""

from __future__ import annotations

from textual.widgets import OptionList
from textual.widgets.option_list import Option


class SlashSuggest(OptionList):
    """Dropdown list of slash-command suggestions that appears above the input."""

    DEFAULT_CSS = """
    SlashSuggest {
        display: none;
        height: auto;
        max-height: 12;
        border: solid $primary;
        background: $surface;
        margin: 0 0 1 0;
    }
    SlashSuggest.--visible {
        display: block;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._all_commands: list[tuple[str, str]] = []

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Set the full command list used for filtering."""
        self._all_commands = commands

    def update_suggestions(self, text: str) -> None:
        """Filter commands by the current input text and show/hide accordingly."""
        prefix = text.strip()
        if not prefix.startswith("/"):
            self.clear_options()
            self.remove_class("--visible")
            return

        # Filter matching commands
        self.clear_options()
        matching = [
            (cmd, desc)
            for cmd, desc in self._all_commands
            if cmd.startswith(prefix) or prefix.startswith(cmd)
        ]
        # Also match if the command contains the prefix (partial match)
        matching2 = [
            (cmd, desc)
            for cmd, desc in self._all_commands
            if prefix in cmd and (cmd, desc) not in matching
        ]
        all_matching = matching + matching2

        if not all_matching:
            self.remove_class("--visible")
            return

        for cmd, desc in all_matching:
            self.add_option(Option(f"{cmd}  — {desc}", id=cmd))

        if len(self._options) == 1:
            self.highlight = 0
        self.add_class("--visible")

    def selected_command(self) -> str | None:
        """Return the command string of the highlighted option, or None."""
        if self.highlight is not None and self._options:
            opt = self._options[self.highlight]
            return str(opt.id) if opt.id else None
        return None