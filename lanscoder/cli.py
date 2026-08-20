"""Command-line entry point for single-turn LansCoder runs."""

# ============================================================================
# 阅读路径导航 (Reading Path Guide)
# ============================================================================
# 这是 LansCoder 的最外层入口。核心流程：
#   main() → 解析参数 → 选择运行模式
#     ├─ TUI/交互模式 → create_cli_app() → create_lanscoder_app()
#     └─ 单次执行     → create_cli_app() → run_single_turn()
# 组装后的核心调用链：
#   AgentChatRunner.run_user_turn() → AgentLoop → provider.complete()
# → 下一步阅读：lanscoder/app/factory.py (create_lanscoder_app)
# ============================================================================

from __future__ import annotations
from lanscoder.app.ports import ChatRunnerLike

import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from lanscoder.agent.loop_limits import AgentLoopLimits
from lanscoder.app.factory import create_lanscoder_app
from lanscoder.config import load_config
from lanscoder.config.settings import default_global_config_path, project_config_path, render_default_config
from lanscoder.mcp.config_store import McpConfigStore, McpConfigStoreError
from lanscoder.permissions.types import PermissionMode


# ----------------------------------------------------------------------------
# CliConfig: CLI 层传给下层的配置快照
# ----------------------------------------------------------------------------
# 注意：它只保存命令行解析出来的参数（session、message、model_spec 等）。
# provider/model 的真正配置在 lanscoder.config.load_config() 里加载，
# 由 create_lanscoder_app() 内部消费，不会出现在这里。
# 所以 CliConfig 是 "用户这次想跑什么"，不是 "系统怎么连 LLM"。
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CliConfig:
    project_root: Path
    data_root: Path | None
    session_id: str | None
    message: str
    model_spec: str | None = None
    max_tool_rounds: int | None = None
    reasoning_effort: str | None = None
    benchmark: bool = False
    resume_session: bool = False


CliRunner = Callable[[CliConfig], str]


def read_message(message: str | None, *, stdin_text: str | None = None) -> str:
    """Return a user message from an argument or stdin."""

    if message is not None:
        return message.strip()
    text = sys.stdin.read() if stdin_text is None else stdin_text
    return text.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a single LansCoder user turn.")
    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="Inspect or initialize LansCoder configuration.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("path", help="Show global and project config paths.")
    config_subparsers.add_parser("show", help="Show effective provider configuration without secrets.")
    init_parser = config_subparsers.add_parser("init", help="Create a starter global config file.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite the existing global config.")
    mcp_parser = subparsers.add_parser("mcp", help="Add, list, or remove MCP server configuration.")
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command")
    add_parser = mcp_subparsers.add_parser("add", help="Add a local command or remote URL MCP server.")
    add_parser.add_argument("name")
    add_parser.add_argument("--url", help="Remote MCP URL. Omit for a local stdio command.")
    add_parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    add_parser.add_argument("--header", action="append", default=[], metavar="KEY=VALUE")
    add_parser.add_argument("--bearer-token-env-var", help="Environment variable containing a remote bearer token.")
    add_parser.add_argument("server_command", nargs="*", metavar="COMMAND")
    mcp_subparsers.add_parser("list", help="List configured MCP servers without secrets.")
    remove_parser = mcp_subparsers.add_parser("remove", help="Remove one configured MCP server.")
    remove_parser.add_argument("name")

    parser.add_argument("--project", default=".", help="Project root for tools and AGENTS.md.")
    parser.add_argument("--data-root", default=None, help="Directory for LansCoder session data.")
    parser.add_argument("--session-id", default=None, help="Session id to create or reuse.")
    parser.add_argument(
        "--resume-session",
        action="store_true",
        help="Resume an existing session id instead of creating a new session.",
    )
    parser.add_argument("--model", default=None, help="Model reference, for example provider/model.")
    parser.add_argument("--message", default=None, help="Single user message. Reads stdin when omitted.")
    parser.add_argument("--interactive", action="store_true", help="Run a line-oriented interactive session.")
    parser.add_argument("--tui", action="store_true", help="Run the Textual TUI.")
    parser.add_argument("--auto-approve", action="store_true", help="Automatically answer permission confirmations with allow_once.")
    parser.add_argument("--max-tool-rounds", type=_positive_int, default=None, help="Override per-turn tool round limit.")
    parser.add_argument("--reasoning-effort", default=None, help="Provider-specific reasoning effort passed in the model request.")
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run the message with the non-interactive benchmark adapter using bypass permissions.",
    )
    return parser


