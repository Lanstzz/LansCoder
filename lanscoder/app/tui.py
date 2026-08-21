"""Textual TUI 应用层主体:输入提交、流式渲染、子agent 进度面板、权限/ask_user 交互及各类选择器。"""

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
from textual.widgets import Button, Static, TextArea

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
    ask_user_prompt_text,
    permission_choice_for_text,
    permission_option_label,
    permission_prompt_text,
)
from lanscoder.app.review_view import render_prewrite_review, review_command_from_text
from lanscoder.app.transcript_view import (
    looks_like_markdown_response,
    looks_like_tool_display_line,
    normalize_stream_text,
)
from lanscoder.app import model_topbar_themes
from lanscoder.app.projector import TranscriptProjector, replay_messages
from lanscoder.app.tui_state import BlockKind, ChildItem, ChildKind, TuiTaskPlanPanelState, TranscriptModel
from lanscoder.app.topbar_view import _provider_name_markup, _provider_model_markup
from lanscoder.app.tui_view import LansCoderViewMixin, _entry_renderable_block
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
    "_format_subagent_line",
    "_observe_markdown_update",
    "_plain_static",
    "_progress_indicator",
    "_provider_model_markup",
    "_provider_name_markup",
]


_PROGRESS_INDICATORS = ("[    ]", "[>   ]", "[->  ]", "[--> ]", "[--->]", "[ ---]", "[  --]", "[   -]")

_MAX_VISIBLE_SUBAGENT_LINES = 3


def _progress_indicator(frame: int) -> str:
    return _PROGRESS_INDICATORS[frame % len(_PROGRESS_INDICATORS)]


def _format_subagent_line(
    *,
    label: str,
    elapsed: float,
    calls: int,
    tokens: int,
    indicator: str,
) -> str:
    """格式化单个子agent 的进度行(耗时 + 调用数 + token)。"""
    token_str = f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)
    return f"{indicator} {label} · {elapsed:.0f}s · {calls} calls · {token_str} tokens"


@dataclass(slots=True)
class _ActiveChatTurn:
    """当前正在进行的聊天回合标识:回合 id 与代际 token。"""

    id: str
    token: int


class LansCoderApp(LansCoderViewMixin, App[None]):
    """Textual 主应用:组合 topbar/输出区/任务计划面板/输入区/子agent 面板,承载全部 TUI 交互。"""

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
    ACTIVITY_IDLE_REVERT_SECONDS = 2.0
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
        """初始化全部 TUI 状态:流式渲染缓冲、子agent 面板、选择器、附件与回合代际。"""
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
        self._stream_rendered_text = ""
        self._stream_flush_timer: Timer | None = None
        self._stream_markdown_update = None
        self._stream_finalizations: dict[LansCoderMarkdown, object] = {}
        self._stream_event_lock = threading.Lock()
        self._stream_event_generation = 0
        self._stream_event_dispatch_scheduled = False
        self._pending_stream_text: list[str] = []
        self._pending_reasoning_text: list[str] = []
        self._ui_epoch = 0
        self._reasoning_is_fallback = False
        self._working_text = ""
        self._working_frame_index = 0
        self._working_timer: Timer | None = None
        self._activity_animation_kind = ""
        self._activity_animation_detail = ""
        self._activity_frame_index = 0
        self._activity_started_at = 0.0
        self._activity_timer: Timer | None = None
        self._activity_idle_revert_timer: Timer | None = None
        self._turn_started_at = 0.0
        self._turn_tool_count = 0
        self._running_tool_call_ids: set[str] = set()
        self._live_tool_events_seen = False
        self._stream_segment_closed_for_tool = False
        self._activity_text = "idle · ready"
        self._permission_buttons: dict[Button, str] = {}
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
        self.transcript = TranscriptModel()
        self.projector = TranscriptProjector(self.transcript)
        self.task_plan_panel_state = TuiTaskPlanPanelState()

    def compose(self) -> ComposeResult:
        """按 Textual 布局 yield 各 UI 组件:topbar、输出区、任务计划面板、输入区、子agent 面板。"""
        yield Static(self._topbar_text(), id="topbar", classes="topbar")
        with Vertical(id="main"):
            yield VerticalScroll(id="output")
            yield _plain_static("", id="task-plan-panel", classes="task-plan-panel hidden")
            yield Static("idle · ready", id="activity", classes="activity-line")
            yield _plain_static("", id="permission-zone", classes="permission-zone hidden")
            # 联想栏放在 composer 上方的兄弟位置:弹出时不再把输入框往下推
            yield SlashSuggest(id="slash-suggest")
            with Vertical(id="composer", classes="composer"):
                yield ComposerTextArea(
                    placeholder="输入消息，Enter 发送，Shift+Enter 换行，Ctrl/Cmd+V 粘贴图片",
                    id="input",
                    show_line_numbers=False,
                    soft_wrap=True,
                    compact=True,
                )
            yield Vertical(id="subagent-panel", classes="hidden")

    def on_mount(self) -> None:
        """挂载后初始化标题、欢迎页、斜杠命令补全、焦点与子agent 完成回调。"""
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
        self.query_one("#output").can_focus = False
        self.set_focus(self.query_one("#input"))
        self._write_pending_input()
        self._subagent_progress_timer = self.set_interval(0.5, self._refresh_subagent_progress)
        if self.chat_runner is not None:
            mgr = getattr(self.chat_runner, "background_manager", None)
            if mgr is not None and hasattr(mgr, "set_on_job_completed"):
                mgr.set_on_job_completed(self._on_subagent_completed)

    def on_app_focus(self) -> None:
        """应用获得焦点时把焦点还给输入区。"""
        try:
            self.set_focus(self.query_one("#input"))
        except Exception:
            pass

    def _refresh_subagent_progress(self) -> None:
        """定时刷新子agent 进度面板:同步前台/后台任务行,超出上限时折叠显示。"""
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
        row_ids = [row.id for row in rows[:_MAX_VISIBLE_SUBAGENT_LINES]]
        # 选中的行即使超出可见上限也要保留,否则选择会指向一个被删除的行
        if self._subagent_selected is not None and self._subagent_selected not in row_ids and any(row.id == self._subagent_selected for row in rows):
            row_ids.append(self._subagent_selected)
        hidden = len(rows) - len(row_ids)
        hint = "↑/↓ 选择 · x 停止 · Esc 返回" if self._subagent_select_mode else "↓ 进入选择 · 点击选择子agent"
        children_by_id = {child.id: child for child in panel.children if isinstance(child.id, str)}
        keep_ids = {f"subagent-row-{row_id}" for row_id in row_ids}
        for child in list(panel.children):
            if isinstance(child.id, str) and child.id not in keep_ids and child.id not in {"subagent-hint", "subagent-footer"}:
                child.remove()
        anchors = [child for child in panel.children if isinstance(child.id, str) and child.id in {"subagent-footer", "subagent-hint"}]
        anchor = anchors[0] if anchors else None
        for row_id in row_ids:
            static = children_by_id.get(f"subagent-row-{row_id}")
            if static is None:
                static = Static(lines_by_id[row_id], id=f"subagent-row-{row_id}")
                panel.mount(static)
            else:
                static.update(lines_by_id[row_id])
            static.set_class(row_id == self._subagent_selected, "selected")
            if anchor is not None:
                panel.move_child(static, before=anchor)
        footer_widget = children_by_id.get("subagent-footer")
        if hidden > 0:
            if footer_widget is None:
                hint_for_order = children_by_id.get("subagent-hint")
                panel.mount(Static(f"…还有 {hidden} 个子agent在跑", id="subagent-footer"), before=hint_for_order)
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
        """按当前行集合校正选中状态,失配时退出选择模式。"""
        if not any(row.id == FG_ID for row in rows):
            self._foreground_cancel_requested = False
        if self._subagent_selected is not None and not any(row.id == self._subagent_selected for row in rows):
            self._subagent_selected = None
            self._subagent_select_mode = False

    def _on_subagent_completed(self, job) -> None:
        self.call_from_thread(self._handle_subagent_completed, job)

    def _handle_subagent_completed(self, job) -> None:
        """子agent 完成后写入 UI 结果行,空闲时补发一次引导回合。"""
        if not getattr(self, "is_mounted", False):
            return
        label = job.label or job.tool_name
        if job.status == "completed":
            ui_msg = f"✅ 子agent [{label}] 已完成"
        elif job.status == "failed":
            ui_msg = f"❌ 子agent [{label}] 失败: {job.error or '未知错误'}"
        else:
            ui_msg = f"⚠️ 子agent [{label}] {job.status}"

        self._ui_line(BlockKind.SYSTEM, ui_msg)

        if not self._chat_busy and self.chat_runner is not None:
            self._submit_nudge_turn()

    def _has_pending_background_completions(self) -> bool:
        """判断当前会话是否有待处理的后台任务完成事件。"""
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
        """注入斜杠命令列表并刷新补全组件。"""
        self._slash_commands = commands
        try:
            suggest = self.query_one("#slash-suggest", SlashSuggest)
            suggest.set_commands(commands)
        except Exception:
            pass

    def _on_terminal_resized(self) -> None:
        self._refresh_session_subtitle()
        self._refresh_welcome_layout()

    def on_unmount(self) -> None:
        """卸载时停止动画并触发关闭回调(只触发一次)。"""
        self._stop_welcome_particles()
        self._stop_provider_glow()
        if not self._shutdown_called and self._on_shutdown is not None:
            self._shutdown_called = True
            self._on_shutdown()

    async def _submit_composer(self) -> None:
        """处理输入框提交:记录历史、处理数字选择/斜杠命令/附件,最后发起聊天回合。"""
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
            if await self._picker_select_number(int(text)):
                return

        attachment_chips = "\n".join(format_attachment_chip(item) for item in attachments)
        user_text = text
        if attachment_chips:
            user_text = f"{user_text}\n{attachment_chips}"
        self._ui_line(BlockKind.USER, user_text)

        if text.startswith("/"):
            if self.command_handler is None:
                self._ui_line(BlockKind.ERROR, "Command handler is not configured.")
                return

            if " ".join(text.split()) == "/compact":
                self._submit_manual_compact(text)
                return

            result = self.command_handler.handle(text)
            if result.handled:
                self._ui_line(BlockKind.COMMAND, result.output)
                await self._handle_command_action(result.action, output=result.output)
                self._refresh_session_subtitle()
                return
            self._ui_line(BlockKind.ERROR, f"Unknown command: {text}")
            return

        self._staged_attachments.clear()
        self._submit_chat_text(text, attachments=attachments)

    def _submit_manual_compact(self, text: str) -> None:
        """手动 /compact:校验空闲且无重复任务后启动压缩 worker。"""
        if self._chat_busy:
            self._ui_line(BlockKind.SYSTEM, "Chat is still running. Please wait before compacting context.")
            return
        if self._compact_worker is not None:
            self._ui_line(BlockKind.SYSTEM, "Manual compact is already running.")
            return
        self._set_activity("compacting context...")
        self._ui_line(BlockKind.SYSTEM, "Manual compact started.")
        self._compact_worker = self.run_worker(self._run_manual_compact_command(text))

    async def _run_manual_compact_command(self, text: str) -> None:
        """在线程池里执行 /compact 命令并刷新输出与活动状态。"""
        try:
            assert self.command_handler is not None
            result = await anyio.to_thread.run_sync(self.command_handler.handle, text)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._ui_line(BlockKind.ERROR, f"Manual compact error: {exc}")
            return
        finally:
            self._compact_worker = None

        if result.handled:
            self._ui_line(BlockKind.COMMAND, result.output)
            await self._handle_command_action(result.action, output=result.output)
            self._refresh_session_subtitle()
        else:
            self._ui_line(BlockKind.ERROR, f"Unknown command: {text}")
        self._set_activity("done")

    async def on_composer_text_area_submitted(self, event: ComposerTextArea.Submitted) -> None:
        """输入区提交事件:有选择器时先选中,否则走提交流程。"""
        event.stop()
        if self._picker is not None:
            await self._picker_select_index(self._picker.selected_index)
            return
        await self._submit_composer()

    async def on_event(self, event: events.Event) -> None:
        """事件入口:子agent 选择模式下先拦截按键交给专用处理。"""
        if isinstance(event, Key) and not event.is_forwarded and self._subagent_select_mode:
            if self._handle_subagent_select_key(event):
                event.stop()
                event.prevent_default()
                return
        await super().on_event(event)

    async def on_key(self, event: Key) -> None:
        """按键处理:选择器/子agent 选择、Esc 中断、方向键回顾输入历史。"""
        if self._picker is not None and await self._handle_picker_key(event):
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
        """Ctrl+C:优先复制选中文本,无选中时退出应用。"""

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
        """复制到剪贴板;macOS 上额外用 pbcopy 兜底(后台线程执行,避免阻塞事件循环)。"""
        super().copy_to_clipboard(text)
        if platform.system() == "Darwin":
            self.run_worker(
                lambda: subprocess.run(["pbcopy"], input=text, text=True, check=False),
                thread=True,
                exclusive=False,
                exit_on_error=False,
            )

    def on_click(self, event: events.Click) -> None:
        """点击子agent 行时选中并进入选择模式。"""
        widget_id = getattr(event.widget, "id", None)
        if isinstance(widget_id, str) and widget_id.startswith("subagent-row-"):
            self._subagent_selected = widget_id[len("subagent-row-") :]
            self._subagent_select_mode = True
            self._refresh_subagent_progress()
            event.stop()

    def on_paste(self, event: Paste) -> None:
        """粘贴事件:输入区聚焦时尝试把剪贴板内容转为附件。"""

        focused = getattr(self, "focused", None)
        if getattr(focused, "id", None) != "input":
            return
        if self._stage_paste_attachments(getattr(event, "text", None)):
            event.stop()
            event.prevent_default()

    def _paste_composer_clipboard_image(self) -> bool:
        """把剪贴板图片作为附件暂存(无文本粘贴时的路径)。"""

        focused = getattr(self, "focused", None)
        if getattr(focused, "id", None) != "input":
            return False
        return self._stage_paste_attachments(None)

    def _notify_clipboard_image_unavailable(self) -> None:
        self._ui_line(BlockKind.SYSTEM, "No clipboard image found. Copy an image first, or paste an image file path instead.")

    def _stage_paste_attachments(self, paste_text: str | None) -> bool:
        """解析并去重暂存粘贴的附件,写入附件提示行。"""
        try:
            attachments = resolve_paste_attachments(paste_text)
        except (OSError, ValueError) as exc:
            self._ui_line(BlockKind.ERROR, f"Could not attach pasted image: {exc}")
            return True
        if not attachments:
            return False
        existing_paths = {item.path for item in self._staged_attachments}
        added = [item for item in attachments if item.path not in existing_paths]
        if not added:
            return True
        self._staged_attachments.extend(added)
        chips = ", ".join(format_attachment_chip(item) for item in added)
        self._ui_line(BlockKind.SYSTEM, f"Attached: {chips}")
        return True

    def _next_chat_turn_token(self) -> int:
        self._chat_turn_token += 1
        return self._chat_turn_token

    def _begin_active_chat_turn(self) -> int:
        """开启新聊天回合:分配代际 token 并记录起始指标。"""
        token = self._next_chat_turn_token()
        self._start_turn_metrics()
        self._active_chat_turn = _ActiveChatTurn(
            id=uuid4().hex,
            token=token,
        )
        return token

    def _resume_active_chat_turn(self) -> int:
        """为挂起回合分配新 token 继续,无活跃回合时新建。"""
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
        return self._active_chat_turn is not None

    def _finish_chat_turn(self, token: int) -> None:
        """回合收尾:回填 thinking 时长、刷新任务计划面板、解除忙状态,必要时续发排队/引导回合。"""
        if not self._is_current_chat_turn(token):
            return
        # reconcile 必须在 pending/nudge 推进 token 之前,否则旧回合的时长回填被整体跳过
        self._apply_turn_reasoning_durations()
        self._refresh_task_plan_panel_from_current_session()
        self._chat_busy = False
        self._chat_worker = None
        if getattr(self.chat_runner, "last_pending_input", None) is None:
            self._active_chat_turn = None

        pending_input = self._pending_user_input
        if pending_input is not None:
            self._pending_user_input = None
            pending_attachments = self._pending_user_attachments
            self._pending_user_attachments = None
            self._submit_chat_text(pending_input, attachments=pending_attachments)
            return

        if self._has_pending_background_completions():
            self._submit_nudge_turn()
            return

    def _apply_turn_reasoning_durations(self) -> None:
        """把本回合 store 里记录的 reasoning 秒数回填到 live thinking 子行。

        语义与 replay_messages 的 merge 规则对位:带 text/tool part 的消息
        结算其 thinking 子行并开启新行;纯 reasoning 消息(live/replay 都)合并进
        上一行,保留该行首条 reasoning 的秒数。物化仅发生在非流式回合(无 live 子行)。
        """
        entries = getattr(self.chat_runner, "last_turn_reasonings", None) or []
        if not entries:
            return
        current_child: ChildItem | None = None
        prev_had_parts = False
        counter = 0
        for reasoning, seconds, had_parts in entries:
            if current_child is None or prev_had_parts:
                block = self.transcript.last_block()
                block_children = [c for c in (block.children if block else []) if c.kind == ChildKind.THINKING]
                if counter < len(block_children):
                    current_child = block_children[counter]
                else:
                    if block is None or block.kind != BlockKind.ASSISTANT:
                        self.projector.start_assistant()
                        block = self.transcript.last_block()
                    self.projector.append_thinking(reasoning, track_duration=False, duration_seconds=seconds)
                    current_child = block.children[-1]
                    block_index = len(self.transcript.blocks) - 1
                    output = self.query_one("#output")
                    self._mount_child_row(output, block_index, current_child)
                counter += 1
                current_child.duration_seconds = seconds
                current_child.finished = True
                block_index = len(self.transcript.blocks) - 1
                self._refresh_child_row(block_index, current_child)
            # 合并条目(上一消息纯 reasoning、无 text/tool part)不进入分支:
            # live/replay 均已把文本并进同一行,时长保留该行首条 reasoning 的值。
            prev_had_parts = had_parts

    def _handle_escape_interrupt(self) -> bool:
        """Esc 双重打断窗口:第一次提示,窗口内再按则中断当前回合。"""
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
        """取消当前回合:取消 provider 与 worker,清空流缓冲并复位动画。"""
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
        self.projector.end_turn()
        self._set_activity("interrupted")
        self._ui_line(BlockKind.SYSTEM, "Interrupted current turn.")

    def _record_input_history(self, text: str) -> None:
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._input_history_index = None

    def _handle_subagent_select_key(self, event: Key) -> bool:
        """子agent 选择模式的按键:上/下移动、x 停止、Esc 退出。"""
        if event.key == "escape":
            self._exit_subagent_selection()
            return True
        if event.key in {"up", "down"}:
            rows = self._subagent_rows()
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
        self._subagent_select_mode = False
        self._subagent_selected = None
        self._refresh_subagent_progress()

    def _stop_selected_subagent(self) -> None:
        """停止选中的子agent:前台走取消回合,后台走管理器取消。"""
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
        """按方向键在输入历史中回溯,返回应加载的文本。"""
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
        """核心提交入口:忙态排队消息,处理挂起的权限/ask_user,否则启动新回合。"""
        if self.chat_runner is None:
            self._ui_line(BlockKind.ERROR, "普通聊天入口尚未接入 AgentLoop。")
            return

        if self._compact_worker is not None:
            self._ui_line(BlockKind.SYSTEM, "Manual compact is still running. Please wait before sending a chat message.")
            return

        if self._chat_busy:
            self._pending_user_input = text
            self._pending_user_attachments = attachments
            self._ui_line(BlockKind.SYSTEM, "消息已排队。当前 turn 结束后自动发送。")
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
                self._show_permission_zone()
                return
            self._submit_permission_choice(choice)
            return
        if getattr(pending, "kind", None) == "ask_user":
            choice = ask_user_choice_for_text(text, pending)
            if choice is not None:
                text = choice
            self._submit_permission_choice(text)
            return

        self._chat_busy = True
        token = self._begin_active_chat_turn()
        self._chat_worker = self.run_worker(self._run_chat_turn(text, token, attachments=attachments))

    def _submit_permission_choice(self, choice: str) -> None:
        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is None:
            return
        self._clear_permission_zone()
        self._chat_busy = True
        token = self._resume_active_chat_turn()
        self._chat_worker = self.run_worker(self._resume_permission_turn(pending.id, choice, token))

    def _show_permission_zone(self) -> None:
        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is None:
            self._clear_permission_zone()
            return
        zone = self.query_one("#permission-zone", Static)
        zone.remove_class("hidden")
        pending_kind = getattr(pending, "kind", None)
        if pending_kind == "permission_confirmation":
            payload = getattr(pending, "payload", {}) or {}
            review_payload = payload.get("prewrite_review")
            if isinstance(review_payload, dict):
                self._review_expanded_paths.clear()
                text = render_prewrite_review(review_payload, expanded_paths=self._review_expanded_paths).plain
            else:
                text = permission_prompt_text(pending)
        else:
            text = ask_user_prompt_text(pending)
        zone.update(text)
        options = list(getattr(pending, "options", []) or [])
        wanted: list[tuple[Button, str]] = []
        for index, option in enumerate(options):
            option_id = str(getattr(option, "id", "") or "")
            label = str(getattr(option, "label", "") or option_id)
            label = permission_option_label(label, option_id)
            button_id = f"permission-{option_id}" if option_id else f"permission-opt-{index}"
            submit_choice = label if pending_kind == "ask_user" else (option_id or label)
            wanted.append((Button(label, id=button_id, classes="permission-button"), submit_choice))
        # 同一次消息循环内可能连续显示 zone(点击按钮后 pending 未清时 worker 会再显示),
        # 而按钮移除是异步 prune,旧按钮在节点表里仍存在;直接重挂同名按钮会触发 DuplicateIds,
        # 这里按 id 复用/更新,只移除这次不需要的按钮。
        existing = {child.id: child for child in list(zone.children) if isinstance(child, Button) and isinstance(child.id, str)}
        wanted_ids = {str(button.id) for button, _ in wanted}
        for button_id in sorted(set(existing).difference(wanted_ids)):
            existing[button_id].remove()
        self._permission_buttons = {}
        for button, choice in wanted:
            prior = existing.get(str(button.id))
            if prior is not None:
                prior.label = button.label
                live = prior
            else:
                zone.mount(button)
                live = button
            self._permission_buttons[live] = choice
        self._set_activity("waiting · permission")

    def _clear_permission_zone(self) -> None:
        zone = self.query_one("#permission-zone", Static)
        zone.update("")
        zone.add_class("hidden")
        self._permission_buttons = {}

    def on_button_pressed(self, event: Button.Pressed) -> None:
        widget_id = str(getattr(event.button, "id", "") or "")
        if not widget_id.startswith("permission-"):
            return
        event.stop()
        choice = self._permission_buttons.get(event.button, "")
        if not choice:
            return
        self._submit_permission_choice(choice)
        self._clear_permission_zone()

    def _submit_nudge_turn(self) -> None:
        """空闲时补发一次引导回合,处理后台任务完成。"""

        if self.chat_runner is None or self._chat_busy:
            return
        self._chat_busy = True
        token = self._begin_active_chat_turn()
        self._chat_worker = self.run_worker(self._run_nudge_turn(token))

    async def _handle_command_action(self, action: dict[str, Any] | None, *, output: str = "") -> bool:
        """按命令动作分派 UI 行为:提交聊天、换会话、打开各类选择器、回放会话等。"""
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
            await self._clear_output()
            if output:
                self._ui_line(BlockKind.COMMAND, output)
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
            await self._replay_current_session()
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

    async def _handle_picker_key(self, event: Key) -> bool:
        """选择器按键:上/下移动、Enter 选中、Esc 取消。"""
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
            await self._picker_select_index(picker.selected_index)
            return True
        if event.key == "escape":
            kind = picker.kind
            self._picker = None
            self._ui_line(BlockKind.COMMAND, f"{kind.capitalize()} selection cancelled.")
            return True
        return False

    async def _picker_select_number(self, number: int) -> bool:
        picker = self._picker
        if picker is None:
            return False
        index = number - 1
        if index < 0 or index >= len(picker.items):
            self._ui_line(BlockKind.ERROR, "Invalid selection.")
            return True
        await self._picker_select_index(index)
        return True

    async def _picker_select_index(self, index: int) -> None:
        """选中选择器某项:构造命令交给命令处理器并刷新输出。"""
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
            self._ui_line(BlockKind.COMMAND, result.output)
        await self._handle_command_action(result.action)
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
        """就地更新最后一条命令输出块,找不到时追加新行。

        不做整树重建:remove+remount 会撞上 Textual 异步 prune,同一帧内
        旧子行(thinking/tool)还没卸载,重挂同名行要么 DuplicateIds 要么
        被僵尸行去重吃掉,导致 picker 上下移动时 transcript 的子行消失。
        """
        block = self.transcript.find_last_command_block()
        if block is None:
            self._ui_line(BlockKind.COMMAND, text)
            return
        block.text = text
        output = self._query_mounted("#output")
        if output is None:
            return
        target = next(
            (w for w in getattr(output, "children", ()) if getattr(w, "id", None) == f"command-block-{id(block)}"),
            None,
        )
        if target is not None and hasattr(target, "update"):
            target.update(_entry_renderable_block(block, text))
            return
        self._append_block(block)

    async def _clear_output(self) -> None:
        """重置 transcript 与输出区,并使在途的流式/工具 UI 回调失效。

        历史重放或换会话时,旧回合迟到的事件(后台工具完成、残留的流 drain)
        若落到新重放的 DOM 上会撞出重复的 child id;这里推进 epoch 并把流缓冲
        作废,让晚到的回调在新视图上无操作。移除是异步 prune,重挂前必须
        等待卸载完成,否则同名子行会 DuplicateIds。
        """
        self._clear_task_plan_panel_if_mounted()
        self._discard_stream_deltas()
        self._start_new_stream_segment()
        self._stop_working_animation()
        self._stop_activity_animation()
        self._ui_epoch += 1
        self.transcript = TranscriptModel()
        self.projector = TranscriptProjector(self.transcript)
        self.task_plan_panel_state = TuiTaskPlanPanelState()
        await self._remove_output_children_async()

    async def _remove_output_children_async(self) -> None:
        output = self.query_one("#output")
        if getattr(self, "is_running", False) and hasattr(output, "remove_children"):
            await output.remove_children()
            return
        self._remove_output_children()

    def _rerender_transcript(self) -> None:
        """按 transcript 块重建输出区内容,保留模型与块索引(供子行点击定位)。"""
        self.projector.end_turn()
        self._remove_output_children()
        for index, block in enumerate(self.transcript.blocks):
            self.render_block_into(block, index)

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

    async def _replay_current_session(self) -> None:
        """把当前会话历史回放到输出区,并同步挂起的输入。"""
        current_session = self.current_session
        if current_session is None:
            return
        rebuild_view = getattr(current_session, "rebuild_view", None)
        if rebuild_view is None:
            return
        view = rebuild_view()
        await self._clear_output()
        if view.task_plan is not None:
            self._render_task_plan_panel(view.task_plan)
        self.projector = TranscriptProjector(self.transcript)
        replay_messages(self.projector, getattr(view, "messages", []))
        for index, block in enumerate(self.transcript.blocks):
            self.render_block_into(block, index)
        sync_pending = getattr(self.chat_runner, "sync_pending_input_from_current_session", None)
        if sync_pending is not None:
            sync_pending()
        self._write_pending_input()

    async def _resume_permission_turn(self, request_id: str, answer: str, token: int) -> None:
        """携带权限/ask_user 答案恢复挂起回合,装好流与工具事件处理器。"""
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
                self._ui_line(BlockKind.ERROR, f"Chat error: {exc}")
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
        """启动一次新聊天回合:装事件处理器、调用 chat_runner 并写回响应。"""
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
                self._ui_line(BlockKind.ERROR, f"Chat error: {exc}")
                self._refresh_session_subtitle()
            return
        finally:
            self._restore_tool_event_handler(previous_tool_handler)
            self._restore_stream_event_handler(previous_stream_handler)
            self._finish_chat_turn(token)

        if self._is_current_chat_turn(token):
            self._write_chat_response(response)

    async def _run_nudge_turn(self, token: int) -> None:
        """启动一次引导回合(处理后台完成)并写回响应。"""
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
                self._ui_line(BlockKind.ERROR, f"Chat error: {exc}")
                self._refresh_session_subtitle()
            return
        finally:
            self._restore_tool_event_handler(previous_tool_handler)
            self._restore_stream_event_handler(previous_stream_handler)
            self._finish_chat_turn(token)

        if self._is_current_chat_turn(token) and getattr(response, "content", ""):
            self._write_chat_response(response)

    def _write_chat_response(self, response) -> None:
        """把回合响应写入输出:去重流式缓冲、过滤工具行,按内容类型渲染。"""
        self._drain_stream_deltas()
        display_lines = list(getattr(self.chat_runner, "last_display_lines", []) or [])
        content = getattr(response, "content", "")
        if self._stream_text_started:
            # 无工具切分时,流缓冲与最终 content 对齐(流被截断时兜底);
            # 有工具事件时缓冲只含工具后的尾段,若用完整 content 覆盖会导致
            # 工具前的文本在尾部块里重复渲染(已 finalize 的前段块不会改变)。
            if not self._live_tool_events_seen and content and normalize_stream_text(content) != normalize_stream_text(self._stream_text_buffer):
                self._stream_text_buffer = content
                block = self.transcript.last_block()
                if block is not None and block.kind == BlockKind.ASSISTANT:
                    block.text = content
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
                elif not looks_like_tool_display_line(line):
                    self._ui_line(BlockKind.SYSTEM, line)
        elif not self._stream_text_started:
            self._write_markdown_message(content or "[assistant response has no text content]")
        self._write_pending_input()
        if getattr(self.chat_runner, "last_pending_input", None) is None:
            self._stop_activity_animation()
            self._set_activity("done")
            self._schedule_activity_idle_revert()
        self._refresh_session_subtitle()

    def _show_activity_animation(self, kind: str, detail: str) -> None:
        """启动底部活动动画,按 kind 决定帧集。"""
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
        self._cancel_activity_idle_revert()

    def _turn_elapsed_seconds(self) -> float:
        if not self._turn_started_at:
            return 0.0
        return max(0.0, time.monotonic() - self._turn_started_at)
