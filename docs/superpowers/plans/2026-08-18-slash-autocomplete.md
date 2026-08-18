# Slash Command Autocomplete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dropdown autocomplete/suggestion for slash commands in the TUI input, similar to Claude Code.

**Architecture:** Each `CommandHandlerLike` declares its commands via `commands()` method. `CompositeCommandHandler` aggregates them. A new `SlashSuggest` widget (OptionList) appears above the composer when user types `/`, filters in real-time, and supports Enter-to-send / Tab-to-fill / Esc-to-dismiss.

**Tech Stack:** Python, Textual (OptionList widget)

**Spec:** Design discussed in brainstorming session — see conversation summary above.

## Global Constraints

- Enter on a selected suggestion sends the command immediately (submits)
- Tab on a selected suggestion fills the command into the input box for further editing
- Esc dismisses the dropdown
- Up/Down arrows navigate suggestions
- Filtering is real-time as user types after `/`
- `HelpCommandHandler` must dynamically collect commands instead of maintaining a hardcoded list

---

### Task 1: Protocol + handlers + composite + help

**Files:**
- Modify: `lanscoder/app/ports.py`
- Modify: `lanscoder/app/commands.py`
- Modify: `lanscoder/app/help_commands.py`
- Modify: `lanscoder/app/recall_commands.py`
- Modify: `lanscoder/app/session_commands.py`
- Modify: `lanscoder/app/model_commands.py`
- Modify: `lanscoder/app/skill_commands.py`
- Modify: `lanscoder/app/permission_commands.py`
- Modify: `lanscoder/app/memory_commands.py`
- Modify: `lanscoder/app/mcp_commands.py`
- Modify: `lanscoder/app/router.py`

**Interfaces:**
- Produces: `CommandHandlerLike.commands() -> list[tuple[str, str]]` on protocol and all handlers
- Produces: `CompositeCommandHandler.all_commands() -> list[tuple[str, str]]` on router
- Consumes: HelpCommandHandler takes `CompositeCommandHandler` to dynamically render help

- [ ] **Step 1: Add `commands()` to `CommandHandlerLike` protocol**

In `lanscoder/app/ports.py`, add `commands()` method to the protocol:

```python
class CommandHandlerLike(Protocol):
    def handle(self, text: str) -> CommandResult: ...
    def commands(self) -> list[tuple[str, str]]: ...
```

- [ ] **Step 2: Add `commands()` to each handler**

For each handler file, add a `commands()` method returning the commands that handler supports. Follow the pattern from `help_commands.py` `HELP_COMMANDS` list:

**`commands.py` — `ContextCommandHandler`:**
```python
def commands(self) -> list[tuple[str, str]]:
    return [
        ("/context", "Inspect context state."),
        ("/compact status", "Show compaction status."),
        ("/compact", "Compact context now."),
    ]
```

**`recall_commands.py` — `RecallCommandHandler`:**
```python
def commands(self) -> list[tuple[str, str]]:
    return [("/recall", "Rewind conversation to a previous turn.")]
```

**`session_commands.py`:** Find the SessionCommandHandler class and add:
```python
def commands(self) -> list[tuple[str, str]]:
    return [
        ("/new [title]", "Start a new session."),
        ("/fork [title]", "Copy the current session into a new branch."),
        ("/sessions", "List saved sessions."),
        ("/session <session_id>", "Show one session summary."),
        ("/resume", "Pick a session to resume."),
        ("/resume <session_id>", "Resume a session directly."),
        ("/share [session_id] [--tool-results]", "Export a shareable transcript."),
        ("/rename <title>", "Rename the current session."),
    ]
```

**`model_commands.py`:** Find the handler class and add:
```python
def commands(self) -> list[tuple[str, str]]:
    return [
        ("/model", "Pick a model to use."),
        ("/model <model|provider/model>", "Switch the active model."),
    ]
```

**`skill_commands.py`:** Find the handler class and add:
```python
def commands(self) -> list[tuple[str, str]]:
    return [
        ("/skills", "Pick a skill to reference."),
        ("/skill <name>", "Show skill details."),
    ]
```

