from __future__ import annotations

import asyncio
import threading

from rich.text import Text
from textual.css.query import NoMatches

from lanscoder.app import model_topbar_themes, theme
from lanscoder.app.activity_view import (
    post_tool_reasoning_text,
    task_plan_panel_text,
    tool_activity_line_text,
    tool_event_status,
    single_line_activity,
    truncate_activity_text,
    turn_metrics_text,
)
from lanscoder.app.permission_view import (
    ask_user_prompt_text,
    permission_options_text,
    permission_prompt_text,
)
from lanscoder.app.review_view import render_prewrite_review
from lanscoder.app.topbar_view import (
    PERMISSION_MODE_COLORS,
    _metadata_markup,
    _markup_width,
    _provider_model_markup,
    _truncate_markup,
)
from lanscoder.app.transcript_view import (
    block_classes,
    child_collapsed_text,
    child_expanded_text,
    child_row_classes,
    entry_markdown_text_block,
)
from lanscoder.app.tui_state import BlockKind, ChildItem, ChildKind, TranscriptBlock
from lanscoder.app.tui_widgets import (
    ChildRow,
    LansCoderMarkdown,
    _observe_markdown_update,
    _plain_static,
)
from lanscoder.app.welcome import welcome_renderable
from lanscoder.planning.models import TaskPlan
from lanscoder.planning.projection import project_plan
from lanscoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES


def _trailing_text_run_start(block: TranscriptBlock) -> int:
    """返回块内末尾连续 TEXT_RUN 段的起始下标;无正文段时返回 len(children)。

    物化 thinking 行必须插到该位置之前,才能保证 thinking 不落在最终回复正文
    下方(末尾正文段就是在它之后流式挂载的最终回答)。
    """
    index = len(block.children)
    while index > 0 and block.children[index - 1].kind == ChildKind.TEXT_RUN:
        index -= 1
    return index


def _last_assistant_markdown_widget(output) -> object | None:
    """输出区最后一个 assistant 正文 markdown widget(即末尾 TEXT_RUN 的 widget)。"""
    for widget in reversed(getattr(output, "children", ())):
        if isinstance(widget, LansCoderMarkdown):
            return widget
    return None


def _entry_renderable_block(block: TranscriptBlock, rendered: str) -> object:
    if block.kind != BlockKind.COMMAND:
        return rendered
    if not any(line.startswith("> ") for line in rendered.splitlines()):
        return rendered
    text = Text()
    for line_index, line in enumerate(rendered.splitlines()):
        if line_index:
            text.append("\n")
        if line.startswith("> "):
            text.append(">", style=f"{theme.ACCENT} bold")
            text.append(line[1:])
        else:
            text.append(line)
    return text


class LansCoderViewMixin:
    def _refresh_session_subtitle(self) -> None:
        session_id = None
        if self.current_session is None:
            self.sub_title = ""
        else:
            session_id = self.current_session.session_id
            self.sub_title = f"Session: {session_id}"
        topbar = self._query_mounted("#topbar")
        if topbar is not None and hasattr(topbar, "update"):
            topbar.update(self._topbar_text(session_id=session_id, width=self._topbar_width()))

    def _topbar_width(self) -> int | None:
        size = getattr(self, "size", None)
        width = getattr(size, "width", None)
        if isinstance(width, int) and width > 0:
            return max(1, width - 4)
        return None

    def _topbar_text(self, *, session_id: str | None = None, width: int | None = None) -> str:
        if session_id is None and self.current_session is not None:
            session_id = self.current_session.session_id
        brand = f"[{theme.ACCENT}]LansCoder[/]"
        metadata_values: list[tuple[str | None, str, int | None]] = []
        if self.config.provider_name or self.config.provider_model:
            provider = self.config.provider_name or "provider"
            model = self.config.provider_model or "model"
            metadata_values.append(
                (
                    None,
                    _provider_model_markup(provider, model, glow_frame=self._provider_glow_frame),
                    18,
                )
            )
        mode = getattr(self.current_session, "mode", None) if self.current_session is not None else None
        if mode:
            mode_text = str(mode)
            mode_color = PERMISSION_MODE_COLORS.get(mode_text, "#6e6d72")
            metadata_values.append((mode_color, mode_text, None))
        if self.config.project_name:
            metadata_values.append(("#6e6d72", f"cwd {self.config.project_name}", 22))
        top_separator = "   [#303238]·[/]   "
        if not metadata_values:
            return brand
        metadata = _metadata_markup(metadata_values, separator=top_separator)
        if width is None:
            return f"{brand}{top_separator}{metadata}"
        brand_width = _markup_width(brand)
        metadata_width = _markup_width(metadata)
        separator_width = _markup_width(top_separator)
        if brand_width + separator_width + metadata_width > width:
            available = max(0, width - brand_width - separator_width)
            metadata = _truncate_markup(metadata, available)
            return f"{brand}{top_separator}{metadata}"
        left_gap = max(3, width - brand_width - metadata_width - 3)
        right_gap = max(3, width - brand_width - metadata_width - left_gap)
        return f"{brand}{' ' * left_gap}{metadata}{' ' * right_gap}"

    def _install_stream_event_handler(self, token: int | None = None):
        if self.chat_runner is None or not hasattr(self.chat_runner, "stream_event_handler"):
            return None
        previous_handler = getattr(self.chat_runner, "stream_event_handler", None)
        self._stream_reasoning_started = False
        self._stream_text_started = False
        self._stream_text_needs_newline = False
        self._stream_text_buffer = ""
        self._stream_text_widget = None
        self._stream_rendered_text = ""
        self._stream_flush_timer = None
        self._stream_markdown_update = None
        self._stream_header_appended = False
        with self._stream_event_lock:
            self._stream_event_generation += 1
            stream_generation = self._stream_event_generation
            self._stream_event_dispatch_scheduled = False
            self._pending_stream_text.clear()
            self._pending_reasoning_text.clear()
        self._reasoning_is_fallback = False
        self._working_text = ""
        self._working_frame_index = 0
        self._stop_working_animation()
        self._stream_segment_closed_for_tool = False

        def handle_event(event) -> None:
            if previous_handler is not None:
                previous_handler(event)
            if token is not None and not self._is_current_chat_turn(token):
                return
            kind = getattr(event, "kind", None)
            text = getattr(event, "text", "") or ""
            if not text:
                return
            if kind == "reasoning_delta":
                self._stream_reasoning_started = True
                self._enqueue_stream_delta(kind, text, stream_generation)
            elif kind == "text_delta":
                self._stream_text_started = True
                self._stream_text_needs_newline = True
                self._enqueue_stream_delta(kind, text, stream_generation)

        setattr(self.chat_runner, "stream_event_handler", handle_event)
        return previous_handler

    def _restore_stream_event_handler(self, previous_handler) -> None:
        self._restore_runner_handler("stream_event_handler", previous_handler)

    def _install_tool_event_handler(self, token: int | None = None):
        if self.chat_runner is None or not hasattr(self.chat_runner, "tool_event_handler"):
            return None
        previous_handler = getattr(self.chat_runner, "tool_event_handler", None)
        self._live_tool_events_seen = False
        epoch = self._ui_epoch

        def handle_event(event) -> None:
            if previous_handler is not None:
                previous_handler(event)
            if token is not None and not self._is_current_chat_turn(token):
                return
            tool_call = getattr(event, "tool_call", None)
            tool_name = str(getattr(tool_call, "name", "") or "tool")
            if tool_name in HIDDEN_TOOL_STATUS_NAMES:
                return
            event_kind = str(getattr(event, "kind", "") or "")
            if event_kind == "prewrite_review":
                review = getattr(event, "prewrite_review", None)
                if isinstance(review, dict):
                    self._call_ui_thread(self._write_review_payload, review)
                return
            if event_kind == "permission_requested":
                return
            self._live_tool_events_seen = True
            self._call_ui_thread(self._close_stream_segment_for_tool)
            self._call_ui_thread(self._record_tool_activity, event, epoch)
            if tool_name in {"task_create", "task_update", "task_revise"} and event_kind == "finished":
                self._call_ui_thread(self._refresh_task_plan_panel_from_current_session)

        setattr(self.chat_runner, "tool_event_handler", handle_event)
        return previous_handler

    def _restore_tool_event_handler(self, previous_handler) -> None:
        self._restore_runner_handler("tool_event_handler", previous_handler)

    def _restore_runner_handler(self, attr: str, previous_handler) -> None:
        if self.chat_runner is not None and hasattr(self.chat_runner, attr):
            setattr(self.chat_runner, attr, previous_handler)

    def _call_ui_thread(self, callback, *args, **kwargs):
        if not getattr(self, "is_running", False):
            return callback(*args, **kwargs)
        if getattr(self, "_thread_id", None) == threading.get_ident():
            return callback(*args, **kwargs)
        return self.call_from_thread(callback, *args, **kwargs)

    def _schedule_ui_callback(self, callback, *args) -> bool:
        if not getattr(self, "is_running", False):
            callback(*args)
            return True
        return self.call_later(callback, *args)

    def _enqueue_stream_delta(self, kind: str, text: str, generation: int) -> None:
        with self._stream_event_lock:
            if generation != self._stream_event_generation:
                return
            pending = self._pending_reasoning_text if kind == "reasoning_delta" else self._pending_stream_text
            pending.append(text)
            if self._stream_event_dispatch_scheduled:
                return
            self._stream_event_dispatch_scheduled = True
        if self._schedule_ui_callback(self._drain_stream_deltas, generation):
            return
        with self._stream_event_lock:
            if generation == self._stream_event_generation:
                self._stream_event_dispatch_scheduled = False

    def _drain_stream_deltas(self, generation: int | None = None) -> None:
        generation = self._stream_event_generation if generation is None else generation
        with self._stream_event_lock:
            if generation != self._stream_event_generation:
                return
            reasoning_text = "".join(self._pending_reasoning_text)
            stream_text = "".join(self._pending_stream_text)
            self._pending_reasoning_text.clear()
            self._pending_stream_text.clear()

        if reasoning_text:
            self._append_reasoning_text(reasoning_text)
        if stream_text:
            self._complete_working_indicator()
            self._append_stream_text(stream_text)

        with self._stream_event_lock:
            if generation != self._stream_event_generation:
                return
            has_pending = bool(self._pending_reasoning_text or self._pending_stream_text)
            if not has_pending:
                self._stream_event_dispatch_scheduled = False
        if has_pending and not self._schedule_ui_callback(self._drain_stream_deltas, generation):
            with self._stream_event_lock:
                if generation == self._stream_event_generation:
                    self._stream_event_dispatch_scheduled = False

    def _discard_stream_deltas(self) -> None:
        with self._stream_event_lock:
            self._stream_event_generation += 1
            self._stream_event_dispatch_scheduled = False
            self._pending_stream_text.clear()
            self._pending_reasoning_text.clear()

    def _is_output_pinned_to_bottom(self, output) -> bool:
        if not hasattr(output, "scroll_y"):
            return False
        scroll_y = float(getattr(output, "scroll_y", 0) or 0)
        max_scroll_y = float(getattr(output, "max_scroll_y", 0) or 0)
        if not max_scroll_y:
            return True
        return scroll_y >= max_scroll_y - 1

    def _scroll_output_end(self, output) -> None:
        if hasattr(output, "scroll_end"):
            output.scroll_end(animate=False)

    def _append_block(self, block: TranscriptBlock) -> None:
        rendered = block.text
        output = self.query_one("#output")
        classes = block_classes(block)
        rendered_text = _entry_renderable_block(block, rendered)
        kwargs = {}
        if block.kind == BlockKind.COMMAND:
            # 稳定 id:picker 就地更新命令块时按块定位,不整树重建
            kwargs["id"] = f"command-block-{id(block)}"
        was_pinned = self._is_output_pinned_to_bottom(output)
        output.mount(_plain_static(rendered_text, classes=classes, **kwargs))
        if was_pinned:
            self._scroll_output_end(output)

    def _ui_line(self, kind: BlockKind, text: str) -> None:
        if kind == BlockKind.USER:
            self.projector.start_user(text)
        else:
            self.projector.flat_block(kind, text)
        block = self.transcript.blocks[-1]
        self._append_block(block)

    def render_block_into(self, block: TranscriptBlock, block_index: int) -> None:
        output = self.query_one("#output")
        was_pinned = self._is_output_pinned_to_bottom(output)
        if block.kind == BlockKind.ASSISTANT:
            # 块级顺序 = children 时间序:[thinking 行, 文本段 markdown, tool 行] 交错。
            # 文本段是 TEXT_RUN 子项,live 流与 replay 重建同序,故同一渲染器结果一致。
            for child in block.children:
                if child.kind == ChildKind.TEXT_RUN:
                    output.mount(
                        LansCoderMarkdown(
                            f"LansCoder:\n\n{child.body}",
                            classes="message assistant-message",
                        )
                    )
                else:
                    self._mount_child_row(output, block_index, child)
            if was_pinned:
                self._scroll_output_end(output)
            return
        kwargs = {}
        if block.kind == BlockKind.COMMAND:
            kwargs["id"] = f"command-block-{id(block)}"
        output.mount(_plain_static(block.text, classes=block_classes(block), **kwargs))
        if was_pinned:
            self._scroll_output_end(output)

    def _mounted_child_row(self, output, block_index: int, key: str) -> ChildRow | None:
        """当前容器内的子行(直接扫 children,不走 Textual 查询缓存)。

        remove-then-remount 同一帧内,query_one 的 id 缓存会返回已移除的僵尸行,
        用它做去重会让新行永远挂不上去;扫容器 children 反映实时的树。
        """
        row_id = f"child-{block_index}-{key}"
        target = next((w for w in getattr(output, "children", ()) if getattr(w, "id", None) == row_id), None)
        return target if isinstance(target, ChildRow) else None

    def _ensure_stream_block_rows(self, block_index: int, block: TranscriptBlock, *, scroll: bool = True) -> None:
        """增量挂载 live assistant 块:只处理上次挂载点之后的 children。

        行与文本段都按 children 时间序追加,thinking 行位于其文本段之前、
        工具行在其后,时间序即 DOM 序。每个 TEXT_RUN 一个流式 markdown
        widget;walked 到的最后一个 TEXT_RUN 是当前开启的文本段,其 widget
        即流式写入目标 ``_stream_text_widget``。
        """
        if block.kind != BlockKind.ASSISTANT:
            return
        output = self._query_mounted("#output")
        if output is None or not hasattr(output, "mount"):
            return
        mounted = self._stream_mounted_child_counts.get(block_index, 0)
        children = block.children[mounted:]
        if not children:
            return
        # 批量挂载新子项会增高输出区,Textual 不会自动贴底跟随,统一补一次滚动;
        # _append_stream_text 会经 flush 兜底滚动,故传入 scroll=False 避免双滚。
        was_pinned = False
        if scroll:
            was_pinned = self._is_output_pinned_to_bottom(output)
        for child in children:
            if child.kind == ChildKind.TEXT_RUN:
                # 构造时不带正文:内容由后续 flush 写入,避免 _on_mount 重复渲染
                widget = LansCoderMarkdown(
                    classes="message assistant-message streaming",
                    selectable=False,
                )
                output.mount(widget)
                self._stream_text_widget = widget
            else:
                self._mount_child_row(output, block_index, child, scroll=False)
            mounted += 1
        self._stream_mounted_child_counts[block_index] = mounted
        if was_pinned:
            self._scroll_output_end(output)

    def _mount_child_row(self, output, block_index: int, child: ChildItem, *, before: object | None = None, scroll: bool = True) -> None:
        existing = self._mounted_child_row(output, block_index, child.key)
        if existing is not None:
            # 防御性去重:迟到/重复的挂载请求刷新既有的行而不是再插一次
            self.refresh_block_row(existing)
            return
        if child.expanded:
            content = child_expanded_text(child)
        else:
            content = child_collapsed_text(child)
        width = getattr(getattr(output, "size", None), "width", None)
        if isinstance(width, int) and width > 0 and not child.expanded:
            content = truncate_activity_text(content, max(1, width - 6))
        row = ChildRow(
            content,
            block_index=block_index,
            child_key=child.key,
            id=f"child-{block_index}-{child.key}",
            classes=child_row_classes(child),
        )
        if child.expanded:
            row.add_class("expanded")
        # THINKING/TOOL 行都按 children 时间序追加;thinking 行可被物化插入到
        # 末尾正文之前(before 指向其下方 TEXT_RUN 的 markdown widget)。
        # 挂载新行会增高输出区,Textual 不会自动贴底跟随,须显式滚动;
        # 批量挂载(scroll=False)由外层 _ensure_stream_block_rows 统一滚一次。
        was_pinned = False
        if scroll:
            was_pinned = self._is_output_pinned_to_bottom(output)
        if before is not None:
            output.mount(row, before=before)
        else:
            output.mount(row)
        if was_pinned:
            self._scroll_output_end(output)

    def _refresh_child_row(self, block_index: int, child: ChildItem) -> None:
        if not getattr(self, "is_running", False):
            return
        output = self._query_mounted("#output")
        if output is None:
            return
        row = self._mounted_child_row(output, block_index, child.key)
        if row is not None:
            self.refresh_block_row(row)
            return
        block = self.transcript.blocks[block_index] if 0 <= block_index < len(self.transcript.blocks) else None
        if block is not None and block.kind == BlockKind.ASSISTANT:
            self._ensure_stream_block_rows(block_index, block)
            row = self._mounted_child_row(output, block_index, child.key)
            if row is not None:
                self.refresh_block_row(row)
                return
        self._mount_child_row(output, block_index, child)

    def _refresh_thinking_row(self, block_index: int) -> None:
        """结算后刷新当前块最近的 THINKING 子行,使折叠文本切到 Thought for。"""
        block = self.transcript.blocks[block_index] if 0 <= block_index < len(self.transcript.blocks) else None
        if block is None:
            return
        for child in reversed(block.children):
            if child.kind == ChildKind.THINKING:
                self._refresh_child_row(block_index, child)
                return

    def _toggle_child_expanded(self, block_index: int, key: str) -> None:
        try:
            block = self.transcript.blocks[block_index]
        except IndexError:
            return
        child = next((c for c in block.children if c.key == key), None)
        if child is None:
            return
        child.expanded = not child.expanded
        output = self._query_mounted("#output")
        if output is None:
            return
        row = self._mounted_child_row(output, block_index, key)
        if row is not None:
            self.refresh_block_row(row)

    def refresh_block_row(self, row: ChildRow) -> None:
        try:
            block = self.transcript.blocks[row.block_index]
        except IndexError:
            return
        child = next((c for c in block.children if c.key == row.child_key), None)
        if child is None:
            row.update("")
            row.remove_class("expanded")
            return
        content = child_expanded_text(child) if child.expanded else child_collapsed_text(child)
        row.update(content)
        if child.expanded:
            row.add_class("expanded")
        else:
            row.remove_class("expanded")

    def _show_welcome(self) -> None:
        output = self.query_one("#output")
        if not hasattr(output, "mount"):
            return
        if hasattr(output, "add_class"):
            output.add_class("welcome-active")
        self._welcome_widget = _plain_static(
            welcome_renderable(compact=self._uses_compact_welcome()),
            id="welcome",
            classes="welcome",
        )
        output.mount(self._welcome_widget)
        if not self._uses_compact_welcome():
            self._start_welcome_particles()

    def _dismiss_welcome(self) -> None:
        self._stop_welcome_particles()
        output = self._query_mounted("#output")
        if output is not None and hasattr(output, "remove_class"):
            output.remove_class("welcome-active")
        widget = self._welcome_widget
        self._welcome_widget = None
        if widget is None:
            return
        remove = getattr(widget, "remove", None)
        if remove is not None:
            remove()

    def _start_interval_timer(self, attr: str, interval: float, callback, *, name: str) -> None:
        if getattr(self, attr) is not None or getattr(self, "_loop", None) is None:
            return
        setattr(self, attr, self.set_interval(interval, callback, name=name))

    def _stop_interval_timer(self, attr: str) -> None:
        timer = getattr(self, attr, None)
        if timer is None:
            return
        timer.stop()
        setattr(self, attr, None)

    def _start_welcome_particles(self) -> None:
        self._start_interval_timer(
            "_welcome_particle_timer",
            self.WELCOME_PARTICLE_INTERVAL_SECONDS,
            self._advance_welcome_particles,
            name="welcome-particles",
        )

    def _stop_welcome_particles(self) -> None:
        self._stop_interval_timer("_welcome_particle_timer")

    def _advance_welcome_particles(self) -> None:
        if self._welcome_widget is None:
            self._stop_welcome_particles()
            return
        if self._uses_compact_welcome():
            self._stop_welcome_particles()
            return
        self._welcome_particle_frame += 1
        self._welcome_widget.update(welcome_renderable(particle_frame=self._welcome_particle_frame))

    def _uses_compact_welcome(self) -> bool:
        size = getattr(self, "size", None)
        width = getattr(size, "width", None)
        height = getattr(size, "height", None)
        return bool(isinstance(width, int) and isinstance(height, int) and (width <= self.COMPACT_WELCOME_MAX_WIDTH or height <= self.COMPACT_WELCOME_MAX_HEIGHT))

    def _refresh_welcome_layout(self) -> None:
        widget = self._welcome_widget
        if widget is None:
            return
        compact = self._uses_compact_welcome()
        widget.update(welcome_renderable(compact=compact, particle_frame=self._welcome_particle_frame))
        if compact:
            self._stop_welcome_particles()
        else:
            self._start_welcome_particles()

    def _sync_provider_glow(self) -> None:
        if model_topbar_themes.should_animate(self.config.provider_model):
            self._start_provider_glow()
        else:
            self._stop_provider_glow()

    def _start_provider_glow(self) -> None:
        self._start_interval_timer(
            "_provider_glow_timer",
            self.PROVIDER_GLOW_INTERVAL_SECONDS,
            self._advance_provider_glow,
            name="model-provider-glow",
        )

    def _stop_provider_glow(self) -> None:
        self._stop_interval_timer("_provider_glow_timer")

    def _advance_provider_glow(self) -> None:
        palette = model_topbar_themes.model_glow_palette(self.config.provider_model)
        if palette is None:
            self._stop_provider_glow()
            return
        self._provider_glow_frame = (self._provider_glow_frame + 1) % len(palette)
        self._refresh_topbar()

    def _record_tool_activity(self, event, epoch: int | None = None) -> None:
        if epoch is not None and epoch != self._ui_epoch:
            # 清理/重放后到账的旧回合工具事件:丢弃,避免污染新 transcript
            return
        tool_call = getattr(event, "tool_call", None)
        name = str(getattr(tool_call, "name", "") or "tool")
        status = tool_event_status(event) or "unknown"
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        if status == "running":
            self._turn_tool_count += 1
            if tool_call_id:
                self._running_tool_call_ids.add(tool_call_id)
        elif tool_call_id:
            self._running_tool_call_ids.discard(tool_call_id)
        if status == "running":
            status_kind = "started"
        elif status in {"success", "error"}:
            status_kind = "finished"
        else:
            status_kind = "denied"
        result = getattr(event, "result", None)
        finalized_thinking = self.projector.tool_event(
            tool_call_id,
            name,
            status_kind,
            arguments=getattr(tool_call, "arguments", None),
            ok=bool(getattr(result, "ok", False)) if status_kind == "finished" else None,
            result_body=str(getattr(result, "content", "") or "") if status_kind == "finished" else "",
        )
        if finalized_thinking:
            self._refresh_thinking_row(len(self.transcript.blocks) - 1)
        block_index = len(self.transcript.blocks) - 1
        block = self.transcript.last_block()
        if block is not None and block.kind == BlockKind.ASSISTANT:
            child = next((c for c in block.children if c.kind == ChildKind.TOOL and c.key == tool_call_id), None)
            if child is not None:
                self._refresh_child_row(block_index, child)
        if status == "success":
            self._show_working_indicator(post_tool_reasoning_text(name))
            return
        self._stop_working_animation()
        if status == "running":
            self._show_activity_animation("running", self._running_tools_activity_detail(name))
            return
        self._show_static_activity(tool_activity_line_text(name, status))

    def _refresh_task_plan_panel_from_current_session(self) -> None:
        current_session = self.current_session
        if current_session is None:
            return
        rebuild_view = getattr(current_session, "rebuild_view", None)
        if rebuild_view is None:
            return
        view = rebuild_view()
        self._render_task_plan_panel(view.task_plan)

    def _clear_task_plan_panel_if_mounted(self) -> None:
        panel = self._query_mounted("#task-plan-panel")
        if panel is None:
            return
        panel.update("")
        if hasattr(panel, "add_class"):
            panel.add_class("hidden")

    def _render_task_plan_panel(self, task_plan: TaskPlan | None) -> None:
        panel = self.query_one("#task-plan-panel")
        if task_plan is None:
            panel.update("")
            if hasattr(panel, "add_class"):
                panel.add_class("hidden")
            self.task_plan_panel_state.last_rendered_revision = None
            return
        if self.task_plan_panel_state.last_rendered_revision == task_plan.revision:
            return
        if hasattr(panel, "remove_class"):
            panel.remove_class("hidden")
        panel.update(task_plan_panel_text(project_plan(task_plan)))
        self.task_plan_panel_state.last_rendered_revision = task_plan.revision

    def _write_markdown_message(self, content: str, *, classes: str = "message assistant-message") -> None:
        self.projector.start_assistant()
        self.projector.append_assistant_text(content)
        block = self.transcript.blocks[-1]
        text = entry_markdown_text_block(block)
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            block_index = len(self.transcript.blocks) - 1
            was_pinned = self._is_output_pinned_to_bottom(output)
            block_markdown = self._stream_text_widget
            if block_markdown is None:
                self._ensure_stream_block_rows(block_index, block)
                block_markdown = self._stream_text_widget
            if block_markdown is not None:
                # 挂载环境：正文复用 block 的 markdown widget(children 已在其后按
                # 模型顺序挂载),去掉 streaming 占位外观并改为可选文本。
                # Textual 延迟挂载,_on_mount 会用 _initial_markdown 或空串重绘;先
                # 写入 _initial_markdown 可防止未挂载前的内容被空串覆盖。
                block_markdown._initial_markdown = text
                block_markdown.remove_class("streaming")
                _observe_markdown_update(block_markdown.update(text))
                block_markdown.set_selectable(True)
            else:
                # 非挂载环境(单元测试直接驱动):退化为独立挂载。
                markdown = LansCoderMarkdown(text, classes=classes)
                output.mount(markdown)
                _observe_markdown_update(markdown.update(text))
            if was_pinned:
                self._scroll_output_end(output)
            return
        if hasattr(output, "write_line"):
            output.write_line(text)

    def _write_pending_input(self) -> None:
        pending = getattr(self.chat_runner, "last_pending_input", None)
        if pending is None:
            return
        self._stop_working_animation()
        self._stop_activity_animation()
        if getattr(pending, "kind", None) == "permission_confirmation":
            payload = getattr(pending, "payload", {}) or {}
            review_payload = payload.get("prewrite_review")
            if isinstance(review_payload, dict):
                self._review_expanded_paths.clear()
                self._write_review_payload(review_payload)
            self._write_output_block(
                permission_prompt_text(pending),
                classes="message permission-message permission-requested",
            )
            self._set_activity("waiting · permission")
            return
        self._write_output_block(ask_user_prompt_text(pending), classes="message permission-message permission-requested")
        self._set_activity("waiting · input")

    def _write_permission_hint(self, pending) -> None:
        """无效权限输入:在输出区补一行提示,不重渲染提示本身。"""
        self._write_output_block(permission_options_text(pending), classes="message permission-message")

    def _write_output_block(self, text: str, *, classes: str = "message") -> None:
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            was_pinned = self._is_output_pinned_to_bottom(output)
            output.mount(_plain_static(text, classes=classes))
            if was_pinned:
                self._scroll_output_end(output)
            return
        if hasattr(output, "write_line"):
            output.write_line(text)

    def _write_review_payload(self, payload: dict[str, object]) -> None:
        rendered = render_prewrite_review(payload, expanded_paths=self._review_expanded_paths)
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            was_pinned = self._is_output_pinned_to_bottom(output)
            output.mount(_plain_static(rendered, classes="message permission-message review-message"))
            if was_pinned:
                self._scroll_output_end(output)
            return
        if hasattr(output, "write_line"):
            output.write_line(rendered.plain)

    def _show_working_indicator(self, text: str) -> None:
        self._stop_activity_animation()
        self._reasoning_is_fallback = True
        self._working_text = text
        self._working_frame_index = 0
        self._set_activity(self._working_indicator_body())
        self._start_working_animation()

    def _complete_working_indicator(self) -> None:
        if self._activity_animation_kind == "streaming" and self._activity_animation_detail == "response":
            return
        self._stop_working_animation()
        self._show_activity_animation("streaming", "response")

    def _append_reasoning_text(self, text: str) -> None:
        if self._reasoning_is_fallback:
            self._reasoning_is_fallback = False
            self._working_text = ""
        block = self.transcript.last_block()
        hoist_above_text = block is not None and block.kind == BlockKind.ASSISTANT and block.children and block.children[-1].kind == ChildKind.TEXT_RUN
        self.projector.append_thinking(text)
        block_index = len(self.transcript.blocks) - 1
        block = self.transcript.last_block()
        if hoist_above_text and block is not None and block.children:
            # 后到的 reasoning 落在正文之下:text_run 只会由 append_assistant_text
            # 追加在其它子项之后,故新 THINKING 必然在块末,直接移回正文段之前。
            if block.children[-1].kind == ChildKind.THINKING:
                child = block.children.pop()
                insert_at = len(block.children)
                while insert_at > 0 and block.children[insert_at - 1].kind == ChildKind.TEXT_RUN:
                    insert_at -= 1
                if insert_at > 0 and block.children[insert_at - 1].kind == ChildKind.THINKING:
                    # 与本行之上已有的 thinking 合并(分片 reasoning 应合成一行)
                    block.children[insert_at - 1].body += child.body
                    self._refresh_child_row(block_index, block.children[insert_at - 1])
                else:
                    block.children.insert(insert_at, child)
                    output = self._query_mounted("#output")
                    if output is not None:
                        before = self._stream_text_widget if self._stream_text_widget is not None else _last_assistant_markdown_widget(output)
                        self._mount_child_row(output, block_index, child, before=before)
                        # 手动挂载取代计数器推进,避免 ensure 重挂已挂载的正文段
                        self._stream_mounted_child_counts[block_index] = self._stream_mounted_child_counts.get(block_index, 0) + 1
                    self._refresh_child_row(block_index, child)
            else:
                # 合并进既有 THINKING(append_thinking 已并入):保持原位置
                self._refresh_child_row(block_index, block.children[-1])
        else:
            if self._stream_text_widget is not None and block is not None and block.kind == BlockKind.ASSISTANT and block.children:
                self._ensure_stream_block_rows(block_index, block)
                self._refresh_child_row(block_index, block.children[-1])
        # 活动区只显示 thinking 状态,不展示推理正文(正文在折叠子行里看)
        self._set_activity(self._working_indicator_body())
        self._start_working_animation()

    def _working_head(self) -> str:
        frame = self.WORKING_FRAMES[self._working_frame_index % len(self.WORKING_FRAMES)]
        return f"thinking {frame}"

    def _working_indicator_body(self, text: str | None = None) -> str:
        return f"{self._working_head()} {text if text is not None else self._working_text}"

    def _start_working_animation(self) -> None:
        self._start_interval_timer(
            "_working_timer",
            self.WORKING_ANIMATION_INTERVAL_SECONDS,
            self._advance_working_animation,
            name="working-indicator",
        )

    def _stop_working_animation(self) -> None:
        self._stop_interval_timer("_working_timer")

    def _advance_working_animation(self) -> None:
        self._working_frame_index += 1
        self._set_activity(self._working_indicator_body())

    def _show_static_activity(self, text: str) -> None:
        self._show_activity_animation("static", text)

    def _activity_animation_body(self) -> str:
        if self._activity_animation_kind == "static":
            return self._activity_animation_detail
        frames = self.ACTIVITY_FRAMES.get(self._activity_animation_kind) or ("[....]",)
        frame = frames[self._activity_frame_index % len(frames)]
        return f"{self._activity_animation_kind} {frame} · {self._activity_animation_detail}"

    def _preserve_turn_metrics(self) -> None:
        if not self._turn_started_at:
            self._start_turn_metrics()

    def _running_tools_activity_detail(self, fallback_name: str) -> str:
        running_count = len(self._running_tool_call_ids)
        if running_count > 1:
            return f"{running_count} tools running"
        return fallback_name

    def _start_activity_animation(self) -> None:
        self._start_interval_timer(
            "_activity_timer",
            self.ACTIVITY_ANIMATION_INTERVAL_SECONDS,
            self._advance_activity_animation,
            name="activity-indicator",
        )

    def _stop_activity_animation(self) -> None:
        self._stop_interval_timer("_activity_timer")
        self._activity_animation_kind = ""
        self._activity_animation_detail = ""

    def _schedule_activity_idle_revert(self) -> None:
        """回合结束后保留一次时长指标显示,DELAY 秒后回 idle · ready。

        新回合开始会取消该计时器(_start_turn_metrics),避免旧回合的
        恢复动作在流式中途把活动区打回 idle。
        """
        timer = self._activity_idle_revert_timer
        if timer is not None:
            timer.stop()
        self._activity_idle_revert_timer = None
        if not getattr(self, "is_running", False):
            return
        self._activity_idle_revert_timer = self.set_timer(
            self.ACTIVITY_IDLE_REVERT_SECONDS,
            self._revert_activity_to_idle,
            name="activity-idle-revert",
        )

    def _cancel_activity_idle_revert(self) -> None:
        timer = self._activity_idle_revert_timer
        self._activity_idle_revert_timer = None
        if timer is not None:
            timer.stop()

    def _revert_activity_to_idle(self) -> None:
        self._activity_idle_revert_timer = None
        self._set_activity("idle · ready")

    def _advance_activity_animation(self) -> None:
        if not self._activity_animation_kind:
            return
        self._activity_frame_index += 1
        self._set_activity(self._activity_animation_body())

    def _query_mounted(self, selector: str):
        if not getattr(self, "is_mounted", False):
            return None
        try:
            return self.query_one(selector)
        except NoMatches:
            return None

    def _set_activity(self, text: str) -> None:
        self._activity_text = text
        activity = self._query_mounted("#activity")
        if activity is None:
            return
        rendered = self.tool_activity_line_text(text, activity)
        if hasattr(activity, "update"):
            activity.update(self._activity_renderable(rendered))

    def _refresh_topbar(self) -> None:
        topbar = self._query_mounted("#topbar")
        if topbar is not None and hasattr(topbar, "update"):
            topbar.update(self._topbar_text(width=self._topbar_width()))

    def _activity_renderable(self, text: str) -> Text:
        return Text(text, style=theme.ACCENT_DARK)

    def tool_activity_line_text(self, text: str, activity) -> str:
        text = single_line_activity(text)
        metrics = turn_metrics_text(self._turn_elapsed_seconds(), self._turn_tool_count)
        width = getattr(getattr(activity, "size", None), "width", None)
        if not isinstance(width, int) or width <= 0:
            return f"{text} · {metrics}" if text != "idle · ready" else text
        if text == "idle · ready":
            return text
        if len(text) + len(metrics) + 1 > width:
            available = max(1, width - len(metrics) - 1)
            text = truncate_activity_text(text, available)
        return f"{text}{' ' * (width - len(text) - len(metrics))}{metrics}"

    def _append_stream_text(self, text: str) -> None:
        if self._stream_segment_closed_for_tool:
            self._start_new_stream_segment()
        self.projector.start_assistant()
        finalized_thinking = self.projector.append_assistant_text(text)
        if finalized_thinking:
            self._refresh_thinking_row(len(self.transcript.blocks) - 1)
        self._stream_text_buffer += text
        output = self.query_one("#output")
        if hasattr(output, "mount"):
            if self._stream_text_widget is None:
                block_index = len(self.transcript.blocks) - 1
                block = self.transcript.last_block()
                if block is not None:
                    # 挂载后紧跟 flush 兜底滚动,无需在此双滚
                    self._ensure_stream_block_rows(block_index, block, scroll=False)
            if not self._stream_rendered_text:
                self._flush_stream_text()
            else:
                self._schedule_stream_flush()
            return
        if hasattr(output, "write"):
            prefix = "LansCoder:\n" if self._stream_text_buffer == text else ""
            output.write(f"{prefix}{text}")

    def _close_stream_segment_for_tool(self) -> None:
        self._drain_stream_deltas()
        if self._stream_text_widget is None and not self._stream_text_buffer:
            return
        if not self._stream_text_buffer and not self._stream_rendered_text:
            # 占位 markdown 尚无文本流入(为排序先行挂载),保持当前 segment,
            # 让后续 append_stream_text 继续填充而不是拆成空段。
            return
        self._finalize_stream_widget()
        self._stream_segment_closed_for_tool = True

    def _finalize_stream_widget(self) -> None:
        widget = self._stream_text_widget
        if widget is None:
            return
        # 防重入标记挂在 widget 实例上,集合不再长期持有已 finalize 的 widget
        if getattr(widget, "_stream_finalized", False):
            return
        widget._stream_finalized = True
        timer = self._stream_flush_timer
        if timer is not None:
            timer.stop()
        self._stream_flush_timer = None
        final_markdown = f"LansCoder:\n\n{self._stream_text_buffer}"
        pending_update = self._stream_markdown_update
        self._stream_markdown_update = None
        self._stream_rendered_text = self._stream_text_buffer
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            output = self.query_one("#output")
            was_pinned = self._is_output_pinned_to_bottom(output)
            update_result = widget.update(final_markdown)
            _observe_markdown_update(update_result)
            widget.set_selectable(True)
            if was_pinned:
                self._scroll_output_end(output)
            return
        completion = loop.create_future()
        self._stream_finalizations[widget] = completion

        async def finalize() -> None:
            try:
                if pending_update is not None:
                    await pending_update
                output = self.query_one("#output")
                was_pinned = self._is_output_pinned_to_bottom(output)
                await widget.update(final_markdown)
                widget.set_selectable(True)
                if was_pinned:
                    self._scroll_output_end(output)
            except BaseException as error:
                if not completion.done():
                    completion.set_exception(error)
            else:
                if not completion.done():
                    completion.set_result(None)
            # 完成后释放映射中的 widget/future 引用;wait_for_stream_finalization
            # 此时可能已持有 completion 对象,仍可正常 await
            if self._stream_finalizations.get(widget) is completion:
                del self._stream_finalizations[widget]

        self.run_worker(
            finalize(),
            exclusive=False,
            group="stream-finalization",
            exit_on_error=False,
        )

    async def wait_for_stream_finalization(self, widget: LansCoderMarkdown) -> None:
        completion = self._stream_finalizations.get(widget)
        if completion is not None:
            await completion

    def _start_new_stream_segment(self) -> None:
        self._stream_text_buffer = ""
        self._stream_text_widget = None
        self._stream_rendered_text = ""
        self._stream_flush_timer = None
        self._stream_markdown_update = None
        self._stream_header_appended = False
        self._stream_segment_closed_for_tool = False

    def _schedule_stream_flush(self) -> None:
        if self._stream_flush_timer is not None:
            return
        if getattr(self, "_loop", None) is None:
            return
        self._stream_flush_timer = self.set_timer(
            self.STREAM_RENDER_INTERVAL_SECONDS,
            self._flush_stream_text,
            name="stream-markdown-flush",
        )

    def _flush_stream_text(self) -> bool:
        self._stream_flush_timer = None
        if self._stream_text_widget is None:
            return False
        buffer = self._stream_text_buffer
        if self._stream_markdown_update is not None:
            return False
        delta_content = buffer[len(self._stream_rendered_text) :]
        if not delta_content:
            return False
        if self._stream_header_appended:
            delta = delta_content
        else:
            # 首个 flush 交付 header+首段内容;之后只 append 尾部增量。
            # 旧实现每次整篇 update() 重解析全部历史并 remove/remount 所有块,
            # 是 O(n^2) 且制造"已 detach 仍残留命中地图"的替换窗口(见 screen
            # 层的 parent-None 兜底);append 只解析最后一块之后的新行。
            delta = f"LansCoder:\n\n{delta_content}"
            self._stream_header_appended = True
        self._stream_rendered_text = buffer
        output = self.query_one("#output")
        was_pinned = self._is_output_pinned_to_bottom(output)
        update_result = self._stream_text_widget.append(delta)
        self._track_stream_markdown_update(update_result)
        _observe_markdown_update(update_result)
        if was_pinned:
            self._scroll_output_end(output)
        return True

    def _track_stream_markdown_update(self, update_result) -> None:
        future = getattr(update_result, "_future", None)
        if future is None or not hasattr(future, "add_done_callback"):
            return
        self._stream_markdown_update = update_result

        def finish_latest_update(_future) -> None:
            self._schedule_ui_callback(self._finish_stream_markdown_update, update_result)

        future.add_done_callback(finish_latest_update)

    def _finish_stream_markdown_update(self, update_result) -> None:
        if self._stream_markdown_update is not update_result:
            return
        self._stream_markdown_update = None
        if self._stream_rendered_text != self._stream_text_buffer:
            self._schedule_stream_flush()
