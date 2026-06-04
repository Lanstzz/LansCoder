"""FirstCoder 最小 Textual TUI。

这一版只提供命令入口外壳：输出区展示状态文本，输入框接收普通文本或 slash command。
普通聊天通过注入的 chat runner 处理，避免 Textual widget 直接依赖 provider/agent 细节。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog

from firstcoder.app.commands import CommandResult


class CommandHandlerLike(Protocol):
    def handle(self, text: str) -> CommandResult:
        ...


class ChatRunnerLike(Protocol):
    def run_user_turn(self, content: str):
        ...


class CurrentSessionLike(Protocol):
    session_id: str


@dataclass(slots=True)
class FirstCoderTuiConfig:
    title: str = "FirstCoder"


class FirstCoderApp(App[None]):
    """最小 TUI 外壳。"""

    BINDINGS = [("ctrl+c", "quit", "Quit")]

    def __init__(
        self,
        *,
        command_handler: CommandHandlerLike | None = None,
        chat_runner: ChatRunnerLike | None = None,
        current_session: CurrentSessionLike | None = None,
        config: FirstCoderTuiConfig | None = None,
    ) -> None:
        super().__init__()
        self.command_handler = command_handler
        self.chat_runner = chat_runner
        self.current_session = current_session
        self.config = config or FirstCoderTuiConfig()
        self._chat_busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="output", wrap=True)
            yield Input(placeholder="输入消息，或使用 /context、/compact status、/compact", id="input")
        yield Footer()

    def on_mount(self) -> None:
        self.title = self.config.title
        self._refresh_session_subtitle()
        output = self.query_one("#output", RichLog)
        output.write(
            "FirstCoder ready. Commands: /sessions, /session, /resume, /share, /rename, "
            "/context, /compact status, /compact"
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        output = self.query_one("#output", RichLog)
        output.write(f"> {text}")

        if text.startswith("/"):
            if self.command_handler is None:
                output.write("Command handler is not configured.")
                return

            result = self.command_handler.handle(text)
            if result.handled:
                output.write(result.output)
                self._refresh_session_subtitle()
                return
            output.write(f"Unknown command: {text}")
            return

        if self.chat_runner is None:
            output.write("普通聊天入口尚未接入 AgentLoop。")
            return

        if self._chat_busy:
            output.write("Chat is still running. Please wait for the current turn to finish.")
            return

        pending = getattr(self.chat_runner, "last_pending_input", None)
        if getattr(pending, "kind", None) == "permission_confirmation":
            choice = _permission_choice_for_text(text, pending)
            if choice is None:
                output.write(_permission_options_text(pending))
                return
            self._chat_busy = True
            self.run_worker(self._resume_permission_turn(pending.id, choice))
            return

        self._chat_busy = True
        self.run_worker(self._run_chat_turn(text))

    async def _resume_permission_turn(self, request_id: str, answer: str) -> None:
        output = self.query_one("#output", RichLog)
        try:
            async_resume = getattr(self.chat_runner, "aresume_with_user_input", None)
            if async_resume is not None:
                response = await async_resume(request_id, answer)
                self._write_chat_response(response)
                return
            resume = getattr(self.chat_runner, "resume_with_user_input", None)
            if resume is None:
                output.write("Permission resume is not configured.")
                return
            response = resume(request_id, answer)
        except Exception as exc:
            output.write(f"Chat error: {exc}")
            self._refresh_session_subtitle()
            return
        finally:
            self._chat_busy = False

        self._write_chat_response(response)

    async def _run_chat_turn(self, text: str) -> None:
        output = self.query_one("#output", RichLog)
        try:
            async_runner = getattr(self.chat_runner, "arun_user_turn", None) if self.chat_runner else None
            if async_runner is not None:
                response = await async_runner(text)
            else:
                response = self.chat_runner.run_user_turn(text)
        except Exception as exc:
            output.write(f"Chat error: {exc}")
            self._refresh_session_subtitle()
            return
        finally:
            self._chat_busy = False

        self._write_chat_response(response)

    def _write_chat_response(self, response) -> None:
        output = self.query_one("#output", RichLog)
        display_lines = list(getattr(self.chat_runner, "last_display_lines", []) or [])
        if display_lines:
            for line in display_lines:
                output.write(line)
        else:
            content = getattr(response, "content", "")
            output.write(content or "[assistant response has no text content]")
        self._refresh_session_subtitle()

    def _refresh_session_subtitle(self) -> None:
        if self.current_session is None:
            return
        self.sub_title = f"Session: {self.current_session.session_id}"


def _permission_choice_for_text(text: str, pending) -> str | None:
    normalized = text.strip().lower().replace(" ", "_")
    aliases = {
        "1": "deny",
        "no": "deny",
        "deny": "deny",
        "2": "allow_once",
        "allow_once": "allow_once",
        "once": "allow_once",
        "allow": "allow_once",
        "3": "allow_always_same_scope",
        "allow_always": "allow_always_same_scope",
        "always": "allow_always_same_scope",
        "allow_always_same_scope": "allow_always_same_scope",
    }
    if normalized in aliases:
        return aliases[normalized]
    for option in getattr(pending, "options", []) or []:
        if normalized in {str(option.id).lower(), str(option.label).strip().lower().replace(" ", "_")}:
            return str(option.id)
    return None


def _permission_options_text(pending) -> str:
    options = getattr(pending, "options", []) or []
    if not options:
        return "请回复权限选择：deny / allow_once / allow_always_same_scope"
    rendered = ", ".join(f"{option.id} ({option.label})" for option in options)
    return f"请回复权限选择：{rendered}"