**`permission_commands.py`:** Find the handler class and add:
```python
def commands(self) -> list[tuple[str, str]]:
    return [
        ("/mode", "Show permission mode."),
        ("/mode <standard|aggressive|bypass>", "Change permission mode."),
    ]
```

**`memory_commands.py`:** Find the handler class and add:
```python
def commands(self) -> list[tuple[str, str]]:
    return [
        ("/memory", "List memories."),
        ("/memory add <content>", "Add a memory."),
        ("/memory delete <id>", "Delete a memory."),
    ]
```

**`mcp_commands.py`:** Find the handler class and add:
```python
def commands(self) -> list[tuple[str, str]]:
    return [
        ("/mcp list", "List MCP server status."),
        ("/mcp doctor <server>", "Inspect one MCP server."),
        ("/mcp reconnect <server|all>", "Reconnect MCP servers in the background."),
    ]
```

**`help_commands.py` — `HelpCommandHandler`:** Replace the static `HELP_COMMANDS` list. The handler now accepts a `CompositeCommandHandler` and dynamically collects commands:

```python
@dataclass(slots=True)
class HelpCommandHandler:
    """Render the current TUI slash command surface."""

    command_handler: CompositeCommandHandler  # injected from factory

    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if command != "/help":
            return CommandResult(handled=False)
        all_commands = self.command_handler.all_commands()
        lines = ["Commands:", *[f"{cmd.ljust(48)} {desc}" for cmd, desc in all_commands]]
        return CommandResult(handled=True, output="\n".join(lines))

    def commands(self) -> list[tuple[str, str]]:
        return [("/help", "Show available commands.")]
```

Remove the module-level `HELP_COMMANDS` and `HELP_TEXT` constants.

- [ ] **Step 3: Add `all_commands()` to `CompositeCommandHandler`**

In `lanscoder/app/router.py`:

```python
@dataclass(slots=True)
class CompositeCommandHandler:
    handlers: list[CommandHandlerLike]

    def handle(self, text: str) -> CommandResult:
        ...  # existing logic unchanged

    def all_commands(self) -> list[tuple[str, str]]:
        """Collect all commands from registered handlers, sorted alphabetically."""
        commands: list[tuple[str, str]] = []
        for handler in self.handlers:
            commands.extend(handler.commands())
        commands.sort(key=lambda x: x[0].removeprefix("/").lstrip())
        return commands
```

- [ ] **Step 4: Update `factory.py` to pass `CompositeCommandHandler` to `HelpCommandHandler`**

In `lanscoder/app/factory.py`, find where `HelpCommandHandler` is instantiated and pass the command handler. The `HelpCommandHandler` now needs to be created AFTER `CompositeCommandHandler`:

```python
help_handler = HelpCommandHandler(command_handler=command_handler)
```

- [ ] **Step 5: Run tests and verify**

```sh
.venv/bin/python -m pytest tests/ -q --tb=short
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: add commands() protocol method and dynamic help rendering"
```

### Task 2: Create SlashSuggest widget

**Files:**
- Create: `lanscoder/app/slash_suggest.py`

**Interfaces:**
- Produces: `SlashSuggest` widget class (extends `OptionList`)
- Consumes: command list from `CompositeCommandHandler.all_commands()`

- [ ] **Step 1: Write the widget**

```python
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
```

- [ ] **Step 2: Run tests**

```sh
.venv/bin/python -m pytest tests/ -q --tb=short
```

- [ ] **Step 3: Commit**

```bash
git add lanscoder/app/slash_suggest.py
git commit -m "feat: add SlashSuggest widget for slash command autocomplete"
```

### Task 3: Integrate SlashSuggest into ComposerTextArea and TUI

**Files:**
- Modify: `lanscoder/app/tui_widgets.py`
- Modify: `lanscoder/app/tui.py`
- Modify: `lanscoder/app/factory.py`

**Interfaces:**
- Consumes: `SlashSuggest` widget from Task 2
- Consumes: `CompositeCommandHandler.all_commands()` from Task 1

- [ ] **Step 1: Update `ComposerTextArea` to trigger slash suggestions**

In `lanscoder/app/tui_widgets.py`, modify `ComposerTextArea._on_key`:

