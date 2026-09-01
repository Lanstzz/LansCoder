import asyncio

import anyio

import threading
from unittest.mock import Mock

import pytest
from markdown_it import MarkdownIt
from rich.text import Text
from textual import events
from textual.color import Color
from textual.widgets import Markdown
from textual.widgets import TextArea

from lanscoder.agent.background import BackgroundJob
from lanscoder.agent.background import BackgroundJobManager
from lanscoder.agent.loop import ToolExecutionEvent
from lanscoder.app.commands import CommandResult
from lanscoder.app.commands import ContextCommandHandler
from lanscoder.permissions.user_input import UserInputOption, UserInputRequest
from lanscoder.app.router import CompositeCommandHandler
from lanscoder.core.runtime import CurrentSessionState
from lanscoder.app.session_commands import SessionCommandHandler
from lanscoder.app.slash_suggest import SlashSuggest
from lanscoder.app.tui import (
    ComposerTextArea,
    LansCoderApp,
    LansCoderScreen,
    LansCoderTuiConfig,
)
from lanscoder.app.tui import LansCoderMarkdown
from lanscoder.app.tui_view import _entry_renderable_block
from lanscoder.app.tui import _provider_name_markup
from lanscoder.app.tui import _provider_model_markup
from lanscoder.app.tui import _plain_static
from lanscoder.app.tui import _observe_markdown_update
from lanscoder.app.tui import _format_subagent_line
from lanscoder.app.tui import _progress_indicator
from lanscoder.app.picker import TuiPickerItem, TuiPickerState, render_picker
from lanscoder.app.picker_adapters import render_picker_item
from lanscoder.app import theme
from lanscoder.app.activity_view import (
    task_plan_panel_text,
    turn_metrics_text,
)
from lanscoder.app.welcome import WELCOME_LOGO_PIXELS, welcome_renderable
from lanscoder.app.tui_state import (
    BlockKind,
    ChildItem,
    ChildKind,
    TuiTaskPlanPanelState,
    TranscriptBlock,
    TranscriptModel,
)
from lanscoder.app.transcript_view import background_notification_ui_text
from lanscoder.app.tui_widgets import ChildRow
from lanscoder.context.models import SessionView
from lanscoder.context.runtime_state import SessionRuntimeState
from lanscoder.context.store import JsonlSessionStore
from lanscoder.context.token_budget import build_context_budget
from lanscoder.context.writer import SessionEventWriter
from lanscoder.agent.session import AgentSession
from lanscoder.input.attachments import UserAttachment
from lanscoder.session.catalog import SessionCatalog
from lanscoder.session.new import NewSessionService
from lanscoder.session.resume import ResumeService
from lanscoder.providers.types import (
    ChatResponse,
    ChatStreamEvent,
    TokenUsage,
    ToolCall,
)
from lanscoder.tools.types import ToolResult
from lanscoder.tools.types import make_text_result
from lanscoder.planning.models import Task, TaskPlan


class FakeOutput:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.mounted: list[object] = []
        self.scroll_end_calls = 0
        self.scroll_y = 0
        self.max_scroll_y = 0

    def write(self, line: str) -> None:
        if self.lines:
            self.lines[-1] += line
        else:
            self.lines.append(line)

    def write_line(self, line: str) -> None:
        self.lines.append(line)

    def mount(self, widget: object) -> None:
        self.mounted.append(widget)
        if type(widget).__name__ == "Static":
            self.lines.append(str(getattr(widget, "content", getattr(widget, "renderable", ""))))
        if isinstance(widget, Markdown):
            widget.updates = []  # type: ignore[attr-defined]

            def update(markdown: str) -> None:
                widget.updates.append(markdown)  # type: ignore[attr-defined]

            def append(markdown: str) -> None:
                widget.updates.append(markdown)  # type: ignore[attr-defined]

            widget.update = update  # type: ignore[method-assign]
            widget.append = append  # type: ignore[method-assign]

    def scroll_end(self, animate: bool = False) -> None:
        self.scroll_end_calls += 1
        return None


def _static_output_text(app: LansCoderApp) -> str:
    static_text = "\n".join(str(getattr(widget, "content", getattr(widget, "renderable", ""))) for widget in app.query_one("#output").query("Static"))
    markdown_text = "\n".join(str(getattr(widget, "source", "") or "\n".join(getattr(widget, "updates", []) or [])) for widget in app.query_one("#output").query("LansCoderMarkdown"))
    return "\n".join(part for part in [static_text, markdown_text] if part)


def _markdown_widget_text(widget) -> str:
    source = str(getattr(widget, "source", "") or "")
    if source:
        return source
    return "\n".join(getattr(widget, "updates", []) or [])


def _child_row_text(widget) -> str:
    content = str(getattr(widget, "content", "") or "")
    if content:
        return content
    return str(getattr(widget, "renderable", ""))


class FakeMarkdownUpdateResult:
    def __init__(self, exception: BaseException | None) -> None:
        self._exception = exception
        self.exception_observed = False
        self.callbacks = []
        self._future = self

    def add_done_callback(self, callback) -> None:
        self.callbacks.append(callback)

    def exception(self) -> BaseException | None:
        self.exception_observed = True
        return self._exception

    def finish(self) -> None:
        for callback in self.callbacks:
            callback(self)


class FakeActivity:
    def __init__(self) -> None:
        self.updates: list[str] = []
        self.renderables: list[object] = []
        self.size = type("Size", (), {"width": 60})()

    def update(self, text: object) -> None:
        self.renderables.append(text)
        plain = getattr(text, "plain", None)
        self.updates.append(str(plain if plain is not None else text))


class FakeTopbar(FakeActivity):
    pass


class FakeTaskPlanPanel(FakeActivity):
    def __init__(self) -> None:
        super().__init__()
        self.classes: set[str] = set()

    def add_class(self, name: str) -> None:
        self.classes.add(name)

    def remove_class(self, name: str) -> None:
        self.classes.discard(name)


def test_progress_indicator_cycles_bar_frames() -> None:
    frames = [_progress_indicator(i) for i in range(9)]
    assert frames[:8] == ["[    ]", "[>   ]", "[->  ]", "[--> ]", "[--->]", "[ ---]", "[  --]", "[   -]"]
    assert frames[8] == "[    ]"  # 回到第一个


def test_format_subagent_line_uses_indicator_and_k_abbrev() -> None:
    line = _format_subagent_line(label="researcher", elapsed=12.0, calls=3, tokens=2500, indicator="[--->]")
    assert line == "[--->] researcher · 12s · 3 calls · 2.5k tokens"
    line_small = _format_subagent_line(label="reviewer", elapsed=0.4, calls=1, tokens=850, indicator="[>   ]")
    assert line_small == "[>   ] reviewer · 0s · 1 calls · 850 tokens"


def test_stage_paste_attachments_adds_clipboard_attachment(monkeypatch, tmp_path) -> None:
    image = tmp_path / "clipboard.png"
    image.write_bytes(b"image")
    attachment = UserAttachment(
        kind="image",
        path=image,
        filename="clipboard.png",
        media_type="image/png",
        size_bytes=image.stat().st_size,
        source="clipboard",
    )
    app = LansCoderApp()
    messages: list[str] = []
    monkeypatch.setattr("lanscoder.app.tui.resolve_paste_attachments", lambda text: [attachment])
    monkeypatch.setattr(app, "_ui_line", lambda kind, text: messages.append(text))

    assert app._stage_paste_attachments(None) is True
    assert app._staged_attachments == [attachment]
    assert messages == ["Attached: 🖼 clipboard.png (5B)"]


def test_stage_paste_attachments_does_not_add_duplicates(monkeypatch, tmp_path) -> None:
    image = tmp_path / "clipboard.png"
    image.write_bytes(b"image")
    attachment = UserAttachment("image", image, "clipboard.png", "image/png", 5, "clipboard")
    app = LansCoderApp()
    app._staged_attachments.append(attachment)
    monkeypatch.setattr("lanscoder.app.tui.resolve_paste_attachments", lambda text: [attachment])

    assert app._stage_paste_attachments(None) is True
    assert app._staged_attachments == [attachment]


def test_stage_paste_attachments_reports_attachment_errors(monkeypatch) -> None:
    app = LansCoderApp()
    messages: list[tuple[BlockKind, str]] = []
    monkeypatch.setattr(
        "lanscoder.app.tui.resolve_paste_attachments",
        lambda text: (_ for _ in ()).throw(ValueError("Image exceeds 20MB limit: clipboard.png")),
    )
    monkeypatch.setattr(
        app,
        "_ui_line",
        lambda kind, text: messages.append((kind, text)),
    )

    assert app._stage_paste_attachments(None) is True
    assert messages == [
        (
            BlockKind.ERROR,
            "Could not attach pasted image: Image exceeds 20MB limit: clipboard.png",
        )
    ]


def test_skill_picker_item_renderer_keeps_name_path_and_description_separate() -> None:
    picker = TuiPickerState(kind="skill", title="Select a skill:", items=[])
    item = TuiPickerItem(
        id="skills/very-long.md",
        label="very-long",
        detail=" ".join(["description"] * 30),
        meta={"scope": "global", "path": "skills/very-long.md"},
    )

    rendered = render_picker_item(picker, item, 0)

    assert rendered == "very-long\n    global · skills/very-long.md"


def test_skill_picker_render_keeps_item_heights_stable_and_detail_in_footer() -> None:
    picker = TuiPickerState(
        kind="skill",
        title="Select a skill:",
        items=[
            TuiPickerItem(
                id="skills/brief.md",
                label="brief",
                detail="Write a brief.",
                meta={"scope": "project", "path": "skills/brief.md"},
            ),
            TuiPickerItem(
                id="skills/review.md",
                label="review",
                detail="",
                meta={"scope": "project", "path": "skills/review.md"},
            ),
        ],
        selected_index=0,
    )

    rendered = render_picker(
        picker,
        limit=20,
        render_item=lambda item, index: render_picker_item(picker, item, index),
    )

    assert "Write a brief." not in rendered.splitlines()[1:5]
    assert "Selected: Write a brief." in rendered
    assert "> 1. brief\n    project · skills/brief.md" in rendered
    assert "  2. review\n    project · skills/review.md" in rendered


def test_command_picker_renderable_colors_selected_cursor() -> None:
    block = TranscriptBlock(BlockKind.COMMAND)

    rendered = _entry_renderable_block(block, "Select:\n> 1. first\n  2. second")

    assert isinstance(rendered, Text)
    assert rendered.plain == "Select:\n> 1. first\n  2. second"
    assert any(span.start == len("Select:\n") and span.end == len("Select:\n>") for span in rendered.spans)
    assert any(span.style == "#4f8cff bold" for span in rendered.spans)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_picker_rerender_updates_last_command_block() -> None:
    app = LansCoderApp()
    app.transcript.add_block(BlockKind.COMMAND, "Select:\n> 1. first\n  2. second")
    app._picker = TuiPickerState(
        kind="model",
        title="Select a model:",
        items=[
            TuiPickerItem(id="old", label="old"),
            TuiPickerItem(id="new", label="new"),
        ],
        selected_index=0,
    )

    async with app.run_test() as pilot:
        app._picker.move(1)
        app._render_picker()
        await pilot.pause()

    block = app.transcript.blocks[-1]
    assert block.kind == BlockKind.COMMAND
    assert block.text.startswith("Select a model:")
    assert "> 2. new" in block.text


class RecordingCommandHandler:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def handle(self, text: str) -> CommandResult:
        self.commands.append(text)
        if text in {"/model", "/models"}:
            return CommandResult(
                handled=True,
                output="Select a model:",
                action={
                    "type": "model_picker",
                    "models": [
                        {"provider": "fake", "model": "old"},
                        {"provider": "fake", "model": "new"},
                    ],
                    "selected_index": 0,
                },
            )
        if text == "/model fake/new":
            return CommandResult(
                handled=True,
                output="Model switched: fake/new",
                action={"type": "model_changed", "provider": "fake", "model": "new"},
            )
        if text == "/skills":
            return CommandResult(
                handled=True,
                output="Skills:",
                action={
                    "type": "skill_picker",
                    "skills": [
                        {
                            "name": "brief",
                            "path": "skills/brief.md",
                            "scope": "project",
                            "description": "Write a brief.",
                        },
                        {
                            "name": "review",
                            "path": "skills/review.md",
                            "scope": "project",
                            "description": "Review work.",
                        },
                    ],
                    "selected_index": 0,
                },
            )
        if text == "/skill-use review":
            return CommandResult(
                handled=True,
                output="Referenced skill: review",
                action={
                    "type": "skill_referenced",
                    "name": "review",
                    "path": "skills/review.md",
                    "reference": "请先调用 load_skill(name=review, args=<你的任务>)，再按照返回的指令继续。",
                },
            )
        return CommandResult(handled=False)


class BlockingCompactCommandHandler:
    def __init__(self) -> None:
        self.call_thread_id: int | None = None

    def handle(self, text: str) -> CommandResult:
        assert text == "/compact"
        self.call_thread_id = threading.get_ident()
        return CommandResult(handled=True, output="Manual compact success")


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected"),
    [
        (0, "0.0s · 0 tools"),
        (59.9, "59.9s · 0 tools"),
        (60, "1m 0s · 0 tools"),
        (61, "1m 1s · 0 tools"),
        (3599, "59m 59s · 0 tools"),
        (3600, "1h 0m 0s · 0 tools"),
        (3661, "1h 1m 1s · 0 tools"),
    ],
)
def test_turn_metrics_time_units_appear_only_after_thresholds(elapsed_seconds, expected) -> None:
    assert turn_metrics_text(elapsed_seconds, 0) == expected


class FakeSession:
    session_id = "sess_test"
    mode = "standard"
    runtime_state = SessionRuntimeState(session_id="sess_test")

    def rebuild_view(self) -> SessionView:
        return SessionView(session_id="sess_test")


def _context_budget(view):
    return build_context_budget(messages=[], tools=[], context_window=32_768, max_output_tokens=4_096)


def test_lanscoder_app_can_be_created_with_command_handler() -> None:
    handler = ContextCommandHandler(session=FakeSession(), budget_provider=_context_budget)

    app = LansCoderApp(command_handler=handler, config=LansCoderTuiConfig(title="TestCoder"))

    assert app.command_handler is handler
    assert app.config.title == "TestCoder"


class _FakeForegroundChatRunner:
    def __init__(self) -> None:
        self.background_manager = None
        self.interrupted_turns = 0
        self._foreground = {
            "label": "researcher",
            "started_at": 0.0,
            "provider_calls": 0,
            "total_tokens": 0,
        }

    def foreground_subagent(self) -> dict | None:
        return self._foreground

    def cancel_current_turn(self) -> None:
        self.interrupted_turns += 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_highlights_selected_row_and_shows_hint() -> None:
    app = LansCoderApp(chat_runner=_FakeForegroundChatRunner())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._subagent_select_mode = True
        app._subagent_selected = "fg"
        app._refresh_subagent_progress()
        panel = app.query_one("#subagent-panel")
        selected = [s for s in panel.query("Static") if s.has_class("selected")]
        assert [s.id for s in selected] == ["subagent-row-fg"]
        hint = app.query_one("#subagent-hint")
        assert "x 停止" in str(hint.render())


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_x_stops_foreground_turn() -> None:
    fake = _FakeForegroundChatRunner()
    app = LansCoderApp(chat_runner=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_subagent_progress()
        await pilot.press("down")
        assert app._subagent_select_mode is True
        assert app._subagent_selected == "fg"
        await pilot.press("x")
        assert fake.interrupted_turns == 1
        app._refresh_subagent_progress()
        fg_row = next(s for s in app.query_one("#subagent-panel").query("Static") if s.id == "subagent-row-fg")
        assert "cancelling" in fg_row.content
        await pilot.press("escape")
        assert app._subagent_select_mode is False


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_up_at_top_exits_selection() -> None:
    """Up on the topmost highlighted subagent returns to the input bar — the
    mirror of down entering selection from the input."""
    fake = _FakeForegroundChatRunner()
    app = LansCoderApp(chat_runner=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_subagent_progress()
        await pilot.press("down")
        assert app._subagent_select_mode is True
        assert app._subagent_selected == "fg"
        await pilot.press("up")
        assert app._subagent_select_mode is False
        assert app._subagent_selected is None


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_escape_clears_highlight() -> None:
    """Esc exits selection mode AND removes the row highlight."""
    fake = _FakeForegroundChatRunner()
    app = LansCoderApp(chat_runner=fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._refresh_subagent_progress()
        await pilot.press("down")
        await pilot.press("escape")
        assert app._subagent_select_mode is False
        assert app._subagent_selected is None
        panel = app.query_one("#subagent-panel")
        selected = [s for s in panel.query("Static") if s.has_class("selected")]
        assert selected == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_x_stops_background_job() -> None:
    gate = threading.Event()

    class _FakeManagerChatRunner:
        def __init__(self, manager) -> None:
            self.background_manager = manager

        def foreground_subagent(self) -> dict | None:
            return None

        def cancel_current_turn(self) -> None:
            pass

        async def anudge_turn(self) -> ChatResponse:
            self.background_manager.collect_completed()
            return ChatResponse(provider="fake", model="fake", content="")

    manager = BackgroundJobManager()
    fake = _FakeManagerChatRunner(manager)
    app = LansCoderApp(chat_runner=fake)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            job = manager.start(
                lambda: gate.wait(5) or make_text_result("delegate", "done"),
                tool_name="delegate",
            )
            app._refresh_subagent_progress()
            await pilot.press("down")
            assert app._subagent_selected == job.id
            await pilot.press("x")
            assert job.cancel_requested is True
            gate.set()
            await pilot.pause()
            await pilot.pause()
    finally:
        gate.set()
        manager.wait(timeout=5)
        manager.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_copies_to_macos_clipboard(monkeypatch) -> None:
    app = LansCoderApp()
    done = threading.Event()
    pbcopy = Mock(side_effect=lambda *args, **kwargs: done.set())
    monkeypatch.setattr("lanscoder.app.tui.platform.system", lambda: "Darwin")
    monkeypatch.setattr("lanscoder.app.tui.subprocess.run", pbcopy)

    async with app.run_test() as pilot:
        app.copy_to_clipboard("copied text")
        assert await anyio.to_thread.run_sync(done.wait, 5.0)
        await pilot.pause()

    pbcopy.assert_called_once_with(["pbcopy"], input="copied text", text=True, check=False)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_runs_manual_compact_command_without_blocking_ui() -> None:
    handler = BlockingCompactCommandHandler()
    app = LansCoderApp(command_handler=handler)

    ui_thread_id = threading.get_ident()
    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/compact")
        await pilot.press("enter")
        await pilot.pause()
        assert handler.call_thread_id is not None
        assert handler.call_thread_id != ui_thread_id
        assert "Manual compact success" in _static_output_text(app)

    assert handler.call_thread_id is not None


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_uses_custom_chrome_instead_of_textual_header_footer() -> None:
    app = LansCoderApp(current_session=FakeSession())

    async with app.run_test():
        widget_types = [type(widget).__name__ for widget in app.query("*")]
        widget_ids = [getattr(widget, "id", None) for widget in app.query("*")]

    assert "Header" not in widget_types
    assert "Footer" not in widget_types
    assert "topbar" in widget_ids
    assert "main" in widget_ids


@pytest.mark.parametrize(
    ("mode", "color"),
    [
        ("standard", "#cfd1d6"),
        ("aggressive", "#f6b73c"),
        ("bypass", "#ff6b5f"),
    ],
)
def test_lanscoder_app_topbar_colors_each_permission_mode(mode, color) -> None:
    class ModeSession(FakeSession):
        pass

    session = ModeSession()
    session.mode = mode
    app = LansCoderApp(current_session=session)

    assert app._topbar_text() == (f"[#4f8cff]LansCoder[/]   [#303238]·[/]   [{color}]{mode}[/]")
    assert "sess_test" not in app._topbar_text()


def test_lanscoder_app_topbar_shows_model_glow_and_hides_session_id() -> None:
    app = LansCoderApp(
        current_session=FakeSession(),
        config=LansCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="gpt-5.5",
            project_name="LansCoder",
        ),
    )

    text = app._topbar_text()

    assert Text.from_markup(text).plain == "LansCoder   ·   yurenapi/gpt-5.5   ·   standard   ·   cwd LansCoder"
    assert "[#7eb6ff]r[/]" in text
    assert "sess_test" not in text


@pytest.mark.parametrize(
    ("model", "colour"),
    [
        ("gpt-5.6-terra", "#18cfcb"),
        ("gpt-5.6-sol", "#ff5c3d"),
        ("gpt-5.6-luna", "#b9c8ff"),
        ("gpt-5.5", "#7eb6ff"),
        ("gpt-5.4", "#57c5f0"),
        ("gpt-5.4-mini", "#9be7c8"),
        ("grok-4.5", "#b57bff"),
        ("fable-5", "#f0a05a"),
        ("claude-opus-4-7", "#ff8f6b"),
        ("claude-opus-4-8", "#ff6f61"),
        ("claude-sonnet-5", "#f0b36a"),
        ("claude-sonnet-4-6", "#f0a18c"),
        ("deepseek-v4-pro", "#143aa0"),
        ("deepseek-v4-flash", "#94c9ff"),
    ],
)
def test_supported_models_use_distinct_moving_colour_bands_for_any_provider(model: str, colour: str) -> None:
    first = _provider_model_markup("OpenAI", model, glow_frame=0)
    next_frame = _provider_model_markup("OpenAI", model, glow_frame=1)

    assert Text.from_markup(first).plain == f"OpenAI/{model}"
    assert first != next_frame
    assert f"[{colour}]" in first
    assert "[#6e6d72]/[/]" in first


def test_unknown_models_keep_the_standard_accent_for_any_provider() -> None:
    assert _provider_name_markup("OpenAI", glow_frame=4) == "[#4f8cff]OpenAI[/]"
    assert _provider_model_markup("OpenAI", "gpt-5.6", glow_frame=4) == "[#4f8cff]OpenAI[/][#6e6d72]/gpt-5.6[/]"
    assert _provider_model_markup("deepseek", "deepseek-chat", glow_frame=4) == ("[#4f8cff]deepseek[/][#6e6d72]/deepseek-chat[/]")
    known_model_glow = _provider_model_markup("yurenapi", "gpt-5.5", glow_frame=0)
    assert Text.from_markup(known_model_glow).plain == "yurenapi/gpt-5.5"
    assert known_model_glow != "[#4f8cff]yurenapi[/][#6e6d72]/gpt-5.5[/]"
    openai_grok_glow = _provider_model_markup("OpenAI", "grok-4.5", glow_frame=0)
    assert Text.from_markup(openai_grok_glow).plain == "OpenAI/grok-4.5"
    assert openai_grok_glow != "[#4f8cff]OpenAI[/][#6e6d72]/grok-4.5[/]"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_model_glow_animates_and_stops_when_the_app_unmounts() -> None:
    app = LansCoderApp(config=LansCoderTuiConfig(provider_name="OpenAI", provider_model="gpt-5.6-terra"))

    async with app.run_test():
        timer = app._provider_glow_timer
        assert timer is not None
        before = app._topbar_text()
        app._advance_provider_glow()
        after = app._topbar_text()
        assert before != after

    assert timer is not None
    assert app._provider_glow_timer is None


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_unsupported_model_does_not_start_provider_glow() -> None:
    app = LansCoderApp(config=LansCoderTuiConfig(provider_name="OpenAI", provider_model="other-model"))

    async with app.run_test():
        assert app._provider_glow_timer is None


def test_observe_markdown_update_consumes_cancelled_update_result() -> None:
    result = FakeMarkdownUpdateResult(asyncio.CancelledError())

    _observe_markdown_update(result)
    result.finish()

    assert result.exception_observed is True


def test_observe_markdown_update_does_not_consume_unexpected_update_errors() -> None:
    result = FakeMarkdownUpdateResult(RuntimeError("markdown failed"))

    _observe_markdown_update(result)
    with pytest.raises(RuntimeError, match="markdown failed"):
        result.finish()


def test_stable_lanscoder_markdown_and_blocks_allow_selection() -> None:
    markdown = LansCoderMarkdown()

    assert markdown.allow_select is True
    assert LansCoderMarkdown.BLOCKS
    assert all(block.ALLOW_SELECT is True for block in LansCoderMarkdown.BLOCKS.values())


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_static_and_completed_markdown_output_allow_selection() -> None:
    app = LansCoderApp()

    async with app.run_test() as pilot:
        app._ui_line(BlockKind.SYSTEM, "plain output")
        app._write_markdown_message("paragraph\n\n- list\n\n```python\nprint('ok')\n```")
        await pilot.pause()

        assert app.ALLOW_SELECT is True
        assert all(widget.allow_select for widget in app.query("#output Static"))
        markdown = app.query_one("LansCoderMarkdown", LansCoderMarkdown)
        assert markdown.allow_select is True
        assert all(block.allow_select for block in markdown.query("*"))


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_assistant_label_paragraph_renders_in_brand_blue() -> None:
    """助手消息第一段（LansCoder 标签行）染品牌蓝，正文段落保持默认色。"""
    app = LansCoderApp()

    async with app.run_test() as pilot:
        box = _plain_static("", classes="assistant-message")
        await app.mount(box)
        paragraph_type = LansCoderMarkdown.BLOCKS["paragraph_open"]
        tokens = MarkdownIt().parse("LansCoder\n\nbody paragraph")
        label = paragraph_type(Markdown("probe"), tokens[0])
        body = paragraph_type(Markdown("probe"), tokens[2])
        await box.mount(label)
        await box.mount(body)
        await pilot.pause()

        assert label.styles.color == Color.parse(theme.ACCENT)
        assert body.styles.color != Color.parse(theme.ACCENT)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_ctrl_c_copies_when_screen_has_selected_output(monkeypatch) -> None:
    app = LansCoderApp()
    copied: list[str] = []
    quit_calls: list[bool] = []

    async with app.run_test():
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: "selected output")
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
        monkeypatch.setattr(app, "action_quit", lambda: quit_calls.append(True))
        await app.action_copy_output_or_quit()

    assert copied == ["selected output"]
    assert quit_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_ctrl_c_quits_when_screen_has_no_selected_output(monkeypatch) -> None:
    app = LansCoderApp()
    quit_calls: list[bool] = []

    async with app.run_test():
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: None)
        monkeypatch.setattr(app, "action_quit", lambda: quit_calls.append(True))
        await app.action_copy_output_or_quit()

    assert quit_calls == [True]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("copy_key", ["ctrl+c", "super+c"])
async def test_composer_selection_copy_does_not_quit(copy_key) -> None:
    app = LansCoderApp()

    async with app.run_test() as pilot:
        composer = app.query_one("#input", ComposerTextArea)
        composer.load_text("copy me")
        await pilot.click("#input")
        composer.selection = ((0, 0), (0, 7))
        await pilot.press(copy_key)

        assert app.clipboard == "copy me"
        assert app.is_running is True


def test_welcome_renderable_uses_colored_box_pixels() -> None:
    renderable = welcome_renderable()
    text = renderable.renderable
    next_text = welcome_renderable(particle_frame=1).renderable

    assert renderable.align == "center"
    assert "█" in text.plain
    assert "LansCoder" not in text.plain
    assert "Commands:" not in text.plain
    # 渐变主色（左亮 #00d4ff → 右深 #0099ff）与粒子色（白 / 浅蓝）。
    assert any(span.style == "#00d4ff" for span in text.spans)
    assert any(span.style == "#0099ff" for span in text.spans)
    assert any(span.style == "#ffffff" for span in text.spans)
    assert any(span.style == "#b0e0ff" for span in text.spans)
    assert text.plain != next_text.plain


def test_welcome_logo_fits_compact_threshold_and_gradients_left_to_right() -> None:
    # 全尺寸 logo 必须在 COMPACT_WELCOME_MAX_WIDTH=80 阈值之上仍能完整显示，
    # 否则常规终端永远看不到像素字。
    assert all(len(row) <= 80 for row in WELCOME_LOGO_PIXELS)
    # 渐变方向：渲染时最左前景是最亮的 #00d4ff，最右前景是最深的 #0099ff。
    text = welcome_renderable().renderable
    bright_starts = [span.start for span in text.spans if span.style == "#00d4ff"]
    deep_starts = [span.start for span in text.spans if span.style == "#0099ff"]
    assert bright_starts and deep_starts
    assert min(bright_starts) < min(deep_starts)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_shows_welcome_until_first_input() -> None:
    runner = FakeAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test(size=(120, 40)) as pilot:
        welcome = app.query_one("#welcome")
        content = welcome.content
        plain = getattr(getattr(content, "renderable", content), "plain", str(content))
        assert "█" in plain
        assert "Commands:" not in plain

        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()

        assert not app.query("#welcome")
        assert app._welcome_particle_timer is None

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_keeps_content_margin_when_populated() -> None:
    app = LansCoderApp(chat_runner=_PanelRunner())
    async with app.run_test(size=(120, 40)) as pilot:
        app._refresh_subagent_progress()
        await pilot.pause()
        panel = app.query_one("#subagent-panel")
        assert panel.styles.margin.left == 3
        assert panel.region.x == 3


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_hidden_when_no_active_jobs() -> None:
    app = LansCoderApp(chat_runner=FakeAsyncChatRunner())
    async with app.run_test(size=(120, 40)):
        app._refresh_subagent_progress()
        panel = app.query_one("#subagent-panel")
        assert panel.has_class("hidden")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_caps_visible_lines_with_remainder_footer() -> None:
    app = LansCoderApp(chat_runner=_PanelRunner(count=4))
    async with app.run_test(size=(120, 40)) as pilot:
        app._refresh_subagent_progress()
        await pilot.pause()
        statics = list(app.query_one("#subagent-panel").query("Static"))
        assert len(statics) == 5  # 3 running lines + remainder footer + binding hint
        assert "还有 1 个子agent在跑" in statics[3].content


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_no_footer_under_cap() -> None:
    app = LansCoderApp(chat_runner=_PanelRunner(count=3))
    async with app.run_test(size=(120, 40)) as pilot:
        app._refresh_subagent_progress()
        await pilot.pause()
        statics = list(app.query_one("#subagent-panel").query("Static"))
        assert len(statics) == 4  # 3 running lines + binding hint
        assert all("还有" not in s.content for s in statics)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_welcome_particles_animate_between_frames() -> None:
    app = LansCoderApp()

    async with app.run_test(size=(120, 40)):
        welcome = app.query_one("#welcome")
        before = welcome.content
        app._advance_welcome_particles()
        after = welcome.content

    assert before != after


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_uses_compact_welcome_in_an_80_by_24_terminal() -> None:
    app = LansCoderApp()

    async with app.run_test(size=(80, 24)) as pilot:
        welcome = app.query_one("#welcome")
        plain = getattr(
            getattr(welcome.content, "renderable", welcome.content),
            "plain",
            str(welcome.content),
        )

        assert "lanscoder" in plain
        assert "█" not in plain
        assert app._welcome_particle_timer is None
        assert app.query_one("#input").display is True

        await pilot.resize_terminal(120, 40)
        await pilot.pause(0.2)

        full_welcome = app.query_one("#welcome")
        full_plain = getattr(
            getattr(full_welcome.content, "renderable", full_welcome.content),
            "plain",
            str(full_welcome.content),
        )
        assert "█" in full_plain
        assert app._welcome_particle_timer is not None


def test_lanscoder_app_topbar_uses_spacious_two_sided_layout_when_width_is_known() -> None:
    app = LansCoderApp(
        current_session=FakeSession(),
        config=LansCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="gpt-5.5",
            project_name="LansCoder",
        ),
    )

    text = app._topbar_text(width=120)

    assert text.startswith("[#4f8cff]LansCoder[/]")
    assert "[#4f8cff]idle · ready[/]" not in text
    assert "sess_test" not in text
    assert "yurenapi/gpt-5.5" in Text.from_markup(text).plain
    assert "[#7eb6ff]r[/]" in text
    assert "[#6e6d72]cwd LansCoder[/]" in text
    assert " " * 20 in text


def test_lanscoder_app_topbar_highlights_bypass_mode_and_truncates_long_session() -> None:
    class BypassSession(FakeSession):
        session_id = "sess_c8d401e2124f"
        mode = "bypass"

    app = LansCoderApp(current_session=BypassSession())

    assert app._topbar_text() == ("[#4f8cff]LansCoder[/]   [#303238]·[/]   [#ff6b5f]bypass[/]")


def test_lanscoder_app_topbar_excludes_live_activity_status() -> None:
    app = LansCoderApp(current_session=FakeSession())

    app._activity_text = "thinking [.. ] planning next step..."

    plain = Text.from_markup(app._topbar_text(width=120)).plain
    assert "thinking" not in plain
    assert "standard" in plain


def test_lanscoder_app_topbar_excludes_long_activity_text() -> None:
    app = LansCoderApp(
        current_session=FakeSession(),
        config=LansCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="very-long-model-name",
            project_name="LansCoder",
        ),
    )
    app._activity_text = "thinking [...] " + "reading think tool result " * 8

    text = app._topbar_text(width=150)

    assert "[#4f8cff]yurenapi[/][#6e6d72]/very-long-model-name[/]" in text
    assert "[#6e6d72]cwd LansCoder[/]" in text
    assert "reading think tool result reading think tool result" not in Text.from_markup(text).plain
    assert "thinking" not in Text.from_markup(text).plain


def test_lanscoder_app_topbar_fits_narrow_width_with_long_activity_and_metadata() -> None:
    app = LansCoderApp(
        current_session=FakeSession(),
        config=LansCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="very-long-model-name",
            project_name="LansCoder",
        ),
    )
    app._activity_text = "thinking [...] " + "reading think tool result " * 8

    text = app._topbar_text(width=80)
    plain = Text.from_markup(text).plain

    assert "\n" not in plain
    assert len(plain) <= 80
    assert "sess_test" not in plain
    assert plain.startswith("LansCoder")

    narrow_plain = Text.from_markup(app._topbar_text(width=60)).plain

    assert "\n" not in narrow_plain
    assert len(narrow_plain) <= 60
    assert "sess_test" not in narrow_plain
    assert narrow_plain.startswith("LansCoder")


def test_lanscoder_app_topbar_truncates_narrow_metadata_to_one_row() -> None:
    app = LansCoderApp(
        current_session=FakeSession(),
        config=LansCoderTuiConfig(
            provider_name="yurenapi",
            provider_model="very-long-model-name",
            project_name="LansCoder",
        ),
    )

    plain = Text.from_markup(app._topbar_text(width=60)).plain

    assert "\n" not in plain
    assert len(plain) <= 60
    assert plain.startswith("LansCoder")
    assert "idle" not in plain
    assert "sess_test" not in plain


def test_lanscoder_app_topbar_single_line_with_multiline_thinking() -> None:
    app = LansCoderApp(
        current_session=FakeSession(),
        config=LansCoderTuiConfig(
            provider_name="deepseek",
            provider_model="deepseek-chat",
            project_name="LansCoder",
        ),
    )
    app._activity_text = "thinking [.. ] 好的，我来分析。\n先看下目录结构"

    text = app._topbar_text(width=120)
    plain = Text.from_markup(text).plain

    assert "\n" not in plain
    assert Text(plain).cell_len <= 120
    assert plain.startswith("LansCoder")
    assert "cwd LansCoder" in plain


def test_tui_transcript_model_records_blocks_and_children() -> None:
    transcript = TranscriptModel()

    transcript.add_block(BlockKind.USER, "hello")
    transcript.add_block(BlockKind.ASSISTANT, "hi")
    tool_block = transcript.add_block(BlockKind.ASSISTANT)
    tool_block.children.append(ChildItem(ChildKind.TOOL, "c1", "tool exec_command running", status="running"))

    assert [block.kind for block in transcript.blocks] == [
        BlockKind.USER,
        BlockKind.ASSISTANT,
        BlockKind.ASSISTANT,
    ]
    assert [block.text for block in transcript.blocks] == ["hello", "hi", ""]
    assert transcript.blocks[-1].children[-1].status == "running"


def test_task_plan_panel_state_keeps_only_last_rendered_revision() -> None:
    state = TuiTaskPlanPanelState()

    assert state.last_rendered_revision is None
    state.last_rendered_revision = 3

    assert state.last_rendered_revision == 3


def test_task_plan_panel_text_renders_linear_tasks_in_ordered_single_column() -> None:
    assert task_plan_panel_text(
        {
            "mode": "linear",
            "revision": 1,
            "ready_task_ids": ["inspect"],
            "blocked_task_ids": ["test"],
            "topological_levels": [["inspect"], ["test"]],
            "tasks": [
                {
                    "id": "test",
                    "content": "跑测试",
                    "status": "pending",
                    "depends_on": [],
                    "order": 20,
                },
                {
                    "id": "inspect",
                    "content": "读代码",
                    "status": "completed",
                    "depends_on": [],
                    "order": 10,
                },
                {
                    "id": "implement",
                    "content": "实现改动",
                    "status": "in_progress",
                    "depends_on": [],
                    "order": 15,
                },
            ],
        }
    ) == ("Task Plan · linear\n[✓] 读代码\n[~] 实现改动\n[!] 跑测试")


def test_task_plan_panel_text_renders_dag_levels_dependencies_and_derived_statuses() -> None:
    assert task_plan_panel_text(
        {
            "mode": "dag",
            "revision": 2,
            "ready_task_ids": ["research_a"],
            "blocked_task_ids": ["summary"],
            "topological_levels": [["research_a", "research_b"], ["summary"]],
            "tasks": [
                {
                    "id": "summary",
                    "content": "汇总",
                    "status": "pending",
                    "depends_on": ["research_a", "research_b"],
                    "order": 30,
                },
                {
                    "id": "research_b",
                    "content": "调研 B",
                    "status": "in_progress",
                    "depends_on": [],
                    "order": 20,
                },
                {
                    "id": "research_a",
                    "content": "调研 A",
                    "status": "pending",
                    "depends_on": [],
                    "order": 10,
                },
            ],
        }
    ) == ("Task Plan · dag\nLevel 0 · parallel\n  [→] 调研 A (research_a)\n  [~] 调研 B (research_b)\nLevel 1\n  [!] 汇总 (summary) · depends on: research_a, research_b")