# ----------------------------------------------------------------------------
# main(): CLI 顶层路由
# ----------------------------------------------------------------------------
# 三分支路由（按优先级从高到低）：
#   1. 子命令分支  → config / mcp（直接处理并返回，不进 agent loop）
#   2. TUI 模式    → --tui 或无 --message 且 stdin 是 tty → 启动 Textual 界面
#   3. 交互模式    → --interactive → run_repl() 行式 REPL
#   4. 单次执行    → 兜底：读一条 message → run_single_turn()
# 所有需要 agent 的分支都走 create_cli_app() 组装应用（→ 见下方函数）。
# ----------------------------------------------------------------------------
def main(
    argv: list[str] | None = None,
    *,
    runner: CliRunner | None = None,
    stdin_text: str | None = None,
) -> int:
    parser = build_parser()
    args, extras = parser.parse_known_args(argv)
    if extras:
        if args.command == "mcp" and args.mcp_command == "add" and not args.url:
            args.server_command.extend(extras)
        else:
            parser.error(f"unrecognized arguments: {' '.join(extras)}")
    if args.command == "config":
        return run_config_command(args)
    if args.command == "mcp":
        return run_mcp_command(args)

    # 分支 1 — TUI 模式：显式 --tui，或无 message 且 stdin 是 tty 且非交互
    #   → create_cli_app() 组装 → app.run() 启动 Textual 事件循环
    if args.tui or (args.message is None and stdin_text is None and sys.stdin.isatty() and not args.interactive):
        config = CliConfig(
            project_root=Path(args.project),
            data_root=Path(args.data_root) if args.data_root is not None else None,
            session_id=args.session_id,
            message="",
            model_spec=args.model,
            max_tool_rounds=args.max_tool_rounds,
            reasoning_effort=args.reasoning_effort,
            benchmark=args.benchmark,
            resume_session=args.resume_session,
        )
        try:
            app = create_cli_app(config)
            app.run()
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    # 分支 2 — 交互模式：显式 --interactive
    #   → create_cli_app() 组装 → run_repl() 走行式 stdin 循环
    if args.interactive:
        config = CliConfig(
            project_root=Path(args.project),
            data_root=Path(args.data_root) if args.data_root is not None else None,
            session_id=args.session_id,
            message="",
            model_spec=args.model,
            max_tool_rounds=args.max_tool_rounds,
            reasoning_effort=args.reasoning_effort,
            benchmark=args.benchmark,
            resume_session=args.resume_session,
        )
        try:
            app = create_cli_app(config)
            lines = stdin_text.splitlines() if stdin_text is not None else None
            run_repl(app.chat_runner, lines, auto_approve=args.auto_approve)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    message = read_message(args.message, stdin_text=stdin_text)
    if not message:
        print("error: message is required via --message or stdin", file=sys.stderr)
        return 2

    # 分支 3 — 单次执行（兜底）：读一条 message，跑一个 turn 就退出
    config = CliConfig(
        project_root=Path(args.project),
        data_root=Path(args.data_root) if args.data_root is not None else None,
        session_id=args.session_id,
        message=message,
        model_spec=args.model,
        max_tool_rounds=args.max_tool_rounds,
        reasoning_effort=args.reasoning_effort,
        benchmark=args.benchmark,
        resume_session=args.resume_session,
    )
    # runner 可注入用于测试；默认走 run_single_turn()
    run = runner or run_single_turn
    try:
        output = run(config)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if output:
        print(output)
    return 0


# ----------------------------------------------------------------------------
# run_single_turn(): 单次 turn 执行路径
# ----------------------------------------------------------------------------
# 这是 main() 单次执行分支的最终落点。
# 通过 create_cli_app() 拿到组装好的 App，再用 app.chat_runner（即
# AgentChatRunner）跑一个 turn。
# ★ 想跟踪 "一个 turn 怎么跑"：入口是 AgentChatRunner.run_user_turn()
# benchmark 模式走特殊分支 run_benchmark_turn()，bypass 权限。
# ----------------------------------------------------------------------------
def run_single_turn(config: CliConfig) -> str:
    if config.benchmark:
        return run_benchmark_turn(config)
    app = create_cli_app(config)
    # ★ 跟踪一个 turn 的核心入口：ChatRunner.run_user_turn()
    response = app.chat_runner.run_user_turn(config.message)
    return response.content