```python
async def _on_key(self, event: events.Key) -> None:
    # Slash suggest: Tab completes, Esc dismisses
    if event.key == "tab":
        suggest = self._get_slash_suggest()
        if suggest is not None and suggest.has_class("--visible"):
            event.stop()
            event.prevent_default()
            cmd = suggest.selected_command()
            if cmd is not None:
                self.load_text(cmd + " ")
                self.cursor_location = len(cmd) + 1
                suggest.remove_class("--visible")
            return
    if event.key == "escape":
        suggest = self._get_slash_suggest()
        if suggest is not None and suggest.has_class("--visible"):
            event.stop()
            event.prevent_default()
            suggest.remove_class("--visible")
            return
    if event.key == "enter":
        suggest = self._get_slash_suggest()
        if suggest is not None and suggest.has_class("--visible"):
            cmd = suggest.selected_command()
            if cmd is not None:
                event.stop()
                event.prevent_default()
                suggest.remove_class("--visible")
                self.load_text(cmd)
                self.post_message(self.Submitted())
                return
        event.stop()
        event.prevent_default()
        self.post_message(self.Submitted())
        return
    if event.key == "shift+enter":
        event.stop()
        event.prevent_default()
        self.insert("\n")
        return
    # Handle up/down keys for slash suggest navigation
    if event.key in ("up", "down"):
        suggest = self._get_slash_suggest()
        if suggest is not None and suggest.has_class("--visible"):
            # Let OptionList handle navigation natively
            await super()._on_key(event)
            # Update suggestions after navigation
            return
    await super()._on_key(event)
    # After any key, update slash suggestions
    self._update_slash_suggest()

def _get_slash_suggest(self):
    """Get the SlashSuggest widget from the app."""
    app = getattr(self, "app", None)
    if app is not None:
        return app.query_one("SlashSuggest", default=None)
    return None

def _update_slash_suggest(self) -> None:
    """Update the slash suggestion dropdown based on current text."""
    suggest = self._get_slash_suggest()
    if suggest is not None:
        suggest.update_suggestions(self.text)
```

- [ ] **Step 2: Mount `SlashSuggest` in `LansCoderApp.compose` and wire commands**

In `lanscoder/app/tui.py`:

1. Add import: `from lanscoder.app.slash_suggest import SlashSuggest`
2. In `compose()`, add `SlashSuggest` inside the composer container, before the `ComposerTextArea`:

```python
with Vertical(id="composer", classes="composer"):
    yield SlashSuggest(id="slash-suggest")
    yield ComposerTextArea(
        id="input",
        classes="input",
        tab_behavior="indent",
    )
```

3. In `on_mount()` or after the app is ready, populate the command list. The `LansCoderApp` needs access to the `CompositeCommandHandler`. Add a method to set it:

```python
def set_slash_commands(self, commands: list[tuple[str, str]]) -> None:
    suggest = self.query_one(SlashSuggest)
    suggest.set_commands(commands)
```

- [ ] **Step 3: Wire in `factory.py`**

In `lanscoder/app/factory.py`, after creating the app and command handler, call:

```python
app.set_slash_commands(command_handler.all_commands())
```

- [ ] **Step 4: Handle up/down keys properly**

The up/down keys need special handling. When SlashSuggest is visible and has focus, up/down should navigate the OptionList. When at the top of the list and pressing up, or at the bottom and pressing down, the key should pass through to the TextArea (to allow moving cursor in the input).

Add this logic to `ComposerTextArea._on_key`:

```python
if event.key in ("up", "down"):
    suggest = self._get_slash_suggest()
    if suggest is not None and suggest.has_class("--visible"):
        # Navigate the OptionList
        if event.key == "up":
            if suggest.highlight is not None and suggest.highlight > 0:
                suggest.action_cursor_up()
            event.stop()
            event.prevent_default()
            return
        elif event.key == "down":
            if suggest.highlight is not None and suggest.highlight < len(suggest._options) - 1:
                suggest.action_cursor_down()
            event.stop()
            event.prevent_default()
            return
```

- [ ] **Step 5: Run full test suite**

```sh
.venv/bin/python -m pytest tests/ -q --tb=short
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: integrate slash command autocomplete dropdown into TUI"
```