def test_lanscoder_app_records_rendered_messages_in_transcript(monkeypatch) -> None:
    output = FakeOutput()
    app = LansCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._ui_line(BlockKind.USER, "hello")
    app._write_markdown_message("**hi**")

    assert [(block.kind, block.text) for block in app.transcript.blocks] == [
        (BlockKind.USER, "hello"),
        (BlockKind.ASSISTANT, "**hi**"),
    ]


class FakeChatRunner:
    def __init__(self) -> None:
        self.inputs = []
        self.attachments: list[list[UserAttachment] | None] = []
        self.last_display_lines = []

    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        self.inputs.append(content)
        self.attachments.append(attachments)
        return ChatResponse(provider="fake", model="fake", content=f"reply:{content}")


class _PanelJob:
    def __init__(self, created_at: float, label: str = "researcher", job_id: str | None = None):
        self.created_at = created_at
        self.tool_name = "delegate"
        self.label = label
        self.progress = {"provider_calls": 3, "total_tokens": 2500}
        self.id = job_id or f"panel-{created_at}"
        self.status = "running"
        self.cancel_requested = False


class _PanelManager:
    def __init__(self, count: int = 1):
        self._jobs = [_PanelJob(created_at=100.0 + i, label=f"researcher{i}", job_id=f"bg_{i:04d}") for i in range(count)]

    def active_jobs(self):
        return self._jobs


class _PanelRunner(FakeChatRunner):
    def __init__(self, count: int = 1):
        super().__init__()
        self.background_manager = _PanelManager(count)
        self._foreground = None

    def foreground_subagent(self):
        return self._foreground


class FakeDisplayChatRunner(FakeChatRunner):
    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        self.inputs.append(content)
        self.attachments.append(attachments)
        self.last_display_lines = [
            "Tool call: echo {}",
            "Tool result: echo success: ok",
            "done",
        ]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakeAsyncChatRunner(FakeChatRunner):
    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        self.inputs.append(content)
        self.attachments.append(attachments)
        self.last_display_lines = ["async reply"]
        return ChatResponse(provider="fake", model="fake", content="async reply")


class FailingAsyncChatRunner(FakeChatRunner):
    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        raise RuntimeError("provider down")


class FakeStreamingAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.seen = []
        self.stream_event_handler = self.seen.append

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="thinking"))
        self.last_display_lines = ["done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakeStreamingTextAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.stream_event_handler = lambda event: None

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.stream_event_handler(ChatStreamEvent(kind="text_delta", text="he"))
        self.stream_event_handler(ChatStreamEvent(kind="text_delta", text="llo"))
        self.last_display_lines = ["hello"]
        return ChatResponse(provider="fake", model="fake", content="hello")


class FakeToolEventAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.tool_event_handler = lambda event: None

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        tool_call = ToolCall(id="call_echo", name="echo", arguments={"text": "hello"})
        self.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=tool_call))
        self.tool_event_handler(
            ToolExecutionEvent(
                kind="finished",
                tool_call=tool_call,
                result=ToolResult(name="echo", ok=True, content="hello"),
            )
        )
        self.last_display_lines = [
            'Tool call: echo {"text": "hello"}',
            "Tool result: echo success: hello",
            "done",
        ]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakePermissionResumeRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.last_pending_input = UserInputRequest(
            id="perm_write",
            kind="permission_confirmation",
            question="允许写 README 吗？",
            options=[
                UserInputOption(id="deny", label="Deny"),
                UserInputOption(id="allow_once", label="Allow once"),
            ],
        )
        self.resumes: list[tuple[str, str]] = []

    async def aresume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        self.resumes.append((request_id, answer))
        self.last_pending_input = None
        self.last_display_lines = ["Tool result: write success: ok", "done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakeAskUserResumeRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.last_pending_input = UserInputRequest(
            id="ask_env",
            kind="ask_user",
            question="Which environment?",
            options=[
                UserInputOption(id="1", label="dev"),
                UserInputOption(id="2", label="prod"),
            ],
        )
        self.resumes: list[tuple[str, str]] = []

    async def aresume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        self.resumes.append((request_id, answer))
        self.last_pending_input = None
        self.last_display_lines = ["部署到 prod", "done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakePermissionMidTurnRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.last_pending_input = None
        self.resumes: list[tuple[str, str]] = []

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.last_pending_input = UserInputRequest(
            id="perm_write",
            kind="permission_confirmation",
            question="允许写 README 吗？",
            options=[
                UserInputOption(id="deny", label="Deny"),
                UserInputOption(id="allow_once", label="Allow once"),
            ],
        )
        return ChatResponse(provider="fake", model="fake", content="等待权限确认。")

    async def aresume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        self.resumes.append((request_id, answer))
        self.last_pending_input = None
        self.last_display_lines = ["done"]
        return ChatResponse(provider="fake", model="fake", content="done")


class FakePermissionWaitingRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.last_pending_input = UserInputRequest(
            id="perm_write",
            kind="permission_confirmation",
            question="允许写 README 吗？",
            options=[
                UserInputOption(id="deny", label="Deny"),
                UserInputOption(id="allow_once", label="Allow once"),
                UserInputOption(id="allow_always_same_scope", label="Allow always"),
            ],
            payload={
                "action": "write_path",
                "target": "README.md",
                "reason": "写入文件需要用户确认。",
                "prewrite_review": {
                    "tool_name": "edit",
                    "files": [
                        {
                            "path": "README.md",
                            "operation": "modify",
                            "diff": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new",
                            "added_lines": 1,
                            "removed_lines": 1,
                        }
                    ],
                    "summary": {"added_lines": 1, "removed_lines": 1},
                    "error": None,
                },
            },
        )


class BlockingAsyncChatRunner(FakeChatRunner):
    def __init__(self) -> None:
        super().__init__()
        import anyio

        self.started = anyio.Event()
        self.release = anyio.Event()

    async def arun_user_turn(self, content: str) -> ChatResponse:
        self.inputs.append(content)
        self.started.set()
        await self.release.wait()
        return ChatResponse(provider="fake", model="fake", content="done")


class BlockingGuidanceAsyncChatRunner(BlockingAsyncChatRunner):
    def __init__(self) -> None:
        super().__init__()
        self.guidance: list[str] = []

    def add_guidance(self, content: str) -> None:
        self.guidance.append(content)


class RecordingSession:
    """Tiny session stand-in that records background-notification writes."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str, str]] = []
        self.session_id = "sess_test"

    def append_background_notification(
        self,
        *,
        content,
        job_id,
        tool_name,
        status,
        task_id=None,
        observed_revision=None,
        label=None,
        error=None,
    ):
        self.writes.append((content, job_id, tool_name, status))
        return "msg_x"


class FakeCurrentSession:
    def __init__(self) -> None:
        self.session = RecordingSession()

    @property
    def session_id(self) -> str:
        return self.session.session_id


class FakeBackgroundManager:
    """Background-manager stand-in with a peek-only pending queue."""

    def __init__(self, pending: list[BackgroundJob] | None = None) -> None:
        self._pending = list(pending or [])

    def pending_completions(self, *, session_id: str | None = None) -> list[BackgroundJob]:
        return list(self._pending)

    def drain(self) -> None:
        """Simulate the agent loop consuming completions during a turn."""
        self._pending = []


class FakeSubagentRunner(FakeChatRunner):
    """Runner that exercises subagent-completion delivery end to end."""

    def __init__(self, pending: list[BackgroundJob] | None = None) -> None:
        super().__init__()
        self.nudges: list[bool] = []
        self.current_session = FakeCurrentSession()
        self.background_manager = FakeBackgroundManager(pending)

    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        self.inputs.append(content)
        self.attachments.append(attachments)
        # A real turn drains pending completions at its first provider request.
        self.background_manager.drain()
        return ChatResponse(provider="fake", model="fake", content=f"reply:{content}")

    async def anudge_turn(self) -> ChatResponse:
        self.nudges.append(True)
        # A real nudge drains pending completions at its first provider request.
        self.background_manager.drain()
        return ChatResponse(provider="fake", model="fake", content="reply:nudge")


class UnhandledCommandHandler:
    def handle(self, text: str) -> CommandResult:
        return CommandResult(handled=False)


class SubmitChatCommandHandler:
    def handle(self, text: str) -> CommandResult:
        return CommandResult(
            handled=True,
            output="Using skill: brief",
            action={"type": "submit_chat", "text": "请使用 skills/brief.md 写日报"},
        )


def test_lanscoder_app_can_be_created_with_composite_handler_and_chat_runner() -> None:
    context_handler = ContextCommandHandler(session=FakeSession(), budget_provider=_context_budget)
    composite = CompositeCommandHandler(
        [
            SessionCommandHandler(catalog=object()),  # constructor storage only; not used by this test
            context_handler,
        ]
    )
    runner = FakeChatRunner()

    app = LansCoderApp(command_handler=composite, chat_runner=runner)

    assert app.command_handler is composite
    assert app.chat_runner is runner


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_runs_plain_chat_when_only_chat_runner_is_configured() -> None:
    runner = FakeChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_sends_staged_paste_attachment_and_clears_it(tmp_path, monkeypatch) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    attachment = UserAttachment(
        kind="image",
        path=image,
        filename="image.png",
        media_type="image/png",
        size_bytes=image.stat().st_size,
        source="paste",
    )
    monkeypatch.setattr("lanscoder.app.tui.resolve_paste_attachments", lambda text: [attachment])
    runner = FakeChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        app._staged_attachments = [attachment]
        await pilot.press(*"describe")
        await pilot.press("enter")

    assert runner.inputs == ["describe"]
    assert runner.attachments == [[attachment]]
    assert app._staged_attachments == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_pasting_a_file_path_stages_attachment_without_inserting_path(
    tmp_path,
) -> None:
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    runner = FakeChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", TextArea)
        await input_widget._on_paste(events.Paste(str(video)))
        await pilot.pause()

        assert input_widget.text == ""
        assert app._staged_attachments[0].path == video
        await pilot.click("#input")
        await pilot.press("enter")

    assert runner.inputs == ["请分析这些附件。"]
    assert runner.attachments[0][0].path == video


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_pasting_plain_text_keeps_text_in_composer(
    monkeypatch,
) -> None:
    monkeypatch.setattr("lanscoder.app.tui.resolve_paste_attachments", lambda text: [])
    app = LansCoderApp()

    async with app.run_test():
        input_widget = app.query_one("#input", TextArea)
        await input_widget._on_paste(events.Paste("explain this"))
        assert input_widget.text == "explain this"

    assert app._staged_attachments == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_composer_advertises_ctrl_or_cmd_v_for_image_paste() -> None:
    app = LansCoderApp()

    async with app.run_test():
        composer = app.query_one("#input", ComposerTextArea)
        assert composer.placeholder == "输入消息，Enter 发送，Shift+Enter 换行，Ctrl/Cmd+V 粘贴图片"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("paste_key", ["ctrl+v", "super+v"])
async def test_composer_paste_shortcut_leaves_plain_text_for_terminal_paste_event(monkeypatch, paste_key) -> None:
    monkeypatch.setattr("lanscoder.app.tui.resolve_paste_attachments", lambda text: [])
    app = LansCoderApp()

    async with app.run_test() as pilot:
        await pilot.click("#input")
        app.copy_to_clipboard("hello")
        await pilot.press(paste_key)
        composer = app.query_one("#input", ComposerTextArea)
        await composer._on_paste(events.Paste("hello"))

        assert composer.text == "hello"
        assert app._staged_attachments == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("paste_key", ["ctrl+v", "super+v", "f8"])
async def test_lanscoder_app_paste_shortcut_stages_clipboard_image_while_composer_is_focused(tmp_path, monkeypatch, paste_key) -> None:
    image = tmp_path / "clipboard.png"
    image.write_bytes(b"image")
    attachment = UserAttachment("image", image, "clipboard.png", "image/png", 5, "clipboard")
    monkeypatch.setattr("lanscoder.app.tui.resolve_paste_attachments", lambda text: [attachment])
    app = LansCoderApp()

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(paste_key)
        input_widget = app.query_one("#input", ComposerTextArea)

    assert input_widget.text == ""
    assert app._staged_attachments == [attachment]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("paste_key", ["ctrl+v", "super+v"])
async def test_composer_paste_shortcut_does_not_report_missing_clipboard_image(monkeypatch, paste_key) -> None:
    monkeypatch.setattr("lanscoder.app.tui.resolve_paste_attachments", lambda text: [])
    app = LansCoderApp()

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(paste_key)
        await pilot.pause()

        assert "No clipboard image found" not in _static_output_text(app)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_composer_image_paste_shortcut_reports_missing_clipboard_image(
    monkeypatch,
) -> None:
    monkeypatch.setattr("lanscoder.app.tui.resolve_paste_attachments", lambda text: [])
    app = LansCoderApp()

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press("f8")
        await pilot.pause()

        assert "No clipboard image found" in _static_output_text(app)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_submits_multiline_composer_text() -> None:
    runner = FakeChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", TextArea)
        input_widget.load_text("第一句\n第二句\n第三句")
        await pilot.click("#input")
        await pilot.press("enter")

    assert runner.inputs == ["第一句\n第二句\n第三句"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_shift_enter_inserts_newline_without_submitting() -> None:
    runner = FakeChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("shift+enter")
        await pilot.press(*"world")
        input_widget = app.query_one("#input", TextArea)

    assert input_widget.text == "hello\nworld"
    assert runner.inputs == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_does_not_send_unhandled_slash_command_to_chat_runner() -> None:
    runner = FakeChatRunner()
    app = LansCoderApp(command_handler=UnhandledCommandHandler(), chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/unknown")
        await pilot.press("enter")

    assert runner.inputs == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_submits_chat_from_command_action() -> None:
    runner = FakeChatRunner()
    app = LansCoderApp(command_handler=SubmitChatCommandHandler(), chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/brief 写日报")
        await pilot.press("enter")

    assert runner.inputs == ["请使用 skills/brief.md 写日报"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_displays_session_id_and_runner_display_lines() -> None:
    runner = FakeDisplayChatRunner()
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())

    async with app.run_test() as pilot:
        assert app.sub_title == "Session: sess_test"
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_awaits_async_chat_runner_when_available() -> None:
    runner = FakeAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")

    assert runner.inputs == ["hello"]
    assert runner.last_display_lines == ["async reply"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_queues_input_when_chat_is_running() -> None:
    """When the user sends a message while a turn is running, it is queued
    and delivered after the current turn finishes."""
    runner = BlockingGuidanceAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"start")
        await pilot.press("enter")
        await runner.started.wait()
        await pilot.press(*"先别总结")
        await pilot.press("enter")
        await pilot.pause()
        runner.release.set()
        await pilot.pause()

    assert runner.inputs == ["start", "先别总结"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_resume_picker_replays_selected_session_history(
    tmp_path,
) -> None:
    store = JsonlSessionStore(tmp_path)
    writer_one = SessionEventWriter(store=store, session_id="sess_one")
    writer_one.append_session_created(title="第一个")
    writer_one.append_user_message("旧问题")
    writer_one.append_assistant_response(ChatResponse(provider="fake", model="fake", content="旧回答"))
    tool_call = ToolCall(id="call_resume", name="grep", arguments={"pattern": "needle"})
    writer_one.append_assistant_response(ChatResponse(provider="fake", model="fake", content="", tool_calls=[tool_call]))
    writer_one.append_tool_result(
        tool_call=tool_call,
        result=ToolResult(name="grep", ok=True, content="result " + "x" * 300),
    )
    writer_two = SessionEventWriter(store=store, session_id="sess_two")
    writer_two.append_session_created(title="第二个")
    writer_two.append_user_message("新问题")
    current = AgentSession.resume(store=store, session_id="sess_one", agents_md="")
    state = CurrentSessionState(current)
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        resume_service=ResumeService(store=store, project_root=tmp_path),
        on_resume=state.set_session,
    )
    app = LansCoderApp(command_handler=handler, current_session=state)
    markdown_rendered = False

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/resume")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Select a session" in output_text
        assert "第二个" in output_text
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        markdown_rendered = bool(app.query_one("#output").query("LansCoderMarkdown"))

    assert state.session.session_id == "sess_one"
    assert "旧问题" in output_text
    assert any(block.text == "旧回答" for block in app.transcript.blocks if block.kind == BlockKind.ASSISTANT)
    tool_children = [child for block in app.transcript.blocks for child in block.children if child.kind == ChildKind.TOOL]
    assert any("grep" in child.label for child in tool_children)
    assert any(child.status == "success" for child in tool_children)
    assert "x" * 300 not in output_text
    assert markdown_rendered
    assert "Select a session" not in output_text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_resume_picker_renders_twenty_visible_rows_and_scrolls(
    tmp_path,
) -> None:
    store = JsonlSessionStore(tmp_path)
    for index in range(25):
        writer = SessionEventWriter(store=store, session_id=f"sess_{index:02d}")
        writer.append_session_created(title=f"标题{index:02d}")
        writer.append_user_message(f"问题{index:02d}")
    current = AgentSession.resume(store=store, session_id="sess_00", agents_md="")
    state = CurrentSessionState(current)
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        resume_service=ResumeService(store=store, project_root=tmp_path),
        on_resume=state.set_session,
    )
    app = LansCoderApp(command_handler=handler, current_session=state)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/resume")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Showing 1-20 of 25 sessions" in output_text
        assert "sess_24" in output_text
        assert "sess_05" in output_text
        assert "sess_04" not in output_text

        for _ in range(20):
            await pilot.press("down")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Showing 2-21 of 25 sessions" in output_text
        assert "sess_24" not in output_text
        assert "sess_04" in output_text
        assert "> 21. sess_04" in output_text
        await pilot.press("enter")
        await pilot.pause()

    assert state.session.session_id == "sess_04"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_model_picker_switches_selected_model() -> None:
    handler = RecordingCommandHandler()
    app = LansCoderApp(
        command_handler=handler,
        config=LansCoderTuiConfig(provider_name="fake", provider_model="old"),
    )

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/model")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Select a model:" in output_text
        assert "> 1. fake/old" in output_text

        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    assert handler.commands == ["/model", "/model fake/new"]
    assert app.config.provider_name == "fake"
    assert app.config.provider_model == "new"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_models_alias_opens_model_picker() -> None:
    handler = RecordingCommandHandler()
    app = LansCoderApp(
        command_handler=handler,
        config=LansCoderTuiConfig(provider_name="fake", provider_model="old"),
    )

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/models")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)

    assert handler.commands == ["/models"]
    assert "Select a model:" in output_text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_skill_picker_references_selected_skill_in_input() -> None:
    handler = RecordingCommandHandler()
    app = LansCoderApp(command_handler=handler)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/skills")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)
        assert "Select a skill:" in output_text
        assert "> 1. brief\n    project · skills/brief.md" in output_text
        assert "Selected: Write a brief." in output_text

        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        input_widget = app.query_one("#input")

    assert handler.commands == ["/skills", "/skill-use review"]
    assert input_widget.text == "请先调用 load_skill(name=review, args=<你的任务>)，再按照返回的指令继续。"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_new_command_clears_previous_output(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_old")
    writer.append_session_created(title="旧会话")
    writer.append_user_message("旧问题")
    current = AgentSession.resume(store=store, session_id="sess_old", agents_md="")
    state = CurrentSessionState(current)
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        new_service=NewSessionService(store=store, project_root=tmp_path),
        on_resume=state.set_session,
    )
    app = LansCoderApp(command_handler=handler, current_session=state)

    async with app.run_test() as pilot:
        app._ui_line(BlockKind.USER, "旧问题")
        app._write_markdown_message("旧回答")
        await pilot.click("#input")
        await pilot.press(*"/new 新会话")
        await pilot.press("enter")
        await pilot.pause()
        output_text = _static_output_text(app)

    assert state.session.session_id != "sess_old"
    assert "New session:" in output_text
    assert "新会话" in output_text
    assert "旧问题" not in output_text
    assert "旧回答" not in output_text
    assert [block.kind for block in app.transcript.blocks] == [BlockKind.COMMAND]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_double_escape_interrupts_running_chat() -> None:
    runner = BlockingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"start")
        await pilot.press("enter")
        await runner.started.wait()
        await pilot.press("escape")
        assert app._chat_busy is True
        await pilot.press("escape")
        await pilot.pause()
        output_text = "\n".join(str(getattr(widget, "content", getattr(widget, "renderable", ""))) for widget in app.query_one("#output").query("Static"))

    assert runner.inputs == ["start"]
    assert app._chat_busy is False
    assert app._activity_text == "interrupted"
    assert "Interrupted current turn." in output_text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_streaming_tui_does_not_render_duplicate_final_markdown() -> None:
    runner = FakeStreamingTextAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        output = app.query_one("#output")
        markdown_widgets = output.query("Markdown")
        assert len(markdown_widgets) == 1
    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_right_clicking_markdown_output_does_not_crash_selection_path() -> None:
    runner = FakeChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        markdown = app.query_one("LansCoderMarkdown")
        assert app.ALLOW_SELECT is True
        assert markdown.allow_select is True
        assert all(block.allow_select for block in markdown.query("*"))
        await pilot.click(markdown, button=3)
        await pilot.pause()

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_right_clicking_markdown_code_block_does_not_crash_selection_path() -> None:
    app = LansCoderApp()

    async with app.run_test() as pilot:
        app._write_markdown_message("```text\nIt was the best of times\n```")
        await pilot.pause()
        markdown = app.query_one("LansCoderMarkdown", LansCoderMarkdown)
        assert app.ALLOW_SELECT is True
        assert markdown.allow_select is True
        assert all(block.allow_select for block in markdown.query("*"))
        await pilot.click(markdown, button=3)
        await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_streaming_markdown_selection_state_applies_to_mounted_blocks() -> None:
    app = LansCoderApp()

    async with app.run_test() as pilot:
        output = app.query_one("#output")
        markdown = LansCoderMarkdown(selectable=False)
        await output.mount(markdown)
        await markdown.update("paragraph\n\n- list\n\n| one | two |\n| --- | --- |\n| 1 | 2 |\n\n```python\nprint('ok')\n```")
        await pilot.pause()

        blocks = list(markdown.query("MarkdownBlock"))
        selection_leaves = [widget for widget in markdown.query("*") if type(widget).__name__.removeprefix("LansCoder") in {"MarkdownBullet", "MarkdownTableCellContents"}]
        assert blocks
        assert {type(widget).__name__.removeprefix("LansCoder") for widget in selection_leaves} == {
            "MarkdownBullet",
            "MarkdownTableCellContents",
        }
        assert markdown.allow_select is False
        screen = app.screen
        assert isinstance(screen, LansCoderScreen)
        assert all(screen._selection_is_blocked_by_streaming_markdown(block) for block in blocks)
        assert all(screen._selection_is_blocked_by_streaming_markdown(widget) for widget in selection_leaves)

        markdown.set_selectable(True)

        assert markdown.allow_select is True
        assert all(not screen._selection_is_blocked_by_streaming_markdown(block) for block in blocks)
        assert all(not screen._selection_is_blocked_by_streaming_markdown(widget) for widget in selection_leaves)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_screen_rejects_all_leaves_inside_unstable_streaming_markdown() -> None:
    app = LansCoderApp()

    async with app.run_test() as pilot:
        output = app.query_one("#output")
        markdown = LansCoderMarkdown(selectable=False)
        await output.mount(markdown)
        await markdown.update("- list\n\n| one | two |\n| --- | --- |\n| 1 | 2 |")
        await pilot.pause()

        selection_leaves = [widget for widget in markdown.query("*") if type(widget).__name__.removeprefix("LansCoder") in {"MarkdownBullet", "MarkdownTableCellContents"}]
        assert selection_leaves
        screen = app.screen
        assert isinstance(screen, LansCoderScreen)
        assert all(screen._selection_is_blocked_by_streaming_markdown(leaf) for leaf in selection_leaves)

        markdown.set_selectable(True)

        assert all(not screen._selection_is_blocked_by_streaming_markdown(leaf) for leaf in selection_leaves)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_click_on_detached_stale_markdown_block_does_not_crash_selection_path() -> None:
    """流式替换窗口内点击 stale 块不崩:命中"已 detach 但残留在命中地图"的块。

    流式更新整篇 rebuild 时,块被 prune(detach,parent=None)到下一帧 layout 刷新之间,
    compositor 命中地图仍返回这个块。screen._forward_event 的文本选择分支取
    ``container = content_widget.parent`` 得 None,旧代码在 ``container.region`` 上抛
    AttributeError。屏幕命中层必须以 parent 为 None 判定不可选,整个选择分支不进入。
    """
    app = LansCoderApp()
    async with app.run_test() as pilot:
        output = app.query_one("#output")
        markdown = LansCoderMarkdown(selectable=False)
        await output.mount(markdown)
        await markdown.update("first paragraph\n\nsecond paragraph")
        await pilot.pause()

        blocks = list(markdown.query("MarkdownBlock"))
        stale = next(b for b in blocks if type(b).__name__ == "LansCoderMarkdownParagraph")
        region = stale.region
        assert stale.parent is not None

        # 复刻真实替换状态:块从 DOM detach(parent 置 None),layout 尚未刷新,
        # 因此 compositor 原始命中地图仍返回它。
        await stale.remove()

        screen = app.screen
        assert isinstance(screen, LansCoderScreen)
        # 前提:原始命中确实命中 stale 块——替换窗口状态为真
        raw_widget, _ = screen.get_widget_at(region.x + 1, region.y)
        assert raw_widget is stale
        assert stale.parent is None

        # 契约:文本选择命中层必须把无父节点的 stale 块整体判为不可选,
        # 使 _forward_event 的选择分支完全不进入(旧代码在此由
        # get_widget_and_offset_at 返回该块,进而在 container.region 上崩溃)。
        hit_widget, hit_offset = screen.get_widget_and_offset_at(region.x + 1, region.y)
        assert hit_widget is None
        assert hit_offset is None

        mouse = events.MouseDown(
            None,
            x=region.x + 1,
            y=region.y,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=False,
            screen_x=region.x + 1,
            screen_y=region.y,
        )
        screen._forward_event(mouse)
        await pilot.pause()

        assert app.is_running


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_streaming_markdown_becomes_selectable_only_after_final_update() -> None:
    app = LansCoderApp()

    async with app.run_test():
        app._append_stream_text("partial")
        app._stream_text_started = True
        markdown = app.query_one("LansCoderMarkdown.streaming", LansCoderMarkdown)
        assert markdown.allow_select is False
        screen = app.screen
        assert isinstance(screen, LansCoderScreen)
        assert all(screen._selection_is_blocked_by_streaming_markdown(block) for block in markdown.query("MarkdownBlock"))

        app._stream_text_buffer = "final answer"
        app._write_chat_response(ChatResponse(provider="fake", model="fake", content="final answer"))
        assert markdown.allow_select is False

        await app.wait_for_stream_finalization(markdown)
        assert markdown.allow_select is True
        assert markdown._markdown == "LansCoder:\n\nfinal answer"
        assert all(not screen._selection_is_blocked_by_streaming_markdown(block) for block in markdown.query("MarkdownBlock"))


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_closing_one_stream_segment_twice_runs_one_final_markdown_update(
    monkeypatch,
) -> None:
    app = LansCoderApp()

    async with app.run_test() as pilot:
        app._append_stream_text("final answer")
        markdown = app.query_one("LansCoderMarkdown.streaming", LansCoderMarkdown)
        await pilot.pause()
        original_update = markdown.update
        final_updates: list[str] = []

        def count_final_update(source: str):
            final_updates.append(source)
            return original_update(source)

        monkeypatch.setattr(markdown, "update", count_final_update)

        app._close_stream_segment_for_tool()
        app._close_stream_segment_for_tool()

        assert markdown.allow_select is False
        await app.wait_for_stream_finalization(markdown)

        assert final_updates == ["LansCoder:\n\nfinal answer"]
        assert markdown.allow_select is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_stream_finalization_records_update_error_without_exiting_tui(
    monkeypatch,
) -> None:
    app = LansCoderApp()
    workers: list[tuple[asyncio.Task[object], dict[str, object]]] = []

    async with app.run_test() as pilot:
        app._append_stream_text("final answer")
        markdown = app.query_one("LansCoderMarkdown.streaming", LansCoderMarkdown)
        await pilot.pause()

        async def failed_update(_source: str) -> None:
            raise RuntimeError("final markdown failed")

        def run_worker(coro, **kwargs):
            task = asyncio.create_task(coro)
            workers.append((task, kwargs))
            return task

        monkeypatch.setattr(markdown, "update", failed_update)
        monkeypatch.setattr(app, "run_worker", run_worker)

        app._close_stream_segment_for_tool()

        with pytest.raises(RuntimeError, match="final markdown failed"):
            await app.wait_for_stream_finalization(markdown)
        await workers[0][0]

        assert workers[0][1]["exit_on_error"] is False
        assert app.is_running is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_starting_a_new_stream_keeps_an_old_stream_finalization_waitable(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)
    update_started = asyncio.Event()
    release_update = asyncio.Event()

    async with app.run_test() as pilot:
        app._append_stream_text("first answer")
        markdown = app.query_one("LansCoderMarkdown.streaming", LansCoderMarkdown)
        await pilot.pause()

        async def delayed_update(_source: str) -> None:
            update_started.set()
            await release_update.wait()

        monkeypatch.setattr(markdown, "update", delayed_update)
        app._close_stream_segment_for_tool()
        await update_started.wait()

        app._install_stream_event_handler()
        finalization_wait = asyncio.create_task(app.wait_for_stream_finalization(markdown))
        await pilot.pause()
        assert finalization_wait.done() is False

        release_update.set()
        await finalization_wait


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_stream_segments_on_both_sides_of_tool_become_selectable() -> None:
    app = LansCoderApp()

    async with app.run_test():
        app._append_stream_text("before tool")
        first = app.query_one("LansCoderMarkdown.streaming", LansCoderMarkdown)
        app._close_stream_segment_for_tool()
        await app.wait_for_stream_finalization(first)
        assert first.allow_select is True
        assert first._markdown == "LansCoder:\n\nbefore tool"
        app.projector.tool_event("call_t", "grep", "started")

        app._append_stream_text("after tool")
        app._stream_text_started = True
        widgets = list(app.query("LansCoderMarkdown.streaming"))
        second = widgets[-1]
        assert second is not first
        assert second.allow_select is False

        app._write_chat_response(ChatResponse(provider="fake", model="fake", content="after tool"))
        await app.wait_for_stream_finalization(second)
        assert second.allow_select is True
        assert second._markdown == "LansCoder:\n\nafter tool"


def test_lanscoder_app_installs_and_restores_stream_event_handler() -> None:
    runner = FakeStreamingAsyncChatRunner()
    original_handler = runner.stream_event_handler
    app = LansCoderApp(chat_runner=runner)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="message_started"))
    app._restore_stream_event_handler(previous_handler)

    assert runner.seen == [ChatStreamEvent(kind="message_started")]
    assert runner.stream_event_handler is original_handler


def test_lanscoder_app_streams_text_delta_without_repeating_final_text(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["hello"]
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="he"))
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="llo"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert [type(widget).__name__ for widget in output.mounted] == ["LansCoderMarkdown"]
    assert output.mounted[0].allow_select is True
    assert output.mounted[0].updates[-1] == "LansCoder:\n\nhello"
    assert app._stream_text_buffer == "hello"
    assert runner.seen == [
        ChatStreamEvent(kind="text_delta", text="he"),
        ChatStreamEvent(kind="text_delta", text="llo"),
    ]


def test_closing_stream_segment_twice_without_event_loop_updates_final_snapshot_once(
    monkeypatch,
) -> None:
    output = FakeOutput()
    app = LansCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._append_stream_text("final answer")
    markdown = output.mounted[0]
    initial_snapshot = "LansCoder:\n\nfinal answer"
    assert markdown.updates == [initial_snapshot]

    original_update = markdown.update
    final_updates: list[str] = []

    def count_final_update(source: str):
        final_updates.append(source)
        return original_update(source)

    monkeypatch.setattr(markdown, "update", count_final_update)

    app._close_stream_segment_for_tool()
    app._close_stream_segment_for_tool()

    assert final_updates == [initial_snapshot]
    assert markdown.updates == [initial_snapshot, initial_snapshot]
    assert markdown.allow_select is True


def test_lanscoder_app_streaming_skips_normalized_duplicate_assistant_line(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["hello\n"]
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    app._start_turn_metrics()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="hello"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert [type(widget).__name__ for widget in output.mounted] == ["LansCoderMarkdown"]
    assert output.mounted[0].allow_select is True
    assert output.mounted[0].updates[-1] == "LansCoder:\n\nhello"


def test_lanscoder_app_streaming_skips_replaying_intermediate_assistant_lines(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = [
        "先看详情：",
        'Tool call: shell {"command": "pytest"}',
        "Tool result: shell success: ok",
        "问题找到了：",
        "最终结论",
    ]
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="最终"))
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="结论"))
    app._live_tool_events_seen = True
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="最终结论"))
    app._restore_stream_event_handler(previous_handler)

    assert [type(widget).__name__ for widget in output.mounted] == ["LansCoderMarkdown"]
    assert output.mounted[0].updates[-1] == "LansCoder:\n\n最终结论"
    assert [block.text for block in app.transcript.blocks if block.kind == BlockKind.ASSISTANT] == ["最终结论"]


def test_lanscoder_app_paces_stream_markdown_updates(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)
    monkeypatch.setattr(app, "set_timer", lambda *args, **kwargs: object())

    app._append_stream_text("我")
    app._append_stream_text("在")
    app._append_stream_text("这里")

    markdown = output.mounted[0]
    assert type(markdown).__name__ == "LansCoderMarkdown"
    assert markdown.allow_select is False
    assert markdown.updates == ["LansCoder:\n\n我"]
    assert app._stream_text_buffer == "我在这里"

    app._flush_stream_text()

    # append 契约:首帧交付 header+首段,之后只 flush 尾部增量
    assert markdown.updates == ["LansCoder:\n\n我", "在这里"]
    assert "".join(markdown.updates) == "LansCoder:\n\n我在这里"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_streaming_cross_boundary_markdown_finalizes_correctly() -> None:
    """流式 append 跨块边界(未闭合代码围栏/列表)不崩,最终快照与整篇等价。

    旧整篇 update 每次重解析所有历史并重建全部块;改为 append 后只解析增量,
    对跨 flush 的围栏分块由 textual 续接合并最后一块。finalize 的整篇 update
    兜底,保证最终源串与缓冲精确一致。
    """
    app = LansCoderApp()
    async with app.run_test() as pilot:
        app._stream_text_started = True
        chunks = ["```python\n", "print(", "'ok'", ")\n```\n\n", "- a\n- b\n\n", "tail"]
        for chunk in chunks:
            app._append_stream_text(chunk)
            app._flush_stream_text()
            for _ in range(100):
                if app._stream_markdown_update is None:
                    break
                await pilot.pause()
            else:
                raise AssertionError("stream markdown append did not settle")
            await pilot.pause()

        markdown = app.query_one("LansCoderMarkdown.streaming", LansCoderMarkdown)
        app._close_stream_segment_for_tool()
        await app.wait_for_stream_finalization(markdown)
        await pilot.pause()

        assert app.is_running
        assert markdown.allow_select is True
        assert markdown._markdown == f"LansCoder:\n\n{''.join(chunks)}"


def test_lanscoder_app_coalesces_stream_chunks_into_one_ui_callback(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)
    scheduled: list[object] = []
    appended: list[str] = []

    monkeypatch.setattr(
        app,
        "_schedule_ui_callback",
        lambda callback, *args: scheduled.append((callback, args)) or True,
    )
    monkeypatch.setattr(app, "_append_stream_text", appended.append)
    monkeypatch.setattr(app, "_complete_working_indicator", lambda: None)

    app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="我"))
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="在"))
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="这里"))

    assert len(scheduled) == 1
    assert appended == []

    callback, args = scheduled.pop()
    callback(*args)

    assert appended == ["我在这里"]


def test_lanscoder_app_discards_coalesced_stream_chunks_after_interrupt(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)
    scheduled: list[object] = []
    appended: list[str] = []

    monkeypatch.setattr(
        app,
        "_schedule_ui_callback",
        lambda callback, *args: scheduled.append((callback, args)) or True,
    )
    monkeypatch.setattr(app, "_append_stream_text", appended.append)
    monkeypatch.setattr(app, "_complete_working_indicator", lambda: None)

    app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="stale"))
    app._discard_stream_deltas()

    callback, args = scheduled.pop()
    callback(*args)

    assert appended == []


def test_lanscoder_app_stream_markdown_append_uses_latest_delta(monkeypatch) -> None:
    output = FakeOutput()
    app = LansCoderApp()
    updates: list[str] = []
    update_results: list[FakeMarkdownUpdateResult] = []
    timers: list[object] = []

    def mount(widget: object) -> None:
        output.mounted.append(widget)
        if isinstance(widget, Markdown):

            def append(markdown: str) -> FakeMarkdownUpdateResult:
                updates.append(markdown)
                result = FakeMarkdownUpdateResult(None)
                update_results.append(result)
                return result

            widget.append = append  # type: ignore[method-assign]

    monkeypatch.setattr(output, "mount", mount)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(
        app,
        "set_timer",
        lambda interval, callback, **kwargs: timers.append(callback) or object(),
    )

    app._append_stream_text("我")
    app._append_stream_text("在")
    app._flush_stream_text()
    app._append_stream_text("这里")
    app._flush_stream_text()

    assert updates == ["LansCoder:\n\n我"]

    update_results[0].finish()
    timers[-1]()

    assert updates == ["LansCoder:\n\n我", "在这里"]


def test_lanscoder_app_does_not_scroll_stream_when_render_is_deferred(
    monkeypatch,
) -> None:
    output = FakeOutput()
    app = LansCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_timer", lambda *args, **kwargs: object())

    app._append_stream_text("我")
    after_initial_render = output.scroll_end_calls
    app._append_stream_text("在")
    app._append_stream_text("这里")

    assert after_initial_render == 1
    assert output.scroll_end_calls == after_initial_render


def test_lanscoder_app_does_not_auto_scroll_stream_when_user_is_reading_history(
    monkeypatch,
) -> None:
    output = FakeOutput()
    output.scroll_y = 1
    output.max_scroll_y = 10
    app = LansCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._append_stream_text("hello")
    app._flush_stream_text()

    assert output.scroll_end_calls == 0


class _LayoutUpdatingFakeOutput(FakeOutput):
    """FakeOutput that simulates Textual's synchronous layout on mount:

    Each mount() grows max_scroll_y (content gets taller) while scroll_y stays
    put, mirroring the real TUI where a new widget pushes the bottom further
    away before the viewport has a chance to follow.
    """

    def __init__(self, growth_per_mount: int = 10) -> None:
        super().__init__()
        self._growth = growth_per_mount

    def mount(self, widget: object) -> None:
        super().mount(widget)
        self.max_scroll_y += self._growth


def test_lanscoder_app_auto_scroll_fires_when_layout_grows_max_scroll_y_on_mount(
    monkeypatch,
) -> None:
    """Regression test: if the user is pinned to the bottom before a widget is
    mounted, auto-scroll must still fire even though the mount grows
    max_scroll_y past the current scroll_y (the race that caused the TUI to
    stop following AI output).
    """
    output = _LayoutUpdatingFakeOutput(growth_per_mount=10)
    output.scroll_y = 10
    output.max_scroll_y = 10  # user is at the bottom
    app = LansCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._ui_line(BlockKind.SYSTEM, "hello")

    assert output.scroll_end_calls == 1
    # After mount, max_scroll_y grew but scroll_y stayed — the old buggy
    # check (scroll_y < max_scroll_y - 1) would have returned without scrolling.
    assert output.max_scroll_y == 20
    assert output.scroll_y == 10


def test_lanscoder_app_auto_scroll_fires_during_stream_when_layout_grows_max_scroll_y(
    monkeypatch,
) -> None:
    """Same race, but via the streaming flush path (the common case during AI
    output): user pinned, widget.update() grows content, scroll_end must still
    be called.
    """
    output = _LayoutUpdatingFakeOutput(growth_per_mount=0)
    output.scroll_y = 10
    output.max_scroll_y = 10
    app = LansCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_timer", lambda *args, **kwargs: object())

    # Use a real-enough markdown widget stand-in: the flush path calls
    # self._stream_text_widget.update(...), so we simulate the content growth
    # by bumping max_scroll_y inside that update.
    class _GrowingWidget:
        def __init__(self, out: _LayoutUpdatingFakeOutput) -> None:
            self._out = out

        def update(self, text: str):
            self._out.max_scroll_y += 10

            class _Result:
                _future = None

            return _Result()

        def append(self, text: str):
            return self.update(text)

    app._stream_text_widget = _GrowingWidget(output)  # type: ignore[attr-defined]
    app._stream_text_buffer = "delta"
    app._stream_rendered_text = ""

    app._flush_stream_text()

    assert output.scroll_end_calls == 1


def test_lanscoder_app_auto_scroll_fires_on_child_row_mount_when_pinned(
    monkeypatch,
) -> None:
    """Regression: mounting a new child row (tool / thinking) grows the output
    pane, and Textual does not follow the bottom automatically. When the user is
    pinned, _refresh_child_row -> _mount_child_row must scroll explicitly.

    Tool-start events in a long task mount a TOOL row with no text flush
    following; without this scroll the pane grows and the user is left looking
    at stale lines.
    """
    output = _LayoutUpdatingFakeOutput(growth_per_mount=4)
    output.scroll_y = 10
    output.max_scroll_y = 10  # user is at the bottom
    app = LansCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)
    monkeypatch.setattr(app, "_query_mounted", lambda *args, **kwargs: output)

    app._begin_active_chat_turn()
    app.projector.start_assistant()
    app.projector.tool_event("call_1", "grep", "started", arguments={"q": "x"})
    block_index = len(app.transcript.blocks) - 1
    app._ensure_stream_block_rows(block_index, app.transcript.blocks[-1])

    assert output.scroll_end_calls == 1
    # mount 增长了内容,但用户贴底,必须依然滚动
    assert output.max_scroll_y == 14
    assert output.scroll_y == 10


def test_lanscoder_app_finalize_scrolls_when_pinned_and_update_grows_content(
    monkeypatch,
) -> None:
    """Regression: _finalize_stream_widget must scroll to the bottom after its
    final widget.update() when the user was pinned, even though the update grows
    max_scroll_y past the current scroll_y. That race froze the TUI after tool
    calls: the next _write_line saw scroll_y < max_scroll_y and skipped scrolling."""
    output = _LayoutUpdatingFakeOutput(growth_per_mount=0)
    output.scroll_y = 10
    output.max_scroll_y = 10  # user is at the bottom
    app = LansCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    class _GrowingWidget:
        def update(self, text: str):
            output.max_scroll_y += 10

            class _Result:
                _future = None

            return _Result()

        def set_selectable(self, _selectable: bool) -> None:
            pass

    app._stream_text_widget = _GrowingWidget()  # type: ignore[attr-defined]
    app._stream_text_buffer = "final"
    app._stream_rendered_text = "final"

    app._finalize_stream_widget()

    assert output.scroll_end_calls == 1
    # The update grew max_scroll_y, but we still scrolled because the user was pinned.
    assert output.max_scroll_y == 20


def test_lanscoder_app_finalize_does_not_scroll_when_user_is_reading_history(
    monkeypatch,
) -> None:
    """Finalization must respect the pin check: if the user scrolled up to read
    history, updating the widget must not yank them back to the bottom."""
    output = _LayoutUpdatingFakeOutput(growth_per_mount=0)
    output.scroll_y = 2
    output.max_scroll_y = 10  # user is reading history, not at the bottom
    app = LansCoderApp()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    class _GrowingWidget:
        def update(self, text: str):
            output.max_scroll_y += 10

            class _Result:
                _future = None

            return _Result()

        def set_selectable(self, _selectable: bool) -> None:
            pass

    app._stream_text_widget = _GrowingWidget()  # type: ignore[attr-defined]
    app._stream_text_buffer = "final"
    app._stream_rendered_text = "final"

    app._finalize_stream_widget()

    assert output.scroll_end_calls == 0


def test_lanscoder_app_records_streaming_assistant_text_in_transcript(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    app._append_stream_text("你")
    app._append_stream_text("好")

    assistant_blocks = [block for block in app.transcript.blocks if block.kind == BlockKind.ASSISTANT]
    assert len(assistant_blocks) == 1
    assert assistant_blocks[0].text == "你好"


def test_lanscoder_app_activity_line_shows_status_only_not_reasoning_content(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    app = LansCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._append_reasoning_text("planning ")
    app._append_reasoning_text("tools")

    thinking_children = [child for block in app.transcript.blocks for child in block.children if child.kind == ChildKind.THINKING]
    assert len(thinking_children) == 1
    assert thinking_children[0].body == "planning tools"
    assert output.mounted == []
    # 推理正文只在折叠子行里;活动区只显示 thinking 状态,不展示内容
    assert activity.updates[0].startswith("thinking [.  ]")
    assert "planning" not in activity.updates[0]
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert activity.updates[1].startswith("thinking [.  ]")
    assert "planning" not in activity.updates[1]
    assert activity.updates[1].rstrip().endswith("0.0s · 0 tools")


def test_lanscoder_app_shows_working_indicator_without_reasoning_delta(
    monkeypatch,
) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._show_working_indicator("planning next step...")
    app._complete_working_indicator()

    assert app.transcript.blocks == []
    assert output.mounted == []
    assert activity.updates[0].startswith("thinking [.  ] planning next step...")
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert activity.updates[1].startswith("streaming [>   ] · response")
    assert activity.updates[1].rstrip().endswith("0.0s · 0 tools")


def test_lanscoder_app_animates_working_indicator(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type(
        "FakeTimer",
        (),
        {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)},
    )()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._show_working_indicator("planning next step...")
    app._advance_working_animation()

    assert app.transcript.blocks == []
    assert activity.updates[-1].startswith("thinking [.. ] planning next step...")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert app._working_timer is timer

    app._complete_working_indicator()

    assert activity.updates[-1].startswith("streaming [>   ] · response")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert timer.stopped is True
    assert app._working_timer is None


def test_lanscoder_app_topbar_omits_reasoning_status(
    monkeypatch,
) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type(
        "FakeTimer",
        (),
        {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)},
    )()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._append_reasoning_text("好的，我来分析。\n先看下目录结构")

    # 下栏 #activity 只显示 thinking 状态,不展示推理正文(正文在折叠子行里)
    assert "好的，我来分析。" not in activity.updates[-1]
    assert activity.updates[-1].startswith("thinking ")
    # 顶栏不再承载任何 reasoning/thinking 状态,只显示 brand + metadata
    topbar_plain = Text.from_markup(app._topbar_text(width=120)).plain
    assert "thinking" not in topbar_plain
    assert "好的，我来分析。" not in topbar_plain


def test_lanscoder_app_streaming_final_response_skips_assistant_display_line(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["hello", "Tool result: echo success: ok"]
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="h"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert sum(isinstance(widget, Markdown) for widget in output.mounted) == 1
    assert [type(widget).__name__ for widget in output.mounted] == [
        "LansCoderMarkdown",
    ]
    assert output.mounted[0].allow_select is True


def test_lanscoder_app_replaces_partial_stream_when_final_response_differs(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    runner.last_display_lines = ["complete ok"]
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="partial"))
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="complete ok"))
    app._restore_stream_event_handler(previous_handler)

    assert [type(widget).__name__ for widget in output.mounted] == ["LansCoderMarkdown"]
    assert output.mounted[0].updates[-1] == "LansCoder:\n\ncomplete ok"
    assert app._stream_text_buffer == "complete ok"


def test_lanscoder_app_stops_streaming_status_after_final_response(monkeypatch) -> None:
    runner = FakeStreamingAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type(
        "FakeTimer",
        (),
        {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)},
    )()
    app = LansCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="hello"))
    app._write_chat_response(
        ChatResponse(
            provider="fake",
            model="fake",
            content="hello",
            usage=TokenUsage(input_tokens=3, output_tokens=5, total_tokens=8),
        )
    )
    app._restore_stream_event_handler(previous_handler)

    assert activity.updates[-1].startswith("done")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert "tok" not in activity.updates[-1]
    assert timer.stopped is True
    assert app._activity_timer is None


def test_lanscoder_app_stream_event_handler_schedules_ui_updates_on_app_thread(
    monkeypatch,
) -> None:
    runner = FakeStreamingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)
    calls: list[tuple[str, str]] = []
    scheduled: list[object] = []

    monkeypatch.setattr(app, "_append_stream_text", lambda text: calls.append(("text", text)))
    monkeypatch.setattr(app, "_complete_working_indicator", lambda: None)
    monkeypatch.setattr(
        app,
        "_schedule_ui_callback",
        lambda callback, *args: scheduled.append((callback, args)) or True,
    )

    previous_handler = app._install_stream_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="hello"))
    app._restore_stream_event_handler(previous_handler)

    assert calls == []
    assert len(scheduled) == 1
    callback, args = scheduled[0]
    callback(*args)
    assert calls == [("text", "hello")]


def test_lanscoder_app_tool_event_handler_schedules_live_status_on_app_thread(
    monkeypatch,
) -> None:
    runner = FakeToolEventAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)
    scheduled: list[object] = []
    output = FakeOutput()
    activity = FakeActivity()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(
        app,
        "_call_ui_thread",
        lambda callback, *args, **kwargs: scheduled.append((callback, args, kwargs)),
    )

    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="started",
            tool_call=ToolCall(id="call_echo", name="echo", arguments={}),
        )
    )
    app._restore_tool_event_handler(previous_handler)

    assert len(scheduled) == 2
    callback, args, kwargs = scheduled[1]
    callback(*args, **kwargs)
    child = next(
        (c for b in app.transcript.blocks for c in b.children if c.kind == ChildKind.TOOL),
        None,
    )
    assert child is not None
    assert child.status == "running"
    assert activity.updates[0].startswith("running [=   ] · echo")


def test_lanscoder_app_updates_activity_line_for_tool_events(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type(
        "FakeTimer",
        (),
        {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)},
    )()
    app = LansCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_tool_event_handler()
    tool_call = ToolCall(id="call_echo", name="echo", arguments={"text": "hello"})
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=tool_call))
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="finished",
            tool_call=tool_call,
            result=ToolResult(name="echo", ok=True, content="hello"),
        )
    )
    app._restore_tool_event_handler(previous_handler)

    assert activity.updates[0].startswith("running [=   ] · echo")
    assert activity.updates[0].rstrip().endswith("0.0s · 1 tool")
    assert activity.updates[1].startswith("thinking [.  ] reading echo result")
    assert activity.updates[1].rstrip().endswith("0.0s · 1 tool")
    assert app._working_timer is timer

    app._advance_working_animation()

    assert activity.updates[-1].startswith("thinking [.. ] reading echo result")
    assert activity.updates[-1].rstrip().endswith("0.0s · 1 tool")


def test_lanscoder_app_stops_post_tool_animation_when_next_tool_starts(
    monkeypatch,
) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type(
        "FakeTimer",
        (),
        {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)},
    )()
    app = LansCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_tool_event_handler()
    first_call = ToolCall(id="call_echo", name="echo", arguments={})
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="finished",
            tool_call=first_call,
            result=ToolResult(name="echo", ok=True, content="hello"),
        )
    )
    runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_ls", name="ls", arguments={})))
    app._restore_tool_event_handler(previous_handler)

    assert timer.stopped is True
    assert app._working_timer is None
    assert activity.updates[-1].startswith("running [=   ] · ls")
    assert activity.updates[-1].rstrip().endswith("0.0s · 1 tool")


def test_lanscoder_app_activity_line_summarizes_parallel_tool_events(
    monkeypatch,
) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    timer = type("FakeTimer", (), {"stop": lambda self: None})()
    app = LansCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="started",
            tool_call=ToolCall(id="call_view_1", name="view", arguments={}),
        )
    )
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="started",
            tool_call=ToolCall(id="call_view_2", name="view", arguments={}),
        )
    )
    app._restore_tool_event_handler(previous_handler)

    assert activity.updates[-1].startswith("running [=   ] · 2 tools running")
    assert activity.updates[-1].rstrip().endswith("0.0s · 2 tools")


def test_lanscoder_app_animates_running_tool_status(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type(
        "FakeTimer",
        (),
        {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)},
    )()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._start_turn_metrics()
    app._show_activity_animation("running", "echo")
    app._advance_activity_animation()

    assert activity.updates[0].startswith("running [=   ] · echo")
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert activity.updates[1].startswith("running [==  ] · echo")
    assert activity.updates[1].rstrip().endswith("0.0s · 0 tools")
    app._advance_activity_animation()
    assert activity.updates[-1].startswith("running [=== ] · echo")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert app._activity_timer is timer

    app._stop_activity_animation()

    assert timer.stopped is True
    assert app._activity_timer is None


def test_lanscoder_app_keeps_elapsed_time_live_after_tool_failure(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type(
        "FakeTimer",
        (),
        {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)},
    )()
    app = LansCoderApp()
    clock = {"now": 100.0}

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)
    monkeypatch.setattr("lanscoder.app.tui.time.monotonic", lambda: clock["now"])

    tool_call = ToolCall(id="call_echo", name="echo", arguments={})
    app._start_turn_metrics()
    app._record_tool_activity(ToolExecutionEvent(kind="started", tool_call=tool_call))
    app._record_tool_activity(
        ToolExecutionEvent(
            kind="finished",
            tool_call=tool_call,
            result=ToolResult(name="echo", ok=False, content="boom"),
        )
    )

    assert activity.updates[-1].startswith("error · echo")
    assert activity.updates[-1].rstrip().endswith("0.0s · 1 tool")
    assert app._activity_timer is timer

    clock["now"] = 102.5
    app._advance_activity_animation()

    assert activity.updates[-1].startswith("error · echo")
    assert activity.updates[-1].rstrip().endswith("2.5s · 1 tool")


def test_lanscoder_app_activity_line_uses_plain_status_text(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._set_activity("running [=   ] · echo")

    renderable = activity.renderables[-1]
    assert getattr(renderable, "plain", "").startswith("running [=   ] · echo")
    assert getattr(renderable, "plain", "").rstrip().endswith("0.0s · 0 tools")
    assert renderable.spans == []


def test_lanscoder_app_activity_metrics_are_pinned_right(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    activity.size = type("Size", (), {"width": 42})()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._start_turn_metrics()
    app._set_activity("running [=   ] · echo")
    first = activity.updates[-1]
    app._set_activity("streaming [>   ] · response")
    second = activity.updates[-1]

    assert first.endswith("0.0s · 0 tools")
    assert second.endswith("0.0s · 0 tools")
    assert first.rfind("0.0s · 0 tools") == second.rfind("0.0s · 0 tools")


def test_lanscoder_app_animates_streaming_status(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type(
        "FakeTimer",
        (),
        {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)},
    )()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._start_turn_metrics()
    app._complete_working_indicator()
    app._advance_activity_animation()

    assert activity.updates[0].startswith("streaming [>   ] · response")
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert activity.updates[1].startswith("streaming [>>  ] · response")
    assert activity.updates[1].rstrip().endswith("0.0s · 0 tools")
    app._advance_activity_animation()
    assert activity.updates[-1].startswith("streaming [>>> ] · response")
    assert activity.updates[-1].rstrip().endswith("0.0s · 0 tools")
    assert app._activity_timer is timer


def test_lanscoder_app_does_not_restart_streaming_status_for_every_token(
    monkeypatch,
) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    timer = type(
        "FakeTimer",
        (),
        {"stopped": False, "stop": lambda self: setattr(self, "stopped", True)},
    )()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "_loop", object())
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: timer)

    app._start_turn_metrics()
    app._complete_working_indicator()
    after_first_token = len(activity.updates)
    app._complete_working_indicator()
    app._complete_working_indicator()

    assert after_first_token == 1
    assert len(activity.updates) == after_first_token
    assert app._activity_timer is timer


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_hides_task_plan_panel_when_session_has_no_plan(
    monkeypatch,
) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    view = SessionView(session_id="sess_without_plan")
    session = FakeSession()
    monkeypatch.setattr(session, "rebuild_view", lambda: view)
    app = LansCoderApp(current_session=session)

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    await app._replay_current_session()

    assert panel.updates == [""]
    assert "hidden" in panel.classes


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_replays_linear_task_plan_from_current_session_view(
    monkeypatch,
) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    view = SessionView(
        session_id="sess_linear_replay",
        task_plan=TaskPlan(
            mode="linear",
            revision=1,
            tasks=(
                Task(id="test", content="恢复测试", status="in_progress", order=20),
                Task(id="inspect", content="恢复代码", status="completed", order=10),
            ),
        ),
    )
    session = FakeSession()
    monkeypatch.setattr(session, "rebuild_view", lambda: view)
    app = LansCoderApp(current_session=session)

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    await app._replay_current_session()

    assert panel.updates[-1] == "Task Plan · linear\n[✓] 恢复代码\n[~] 恢复测试"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_replays_dag_task_plan_from_current_session_view(
    monkeypatch,
) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    view = SessionView(
        session_id="sess_dag_replay",
        task_plan=TaskPlan(
            mode="dag",
            revision=1,
            tasks=(
                Task(id="a", content="调研 A", status="completed", order=10),
                Task(id="b", content="调研 B", status="in_progress", order=20),
                Task(id="c", content="汇总", depends_on=("a", "b"), order=30),
            ),
        ),
    )
    session = FakeSession()
    monkeypatch.setattr(session, "rebuild_view", lambda: view)
    app = LansCoderApp(current_session=session)

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    await app._replay_current_session()

    assert panel.updates[-1] == ("Task Plan · dag\nLevel 0 · parallel\n  [✓] 调研 A (a)\n  [~] 调研 B (b)\nLevel 1\n  [!] 汇总 (c) · depends on: a, b")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_refreshes_task_plan_immediately_after_successful_plan_tool(
    monkeypatch,
) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    panel = FakeTaskPlanPanel()
    session = FakeSession()
    monkeypatch.setattr(
        session,
        "rebuild_view",
        lambda: SessionView(
            session_id="sess_live_plan",
            task_plan=TaskPlan(
                mode="linear",
                revision=1,
                tasks=(Task(id="inspect", content="来自会话", status="in_progress"),),
            ),
        ),
    )
    app = LansCoderApp(chat_runner=runner, current_session=session)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="finished",
            tool_call=ToolCall(id="call_create", name="task_create", arguments={}),
            result=ToolResult(
                name="task_create",
                ok=True,
                content="created",
                data={"snapshot": {"tasks": [{"content": "不是展示来源"}]}},
            ),
        )
    )
    app._restore_tool_event_handler(previous_handler)

    assert panel.updates[-1] == "Task Plan · linear\n[~] 来自会话"


def test_lanscoder_app_skips_same_task_plan_revision_and_updates_existing_panel(
    monkeypatch,
) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    initial = TaskPlan(
        mode="linear",
        revision=1,
        tasks=(Task(id="inspect", content="读代码", status="in_progress"),),
    )
    updated = TaskPlan(
        mode="linear",
        revision=2,
        tasks=(Task(id="inspect", content="读代码", status="completed"),),
    )

    app._render_task_plan_panel(initial)
    app._render_task_plan_panel(initial)
    app._render_task_plan_panel(updated)

    assert panel.updates == [
        "Task Plan · linear\n[~] 读代码",
        "Task Plan · linear\n[✓] 读代码",
    ]
    assert app.task_plan_panel_state.last_rendered_revision == 2


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_clear_output_clears_and_hides_rendered_task_plan_panel(
    monkeypatch,
) -> None:
    output = FakeOutput()
    panel = FakeTaskPlanPanel()
    app = LansCoderApp()

    def query_one(selector, *args, **kwargs):
        if selector == "#task-plan-panel":
            return panel
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    monkeypatch.setattr(app, "is_mounted", True)
    app._render_task_plan_panel(
        TaskPlan(
            mode="linear",
            revision=1,
            tasks=(Task(id="inspect", content="读代码", status="in_progress"),),
        )
    )

    await app._clear_output()

    assert panel.updates[-1] == ""
    assert "hidden" in panel.classes
    assert app.task_plan_panel_state.last_rendered_revision is None


def test_lanscoder_app_activity_change_updates_activity_line_only(monkeypatch) -> None:
    output = FakeOutput()
    activity = FakeActivity()
    topbar = FakeTopbar()
    app = LansCoderApp(current_session=FakeSession())
    monkeypatch.setattr(app, "is_mounted", True)
    monkeypatch.setattr(app, "_topbar_width", lambda: 120)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        if selector == "#topbar":
            return topbar
        return output

    monkeypatch.setattr(app, "query_one", query_one)

    app._set_activity("waiting · permission")

    assert activity.updates[0].startswith("waiting · permission")
    assert activity.updates[0].rstrip().endswith("0.0s · 0 tools")
    assert app._activity_text == "waiting · permission"
    assert topbar.updates == []


def test_lanscoder_app_live_tool_events_filter_final_tool_summary(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="started",
            tool_call=ToolCall(id="call_echo", name="echo", arguments={}),
        )
    )
    runner.last_display_lines = [
        'Tool call: echo {"text": "hello"}',
        "Tool result: echo success: hello",
        "done",
    ]
    app._write_chat_response(ChatResponse(provider="fake", model="fake", content="done"))
    app._restore_tool_event_handler(previous_handler)

    rendered = "\n".join(output.lines)
    tool_children = [child for block in app.transcript.blocks for child in block.children if child.kind == ChildKind.TOOL]
    assert len(tool_children) == 1
    assert tool_children[0].label == "tool echo"
    assert tool_children[0].status == "running"
    assert "Tool call:" not in rendered
    assert "Tool result:" not in rendered
    # 完成回合与 render_block_into 一致:[工具子行, 正文 markdown] 按模型顺序;
    # 正文复用占位 widget,因此不残留空占位,也不产生重复 markdown。
    assert [type(widget).__name__ for widget in output.mounted] == [
        "ChildRow",
        "LansCoderMarkdown",
    ]
    tool_row, answer_markdown = output.mounted
    assert "tool echo" in str(tool_row.content)
    assert answer_markdown.allow_select is True
    assert answer_markdown.updates[-1] == "LansCoder:\n\ndone"
    assert not answer_markdown.has_class("streaming")


def test_lanscoder_app_starts_new_stream_block_after_tool_event(monkeypatch) -> None:
    runner = FakeToolEventAsyncChatRunner()
    runner.stream_event_handler = lambda event: None
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_stream_handler = app._install_stream_event_handler()
    previous_tool_handler = app._install_tool_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="我先看看。"))
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="started",
            tool_call=ToolCall(id="call_echo", name="echo", arguments={}),
        )
    )
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="看完了。"))
    app._restore_tool_event_handler(previous_tool_handler)
    app._restore_stream_event_handler(previous_stream_handler)

    mounted_types = [type(widget).__name__ for widget in output.mounted]
    assert mounted_types == ["LansCoderMarkdown", "ChildRow", "LansCoderMarkdown"]
    first_markdown, tool_row, second_markdown = output.mounted
    assert "tool echo" in str(tool_row.content)
    assert first_markdown.allow_select is True
    assert second_markdown.allow_select is False
    assert first_markdown.updates[-1] == "LansCoder:\n\n我先看看。"
    assert second_markdown.updates[-1] == "LansCoder:\n\n看完了。"
    assert (
        next(
            (c for b in app.transcript.blocks for c in b.children if c.kind == ChildKind.TOOL),
            None,
        ).status
        == "running"
    )


def test_lanscoder_app_renders_bypass_prewrite_review_without_permission_prompt(
    monkeypatch,
) -> None:
    runner = FakeToolEventAsyncChatRunner()
    output = FakeOutput()
    activity = FakeActivity()
    app = LansCoderApp(chat_runner=runner)

    def query_one(selector, *args, **kwargs):
        if selector == "#activity":
            return activity
        return output

    monkeypatch.setattr(app, "query_one", query_one)
    previous_handler = app._install_tool_event_handler()
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="prewrite_review",
            tool_call=ToolCall(id="call_write", name="write", arguments={}),
            prewrite_review={
                "tool_name": "write",
                "summary": {
                    "created_files": 1,
                    "modified_files": 0,
                    "deleted_files": 0,
                    "added_lines": 1,
                    "removed_lines": 0,
                },
                "files": [
                    {
                        "path": "README.md",
                        "operation": "create",
                        "diff": "--- /dev/null\n+++ b/README.md\n@@ -0,0 +1 @@\n+hello",
                        "added_lines": 1,
                        "removed_lines": 0,
                    }
                ],
            },
        )
    )
    app._restore_tool_event_handler(previous_handler)

    rendered = "\n".join(output.lines)
    assert "README.md" in rendered
    assert "+hello" in rendered
    assert "允许" not in rendered
    assert "permission" not in rendered.lower()
    assert app._activity_text != "waiting · permission"


def test_plain_static_renders_tool_arguments_with_markup_characters_as_text() -> None:
    content = 'tool shell running\n  正在调用工具：shell {"cmd": "python -m pytest tests/test_app_tui.py -q", "args": ["-q"]}'
    widget = _plain_static(content, classes="message tool-message tool-running")

    rendered = widget.render()
    plain = rendered.plain if isinstance(rendered, Text) else str(rendered)

    assert plain == content


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_displays_live_tool_status_during_turn() -> None:
    runner = FakeToolEventAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        output = app.query_one("#output")
        text = "\n".join(str(getattr(widget, "content", getattr(widget, "renderable", ""))) for widget in output.query("Static"))

    assert "[>] tool echo" in text
    assert "✓" in text
    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_recalls_input_history_with_arrow_keys() -> None:
    runner = FakeAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"first")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*"second")
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("up")
        input_widget = app.query_one("#input")
        assert input_widget.text == "second"

        await pilot.press("up")
        assert input_widget.text == "first"

        await pilot.press("down")
        assert input_widget.text == "second"

        await pilot.press("down")
        assert input_widget.text == ""

    assert runner.inputs == ["first", "second"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_displays_pending_permission_prompt_inline() -> None:
    runner = FakePermissionWaitingRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        app._write_chat_response(ChatResponse(provider="fake", model="fake", content="等待权限确认。"))
        await pilot.pause()
        # 点击区已删除:权限提示作为消息块写进输出区
        rendered = "\n".join(str(w.render()) for w in app.query("#output Static"))
        assert "Review before writing · 1 file · +1 -1" in rendered
        assert "-old" in rendered
        assert "+new" in rendered
        assert "permission requested" in rendered
        assert "[1] deny  [2] allow once  [3] allow always" in rendered
        assert app._activity_text == "waiting · permission"
        assert [block.kind for block in app.transcript.blocks] == [BlockKind.ASSISTANT]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_displays_chat_errors_from_worker() -> None:
    runner = FailingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == ["hello"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_queues_input_even_without_guidance_support() -> None:
    """Even when the chat runner lacks add_guidance, user input is queued
    and delivered after the current turn finishes."""
    runner = BlockingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"first")
        await pilot.press("enter")
        await runner.started.wait()
        await pilot.click("#input")
        await pilot.press(*"second")
        await pilot.press("enter")
        await pilot.pause()
        runner.release.set()
        await pilot.pause()

    assert runner.inputs == ["first", "second"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_completed_idle_auto_turns_without_session_write() -> None:
    """A completed subagent wakes an idle main agent without a redundant
    short-summary session write (the loop delivers the full result itself)."""
    runner = FakeSubagentRunner()
    app = LansCoderApp(chat_runner=runner)
    job = BackgroundJob(id="bg_0001", tool_name="delegate", label="researcher", status="completed")

    async with app.run_test() as pilot:
        app._handle_subagent_completed(job)
        await pilot.pause()

    assert runner.current_session.session.writes == []
    assert runner.inputs == []
    assert runner.nudges == [True]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_completed_busy_does_not_write_or_auto_turn() -> None:
    """When a turn is running, a subagent completion neither writes to the
    session nor starts an extra reporting turn — the running turn drains it."""
    runner = FakeSubagentRunner()
    app = LansCoderApp(chat_runner=runner)
    job = BackgroundJob(id="bg_0001", tool_name="delegate", label="researcher", status="completed")

    async with app.run_test() as pilot:
        app._chat_busy = True
        app._handle_subagent_completed(job)
        await pilot.pause()

    assert runner.current_session.session.writes == []
    assert runner.inputs == []
    assert runner.nudges == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_wakes_main_agent_for_pending_completions() -> None:
    """A turn that ends with undelivered background completions auto-starts a
    reporting turn so the result is not stranded until the next user message."""
    job = BackgroundJob(id="bg_0001", tool_name="delegate", label="researcher", status="completed")
    runner = FakeSubagentRunner(pending=[job])
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()

    assert runner.inputs == []
    assert runner.nudges == [True]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_does_not_wake_when_no_pending_completions() -> None:
    """Without undelivered completions, a normal turn end starts nothing new."""
    runner = FakeSubagentRunner(pending=[])
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()

    assert runner.inputs == []
    assert runner.nudges == []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_routes_permission_answer_to_resume() -> None:
    runner = FakePermissionResumeRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"2")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == []
    assert runner.resumes == [("perm_write", "allow_once")]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_rejects_permission_text_alias_with_hint() -> None:
    runner = FakePermissionResumeRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"allow once")
        await pilot.press("enter")
        await pilot.pause()

        # 严格 1/2/3:文字别名不识别,只写提示行,不恢复回合
        assert runner.resumes == []
        rendered = "\n".join(str(w.render()) for w in app.query("#output Static"))
        assert "只能输入 1/2" in rendered


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_routes_ask_user_answer_to_resume() -> None:
    runner = FakeAskUserResumeRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"2")
        await pilot.press("enter")
        await pilot.pause()

    # ask_user 与权限统一走 resume 协议；序号 "2" 规范化为选项 label "prod"。
    assert runner.inputs == []
    assert runner.resumes == [("ask_env", "prod")]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_routes_permission_rejection_feedback_to_resume() -> None:
    runner = FakePermissionResumeRunner()
    # reject: 反馈 仅在 prewrite_review 场景生效
    runner.last_pending_input.payload["prewrite_review"] = {
        "tool_name": "edit",
        "files": [
            {
                "path": "README.md",
                "operation": "modify",
                "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new",
                "added_lines": 1,
                "removed_lines": 1,
            }
        ],
        "summary": {"added_lines": 1, "removed_lines": 1},
        "error": None,
    }
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"reject: 请保留原标题")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == []
    assert runner.resumes == [("perm_write", "reject_with_feedback: 请保留原标题")]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_lanscoder_app_permission_resume_keeps_same_active_turn_metrics(
    monkeypatch,
) -> None:
    runner = FakePermissionMidTurnRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"write readme")
        await pilot.press("enter")
        await pilot.pause()

        active_turn = app._active_chat_turn
        assert active_turn is not None
        assert app._chat_busy is False
        started_at = app._turn_started_at
        assert started_at > 0

        app._turn_tool_count = 2
        await pilot.press(*"2")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == ["write readme"]
    assert runner.resumes == [("perm_write", "allow_once")]
    assert app._active_chat_turn is None
    assert app._turn_started_at == started_at
    assert app._turn_tool_count == 2


class _RecordingSlashCommands:
    """记录被提交的斜杠命令;all_commands 供联想栏使用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def all_commands(self):
        return [("/help", "显示帮助"), ("/model", "切换模型")]

    def handle(self, text: str) -> CommandResult:
        self.calls.append(text)
        return CommandResult(handled=True, output=f"handled:{text}")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_slash_suggest_appears_above_input_without_moving_it() -> None:
    class _ManyCommands(_RecordingSlashCommands):
        def all_commands(self):
            return [
                ("/help", "显示帮助"),
                ("/model", "切换模型"),
                ("/resume", "恢复会话"),
                ("/skills", "技能列表"),
                ("/new", "新建会话"),
                ("/compact", "压缩上下文"),
                ("/sessions", "会话列表"),
                ("/fork", "派生会话"),
            ]

    app = LansCoderApp(command_handler=_ManyCommands(), config=LansCoderTuiConfig(title="TestCoder"))
    async with app.run_test(size=(80, 24)) as pilot:
        input_w = app.query_one("#input")
        suggest = app.query_one("#slash-suggest", SlashSuggest)
        assert suggest.has_class("--visible") is False
        before = input_w.region
        await pilot.click("#input")
        await pilot.press("/")
        await pilot.pause()
        assert suggest.has_class("--visible") is True
        assert input_w.region == before
        assert suggest.region.bottom <= input_w.region.y
        # 联想栏完全可见(未被终端底边裁剪),输入框也完整可见
        assert suggest.region.bottom <= app.screen.size.height
        assert input_w.region.y + input_w.region.height <= app.screen.size.height


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_tool_split_keeps_tail_buffer_without_duplicating_pre_tool_text() -> None:
    app = LansCoderApp()

    async with app.run_test():
        app._append_stream_text("before the tool")
        first = app.query_one("LansCoderMarkdown.streaming", LansCoderMarkdown)
        app._close_stream_segment_for_tool()
        await app.wait_for_stream_finalization(first)
        assert first._markdown == "LansCoder:\n\nbefore the tool"
        app.projector.tool_event("call_t", "grep", "started")

        app._stream_text_started = True
        app._live_tool_events_seen = True
        app._append_stream_text("after the tool")
        second = list(app.query("LansCoderMarkdown.streaming"))[-1]
        assert second is not first

        app._write_chat_response(ChatResponse(provider="fake", model="fake", content="before the tool after the tool"))
        await app.wait_for_stream_finalization(second)
        # 工具前的文本只出现一次;尾部块不被完整 content 覆盖
        assert first._markdown == "LansCoder:\n\nbefore the tool"
        assert second._markdown == "LansCoder:\n\nafter the tool"
        assert [block.text for block in app.transcript.blocks if block.kind == BlockKind.ASSISTANT] == ["before the toolafter the tool"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_composer_enter_completes_highlighted_suggestion_after_arrow_move() -> None:
    handler = _RecordingSlashCommands()
    app = LansCoderApp(command_handler=handler)
    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press("/")
        await pilot.press("m")
        suggest = app.query_one("#slash-suggest", SlashSuggest)
        assert suggest.has_class("--visible")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert handler.calls == ["/model"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_composer_enter_without_arrow_keeps_typed_text() -> None:
    handler = _RecordingSlashCommands()
    app = LansCoderApp(command_handler=handler)
    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press("/")
        await pilot.press("m")
        await pilot.press("enter")
        await pilot.pause()
        # 未手动移动高亮时,Enter 保持提交原输入文本
        assert handler.calls == ["/m"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_keeps_selected_row_beyond_cap_visible() -> None:
    app = LansCoderApp(chat_runner=_PanelRunner(count=4))
    async with app.run_test(size=(120, 40)) as pilot:
        app._subagent_selected = "bg_0003"
        app._subagent_select_mode = True
        app._refresh_subagent_progress()
        await pilot.pause()
        panel = app.query_one("#subagent-panel")
        statics = list(panel.query("Static"))
        row_ids = [s.id for s in statics if s.id.startswith("subagent-row-")]
        # 前 3 行之外被选中的第 4 行仍然保持渲染
        assert "subagent-row-bg_0003" in row_ids
        selected = [s for s in statics if s.has_class("selected")]
        assert [s.id for s in selected] == ["subagent-row-bg_0003"]
        # 4 行全部可见,无剩余,不显示 footer
        assert len(row_ids) == 4
        assert all("还有" not in s.content for s in statics)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_subagent_panel_rows_rebuild_in_order_above_footer_and_hint() -> None:
    manager = _PanelManager(count=2)
    runner = _PanelRunner(count=0)
    runner.background_manager = manager
    runner._foreground = None
    app = LansCoderApp(chat_runner=runner)
    async with app.run_test(size=(120, 40)) as pilot:
        app._refresh_subagent_progress()
        await pilot.pause()
        panel = app.query_one("#subagent-panel")
        statics = list(panel.query("Static"))
        assert [s.id for s in statics] == [
            "subagent-row-bg_0000",
            "subagent-row-bg_0001",
            "subagent-hint",
        ]
        # 新任务出现在运行中,刷新后仍按行在前、提示在后的顺序
        manager._jobs.append(_PanelJob(created_at=200.0, label="researcher2", job_id="bg_0002"))
        manager._jobs.append(_PanelJob(created_at=201.0, label="researcher3", job_id="bg_0003"))
        app._refresh_subagent_progress()
        await pilot.pause()
        statics = list(panel.query("Static"))
        assert [s.id for s in statics] == [
            "subagent-row-bg_0000",
            "subagent-row-bg_0001",
            "subagent-row-bg_0002",
            "subagent-footer",
            "subagent-hint",
        ]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_stream_finalization_releases_widget_reference_after_completion() -> None:
    app = LansCoderApp()

    async with app.run_test():
        app._append_stream_text("final answer")
        markdown = app.query_one("LansCoderMarkdown.streaming", LansCoderMarkdown)
        assert markdown not in app._stream_finalizations
        app._close_stream_segment_for_tool()
        await app.wait_for_stream_finalization(markdown)
        # finalize 完成后映射不再持有该 widget,widget 可随 DOM 移除被回收
        assert markdown not in app._stream_finalizations


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_stream_finalization_repeated_close_does_not_render_twice_under_loop(
    monkeypatch,
) -> None:
    app = LansCoderApp()

    async with app.run_test():
        app._append_stream_text("final answer")
        markdown = app.query_one("LansCoderMarkdown.streaming", LansCoderMarkdown)
        calls = []
        original_update = markdown.update

        def count_update(source: object) -> object:
            calls.append(source)
            return original_update(source)

        monkeypatch.setattr(markdown, "update", count_update)
        app._close_stream_segment_for_tool()
        await app.wait_for_stream_finalization(markdown)
        app._close_stream_segment_for_tool()
        await app.wait_for_stream_finalization(markdown)

        # 挂载初始渲染可能产生空 update,但不能出现第二次 finalize 渲染
        assert calls.count("LansCoder:\n\nfinal answer") == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_ask_user_prompt_written_inline_in_output() -> None:
    runner = FakeAskUserResumeRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        app._write_pending_input()
        await pilot.pause()
        rendered = "\n".join(str(w.render()) for w in app.query("#output Static"))
        assert "Which environment?" in rendered
        assert "[1] dev" in rendered
        assert "[2] prod" in rendered


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_permission_invalid_typed_answer_writes_hint_without_resuming() -> None:
    runner = FakePermissionResumeRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"no such option")
        await pilot.press("enter")
        await pilot.pause()
        assert runner.resumes == []
        assert app._chat_busy is False
        # 非法答案只在输出区补一条提示,不写入新的 transcript 块
        assert [block.kind for block in app.transcript.blocks] == [BlockKind.USER]
        rendered = "\n".join(str(w.render()) for w in app.query("#output Static"))
        assert "只能输入 1/2" in rendered


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_child_row_click_toggles_collapsed_tool_child() -> None:
    app = LansCoderApp()

    async with app.run_test() as pilot:
        block = app.transcript.add_block(BlockKind.ASSISTANT, "reply")
        block.children.append(ChildItem(ChildKind.TOOL, "c1", "tool read auth.py", status="success", body="ok 200"))
        app._rerender_transcript()
        await pilot.pause()
        row = app.query_one("#child-0-c1", ChildRow)
        child = app.transcript.blocks[0].children[0]
        assert child.expanded is False
        assert "[>] tool read auth.py" in str(row.content)
        assert not row.has_class("expanded")
        assert row.size.height == 1

        await pilot.click(row)
        await pilot.pause()

        assert child.expanded is True
        assert "tool: tool read auth.py" in str(row.content)
        assert "ok 200" in str(row.content)
        assert row.has_class("expanded")
        assert row.size.height > 1

        await pilot.click(row)
        await pilot.pause()

        assert child.expanded is False
        assert "✓" in str(row.content)
        assert not row.has_class("expanded")
        assert row.size.height == 1


class _FakeDenyFlowRunner(FakeChatRunner):
    """同时具备 stream/tool 事件挂点的假 runner,用于驱动拒绝后的事件流。"""

    def __init__(self) -> None:
        super().__init__()
        self.stream_event_handler = lambda event: None
        self.tool_event_handler = lambda event: None


def _output_child_classes(app):
    return [type(w).__name__ for w in app.query_one("#output").children]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_deny_resume_thinking_row_mounts_before_answer_text() -> None:
    """拒绝后新建的 THINKING 行必须挂在正文 markdown 之前,而非 output 末尾。

    用户拒绝后:denied 工具事件先挂工具行与流式 markdown 占位,随后
    reasoning 建 thinking 子行、answer 文本填充占位;修复前 thinking 被
    append 到末尾(markdown 之下,即用户看到的"回答下方两个 thought")。
    """
    runner = _FakeDenyFlowRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        app.projector.start_user("run python")
        token = app._begin_active_chat_turn()
        app._install_stream_event_handler(token)
        app._install_tool_event_handler(token)
        await pilot.pause()

        # 权限请求时刻(UI 忽略 permission_requested,不产生工具行)
        runner.tool_event_handler(
            ToolExecutionEvent(
                kind="permission_requested",
                tool_call=ToolCall(id="call_py", name="python", arguments={}),
            )
        )
        await pilot.pause()

        # 拒绝后重装事件处理器(等价 _resume_permission_turn),denied 先到
        token2 = app._resume_active_chat_turn()
        app._install_stream_event_handler(token2)
        app._install_tool_event_handler(token2)
        runner.tool_event_handler(
            ToolExecutionEvent(
                kind="denied",
                tool_call=ToolCall(id="call_py", name="python", arguments={}),
                result=ToolResult(name="python", ok=False, content="用户拒绝"),
            )
        )
        await pilot.pause()
        runner.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="用户拒了,改走本地路径"))
        await pilot.pause()
        runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="好的,不运行了。"))
        await pilot.pause()
        await pilot.pause()

        children = list(app.query_one("#output").children)
        kinds = [type(w).__name__ for w in children]
        rendered = [str(w.render()) for w in children]
        tool_at = next(i for i, k in enumerate(kinds) if k == "ChildRow" and "✕" in rendered[i])
        thought_at = next(i for i, k in enumerate(kinds) if k == "ChildRow" and "Thought" in rendered[i])
        markdown_at = kinds.index("LansCoderMarkdown")
        # 正确顺序:工具失败行 < thinking 行 < 回答正文
        assert tool_at < thought_at < markdown_at


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_live_turn_mounts_thinking_row_above_markdown() -> None:
    """Reasoning finalized by the first stream text renders [thinking row, markdown].

    The thinking row mounts on the first stream-text mount and lands above the
    block's text widget; finalize switches its fold line to "Thought for".
    """
    runner = FakeStreamingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        app._dismiss_welcome()
        previous_stream_handler = app._install_stream_event_handler()

        runner.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="think step one"))
        await pilot.pause()

        output = app.query_one("#output")
        assert list(output.children) == []
        thinking_child = next(
            (c for b in app.transcript.blocks for c in b.children if c.kind == ChildKind.THINKING),
            None,
        )
        assert thinking_child is not None
        assert thinking_child.body == "think step one"
        assert thinking_child.finished is False

        runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="hello"))
        await pilot.pause()

        mounted_types = [type(widget).__name__ for widget in output.children]
        assert mounted_types == ["ChildRow", "LansCoderMarkdown"]
        thinking_row = app.query_one("#child-0-t0", ChildRow)
        assert thinking_child.finished is True
        assert thinking_child.duration_seconds is not None
        assert "Thought for" in str(thinking_row.content)
        assert thinking_row.size.height >= 1
        app._restore_stream_event_handler(previous_stream_handler)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_live_turn_tool_row_before_text_lands_above_markdown() -> None:
    """A tool 'started' arriving before any stream text renders above the text widget.

    Expanded row shows the full tool call and result.
    """
    runner = FakeToolEventAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        app._dismiss_welcome()
        previous_tool_handler = app._install_tool_event_handler()

        runner.tool_event_handler(
            ToolExecutionEvent(
                kind="started",
                tool_call=ToolCall(id="call_echo", name="echo", arguments={}),
            )
        )
        await pilot.pause()

        output = app.query_one("#output")
        mounted_types = [type(widget).__name__ for widget in output.children]
        assert mounted_types == ["ChildRow"]
        tool_row = app.query_one("#child-0-call_echo", ChildRow)
        assert "[>] tool echo" in str(tool_row.content)
        assert tool_row.size.height >= 1

        runner.tool_event_handler(
            ToolExecutionEvent(
                kind="finished",
                tool_call=ToolCall(id="call_echo", name="echo", arguments={}),
                result=ToolResult(name="echo", ok=True, content="hello"),
            )
        )
        await pilot.pause()
        tool_child = app.transcript.blocks[0].children[0]
        assert tool_child.status == "success"
        assert tool_child.body == "hello"
        assert "✓" in str(tool_row.content)

        await pilot.click(tool_row)
        await pilot.pause()
        assert "Tool call: echo" in str(tool_row.content)
        assert "Tool result: hello" in str(tool_row.content)
        await pilot.click(tool_row)
        await pilot.pause()

        app._append_stream_text("done")
        await pilot.pause()

        markdown = output.children[1]
        assert app._stream_text_widget is markdown
        assert app._stream_text_buffer == "done"
        mounted_types = [type(widget).__name__ for widget in output.children]
        assert mounted_types == ["ChildRow", "LansCoderMarkdown"]
        app._restore_tool_event_handler(previous_tool_handler)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_live_turn_interleaves_tool_below_preceding_text() -> None:
    """thinking → text → tool → 续文 在输出区保持事件时间序。

    工具子行挂在它触发前的正文段下方,不被统一提到所有正文之上:回归
    "好的,我开一个子agent" 之后触发的 tool 应显示在这段文字下面。
    """
    runner = FakeStreamingAsyncChatRunner()
    runner.tool_event_handler = lambda event: None
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        app._dismiss_welcome()
        previous_stream_handler = app._install_stream_event_handler()
        previous_tool_handler = app._install_tool_event_handler()

        runner.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="decide"))
        runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="好的，我开一个子agent"))
        await pilot.pause()
        runner.tool_event_handler(
            ToolExecutionEvent(
                kind="started",
                tool_call=ToolCall(id="call_spawn", name="read", arguments={}),
            )
        )
        runner.tool_event_handler(
            ToolExecutionEvent(
                kind="finished",
                tool_call=ToolCall(id="call_spawn", name="read", arguments={}),
                result=ToolResult(name="read", ok=True, content="spawned ok"),
            )
        )
        await pilot.pause()
        runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="最后进行回复"))
        await pilot.pause()

        output = app.query_one("#output")
        mounted_types = [type(widget).__name__ for widget in output.children]
        assert mounted_types == ["ChildRow", "LansCoderMarkdown", "ChildRow", "LansCoderMarkdown"]
        thinking_row, first_text, tool_row, second_text = output.children
        assert app.transcript.blocks[0].children[0].kind == ChildKind.THINKING
        assert "Thought for" in str(thinking_row.content)
        assert isinstance(tool_row, ChildRow)
        assert "[>] tool read" in str(tool_row.content)
        assert app._stream_text_widget is second_text
        app._restore_stream_event_handler(previous_stream_handler)
        app._restore_tool_event_handler(previous_tool_handler)