# ----------------------------------------------------------------------------
# run_benchmark_turn(): benchmark 模式特殊处理
# ----------------------------------------------------------------------------
# Harbor benchmark 要求无人值守跑完一个 turn，所以这里：
#   • 权限模式切到 BYPASS       — 不再弹确认，直接 allow_once
#   • 关掉 prewrite review      — 避免 HITL 卡住
#   • 设置 benchmark task       — session 记录当前任务文本
#   • 用 swe_lite limits        — 工具轮次/上下文按 benchmark 规范收紧
# 任何 Harbor 任务都走这条路径；非 benchmark 请走 run_single_turn() 的正常分支。
# ----------------------------------------------------------------------------
def run_benchmark_turn(config: CliConfig) -> str:
    """Run Harbor's non-interactive turn with benchmark-safe session settings."""

    app = create_cli_app(config)
    # benchmark 无人值守：权限全部 bypass
    app.current_session.set_permission_mode(PermissionMode.BYPASS)
    # 关掉写文件前的 HITL review，避免卡住 verifier
    app.current_session.session.require_prewrite_review = False
    # 让 session 知道当前 benchmark task 文本（用于日志/恢复）
    app.current_session.session.set_benchmark_task(config.message)
    # 用 swe_lite 预设 limits，保证与 benchmark 协议对齐
    app.chat_runner.limits = _benchmark_limits(config.max_tool_rounds)
    response = app.chat_runner.run_user_turn(config.message)
    return response.content


# ----------------------------------------------------------------------------
# create_cli_app(): CLI → factory 的桥梁
# ----------------------------------------------------------------------------
# 把 CliConfig 翻译成 create_lanscoder_app() 需要的参数，再把 CLI 专属
# 的两个覆盖项（--max-tool-rounds、--reasoning-effort）应用到组装好的 app 上。
#   ★ 实际组装函数是 create_lanscoder_app()
#     → 见 lanscoder/app/factory.py:create_lanscoder_app()
# 调用方：main() 三个需要 agent 的分支、run_single_turn()、run_benchmark_turn()
# ----------------------------------------------------------------------------
def create_cli_app(config: CliConfig):
    # ★ 真正的组装在这里：构建 session、chat_runner、provider、tool registry
    app = create_lanscoder_app(
        project_root=config.project_root,
        data_root=config.data_root,
        session_id=config.session_id,
        model_spec=config.model_spec,
        resume_session=config.resume_session,
    )
    # CLI 覆盖项 1：--max-tool-rounds 改写 agent loop 的工具轮次上限
    if config.max_tool_rounds is not None:
        app.chat_runner.limits = AgentLoopLimits.default().with_max_tool_rounds(config.max_tool_rounds)
    # CLI 覆盖项 2：--reasoning-effort 注入到 provider 请求的 extra_body
    if config.reasoning_effort is not None:
        effort = config.reasoning_effort.strip()
        if not effort:
            raise ValueError("reasoning_effort must be a non-blank string")
        options = app.chat_runner.request_options
        extra_body = dict(options.extra_body)
        extra_body["reasoning_effort"] = effort
        # dataclasses.replace() 返回新的 RequestOptions，不破坏原对象
        app.chat_runner.request_options = replace(options, extra_body=extra_body)
    return app


def run_config_command(args: argparse.Namespace) -> int:
    command = args.config_command or "show"
    project_root = Path(args.project)
    if command == "path":
        print(f"global: {default_global_config_path()}")
        print(f"project: {project_config_path(project_root)}")
        return 0
    if command == "init":
        path = default_global_config_path()
        if path.exists() and not args.force:
            print(f"config already exists: {path}", file=sys.stderr)
            print("use --force to overwrite", file=sys.stderr)
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_default_config(), encoding="utf-8")
        print(f"created: {path}")
        return 0
    if command == "show":
        config = load_config(project_root=project_root)
        catalog = config.model_catalog()
        print(f"provider: {_effective_provider(config)}")
        print(f"model: {_effective_model(config)}")
        if catalog.profiles:
            print(f"default_model: {catalog.default_ref or '<first configured model>'}")
            print("models:")
            for profile in catalog.list():
                print(f"  - {profile.ref} ({profile.label})")
        print(f"base_url: {_effective_base_url(config)}")
        print(f"parallel_tool_calls: {_effective_parallel_tool_calls(config)}")
        print("config_files:")
        for path in config.loaded_config_paths:
            print(f"  - {path}")
        if not config.loaded_config_paths:
            print("  - <none>")
        return 0
    print(f"error: unknown config command: {command}", file=sys.stderr)
    return 2


