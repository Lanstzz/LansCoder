"""LansCoder 最小 Textual TUI。

这一版只提供命令入口外壳：输出区展示状态文本，输入框接收普通文本或 slash command。
普通聊天通过注入的 chat runner 处理，避免 Textual widget 直接依赖 provider/agent 细节。
"""

from __future__ import annotations

import asyncio
import platform
import subprocess
from inspect import isawaitable
import threading
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import anyio

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key, Paste
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Static, TextArea

from lanscoder.app.ports import ChatRunnerLike, CommandHandlerLike, CurrentSessionLike
from lanscoder.app.picker import TuiPickerState, render_picker
from lanscoder.app.slash_suggest import SlashSuggest
from lanscoder.app.picker_adapters import (
    model_picker_item,
    picker_command,
    recall_picker_item,
    render_picker_item,
    session_picker_item,
    skill_picker_item,
)
from lanscoder.app.session_commands import SESSION_LIST_VISIBLE_LIMIT
from lanscoder.app.subagent_panel_state import (
    FG_ID,
    SubagentRow,
    build_rows,
    can_enter_selection,
    move_selection,
    stop_target,
)
from lanscoder.app.permission_view import (
    ask_user_choice_for_text,
    permission_choice_for_text,
    permission_options_text,
)
from lanscoder.app.review_view import review_command_from_text
from lanscoder.app.transcript_view import (
    display_line_kind,
    display_line_status,
    entry_plain_text,
    looks_like_markdown_response,
    looks_like_tool_display_line,
    normalize_stream_text,
)
from lanscoder.app import model_topbar_themes
from lanscoder.app.activity_view import compact_tool_arguments, compact_tool_content
from lanscoder.app.tui_state import TuiEntryKind, TuiTaskPlanPanelState, TuiTranscript, TuiTranscriptEntry
from lanscoder.app.topbar_view import _provider_name_markup, _provider_model_markup
from lanscoder.app.tui_view import LansCoderViewMixin, _entry_renderable
from lanscoder.app.tui_widgets import (
    ComposerTextArea,
    LansCoderMarkdown,
    LansCoderScreen,
    LansCoderTuiConfig,
    _observe_markdown_update,
    _plain_static,
)
from lanscoder.input.attachments import UserAttachment, format_attachment_chip, resolve_paste_attachments

__all__ = [
    "ComposerTextArea",
    "LansCoderApp",
    "LansCoderMarkdown",
    "LansCoderTuiConfig",
    "_entry_renderable",
    "_format_subagent_line",
    "_observe_markdown_update",
    "_plain_static",
    "_progress_indicator",
    "_provider_model_markup",
    "_provider_name_markup",
]


_PROGRESS_INDICATORS = ("[    ]", "[>   ]", "[->  ]", "[--> ]", "[--->]", "[ ---]", "[  --]", "[   -]")

# 子 agent 活动区最多同时显示的行数，超出以一行 "…还有 N 个子agent在跑" 汇总，
# 避免大量后台任务把输入框顶高。
_MAX_VISIBLE_SUBAGENT_LINES = 3


def _progress_indicator(frame: int) -> str:
    """搜索时的进度条指示：`[>   ]` → `[--->]` → `[   -]` → 回到空条循环。"""
    return _PROGRESS_INDICATORS[frame % len(_PROGRESS_INDICATORS)]


def _format_subagent_line(
    *,
    label: str,
    elapsed: float,
    calls: int,
    tokens: int,
    indicator: str,
) -> str:
    token_str = f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)
    return f"{indicator} {label} · {elapsed:.0f}s · {calls} calls · {token_str} tokens"


@dataclass(slots=True)
class _ActiveChatTurn:
    id: str
    token: int