def _recount_messages(*, turn: int) -> list:
    from types import SimpleNamespace

    from tests.test_context_compaction_pipeline import _message, _tool_call, _tool_result

    return [
        _message("u1", role="user", kind="text", content="你好", created_turn=turn),
        SimpleNamespace(
            role="assistant",
            id="a1",
            parts=[SimpleNamespace(kind="text", content="我来看看", metadata={})],
            metadata={"diagnostics": {"reasoning": "分析中"}},
        ),
        _tool_call("c1", "read", {"path": "a.py"}),
        _tool_result("c1", "read", content="ok 200"),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_replay_multi_turn_session_mounts_unique_child_ids() -> None:
    """/recall 重放一个含 thinking + tool 的会话,子行 id 必须唯一。

    回归:重放后迟到的挂载(旧回合回调、重复 render)不得撞出 DuplicateIds。
    """
    view = SessionView(session_id="sess_recall", messages=_recount_messages(turn=1))
    session = FakeSession()
    session.rebuild_view = lambda: view
    app = LansCoderApp(current_session=session)

    async with app.run_test() as pilot:
        await app._replay_current_session()
        await pilot.pause()
        ids = [child.id for child in app.query_one("#output").children if child.id]
        assert len(ids) == len(set(ids)), ids
        assert any(child.id == "child-1-t0" for child in app.query_one("#output").children)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_double_render_block_into_does_not_duplicate_child_rows() -> None:
    """同一块重复渲染(如双触发重放)对子行是幂等刷新,不重复插入。"""
    view = SessionView(session_id="sess_recall", messages=_recount_messages(turn=1))
    session = FakeSession()
    session.rebuild_view = lambda: view
    app = LansCoderApp(current_session=session)

    async with app.run_test() as pilot:
        await app._replay_current_session()
        await pilot.pause()
        output = app.query_one("#output")
        block = app.transcript.blocks[1]
        app.render_block_into(block, 1)
        await pilot.pause()
        ids = [child.id for child in output.children if child.id]
        assert len(ids) == len(set(ids)), ids
        assert ids.count("child-1-t0") == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_picker_replace_command_block_preserves_child_rows() -> None:
    """picker 上下移动只就地更新命令块,不重建 transcript 子行。

    回归:整树 remove+remount 撞上 Textual 异步 prune 会让 thinking/tool
    子行在 picker 打开与按键时消失;就地更新命令块则 transcript 原样。
    """
    view = SessionView(session_id="sess_recall", messages=_recount_messages(turn=1))
    session = FakeSession()
    session.rebuild_view = lambda: view
    app = LansCoderApp(current_session=session)

    async with app.run_test() as pilot:
        app._dismiss_welcome()
        await app._replay_current_session()
        await pilot.pause()
        output = app.query_one("#output")
        assert "child-1-t0" in [w.id for w in output.children if w.id]
        assert "child-1-c1" in [w.id for w in output.children if w.id]

        # /recall bare:先落一条 COMMAND 行,再被 picker 输出就地替换多次(模拟上下选择)
        app._ui_line(BlockKind.COMMAND, "/recall")
        await pilot.pause()
        app._replace_last_command_output("picker: 1. turn one")
        await pilot.pause()
        app._replace_last_command_output("picker: 2. turn two")
        await pilot.pause()

        ids = [w.id for w in app.query_one("#output").children if w.id]
        assert ids.count("child-1-t0") == 1, ids
        assert "child-1-c1" in ids
        assert app.transcript.find_last_command_block().text == "picker: 2. turn two"
        assert ids.count(ids[ids.index("child-1-t0")]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_stale_tool_event_after_recall_epoch_is_dropped() -> None:
    """清理/重放后到达的旧回合工具事件按 epoch 丢弃,不污染新 transcript。"""
    view = SessionView(session_id="sess_recall", messages=_recount_messages(turn=1))
    session = FakeSession()
    session.rebuild_view = lambda: view
    runner = FakeToolEventAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner, current_session=session)

    async with app.run_test() as pilot:
        app._dismiss_welcome()
        previous_tool_handler = app._install_tool_event_handler()
        runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_x", name="echo", arguments={})))
        await pilot.pause()
        assert any(c.key == "call_x" for b in app.transcript.blocks for c in b.children)

        await app._replay_current_session()
        await pilot.pause()
        tools_after_replay = [c.key for b in app.transcript.blocks for c in b.children if c.kind == ChildKind.TOOL]

        runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="call_stale", name="echo", arguments={})))
        await pilot.pause()
        tools_final = [c.key for b in app.transcript.blocks for c in b.children if c.kind == ChildKind.TOOL]
        assert "call_stale" not in tools_final
        assert tools_final == tools_after_replay
        app._restore_tool_event_handler(previous_tool_handler)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_non_streaming_turn_completion_mounts_child_rows_then_markdown() -> None:
    """A tool arriving before any text renders [child rows, markdown] in arrival order.

    The completion markdown must reuse the block's markdown widget (the empty
    placeholder mounted for live tool rows), so no empty placeholder survives.
    """
    runner = FakeToolEventAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()

        output = app.query_one("#output")
        children = list(output.children)
        assert [type(widget).__name__ for widget in children] == [
            "Static",
            "ChildRow",
            "LansCoderMarkdown",
        ]

        tool_row, markdown = children[1:]
        assert isinstance(markdown, LansCoderMarkdown)
        # block's markdown carries the answer; no leftover empty placeholder
        assert markdown.source == "LansCoder:\n\ndone"
        assert markdown.allow_select is True

        assert isinstance(tool_row, ChildRow)
        block_index = len(app.transcript.blocks) - 1
        tool_child = app.transcript.blocks[block_index].children[0]
        assert tool_row.block_index == block_index
        assert tool_row.child_key == tool_child.key
        assert tool_row is app.query_one(f"#child-{block_index}-{tool_child.key}", ChildRow)
        # child row is drawn above the markdown, in model order
        assert "[>] tool echo" in str(tool_row.content)
        assert "✓" in str(tool_row.content)
        assert tool_row.size.height >= 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_toggle_child_expanded_out_of_range_block_returns_early() -> None:
    """Clicking a stale child row (block flushed from the transcript) must not raise."""
    app = LansCoderApp()

    async with app.run_test() as pilot:
        app._toggle_child_expanded(99, "missing")
        app._toggle_child_expanded(0, "missing")
        await pilot.pause()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_interrupt_chat_turn_settles_running_tool_child_to_error() -> None:
    runner = FakeToolEventAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)

    async with app.run_test() as pilot:
        app._start_turn_metrics()
        previous_tool_handler = app._install_tool_event_handler()
        runner.tool_event_handler(
            ToolExecutionEvent(
                kind="started",
                tool_call=ToolCall(id="call_echo", name="echo", arguments={}),
            )
        )
        app._restore_tool_event_handler(previous_tool_handler)
        child = next(
            (c for b in app.transcript.blocks for c in b.children if c.kind == ChildKind.TOOL),
            None,
        )
        assert child.status == "running"

        app._interrupt_chat_turn()
        await pilot.pause()

        child = next(
            (c for b in app.transcript.blocks for c in b.children if c.kind == ChildKind.TOOL),
            None,
        )
        assert child.status == "error"
        assert app.transcript.blocks[-1].kind == BlockKind.SYSTEM
        assert app.transcript.blocks[-1].text == "Interrupted current turn."


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_resume_after_permission_pending_rewrites_prompt_inline() -> None:
    """重放当前会话后,挂起的权限请求重新写入输出区提示,数字输入仍能提交。

    `_replay_current_session` 的收尾路径是 `_write_pending_input()` → 把
    permission 提示作为消息块写进输出区;此处验证重放后提示在输出区出现且
    键入 "2" 走正常 resume 协议。
    """
    runner = FakePermissionResumeRunner()
    runner.sync_pending_input_from_current_session = lambda: runner.last_pending_input
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())

    async with app.run_test() as pilot:
        await app._replay_current_session()
        await pilot.pause()

        rendered = "\n".join(str(w.render()) for w in app.query("#output Static"))
        assert "permission requested" in rendered
        assert "[1] deny  [2] allow once" in rendered
        assert app._activity_text == "waiting · permission"

        await pilot.click("#input")
        await pilot.press(*"2")
        await pilot.press("enter")
        await pilot.pause()

    assert runner.inputs == []
    assert runner.resumes == [("perm_write", "allow_once")]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_activity_metrics_show_after_turn_then_revert_to_idle() -> None:
    """回合结束后保留一次 elapsed · N tools 显示,2 秒后回 idle · ready。

    新回合开始(_start_turn_metrics)会取消挂起的恢复计时器,避免旧回合的
    恢复动作在流式中途打回 idle。
    """
    runner = FakeStreamingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)
    app.ACTIVITY_IDLE_REVERT_SECONDS = 0.1

    async with app.run_test() as pilot:
        app._start_turn_metrics()
        app._turn_tool_count = 2
        await pilot.pause()
        app._write_chat_response(ChatResponse(provider="fake", model="fake", content="done"))
        await pilot.pause()

        line = str(app.query_one("#activity").render())
        assert "done" in line
        assert "2 tools" in line
        assert app._activity_text != "idle · ready"

        await pilot.pause(0.3)

        assert app._activity_text == "idle · ready"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_new_turn_cancels_pending_activity_idle_revert() -> None:
    """新回合开始取消旧回合的 idle 恢复计时器,活动区保持新回合状态。"""
    runner = FakeStreamingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner)
    app.ACTIVITY_IDLE_REVERT_SECONDS = 0.1

    async with app.run_test() as pilot:
        app._start_turn_metrics()
        await pilot.pause()
        app._write_chat_response(ChatResponse(provider="fake", model="fake", content="done"))
        await pilot.pause()
        assert app._activity_text == "done"

        app._start_turn_metrics()
        app._set_activity("running · next turn")
        await pilot.pause(0.3)

        assert app._activity_text == "running · next turn"


class ReasoningRecordingRunner:
    def __init__(self, reasonings):
        self.last_turn_reasonings = reasonings
        self.last_pending_input = None
        self.background_manager = None
        self.stream_event_handler = None
        self.tool_event_handler = None
        self.last_display_lines = []


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_backfills_reasoning_duration() -> None:
    runner = ReasoningRecordingRunner([("replayed think", 11.5, True)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app._begin_active_chat_turn()
        app.projector.append_thinking("replayed think", track_duration=True)
        app.projector.end_turn()
        child = app.transcript.blocks[-1].children[0]
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
    assert child.duration_seconds == 11.5
    assert child.finished is True


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_reconcile_survives_nudge_token_advance() -> None:
    """P1-1 回归:turn 内待完成后台任务触发 nudge、token 提前推进,reconcile 仍执行。"""
    job = BackgroundJob(id="bg_1", tool_name="delegate", label="r", status="completed")
    runner = FakeSubagentRunner(pending=[job])
    runner.last_turn_reasonings = [("think", 5.0, True)]
    app = LansCoderApp(chat_runner=runner)
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app._begin_active_chat_turn()
        app.projector.append_thinking("think", track_duration=True)
        app.projector.end_turn()
        child = app.transcript.blocks[-1].children[0]
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
    assert child.duration_seconds == 5.0
    assert runner.nudges == [True]  # nudge 照常触发


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_resume_does_not_materialize_duplicate_reasoning() -> None:
    """权限暂停/恢复的同一回合:首段 reasoning 已在请求段配对,恢复段只配对新条目。

    用户回复产生独立 USER 块与新 assistant 块;收尾 reconcile 若拿全量条目
    配对,会用 R1 顶掉 R2 的 live 行并再物化一条 R2,出现两个相同 thinking。
    """
    runner = ReasoningRecordingRunner([("R1", 3.0, True)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        # 请求段:现场推理 R1,回合挂起待输入 -> 请求段收尾配对(R1)
        app.projector.start_user("写 README")
        app._begin_active_chat_turn()
        app.projector.append_thinking("R1", track_duration=True)
        app.projector.end_turn()
        runner.last_pending_input = "pending"  # 挂起中,_finish_chat_turn 保留同一回合
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()

        # 恢复段(同一回合):用户回复后重新武装,新 assistant 块推理 R2 再执行工具
        app.projector.start_user("2")
        app._resume_active_chat_turn()
        app.projector.append_thinking("R2", track_duration=True)
        app.projector.tool_event("c1", "write", "finished", ok=True, result_body="ok")
        app.projector.end_turn()
        runner.last_turn_reasonings = [("R1", 3.0, True), ("R2", 4.0, False)]
        runner.last_pending_input = None
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()

    thinking = [c for block in app.transcript.blocks for c in block.children if c.kind == ChildKind.THINKING]
    assert [c.body for c in thinking] == ["R1", "R2"]
    assert [c.duration_seconds for c in thinking] == [3.0, 4.0]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_materializes_non_streaming_thinking_row() -> None:
    runner = ReasoningRecordingRunner([("offline think", 3.0, True)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
        block = app.transcript.blocks[-1]
        assert block.kind == BlockKind.ASSISTANT
        child = [c for c in block.children if c.kind == ChildKind.THINKING]
        assert len(child) == 1
        assert child[0].duration_seconds == 3.0
        # 物化行位于输出区,正文 markdown 随后由 _write_chat_response 挂载在其下方
        output = app.query_one("#output")
        assert [w.id for w in output.children if isinstance(w.id, str) and w.id.startswith("child-")]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_live_late_reasoning_hoists_above_open_text() -> None:
    """后到的 reasoning(先 text 后 reasoning 的乱序流)不得落在正文之下。

    回归:OpenAI-compatible 同 chunk 内先发 content 后发 reasoning_content 时,
    append_thinking 推理行排在 TEXT_RUN 之后,显示在最终回复下方。
    """
    runner = FakeStreamingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app._dismiss_welcome()
        app._install_stream_event_handler()
        app._begin_active_chat_turn()
        runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="final answer"))
        await pilot.pause()
        runner.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="late think"))
        await pilot.pause()
        block = app.transcript.blocks[-1]
        assert [c.kind for c in block.children] == [ChildKind.THINKING, ChildKind.TEXT_RUN]
        output = app.query_one("#output")
        mounted = [type(w).__name__ for w in output.children if isinstance(w, ChildRow) or isinstance(w, LansCoderMarkdown)]
        assert mounted == ["ChildRow", "LansCoderMarkdown"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_live_late_reasoning_chunks_merge_into_one_hoisted_row() -> None:
    """分片 reasoning 在正文之后到达时合并为单行,且仍位于正文之上。"""
    runner = FakeStreamingAsyncChatRunner()
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app._dismiss_welcome()
        app._install_stream_event_handler()
        app._begin_active_chat_turn()
        runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="final answer"))
        await pilot.pause()
        runner.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="part1 "))
        await pilot.pause()
        runner.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="part2"))
        await pilot.pause()
        block = app.transcript.blocks[-1]
        assert [c.kind for c in block.children] == [ChildKind.THINKING, ChildKind.TEXT_RUN]
        thinking_row = app.query_one("#child-0-t1", ChildRow)
        assert "part1 part2" in str(thinking_row.content)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_materialized_thinking_sits_above_final_text() -> None:
    """长 task 收尾 reconcile 物化的 thinking 行必须位于最终正文段之上。

    回归:最终文本已在流式阶段挂载(TEXT_RUN 是块末位子项),物化分支此前
    盲目 append 到 children 末尾,使 thinking 行渲染在最终回复下方。
    """
    runner = ReasoningRecordingRunner([("r1", 3.0, True), ("r2-missing", 4.0, False)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app._dismiss_welcome()
        app._install_stream_event_handler()
        app._install_tool_event_handler()
        app._begin_active_chat_turn()
        # 第一轮 thinking + tool(流式,存在 live 行)
        runner.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="r1 "))
        runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="stage one "))
        await pilot.pause()
        runner.tool_event_handler(ToolExecutionEvent(kind="started", tool_call=ToolCall(id="c1", name="grep", arguments={})))
        await pilot.pause()
        # 最终回复文本已流式挂载,其 reasoning 条目在 store 里但没有 live 行
        runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="final answer"))
        await pilot.pause()
        app._stream_text_started = True
        app._live_tool_events_seen = True
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
        app._write_chat_response(ChatResponse(provider="fake", model="fake", content="final answer"))
        await pilot.pause()

        block = app.transcript.blocks[-1]
        kinds = [c.kind for c in block.children]
        # 物化的 thinking 行插在末尾正文段之前,不落在最终回复下方
        assert kinds == [ChildKind.THINKING, ChildKind.TEXT_RUN, ChildKind.TOOL, ChildKind.THINKING, ChildKind.TEXT_RUN]
        thinking = [c for c in block.children if c.kind == ChildKind.THINKING]
        assert thinking[-1].duration_seconds == 4.0
        # DOM 顺序与 children 一致:物化 ChildRow 在最终 markdown 之前
        output = app.query_one("#output")
        mounted = [type(w).__name__ for w in output.children if isinstance(w, ChildRow) or isinstance(w, LansCoderMarkdown)]
        assert mounted == ["ChildRow", "LansCoderMarkdown", "ChildRow", "ChildRow", "LansCoderMarkdown"]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_merges_consecutive_reasoning_only_entries() -> None:
    """相邻 reasoning-only 消息(无 parts)合并进同一子行,首个秒数胜出。"""
    runner = ReasoningRecordingRunner([("a", 3.0, False), ("b", 4.0, False)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
    block = app.transcript.blocks[-1]
    thinking = [c for c in block.children if c.kind == ChildKind.THINKING]
    assert len(thinking) == 1
    assert thinking[0].duration_seconds == 3.0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_tool_call_breaks_merge_chain() -> None:
    runner = ReasoningRecordingRunner([("think tool", 3.0, True), ("after tool", 4.0, False)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
    block = app.transcript.blocks[-1]
    thinking = [c for c in block.children if c.kind == ChildKind.THINKING]
    assert len(thinking) == 2
    assert [c.duration_seconds for c in thinking] == [3.0, 4.0]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_finish_chat_turn_second_reconcile_keeps_prior_turn_rows() -> None:
    """共享同一 assistant 块的 nudge 回合 reconcile 不得覆盖上一回合一行的时长。"""
    runner = ReasoningRecordingRunner([("first", 3.0, True)])
    app = LansCoderApp(chat_runner=runner, current_session=FakeSession())
    async with app.run_test() as pilot:
        app.projector.start_user("hi")
        app._begin_active_chat_turn()
        app.projector.append_thinking("first", track_duration=True)
        app.projector.tool_event("c1", "read", "started")
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        # 回合2(nudge):同一块共享,不能碰上一回合的行
        runner.last_turn_reasonings = [("second", 7.0, False)]
        app._begin_active_chat_turn()
        app._chat_busy = True
        app._finish_chat_turn(app._chat_turn_token)
        await pilot.pause()
    block = app.transcript.blocks[-1]
    thinking = [c for c in block.children if c.kind == ChildKind.THINKING]
    assert [c.duration_seconds for c in thinking] == [3.0, 7.0]


def test_background_notification_ui_text_full_matrix() -> None:
    assert background_notification_ui_text(label="r", tool_name="delegate", status="completed", error=None) == "✅ 子agent [r] 已完成"
    assert background_notification_ui_text(label=None, tool_name="delegate", status="completed", error=None) == "✅ 子agent [delegate] 已完成"
    assert background_notification_ui_text(label="r", tool_name="delegate", status="failed", error="boom") == "❌ 子agent [r] 失败: boom"
    assert background_notification_ui_text(label="r", tool_name="delegate", status="failed", error=None) == "❌ 子agent [r] 失败: 未知错误"
    assert background_notification_ui_text(label="r", tool_name="delegate", status="cancelled", error=None) == "⚠️ 子agent [r] cancelled"
    assert background_notification_ui_text(label="r", tool_name="delegate", status="", error=None) == "⚠️ 子agent [r]"


def _two_assistant_turn_messages() -> list:
    from types import SimpleNamespace

    def text(s):
        return SimpleNamespace(kind="text", content=s, metadata={})

    def call(cid, name, args):
        return SimpleNamespace(kind="tool_call", content=None, id=cid, metadata={"tool_call_id": cid, "tool_name": name, "arguments": args})

    def result(cid, name, ok, body):
        return SimpleNamespace(kind="tool_result", id=cid, content=body, metadata={"tool_call_id": cid, "tool_name": name, "ok": ok})

    return [
        SimpleNamespace(role="user", parts=[text("帮我改登录")]),
        SimpleNamespace(
            role="assistant",
            parts=[text("先看实现"), call("c1", "read", {"path": "auth.py"})],
            metadata={"diagnostics": {"reasoning": "核心在 session 校验"}},
        ),
        SimpleNamespace(role="tool", parts=[result("c1", "read", True, "ok 200")]),
        SimpleNamespace(
            role="assistant",
            parts=[text("改好了")],
            metadata={"diagnostics": {"reasoning": "再核一遍"}},
        ),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_render_block_into_renders_canonical_run_order() -> None:
    """render_block_into 按 children 时间序渲染:thinking 行、文本段、工具行交错。"""
    app = LansCoderApp()

    async with app.run_test() as pilot:
        app._dismiss_welcome()
        app.projector.start_user("hi")
        app.projector.append_thinking("first thought")
        app.projector.append_assistant_text("text one")
        app.projector.tool_event("c1", "read", "started", arguments={"path": "a.py"})
        app.projector.append_thinking("second thought")
        app.projector.append_assistant_text("text two")
        app.projector.end_turn()
        app.render_block_into(app.transcript.blocks[1], 1)
        await pilot.pause()

        kinds = [type(w).__name__ for w in app.query_one("#output").children]
        assert kinds == ["ChildRow", "LansCoderMarkdown", "ChildRow", "ChildRow", "LansCoderMarkdown"]
        widgets = list(app.query_one("#output").children)
        markdown_indexes = [i for i, kind in enumerate(kinds) if kind == "LansCoderMarkdown"]
        assert len(markdown_indexes) == 2
        first_run, second_run = (_markdown_widget_text(widgets[i]) for i in markdown_indexes)
        assert "text one" in first_run
        assert "text two" in second_run
        assert "text two" not in first_run
        assert "text one" not in second_run


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_replay_renders_turn_text_runs_in_canonical_order() -> None:
    """/resume 重放一个 turn 的两次模型调用:文本段按工具边界分开,第二次 thinking 在工具之下。"""
    view = SessionView(session_id="sess_runs", messages=_two_assistant_turn_messages())
    session = FakeSession()
    session.rebuild_view = lambda: view
    app = LansCoderApp(current_session=session)

    async with app.run_test() as pilot:
        app._dismiss_welcome()
        await app._replay_current_session()
        await pilot.pause()

        kinds = [type(w).__name__ for w in app.query_one("#output").children]
        assert kinds == ["Static", "ChildRow", "LansCoderMarkdown", "ChildRow", "ChildRow", "LansCoderMarkdown"]
        widgets = list(app.query_one("#output").children)
        markdown_indexes = [i for i, kind in enumerate(kinds) if kind == "LansCoderMarkdown"]
        assert len(markdown_indexes) == 2
        first_run, second_run = (_markdown_widget_text(widgets[i]) for i in markdown_indexes)
        assert "先看实现" in first_run
        assert "改好了" in second_run
        assert "先看实现" not in second_run
        assert "改好了" not in first_run
        tool_at = next(i for i, kind in enumerate(kinds) if kind == "ChildRow" and "[>] tool read" in _child_row_text(widgets[i]))
        second_thought_at = next(i for i, kind in enumerate(kinds) if kind == "ChildRow" and "Thought" in _child_row_text(widgets[i]) and i > tool_at)
        assert tool_at < second_thought_at < markdown_indexes[1]


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_live_stream_thinking_after_tool_mounts_below_prior_text(monkeypatch) -> None:
    """live 流中,工具之后到达的 thinking 行必须挂在工具之下,而非旧文本段之上。"""
    runner = FakeToolEventAsyncChatRunner()
    runner.stream_event_handler = lambda event: None
    output = FakeOutput()
    app = LansCoderApp(chat_runner=runner)
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: output)

    previous_stream_handler = app._install_stream_event_handler()
    previous_tool_handler = app._install_tool_event_handler()
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="我先看看。"))
    runner.tool_event_handler(
        ToolExecutionEvent(
            kind="started",
            tool_call=ToolCall(id="call_echo", name="echo", arguments={}),
        )
    )
    runner.stream_event_handler(ChatStreamEvent(kind="reasoning_delta", text="再验证一下"))
    runner.stream_event_handler(ChatStreamEvent(kind="text_delta", text="看完了。"))
    app._restore_tool_event_handler(previous_tool_handler)
    app._restore_stream_event_handler(previous_stream_handler)

    mounted_types = [type(w).__name__ for w in output.mounted]
    assert mounted_types == ["LansCoderMarkdown", "ChildRow", "ChildRow", "LansCoderMarkdown"]
    first_markdown, tool_row, thought_row, second_markdown = output.mounted
    assert "tool echo" in str(tool_row.content)
    assert "child-thinking" in str(thought_row.classes)
    assert first_markdown.updates[-1] == "LansCoder:\n\n我先看看。"
    assert second_markdown.updates[-1] == "LansCoder:\n\n看完了。"


class _FakeInput:
    def __init__(self, text: str) -> None:
        self.text = text

    def clear(self) -> None:
        pass


@pytest.mark.anyio
async def test_lanscoder_app_force_scrolls_to_bottom_after_normal_submit(monkeypatch) -> None:
    output = FakeOutput()
    output.scroll_y = 1
    output.max_scroll_y = 10
    app = LansCoderApp()
    app._picker = None
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: _FakeInput("你好") if args[0] == "#input" else output)
    submitted: list[str] = []
    monkeypatch.setattr(app, "_submit_chat_text", lambda text, attachments=None: submitted.append(text))

    await app._submit_composer()

    assert submitted == ["你好"]
    assert output.scroll_end_calls >= 1


@pytest.mark.anyio
async def test_lanscoder_app_force_scrolls_to_bottom_after_slash_command(monkeypatch) -> None:
    class _Handler:
        class Result:
            handled = True
            output = "done"
            action = None

        def handle(self, text: str):
            return self.Result()

    output = FakeOutput()
    output.scroll_y = 1
    output.max_scroll_y = 10
    app = LansCoderApp()
    app._picker = None
    app.command_handler = _Handler()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: _FakeInput("/foo") if args[0] == "#input" else output)

    async def _noop_action(action=None, output=""):
        return None

    monkeypatch.setattr(app, "_handle_command_action", _noop_action)
    monkeypatch.setattr(app, "_refresh_session_subtitle", lambda: None)

    await app._submit_composer()

    assert output.scroll_end_calls >= 1


@pytest.mark.anyio
async def test_lanscoder_app_force_scrolls_to_bottom_after_unknown_command(monkeypatch) -> None:
    class _Handler:
        class Result:
            handled = False
            output = ""
            action = None

        def handle(self, text: str):
            return self.Result()

    output = FakeOutput()
    output.scroll_y = 1
    output.max_scroll_y = 10
    app = LansCoderApp()
    app._picker = None
    app.command_handler = _Handler()
    monkeypatch.setattr(app, "query_one", lambda *args, **kwargs: _FakeInput("/nope") if args[0] == "#input" else output)

    await app._submit_composer()

    assert output.scroll_end_calls >= 1