def run_mcp_command(args: argparse.Namespace) -> int:
    """编辑全局 MCP 配置；运行期连接仍由 app factory 管理。"""

    if args.mcp_command is None:
        print("error: choose mcp add, list, or remove", file=sys.stderr)
        return 2
    store = McpConfigStore(default_global_config_path())
    try:
        if args.mcp_command == "list":
            servers = store.list_servers()
            if not servers:
                print("No MCP servers configured.")
                return 0
            for server in servers:
                status = "enabled" if server["enabled"] else "disabled"
                print(f'{server["name"]} {server["type"]} {server["endpoint"]} {status}')
            return 0
        if args.mcp_command == "remove":
            if not store.remove(args.name):
                print(f"MCP server not found: {args.name}", file=sys.stderr)
                return 1
            print(f"Removed MCP server: {args.name}")
            return 0
        env = _key_values(args.env, "--env")
        headers = _key_values(args.header, "--header")
        if args.url:
            if env:
                print("error: --env is only supported for local MCP servers", file=sys.stderr)
                return 2
            if args.server_command:
                print("error: local command cannot be used with --url", file=sys.stderr)
                return 2
            store.add_remote(
                args.name,
                args.url,
                headers=headers,
                bearer_token_env_var=args.bearer_token_env_var,
            )
            print(f"Added remote MCP server: {args.name}")
            return 0
        if headers:
            print("error: --header is only supported for remote MCP servers", file=sys.stderr)
            return 2
        if args.bearer_token_env_var:
            print("error: --bearer-token-env-var is only supported for remote MCP servers", file=sys.stderr)
            return 2
        store.add_local(args.name, args.server_command, env=env)
        print(f"Added local MCP server: {args.name}")
        return 0
    except McpConfigStoreError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _key_values(values: list[str], option: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, content = value.partition("=")
        if not separator or not key or not content:
            raise McpConfigStoreError(f"{option} 必须使用 KEY=VALUE 格式")
        result[key] = content
    return result


def _effective_model(config) -> str:
    catalog = config.model_catalog()
    if catalog.default_ref:
        return catalog.default_ref
    profiles = catalog.list()
    return profiles[0].ref if profiles else "<not configured>"


def _effective_provider(config) -> str:
    profile = _effective_profile(config)
    return profile.provider.id if profile is not None else "<not configured>"


def _effective_profile(config):
    catalog = config.model_catalog()
    profile = catalog.get(catalog.default_ref) if catalog.default_ref else None
    if profile is None and catalog.profiles:
        profile = catalog.profiles[0]
    return profile


def _effective_base_url(config) -> str:
    profile = _effective_profile(config)
    return profile.provider.base_url if profile and profile.provider.base_url else "<provider default>"


def _effective_parallel_tool_calls(config) -> str:
    profile = _effective_profile(config)
    if profile is None:
        return "false"
    enabled = config.get_provider_bool(
        "parallel_tool_calls",
        env="LANSCODER_PARALLEL_TOOL_CALLS",
        default=False,
        provider_name=profile.provider.id,
    )
    return "true" if enabled else "false"


def _benchmark_limits(max_tool_rounds: int | None) -> AgentLoopLimits:
    base = AgentLoopLimits.swe_lite()
    if max_tool_rounds is None:
        return base
    return base.with_max_tool_rounds(max_tool_rounds)


def run_repl(
    chat_runner: ChatRunnerLike,
    lines: Iterable[str] | None = None,
    *,
    auto_approve: bool = False,
) -> None:
    source = iter(lines) if lines is not None else _stdin_lines()
    pending = None
    for raw_line in source:
        line = raw_line.strip()
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            break

        if pending is not None:
            kind = _pending_kind(pending)
            if kind == "permission_confirmation":
                choice = _permission_choice_for_text(line, pending)
                if choice is None:
                    print(f"Unknown permission choice: {line}")
                    print(_permission_choice_help_text(pending))
                    print(_permission_options_text(pending))
                    continue
                response = chat_runner.resume_with_user_input(_pending_id(pending), choice)
            elif kind == "ask_user":
                # ask_user 与权限统一走 resume 协议：回答后 loop 会继续执行同批次
                # 剩余工具（deferred batch continuation）。输入匹配某选项时规范化为其 label。
                matched = _ask_user_choice_for_text(line, pending)
                if matched is not None:
                    line = matched
                response = chat_runner.resume_with_user_input(_pending_id(pending), line)
            else:
                response = chat_runner.resume_with_user_input(_pending_id(pending), line)
        else:
            response = chat_runner.run_user_turn(line)

        print(f"LansCoder> {response.content}")
        pending = getattr(chat_runner, "last_pending_input", None)
        while pending is not None and auto_approve and _pending_kind(pending) == "permission_confirmation":
            print("Auto-approve> allow_once")
            response = chat_runner.resume_with_user_input(_pending_id(pending), "allow_once")
            print(f"LansCoder> {response.content}")
            pending = getattr(chat_runner, "last_pending_input", None)

        if pending is not None:
            kind = _pending_kind(pending)
            if kind == "permission_confirmation":
                print(_permission_options_text(pending))
            elif kind == "ask_user":
                print(_ask_user_prompt_text(pending))
            else:
                print(f"Permission> {_pending_question(pending)}")


def _stdin_lines():
    prompt = _create_prompt_session()
    if prompt is not None:
        while True:
            try:
                yield prompt.prompt("You> ")
            except (EOFError, KeyboardInterrupt):
                break
        return

    while True:
        try:
            yield input("You> ")
        except EOFError:
            break


def _create_prompt_session():
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
    except ImportError:
        return None
    return PromptSession(history=InMemoryHistory())


def _pending_id(pending: object) -> str:
    return str(getattr(pending, "id"))


def _pending_question(pending: object) -> str:
    return str(getattr(pending, "question", "需要用户输入。"))


def _pending_kind(pending: object) -> str:
    return str(getattr(pending, "kind", ""))


def _permission_choice_for_text(text: str, pending: object) -> str | None:
    normalized = text.strip().lower().replace(" ", "_")
    raw = text.strip()
    if raw.lower().startswith(("reject:", "reject_with_feedback:")):
        return f"reject_with_feedback: {raw.split(':', 1)[1].strip()}"
    aliases = {
        "1": "deny",
        "n": "deny",
        "no": "deny",
        "deny": "deny",
        "reject": "reject_with_feedback",
        "reject_with_feedback": "reject_with_feedback",
        "2": "allow_once",
        "y": "allow_once",
        "yes": "allow_once",
        "allow": "allow_once",
        "once": "allow_once",
        "allow_once": "allow_once",
        "3": "allow_always_same_scope",
        "always": "allow_always_same_scope",
        "allow_always": "allow_always_same_scope",
        "allow_always_same_scope": "allow_always_same_scope",
    }
    if normalized in aliases:
        return aliases[normalized]

    for index, option in enumerate(_permission_options(pending), start=1):
        option_id = _option_id(option)
        label = _option_label(option)
        values = {
            str(index).lower(),
            option_id.lower(),
            label.strip().lower().replace(" ", "_"),
        }
        if normalized in values:
            return option_id
    return None


def _permission_options_text(pending: object) -> str:
    question = _pending_question(pending)
    options = _permission_options(pending)
    option_lines = [f"  {index}. {_option_label(option)}" + (f" ({_option_id(option)})" if _option_id(option) != _option_label(option) else "") for index, option in enumerate(options, start=1)]
    if not option_lines:
        option_lines = [
            "  1. Deny",
            "  2. Allow once",
            "  3. Allow always for same scope",
        ]
    return "\n".join(
        [
            f"Permission> {question}",
            "Choose:",
            *option_lines,
        ]
    )


def _permission_choice_help_text(pending: object) -> str:
    count = len(_permission_options(pending)) or 3
    choices = ", ".join(str(index) for index in range(1, count + 1))
    return f"Please choose {choices}."


def _permission_options(pending: object) -> list[object]:
    return list(getattr(pending, "options", []) or [])


def _option_id(option: object) -> str:
    if isinstance(option, dict):
        return str(option.get("id") or option.get("label") or "")
    return str(getattr(option, "id", getattr(option, "label", "")))


def _option_label(option: object) -> str:
    if isinstance(option, dict):
        return str(option.get("label") or option.get("id") or "")
    return str(getattr(option, "label", getattr(option, "id", "")))


def _ask_user_prompt_text(pending: object) -> str:
    question = _pending_question(pending)
    options = _permission_options(pending)
    if not options:
        return f"Permission> {question}"
    option_lines = [f"  {index}. {_option_label(option)}" for index, option in enumerate(options, start=1)]
    return "\n".join([f"Permission> {question}", *option_lines])


def _ask_user_choice_for_text(text: str, pending: object) -> str | None:
    """Match a typed answer to one ask_user option by index, id, or label.

    Returns the option's canonical label so the answer keeps its meaning when
    sent back as the next user message; None means free-text answer.
    """
    normalized = text.strip().lower().replace(" ", "_")
    for index, option in enumerate(_permission_options(pending), start=1):
        label = _option_label(option)
        option_id = _option_id(option)
        values = {
            str(index).lower(),
            option_id.lower(),
            label.strip().lower().replace(" ", "_"),
        }
        if normalized in values:
            return label
    return None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed
