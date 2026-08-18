"""Textual widgets used by the LansCoder TUI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Markdown, Static, TextArea


class LansCoderMarkdown(Markdown):
    """Markdown output with selection gated by its render lifecycle."""

    ALLOW_SELECT = True

    def __init__(self, *args, selectable: bool = True, **kwargs) -> None:
        self._selectable = selectable
        super().__init__(*args, **kwargs)

    @property
    def allow_select(self) -> bool:
        return self._selectable

    def set_selectable(self, selectable: bool) -> None:
        """Toggle selection once no more Markdown updates will replace blocks."""

        self._selectable = selectable
        self.refresh()


LansCoderMarkdown.BLOCKS = {
    name: type(
        f"LansCoder{block.__name__}",
        (block,),
        {"ALLOW_SELECT": True},
    )
    for name, block in Markdown.BLOCKS.items()
}


class ComposerTextArea(TextArea):
    """Multiline composer where Enter submits and Shift+Enter inserts a newline."""

    # TextArea owns Ctrl+V, so an App binding is not invoked while the composer
    # has focus. Route the key through this widget to stage clipboard images;
    # terminal Paste events remain responsible for inserting plain text.
    BINDINGS = [
        Binding("ctrl+v", "paste", show=False, priority=True),
        Binding("super+v", "paste", show=False, priority=True),
        Binding("f8", "paste_image", show=False, priority=True),
    ]

    class Submitted(Message):
        pass

    def _get_slash_suggest(self):
        """Return the SlashSuggest widget, or None if not found."""
        try:
            return self.app.query_one("#slash-suggest")
        except Exception:
            return None

    def _update_slash_suggest(self, suggest) -> None:
        """Update slash-command suggestions based on current input text."""
        suggest.update_suggestions(self.text)

    async def _on_key(self, event: events.Key) -> None:
        # ── slash-command autocomplete ──────────────────────────────
        suggest = self._get_slash_suggest()
        if suggest is not None and suggest.has_class("--visible"):
            if event.key == "enter":
                event.stop()
                event.prevent_default()
                cmd = suggest.selected_command()
                if cmd:
                    self.load_text(cmd)
                    self.cursor_location = self.document.end
                suggest.remove_class("--visible")
                self.post_message(self.Submitted())
                return
            if event.key == "tab":
                event.stop()
                event.prevent_default()
                cmd = suggest.selected_command()
                if cmd:
                    self.load_text(cmd + " ")
                    self.cursor_location = self.document.end
                suggest.remove_class("--visible")
                return
            if event.key == "escape":
                event.stop()
                event.prevent_default()
                suggest.remove_class("--visible")
                return
            if event.key == "up":
                event.stop()
                event.prevent_default()
                if suggest.highlight is not None and suggest.highlight > 0:
                    suggest.action_cursor_up()
                return
            if event.key == "down":
                event.stop()
                event.prevent_default()
                options = suggest._options
                if suggest.highlight is not None and suggest.highlight < len(options) - 1:
                    suggest.action_cursor_down()
                return
        # ── existing key handling ───────────────────────────────────
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)
        # ── update suggestions after text change ────────────────────
        if suggest is not None:
            self._update_slash_suggest(suggest)

    async def _on_paste(self, event: events.Paste) -> None:
        """Stage pasted files before TextArea inserts their paths as text."""

        stage_attachments = getattr(self.app, "_stage_paste_attachments", None)
        if callable(stage_attachments) and stage_attachments(event.text):
            event.stop()
            event.prevent_default()
            return
        await super()._on_paste(event)

    def action_paste(self) -> None:
        """Attach an OS clipboard image before the terminal emits its Paste event."""

        paste_attachment = getattr(self.app, "_paste_composer_clipboard_image", None)
        if callable(paste_attachment) and paste_attachment():
            return

    def action_paste_image(self) -> None:
        """Attach an OS clipboard image and report when no image is available."""

        paste_attachment = getattr(self.app, "_paste_composer_clipboard_image", None)
        if callable(paste_attachment) and paste_attachment():
            return
        paste_unavailable = getattr(self.app, "_notify_clipboard_image_unavailable", None)
        if callable(paste_unavailable):
            paste_unavailable()


def _plain_static(content: object = "", *args, **kwargs) -> Static:
    kwargs.setdefault("markup", False)
    return Static(content, *args, **kwargs)


def _observe_markdown_update(update_result) -> None:
    future = getattr(update_result, "_future", None)
    if future is None or not hasattr(future, "add_done_callback"):
        return

    def observe_cancelled_update(done_future) -> None:
        try:
            exception = done_future.exception()
        except asyncio.CancelledError:
            return
        if isinstance(exception, asyncio.CancelledError):
            return
        if exception is not None:
            raise exception

    future.add_done_callback(observe_cancelled_update)


@dataclass(slots=True)
class LansCoderTuiConfig:
    title: str = "LansCoder"
    provider_name: str | None = None
    provider_model: str | None = None
    project_name: str | None = None


class LansCoderScreen(Screen[None]):
    """Notify the app after Textual has committed a new terminal size."""

    @staticmethod
    def _selection_is_blocked_by_streaming_markdown(widget) -> bool:
        """Reject a leaf while any owning Markdown document is still updating."""

        parent = widget
        while parent is not None:
            if isinstance(parent, LansCoderMarkdown) and not parent.allow_select:
                return True
            parent = getattr(parent, "parent", None)
        return False

    def get_widget_and_offset_at(self, x: int, y: int):
        widget, offset = super().get_widget_and_offset_at(x, y)
        if widget is not None and self._selection_is_blocked_by_streaming_markdown(widget):
            return None, None
        return widget, offset

    def _screen_resized(self, size) -> None:
        super()._screen_resized(size)
        callback = getattr(self.app, "_on_terminal_resized", None)
        if callback is not None:
            callback()