class LansCoderApp(LansCoderViewMixin, App[None]):
    """最小 TUI 外壳。"""

    CSS_PATH = "tui.tcss"
    ALLOW_SELECT = True
    BINDINGS = [
        ("ctrl+c", "copy_output_or_quit", "Copy output or quit"),
    ]
    STREAM_RENDER_INTERVAL_SECONDS = 0.2
    WORKING_ANIMATION_INTERVAL_SECONDS = 0.18
    WORKING_FRAMES = ("[.  ]", "[.. ]", "[...]", "[ ..]", "[  .]")
    ESC_INTERRUPT_WINDOW_SECONDS = 1.0
    ACTIVITY_ANIMATION_INTERVAL_SECONDS = 0.24
    WELCOME_PARTICLE_INTERVAL_SECONDS = 0.85
    PROVIDER_GLOW_INTERVAL_SECONDS = model_topbar_themes.GLOW_INTERVAL_SECONDS
    COMPACT_WELCOME_MAX_WIDTH = 80
    COMPACT_WELCOME_MAX_HEIGHT = 24
    ACTIVITY_FRAMES = {
        "running": ("[=   ]", "[==  ]", "[=== ]", "[ ===]", "[  ==]", "[   =]"),
        "streaming": ("[>   ]", "[>>  ]", "[>>> ]", "[ >>>]", "[  >>]", "[   >]"),
    }

    def get_default_screen(self) -> Screen:
        return LansCoderScreen(id="_default")

    def __init__(
        self,
        *,
        command_handler: CommandHandlerLike | None = None,
        chat_runner: ChatRunnerLike | None = None,
        current_session: CurrentSessionLike | None = None,
        config: LansCoderTuiConfig | None = None,
        on_shutdown: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.command_handler = command_handler
        self.chat_runner = chat_runner
        self.current_session = current_session
        self.config = config or LansCoderTuiConfig()
        self._on_shutdown = on_shutdown
        self._shutdown_called = False
        self._chat_busy = False
        self._chat_worker = None
        self._compact_worker = None
        self._chat_turn_token = 0
        self._active_chat_turn: _ActiveChatTurn | None = None
        self._last_escape_at = 0.0
        self._stream_reasoning_started = False
        self._stream_text_started = False
        self._stream_text_needs_newline = False
        self._stream_text_buffer = ""
        self._stream_text_widget = None
        self._stream_text_entry: TuiTranscriptEntry | None = None
        self._stream_rendered_text = ""
        self._stream_flush_timer: Timer | None = None
        self._stream_markdown_update = None
        self._stream_finalizations: dict[LansCoderMarkdown, object] = {}
        self._finalized_stream_widgets: set[LansCoderMarkdown] = set()
        self._stream_event_lock = threading.Lock()
        self._stream_event_generation = 0
        self._stream_event_dispatch_scheduled = False
        self._pending_stream_text: list[str] = []
        self._pending_reasoning_text: list[str] = []
        self._reasoning_buffer = ""
        self._reasoning_is_fallback = False
        self._working_text = ""
        self._working_frame_index = 0
        self._working_timer: Timer | None = None
        self._activity_animation_kind = ""
        self._activity_animation_detail = ""
        self._activity_frame_index = 0
        self._activity_started_at = 0.0
        self._activity_timer: Timer | None = None
        self._turn_started_at = 0.0
        self._turn_tool_count = 0
        self._running_tool_call_ids: set[str] = set()
        self._live_tool_events_seen = False
        self._stream_segment_closed_for_tool = False
        self._activity_text = "idle · ready"
        self._topbar_status = ""
        self._input_history: list[str] = []
        self._input_history_index: int | None = None
        self._picker: TuiPickerState | None = None
        self._review_expanded_paths: set[str] = set()
        self._staged_attachments: list[UserAttachment] = []
        self._welcome_widget: Static | None = None
        self._welcome_particle_timer: Timer | None = None
        self._welcome_particle_frame = 0
        self._provider_glow_timer: Timer | None = None
        self._provider_glow_frame = 0
        self._subagent_progress_timer: Timer | None = None
        self._activity_frame = 0
        self._subagent_selected: str | None = None
        self._subagent_select_mode = False
        self._foreground_cancel_requested = False
        self._pending_user_input: str | None = None
        self._pending_user_attachments: list[UserAttachment] | None = None
        self.transcript = TuiTranscript()
        self.task_plan_panel_state = TuiTaskPlanPanelState()

    def compose(self) -> ComposeResult:
        yield Static(self._topbar_text(), id="topbar", classes="topbar")
        with Vertical(id="main"):
            yield VerticalScroll(id="output")
            yield _plain_static("", id="task-plan-panel", classes="task-plan-panel hidden")
            yield Static("idle · ready", id="activity", classes="activity-line")
            with Vertical(id="composer", classes="composer"):
                yield SlashSuggest(id="slash-suggest")
                yield ComposerTextArea(
                    placeholder="输入消息，Enter 发送，Shift+Enter 换行，Ctrl/Cmd+V 粘贴图片",
                    id="input",
                    show_line_numbers=False,
                    soft_wrap=True,
                    compact=True,
                )
            yield Vertical(id="subagent-panel", classes="hidden")

    def on_mount(self) -> None:
        self.title = self.config.title
        self._refresh_session_subtitle()
        self._show_welcome()
        self._sync_provider_glow()
        commands = getattr(self, "_slash_commands", None)
        if commands is None and self.command_handler is not None:
            fetch = getattr(self.command_handler, "all_commands", None)
            if fetch is not None:
                commands = fetch()
        if commands is not None:
            suggest = self.query_one("#slash-suggest", SlashSuggest)
            suggest.set_commands(commands)
        # Keep focus on the input — clicking elsewhere should not steal it.
        self.query_one("#output").can_focus = False
        self.set_focus(self.query_one("#input"))
        # Periodically update the subagent panel with progress.
        self._subagent_progress_timer = self.set_interval(0.5, self._refresh_subagent_progress)
        # Register for proactive subagent completion delivery.
        if self.chat_runner is not None:
            mgr = getattr(self.chat_runner, "background_manager", None)
            if mgr is not None and hasattr(mgr, "set_on_job_completed"):
                mgr.set_on_job_completed(self._on_subagent_completed)

    def on_app_focus(self) -> None:
        """When the terminal window regains focus, put it back on the input."""
        try:
            self.set_focus(self.query_one("#input"))
        except Exception:
            pass

    def _refresh_subagent_progress(self) -> None:
        """Periodic timer: render running sub-agents (foreground first, then background)."""
        manager = None
        foreground = None
        if self.chat_runner is not None:
            manager = getattr(self.chat_runner, "background_manager", None)
            foreground = self._effective_foreground(getattr(self.chat_runner, "foreground_subagent", lambda: None)())
        try:
            panel = self.query_one("#subagent-panel")
        except Exception:
            return
        jobs = manager.active_jobs() if manager is not None else []
        rows = build_rows(foreground, jobs)
        self._sync_subagent_selection(rows)
        if not rows:
            for child in list(panel.children):
                if isinstance(child.id, str):
                    child.remove()
            panel.add_class("hidden")
            return
        panel.remove_class("hidden")
        self._activity_frame += 1
        indicator = _progress_indicator(self._activity_frame)
        now = time.monotonic()
        lines_by_id: dict[str, str] = {}
        if foreground is not None:
            line = _format_subagent_line(
                label=foreground.get("label") or "delegate",
                elapsed=now - foreground["started_at"],
                calls=foreground.get("provider_calls", 0),
                tokens=foreground.get("total_tokens", 0),
                indicator=indicator,
            )
            if foreground.get("cancel_requested"):
                line = f"{line} · cancelling"
            lines_by_id[FG_ID] = line
        for job in jobs:
            progress = job.progress or {}
            line = _format_subagent_line(
                label=job.label or job.tool_name,
                elapsed=now - job.created_at,
                calls=progress.get("provider_calls", 0),
                tokens=progress.get("total_tokens", 0),
                indicator=indicator,
            )
            if job.cancel_requested:
                line = f"{line} · cancelling"
            lines_by_id[job.id] = line
        hidden = len(rows) - _MAX_VISIBLE_SUBAGENT_LINES
        hint = "↑/↓ 选择 · x 停止 · Esc 返回" if self._subagent_select_mode else "↓ 进入选择 · 点击选择子agent"
        row_ids = [row.id for row in rows[:_MAX_VISIBLE_SUBAGENT_LINES]]
        keep_ids = {f"subagent-row-{row_id}" for row_id in row_ids}
        children_by_id = {child.id: child for child in panel.children if isinstance(child.id, str)}
        # Textual's remove() prunes asynchronously, so remounting a same-id row
        # in the same turn would raise DuplicateIds before the old one detaches.
        # Update matching rows in place and only mount genuinely new ones.
        for child in list(panel.children):
            if isinstance(child.id, str) and child.id not in keep_ids and child.id not in {"subagent-hint", "subagent-footer"}:
                child.remove()
        for row_id in row_ids:
            static = children_by_id.get(f"subagent-row-{row_id}")
            if static is None:
                static = Static(lines_by_id[row_id], id=f"subagent-row-{row_id}")
                panel.mount(static)
            else:
                static.update(lines_by_id[row_id])
            static.set_class(row_id == self._subagent_selected, "selected")
        footer_widget = children_by_id.get("subagent-footer")
        if hidden > 0:
            if footer_widget is None:
                panel.mount(Static(f"…还有 {hidden} 个子agent在跑", id="subagent-footer"))
            else:
                footer_widget.update(f"…还有 {hidden} 个子agent在跑")
        elif footer_widget is not None:
            footer_widget.remove()
        hint_widget = children_by_id.get("subagent-hint")
        if hint_widget is None:
            panel.mount(Static(hint, id="subagent-hint", classes="subagent-hint"))
        else:
            hint_widget.update(hint)

    def _subagent_rows(self) -> list[SubagentRow]:
        manager = None
        foreground = None
        if self.chat_runner is not None:
            manager = getattr(self.chat_runner, "background_manager", None)
            foreground = self._effective_foreground(getattr(self.chat_runner, "foreground_subagent", lambda: None)())
        jobs = manager.active_jobs() if manager is not None else []
        return build_rows(foreground, jobs)

    def _effective_foreground(self, foreground: dict[str, Any] | None) -> dict[str, Any] | None:
        if foreground is None or not self._foreground_cancel_requested:
            return foreground
        return {**foreground, "cancel_requested": True}

    def _sync_subagent_selection(self, rows: list[SubagentRow]) -> None:
        if not any(row.id == FG_ID for row in rows):
            # 前台子 agent 已结束：停止标记不再有意义。
            self._foreground_cancel_requested = False
        if self._subagent_selected is not None and not any(row.id == self._subagent_selected for row in rows):
            self._subagent_selected = None
            self._subagent_select_mode = False

    def _on_subagent_completed(self, job) -> None:
        """Called from background thread when a job finishes."""
        self.call_from_thread(self._handle_subagent_completed, job)

    def _handle_subagent_completed(self, job) -> None:
        """Deliver a subagent completion to the main conversation.

        The full result is delivered separately by the agent loop's background
        notification drain on its next provider request, so here we only write
        a short human-readable line to the UI and wake the main agent when it
        is idle.  When a turn is already running, that turn consumes the result
        itself — no redundant session write or extra reporting turn is needed.
        """
        if not getattr(self, "is_mounted", False):
            return
        label = job.label or job.tool_name
        if job.status == "completed":
            ui_msg = f"✅ 子agent [{label}] 已完成"
        elif job.status == "failed":
            ui_msg = f"❌ 子agent [{label}] 失败: {job.error or '未知错误'}"
        else:
            ui_msg = f"⚠️ 子agent [{label}] {job.status}"

        # Write to UI output only; the loop delivers the full result to the model.
        self._write_line(ui_msg, kind=TuiEntryKind.SYSTEM)

        # If idle, start a new turn so the model can report immediately.
        if not self._chat_busy and self.chat_runner is not None:
            self._submit_nudge_turn()

    def _has_pending_background_completions(self) -> bool:
        """Whether completed background jobs are still awaiting delivery.

        Peeks the background manager's completion queue without consuming it,
        so the loop's own drain remains the single consumer.  Used by
        ``_finish_chat_turn`` to wake the main agent only when a subagent
        finished after the running turn's final provider call.
        """

        if self.chat_runner is None:
            return False
        manager = getattr(self.chat_runner, "background_manager", None)
        if manager is None:
            return False
        peek = getattr(manager, "pending_completions", None)
        if peek is None:
            return False
        current = getattr(self.chat_runner, "current_session", None)
        session_id = getattr(current, "session_id", None)
        return bool(peek(session_id=session_id))

    def set_slash_commands(self, commands: list[tuple[str, str]]) -> None:
        """Set the full command list for the slash-command autocomplete dropdown."""
        self._slash_commands = commands
        try:
            suggest = self.query_one("#slash-suggest", SlashSuggest)
            suggest.set_commands(commands)
        except Exception:
            pass  # DOM not composed yet; on_mount will apply

    def _on_terminal_resized(self) -> None:
        """Refresh chrome after Textual has applied a terminal-size change."""
        self._refresh_session_subtitle()
        self._refresh_welcome_layout()

    def on_unmount(self) -> None:
        self._stop_welcome_particles()
        self._stop_provider_glow()
        if not self._shutdown_called and self._on_shutdown is not None:
            self._shutdown_called = True
            self._on_shutdown()

    async def _submit_composer(self) -> None:
        input_widget = self.query_one("#input", TextArea)
        text = input_widget.text.strip()
        input_widget.clear()
        attachments = list(self._staged_attachments)
        if not text and not attachments:
            return
        if not text:
            text = "请分析这些附件。"
        self._dismiss_welcome()
        self._record_input_history(text)

        if self._picker is not None and text.isdigit():
            if self._picker_select_number(int(text)):
                return

        attachment_chips = "\n".join(format_attachment_chip(item) for item in attachments)
        user_display = f"> {text}"
        if attachment_chips:
            user_display = f"{user_display}\n{attachment_chips}"
        self._write_line(user_display, kind=TuiEntryKind.USER)

        if text.startswith("/"):
            if self.command_handler is None:
                self._write_line("Command handler is not configured.", kind=TuiEntryKind.ERROR)
                return

            if " ".join(text.split()) == "/compact":
                self._submit_manual_compact(text)
                return

            result = self.command_handler.handle(text)
            if result.handled:
                self._write_line(result.output, kind=TuiEntryKind.COMMAND)
                self._handle_command_action(result.action, output=result.output)
                self._refresh_session_subtitle()
                return
            self._write_line(f"Unknown command: {text}", kind=TuiEntryKind.ERROR)
            return

        self._staged_attachments.clear()
        self._submit_chat_text(text, attachments=attachments)

    def _submit_manual_compact(self, text: str) -> None:
        if self._chat_busy:
            self._write_line("Chat is still running. Please wait before compacting context.", kind=TuiEntryKind.SYSTEM)
            return
        if self._compact_worker is not None:
            self._write_line("Manual compact is already running.", kind=TuiEntryKind.SYSTEM)
            return
        self._set_activity("compacting context...")
        self._write_line("Manual compact started.", kind=TuiEntryKind.SYSTEM)
        self._compact_worker = self.run_worker(self._run_manual_compact_command(text))

    async def _run_manual_compact_command(self, text: str) -> None:
        try:
            assert self.command_handler is not None
            result = await anyio.to_thread.run_sync(self.command_handler.handle, text)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._write_line(f"Manual compact error: {exc}", kind=TuiEntryKind.ERROR)
            return
        finally:
            self._compact_worker = None

        if result.handled:
            self._write_line(result.output, kind=TuiEntryKind.COMMAND)
            self._handle_command_action(result.action, output=result.output)
            self._refresh_session_subtitle()
        else:
            self._write_line(f"Unknown command: {text}", kind=TuiEntryKind.ERROR)
        self._set_activity("done")

    async def on_composer_text_area_submitted(self, event: ComposerTextArea.Submitted) -> None:
        event.stop()
        if self._picker is not None:
            self._picker_select_index(self._picker.selected_index)
            return
        await self._submit_composer()

    async def on_event(self, event: events.Event) -> None:
        # Printable keys are consumed by the focused TextArea before they would
        # bubble up to on_key, so intercept selection-mode keys here, before the
        # App forwards them to the focused widget.
        if isinstance(event, Key) and not event.is_forwarded and self._subagent_select_mode:
            if self._handle_subagent_select_key(event):
                event.stop()
                event.prevent_default()
                return
        await super().on_event(event)

    def on_key(self, event: Key) -> None:
        if self._picker is not None and self._handle_picker_key(event):
            event.stop()
            event.prevent_default()
            return
        if self._subagent_select_mode:
            if self._handle_subagent_select_key(event):
                event.stop()
                event.prevent_default()
            return
        if event.key == "escape":
            if self._handle_escape_interrupt():
                event.stop()
                event.prevent_default()
            return
        if event.key not in {"up", "down"}:
            return
        focused = getattr(self, "focused", None)
        if getattr(focused, "id", None) != "input":
            return
        input_widget = self.query_one("#input", TextArea)
        recalled = self._recall_input_history(event.key)
        if event.key == "down" and can_enter_selection(self._subagent_rows(), recalled):
            self._enter_subagent_selection()
            event.stop()
            event.prevent_default()
            return
        if recalled is None:
            return
        event.stop()
        event.prevent_default()
        input_widget.load_text(recalled)
        input_widget.cursor_location = input_widget.document.end

    async def action_copy_output_or_quit(self) -> None:
        """Copy a selection first; retain Ctrl+C as quit when nothing is selected."""

        focused = self.focused
        if isinstance(focused, TextArea) and focused.selected_text:
            focused.action_copy()
            return
        selected_text = self.screen.get_selected_text()
        if selected_text is not None:
            self.copy_to_clipboard(selected_text)
            return
        result = self.action_quit()
        if isawaitable(result):
            await result

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)
        if platform.system() == "Darwin":
            subprocess.run(["pbcopy"], input=text, text=True, check=False)

    def on_click(self, event: events.Click) -> None:
        widget_id = getattr(event.widget, "id", None)
        if isinstance(widget_id, str) and widget_id.startswith("subagent-row-"):
            self._subagent_selected = widget_id[len("subagent-row-") :]
            self._subagent_select_mode = True
            self._refresh_subagent_progress()
            event.stop()

    def on_paste(self, event: Paste) -> None:
        """Turn pasted file paths or clipboard images into pending attachments."""

        focused = getattr(self, "focused", None)
        if getattr(focused, "id", None) != "input":
            return
        if self._stage_paste_attachments(getattr(event, "text", None)):
            event.stop()
            event.prevent_default()

    def _paste_composer_clipboard_image(self) -> bool:
        """Attach a clipboard image when the focused TextArea handles Ctrl/Cmd+V."""

        focused = getattr(self, "focused", None)
        if getattr(focused, "id", None) != "input":
            return False
        return self._stage_paste_attachments(None)

    def _notify_clipboard_image_unavailable(self) -> None:
        """Confirm that the paste shortcut ran when its clipboard image lookup failed."""

        self._write_line(
            "No clipboard image found. Copy an image first, or paste an image file path instead.",
            kind=TuiEntryKind.SYSTEM,
        )

    def _stage_paste_attachments(self, paste_text: str | None) -> bool:
        try:
            attachments = resolve_paste_attachments(paste_text)
        except (OSError, ValueError) as exc:
            self._write_line(f"Could not attach pasted image: {exc}", kind=TuiEntryKind.ERROR)
            return True
        if not attachments:
            return False
        existing_paths = {item.path for item in self._staged_attachments}
        added = [item for item in attachments if item.path not in existing_paths]
        if not added:
            return True
        self._staged_attachments.extend(added)
        chips = ", ".join(format_attachment_chip(item) for item in added)
        self._write_line(f"Attached: {chips}", kind=TuiEntryKind.SYSTEM)
        return True

    def _next_chat_turn_token(self) -> int:
        self._chat_turn_token += 1
        return self._chat_turn_token

    def _begin_active_chat_turn(self) -> int:
        token = self._next_chat_turn_token()
        self._start_turn_metrics()
        self._active_chat_turn = _ActiveChatTurn(
            id=uuid4().hex,
            token=token,
        )
        return token

    def _resume_active_chat_turn(self) -> int:
        active_turn = self._active_chat_turn
        if active_turn is not None:
            token = self._next_chat_turn_token()
            active_turn.token = token
            self._preserve_turn_metrics()
            return token
        return self._begin_active_chat_turn()

    def _is_current_chat_turn(self, token: int) -> bool:
        return token == self._chat_turn_token

    def is_turn_active(self) -> bool:
        """True while a chat turn is running or paused for user input.

        Used by command handlers to refuse session-swap/rewind operations that
        would race an in-flight loop. ``_active_chat_turn`` is cleared only when
        a turn ends cleanly (no pending permission/ask_user), so it also covers
        the paused-for-input state where ``_chat_busy`` is already False.
        """

        return self._active_chat_turn is not None

    def _finish_chat_turn(self, token: int) -> None:
        if not self._is_current_chat_turn(token):
            return
        self._refresh_task_plan_panel_from_current_session()
        self._chat_busy = False
        self._chat_worker = None
        if getattr(self.chat_runner, "last_pending_input", None) is None:
            self._active_chat_turn = None

        # 1. Process queued user input first.
        pending_input = self._pending_user_input
        if pending_input is not None:
            self._pending_user_input = None
            pending_attachments = self._pending_user_attachments
            self._pending_user_attachments = None
            self._submit_chat_text(pending_input, attachments=pending_attachments)
            return

        # 2. Then wake the main agent for background results that completed
        #    while no turn was around to drain them.  A running turn already
        #    drains completions on its next provider request, so this only
        #    fires when something is genuinely still undelivered.
        if self._has_pending_background_completions():
            self._submit_nudge_turn()
            return

    def _handle_escape_interrupt(self) -> bool:
        if not self._chat_busy:
            self._last_escape_at = 0.0
            return False
        now = time.monotonic()
        if now - self._last_escape_at > self.ESC_INTERRUPT_WINDOW_SECONDS:
            self._last_escape_at = now
            self._set_activity("running · press Esc again to interrupt")
            return True
        self._last_escape_at = 0.0
        self._interrupt_chat_turn()
        return True

    def _interrupt_chat_turn(self) -> None:
        self._chat_turn_token += 1
        self._discard_stream_deltas()
        cancel_current_turn = getattr(self.chat_runner, "cancel_current_turn", None)
        if cancel_current_turn is not None:
            cancel_current_turn()
        worker = self._chat_worker
        self._chat_worker = None
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()
        self._chat_busy = False
        self._active_chat_turn = None
        self._running_tool_call_ids.clear()
        self._stop_working_animation()
        self._stop_activity_animation()
        self._set_activity("interrupted")
        self._write_line("Interrupted current turn.", kind=TuiEntryKind.SYSTEM)

    def _record_input_history(self, text: str) -> None:
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._input_history_index = None

    def _handle_subagent_select_key(self, event: Key) -> bool:
        """选择模式下的按键：↑/↓ 移动、x 停止、Esc 返回；其它键退出选择并交由原逻辑。"""
        if event.key == "escape":
            self._exit_subagent_selection()
            return True
        if event.key in {"up", "down"}:
            rows = self._subagent_rows()
            # 顶部按上 = 输入栏按下的镜像：退出选择回输入栏。
            if event.key == "up" and self._subagent_selected == (rows[0].id if rows else None):
                self._exit_subagent_selection()
                return True
            self._subagent_selected = move_selection(rows, self._subagent_selected, event.key)
            self._refresh_subagent_progress()
            return True
        if event.key == "x":
            self._stop_selected_subagent()
            return True
        self._exit_subagent_selection()
        return False

    def _enter_subagent_selection(self) -> None:
        self._subagent_select_mode = True
        self._subagent_selected = move_selection(self._subagent_rows(), None, "down")
        self._refresh_subagent_progress()

    def _exit_subagent_selection(self) -> None:
        """退出选择模式：清 mode 与选中项并重渲染，避免高亮残留。"""
        self._subagent_select_mode = False
        self._subagent_selected = None
        self._refresh_subagent_progress()

    def _stop_selected_subagent(self) -> None:
        rows = self._subagent_rows()
        target = stop_target(rows, self._subagent_selected)
        if target is None:
            return
        if target == FG_ID:
            cancel = getattr(self.chat_runner, "cancel_current_turn", None)
            if cancel is not None:
                self._foreground_cancel_requested = True
                cancel()
            return
        manager = getattr(self.chat_runner, "background_manager", None) if self.chat_runner is not None else None
        if manager is not None:
            manager.cancel(target)

    def _recall_input_history(self, direction: str) -> str | None:
        if not self._input_history:
            return None
        if direction == "up":
            if self._input_history_index is None:
                self._input_history_index = len(self._input_history) - 1
            else:
                self._input_history_index = max(0, self._input_history_index - 1)
            return self._input_history[self._input_history_index]
        if direction == "down":
            if self._input_history_index is None:
                return None
            if self._input_history_index >= len(self._input_history) - 1:
                self._input_history_index = None
                return ""
            self._input_history_index += 1
            return self._input_history[self._input_history_index]
        return None

    def _submit_chat_text(self, text: str, *, attachments: list[UserAttachment] | None = None) -> None:
        if self.chat_runner is None:
            self._write_line("普通聊天入口尚未接入 AgentLoop。", kind=TuiEntryKind.ERROR)
            return

        if self._compact_worker is not None:
            self._write_line("Manual compact is still running. Please wait before sending a chat message.", kind=TuiEntryKind.SYSTEM)
            return

        if self._chat_busy:
            self._pending_user_input = text
            self._pending_user_attachments = attachments
            self._write_line(
                "消息已排队。当前 turn 结束后自动发送。",
                kind=TuiEntryKind.SYSTEM,
            )
            self._set_activity("running · message queued")
            return

        pending = getattr(self.chat_runner, "last_pending_input", None)
        if getattr(pending, "kind", None) == "permission_confirmation":
            payload = getattr(pending, "payload", {}) or {}
            review_payload = payload.get("prewrite_review")
            if isinstance(review_payload, dict):
                review_command = review_command_from_text(text, review_payload)
                if review_command is not None:
                    action, path = review_command
                    if action == "all":
                        self._review_expanded_paths = {str(item.get("path") or "") for item in review_payload.get("files", []) if isinstance(item, dict)}
                    elif action == "clear":
                        self._review_expanded_paths.clear()
                    elif path:
                        self._review_expanded_paths.add(path)
                    self._write_review_payload(review_payload)
                    return
            choice = permission_choice_for_text(text, pending)
            if choice is None:
                self._write_line(permission_options_text(pending), kind=TuiEntryKind.PERMISSION)
                return
            self._chat_busy = True
            token = self._resume_active_chat_turn()
            self._chat_worker = self.run_worker(self._resume_permission_turn(pending.id, choice, token))
            return
        if getattr(pending, "kind", None) == "ask_user":
            # ask_user 与权限统一走 resume 协议：回答后 loop 继续执行同批次剩余工具
            # （deferred batch continuation）。输入若匹配某选项则规范化为其 label。
            choice = ask_user_choice_for_text(text, pending)
            if choice is not None:
                text = choice
            self._chat_busy = True
            token = self._resume_active_chat_turn()
            self._chat_worker = self.run_worker(self._resume_permission_turn(pending.id, text, token))
            return

        self._chat_busy = True
        token = self._begin_active_chat_turn()
        self._chat_worker = self.run_worker(self._run_chat_turn(text, token, attachments=attachments))

    def _submit_nudge_turn(self) -> None:
        """开始一个不带用户输入的唤醒轮次，投递后台子 agent 完成通知。"""

        if self.chat_runner is None or self._chat_busy:
            return
        self._chat_busy = True
        token = self._begin_active_chat_turn()
        self._chat_worker = self.run_worker(self._run_nudge_turn(token))

    def _handle_command_action(self, action: dict[str, Any] | None, *, output: str = "") -> bool:
        if not action:
            return False
        action_type = action.get("type")
        if action_type == "submit_chat":
            text = str(action.get("text") or "").strip()
            if text:
                self._submit_chat_text(text)
            return True
        if action_type == "new_session":
            self._picker = None
            self._clear_output()
            if output:
                self._write_line(output, kind=TuiEntryKind.COMMAND)
            return False
        picker_specs = {
            "resume_picker": (
                "resume",
                "Select a session:",
                "sessions",
                session_picker_item,
                "No sessions.",
                "Use up/down and enter to resume, or type a number.",
                "sessions",
            ),
            "model_picker": (
                "model",
                "Select a model:",
                "models",
                model_picker_item,
                "No model choices.",
                "Use up/down and enter to switch, or type /model <provider>/<model>.",
                "models",
            ),
            "skill_picker": (
                "skill",
                "Select a skill:",
                "skills",
                skill_picker_item,
                "No skills.",
                "Use up/down and enter to reference, or type a number.",
                "skills",
            ),
            "recall_picker": (
                "recall",
                "Select a turn to recall to:",
                "turns",
                recall_picker_item,
                "No turns to recall.",
                "Use up/down and enter to recall, or type a number.",
                "turns",
            ),
        }
        picker_spec = picker_specs.get(action_type)
        if picker_spec is not None:
            kind, title, items_key, item_factory, empty_text, footer, count_label = picker_spec
            self._open_picker(
                kind=kind,
                title=title,
                items=[item_factory(item) for item in action.get(items_key, []) if isinstance(item, dict)],
                selected_index=int(action.get("selected_index") or 0),
                empty_text=empty_text,
                footer=footer,
                count_label=count_label,
            )
            return False
        if action_type == "replay_session":
            self._picker = None
            self._replay_current_session()
            recalled_text = str(action.get("recalled_text") or "").strip()
            if recalled_text:
                self._insert_input_text(recalled_text)
            return False
        if action_type == "model_changed":
            self._picker = None
            self.config.provider_name = str(action.get("provider") or "")
            self.config.provider_model = str(action.get("model") or "")
            self._sync_provider_glow()
            return False
        if action_type == "skill_referenced":
            self._picker = None
            self._insert_input_text(str(action.get("reference") or ""))
            return False
        return False

    def _handle_picker_key(self, event: Key) -> bool:
        picker = self._picker
        if picker is None:
            return False
        if event.key == "up":
            picker.move(-1)
            self._render_picker()
            return True
        if event.key == "down":
            picker.move(1)
            self._render_picker()
            return True
        if event.key == "enter":
            self._picker_select_index(picker.selected_index)
            return True
        if event.key == "escape":
            kind = picker.kind
            self._picker = None
            self._write_line(f"{kind.capitalize()} selection cancelled.", kind=TuiEntryKind.COMMAND)
            return True
        return False

    def _picker_select_number(self, number: int) -> bool:
        picker = self._picker
        if picker is None:
            return False
        index = number - 1
        if index < 0 or index >= len(picker.items):
            self._write_line("Invalid selection.", kind=TuiEntryKind.ERROR)
            return True
        self._picker_select_index(index)
        return True

    def _picker_select_index(self, index: int) -> None:
        picker = self._picker
        if picker is None or self.command_handler is None:
            return
        if index < 0 or index >= len(picker.items):
            return
        item = picker.items[index]
        command = picker_command(picker.kind, item)
        if not command:
            return
        result = self.command_handler.handle(command)
        if result.output:
            self._write_line(result.output, kind=TuiEntryKind.COMMAND)
        self._handle_command_action(result.action)
        self._refresh_session_subtitle()

    def _open_picker(self, **fields) -> None:
        self._picker = TuiPickerState(**fields)
        self._render_picker()

    def _render_picker(self) -> None:
        picker = self._picker
        if picker is None:
            return
        self._replace_last_command_output(
            render_picker(
                picker,
                limit=SESSION_LIST_VISIBLE_LIMIT,
                render_item=lambda item, index: render_picker_item(picker, item, index),
            )
        )

    def _insert_input_text(self, text: str) -> None:
        if not text:
            return
        input_widget = self.query_one("#input", TextArea)
        existing = input_widget.text
        prefix = "" if not existing or existing.endswith((" ", "\n")) else " "
        input_widget.load_text(f"{existing}{prefix}{text}")
        input_widget.cursor_location = input_widget.document.end
        input_widget.focus()

    def _replace_last_command_output(self, text: str) -> None:
        for entry in reversed(self.transcript.entries):
            if entry.kind == TuiEntryKind.COMMAND:
                entry.body = text
                rendered = entry_plain_text(entry)
                widget = entry.widget
                if widget is not None and hasattr(widget, "update"):
                    widget.update(_entry_renderable(entry, rendered))
                    return
                self._rerender_transcript()
                return
        self._write_line(text, kind=TuiEntryKind.COMMAND)

    def _clear_output(self) -> None:
        self._clear_task_plan_panel_if_mounted()
        self.transcript = TuiTranscript()
        self.task_plan_panel_state = TuiTaskPlanPanelState()
        self._remove_output_children()

    def _rerender_transcript(self) -> None:
        entries = list(self.transcript.entries)
        self.transcript = TuiTranscript()
        self._remove_output_children()
        for entry in entries:
            if entry.kind == TuiEntryKind.ASSISTANT:
                self._write_markdown_message(entry.body)
            else:
                self._write_line(entry.body, kind=entry.kind, label=entry.label, status=entry.status)

    def _remove_output_children(self) -> None:
        output = self.query_one("#output")
        if hasattr(output, "remove_children"):
            output.remove_children()
            return
        if hasattr(output, "children"):
            for child in list(output.children):
                remove = getattr(child, "remove", None)
                if remove is not None:
                    remove()

    def _replay_current_session(self) -> None:
        current_session = self.current_session
        if current_session is None:
            return
        rebuild_view = getattr(current_session, "rebuild_view", None)
        if rebuild_view is None:
            return
        view = rebuild_view()
        self._clear_output()
        if view.task_plan is not None:
            self._render_task_plan_panel(view.task_plan)
        for message in getattr(view, "messages", []):
            if message.role == "user":
                content = "\n".join(part.content for part in message.parts if getattr(part, "content", ""))
                if not content:
                    continue
                self._write_line(f"> {content}", kind=TuiEntryKind.USER)
            elif message.role == "assistant":
                for part in message.parts:
                    if part.kind == "text" and part.content:
                        self._write_markdown_message(part.content)
                    elif part.kind == "tool_call":
                        name = str(part.metadata.get("tool_name") or "tool")
                        arguments = compact_tool_arguments(part.metadata.get("arguments"))
                        suffix = f" {arguments}" if arguments else ""
                        self._write_line(
                            f"正在调用工具：{name}{suffix}",
                            kind=TuiEntryKind.TOOL,
                            label=f"tool {name} running",
                            status="running",
                        )
            elif message.role == "notification":
                content = "\n".join(part.content for part in message.parts if part.kind == "text" and part.content)
                if content:
                    self._write_line(content, kind=TuiEntryKind.SYSTEM)
            else:
                for part in message.parts:
                    if part.kind != "tool_result":
                        continue
                    name = str(part.metadata.get("tool_name") or "tool")
                    ok = bool(part.metadata.get("ok", True))
                    status = "success" if ok else "error"
                    result = compact_tool_content(part.content)
                    suffix = f"：{result}" if result else ""
                    self._write_line(
                        f"工具{'完成' if ok else '失败'}：{name}{suffix}",
                        kind=TuiEntryKind.TOOL,
                        label=f"tool {name} {status}",
                        status=status,
                    )
        sync_pending = getattr(self.chat_runner, "sync_pending_input_from_current_session", None)
        if sync_pending is not None:
            sync_pending()
        self._write_pending_input()

    async def _resume_permission_turn(self, request_id: str, answer: str, token: int) -> None:
        previous_stream_handler = None
        previous_tool_handler = None
        try:
            previous_stream_handler = self._install_stream_event_handler(token)
            previous_tool_handler = self._install_tool_event_handler(token)
            self._preserve_turn_metrics()
            self._show_working_indicator("resuming with permission answer...")
            response = await self.chat_runner.aresume_with_user_input(request_id, answer)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._is_current_chat_turn(token):
                self._write_line(f"Chat error: {exc}", kind=TuiEntryKind.ERROR)
                self._refresh_session_subtitle()
            return
        finally:
            self._restore_tool_event_handler(previous_tool_handler)
            self._restore_stream_event_handler(previous_stream_handler)
            self._finish_chat_turn(token)

        if self._is_current_chat_turn(token):
            self._write_chat_response(response)

    async def _run_chat_turn(
        self,
        text: str,
        token: int,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> None:
        previous_stream_handler = None
        previous_tool_handler = None
        try:
            previous_stream_handler = self._install_stream_event_handler(token)
            previous_tool_handler = self._install_tool_event_handler(token)
            if self._active_chat_turn is None:
                self._start_turn_metrics()
                self._active_chat_turn = _ActiveChatTurn(
                    id=uuid4().hex,
                    token=token,
                )
            self._show_working_indicator("planning next step...")
            response = await self.chat_runner.arun_user_turn(text, attachments=attachments) if attachments else await self.chat_runner.arun_user_turn(text)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._is_current_chat_turn(token):
                self._write_line(f"Chat error: {exc}", kind=TuiEntryKind.ERROR)
                self._refresh_session_subtitle()
            return
        finally:
            self._restore_tool_event_handler(previous_tool_handler)
            self._restore_stream_event_handler(previous_stream_handler)
            self._finish_chat_turn(token)

        if self._is_current_chat_turn(token):
            self._write_chat_response(response)

    async def _run_nudge_turn(self, token: int) -> None:
        previous_stream_handler = None
        previous_tool_handler = None
        try:
            previous_stream_handler = self._install_stream_event_handler(token)
            previous_tool_handler = self._install_tool_event_handler(token)
            if self._active_chat_turn is None:
                self._start_turn_metrics()
                self._active_chat_turn = _ActiveChatTurn(
                    id=uuid4().hex,
                    token=token,
                )
            self._show_working_indicator("planning next step...")
            response = await self.chat_runner.anudge_turn()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._is_current_chat_turn(token):
                self._write_line(f"Chat error: {exc}", kind=TuiEntryKind.ERROR)
                self._refresh_session_subtitle()
            return
        finally:
            self._restore_tool_event_handler(previous_tool_handler)
            self._restore_stream_event_handler(previous_stream_handler)
            self._finish_chat_turn(token)

        if self._is_current_chat_turn(token) and getattr(response, "content", ""):
            self._write_chat_response(response)

    def _write_chat_response(self, response) -> None:
        self._drain_stream_deltas()
        display_lines = list(getattr(self.chat_runner, "last_display_lines", []) or [])
        content = getattr(response, "content", "")
        if self._stream_text_started:
            if content and normalize_stream_text(content) != normalize_stream_text(self._stream_text_buffer):
                self._stream_text_buffer = content
                if self._stream_text_entry is not None:
                    self._stream_text_entry.body = content
            display_lines = [line for line in display_lines if looks_like_tool_display_line(line) or normalize_stream_text(line) != normalize_stream_text(self._stream_text_buffer)]
            self._finalize_stream_widget()
        if self._live_tool_events_seen:
            display_lines = [line for line in display_lines if not looks_like_tool_display_line(line)]
        if self._live_tool_events_seen and self._stream_text_started:
            display_lines = []
        if display_lines:
            for line in display_lines:
                if line == content or looks_like_markdown_response(line):
                    self._write_markdown_message(line)
                else:
                    self._write_line(line, kind=display_line_kind(line), status=display_line_status(line))
        elif not self._stream_text_started:
            self._write_markdown_message(content or "[assistant response has no text content]")
        self._write_pending_input()
        if getattr(self.chat_runner, "last_pending_input", None) is None:
            self._stop_activity_animation()
            self._set_activity("done")
        self._refresh_session_subtitle()

    def _show_activity_animation(self, kind: str, detail: str) -> None:
        self._activity_animation_kind = kind
        self._activity_animation_detail = detail
        self._activity_frame_index = 0
        self._activity_started_at = time.monotonic()
        self._set_activity(self._activity_animation_body())
        self._start_activity_animation()

    def _start_turn_metrics(self) -> None:
        self._turn_started_at = time.monotonic()
        self._turn_tool_count = 0
        self._running_tool_call_ids = set()

    def _turn_elapsed_seconds(self) -> float:
        if not self._turn_started_at:
            return 0.0
        return max(0.0, time.monotonic() - self._turn_started_at)
