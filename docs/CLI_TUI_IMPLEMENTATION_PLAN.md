# CLI/TUI 体验增强实施计划

> **给 agentic worker 的要求：** 执行本计划时必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`。所有步骤使用 checkbox（`- [ ]`）格式，方便逐项跟踪。

**目标：** 让 FirstCoder 可以通过 `python -m firstcoder` 直接启动，并补齐基础 CLI/TUI 命令，让用户能自然体验一个本地 coding agent。

**架构：** 继续保持 `firstcoder/app` 作为薄应用层。新增 CLI 启动入口和命令处理器，复用现有 provider、session、context、permission 服务，不把业务逻辑塞进 Textual widget。

**技术栈：** Python 3.11+、Textual、pytest、现有 JSONL session store、现有 provider/session/context/permission 模块。

---

## 当前约束

- 项目目前没有 `pyproject.toml`、`setup.py`、`firstcoder/__main__.py`。
- app 组装入口在 `firstcoder/app/factory.py`。
- slash command 通过 `CompositeCommandHandler` 和多个聚焦 handler 路由。
- session 命令目前是纯文本模式；`/resume <session_id>` 可用，但 `/resume` 交互式选择器还没有。
- permission grant 已通过 `FilePermissionGrantStore` 持久化，但没有命令查看或撤销。
- LLM 当前通过环境变量或 `.env` 配置；项目没有专门的 JSON 配置文件。
- 默认 provider 是 `openai`，如果用户没有配置 `OPENAI_API_KEY`，当前直接创建 provider 会失败。
- 不做 `/tools enable` 或 `/tools disable`；工具集合保持内部实现细节，由 permission 控制风险。

## 文件改动地图

- 新建 `firstcoder/cli.py`：解析 CLI 参数、创建 Textual app、运行 app。
- 新建 `firstcoder/__main__.py`：支持 `python -m firstcoder`。
- 修改 `firstcoder/app/factory.py`：接入新的命令处理器和运行期配置视图。
- 修改 `firstcoder/app/runtime.py`：必要时承载 project/data/provider 等 `/config` 所需状态。
- 修改 `firstcoder/providers/factory.py`：增加 provider 配置状态检查，支持 `/config` 展示 readiness。
- 新建 `firstcoder/app/help_commands.py`：实现 `/help`。
- 新建 `firstcoder/app/config_commands.py`：实现 `/config`。
- 修改 `firstcoder/app/session_commands.py`：实现 `/new`，并保留 `/resume <id>`。
- 修改 `firstcoder/app/tui.py`：支持 `/resume` 无参数时打开 Textual picker。
- 新建 `firstcoder/app/permission_grant_commands.py`：实现 `/permissions` 和 `/permissions revoke <id>`。
- 修改 `firstcoder/permissions/grants.py`：为内存和文件 grant store 增加 `remove(grant_id)`。
- 修改 `README.md`：补 Python 版本、安装、LLM 环境变量和启动命令。
- 新增或扩展测试：
  - `tests/test_cli.py`
  - `tests/test_config.py`
  - `tests/test_app_help_commands.py`
  - `tests/test_app_config_commands.py`
  - `tests/test_app_session_commands.py`
  - `tests/test_app_tui.py`
  - `tests/test_permission_commands.py`
  - `tests/test_permissions_grants.py`
  - `tests/test_app_factory.py`

---

## 已有命令的覆盖说明

以下目标文档中的命令已经存在，本计划不重写，只在最终验收中回归验证：

- `/sessions`
- `/session <session_id>`
- `/resume <session_id>`
- `/share [session_id] [--tool-results]`
- `/rename <title>`
- `/mode`
- `/mode conservative|standard|aggressive`
- `/context`
- `/compact status`
- `/compact`

普通用户输入进入 `AgentLoop` 的能力也已经存在于 `AgentChatRunner` 和 `FirstCoderApp`，本计划只要求在最终验收中继续跑现有 app/runtime 测试。

---

## 任务 0：补齐 Provider 配置状态与启动错误体验

**目标覆盖：**
- `/config` 能说明 LLM 配置是否完整。
- 用户没配 API key 时，不看到难懂 traceback。
- 当前仍然使用环境变量或 `.env`，不新增 JSON 配置文件。

**文件：**
- 修改：`firstcoder/providers/factory.py`
- 测试：`tests/test_config.py`

- [ ] **步骤 1：先写 provider 配置状态失败测试**

追加到 `tests/test_config.py`：

```python
from firstcoder.providers.factory import inspect_provider_config


def test_inspect_provider_config_reports_missing_api_key() -> None:
    config = AppConfig(provider_name="openai", env={})

    status = inspect_provider_config(config)

    assert status.provider_name == "openai"
    assert status.ready is False
    assert status.missing_env == "OPENAI_API_KEY"
    assert "OPENAI_API_KEY" in status.error


def test_inspect_provider_config_reports_ready_provider() -> None:
    config = AppConfig(provider_name="deepseek", env={"DEEPSEEK_API_KEY": "test-key"})

    status = inspect_provider_config(config)

    assert status.provider_name == "deepseek"
    assert status.model == "deepseek-chat"
    assert status.ready is True
    assert status.error is None
```

- [ ] **步骤 2：运行测试，确认失败原因正确**

运行：

```bash
.venv/bin/python -m pytest tests/test_config.py::test_inspect_provider_config_reports_missing_api_key tests/test_config.py::test_inspect_provider_config_reports_ready_provider -q
```

预期：失败，原因是 `inspect_provider_config` 不存在。

- [ ] **步骤 3：实现 provider 配置状态 helper**

在 `firstcoder/providers/factory.py` 中增加：

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderConfigStatus:
    provider_name: str
    model: str
    base_url: str | None
    ready: bool
    missing_env: str | None = None
    error: str | None = None


def inspect_provider_config(config: AppConfig) -> ProviderConfigStatus:
    selected = config.provider_name
    if selected in {"openai-compatible", "custom"}:
        api_key = config.get_env("FIRSTCODER_API_KEY")
        model = config.get_env("FIRSTCODER_MODEL") or ""
        base_url = config.get_env("FIRSTCODER_BASE_URL")
        if not api_key:
            return ProviderConfigStatus(selected, model, base_url, False, "FIRSTCODER_API_KEY", "缺少环境变量：FIRSTCODER_API_KEY")
        if not model:
            return ProviderConfigStatus(selected, "", base_url, False, "FIRSTCODER_MODEL", "缺少环境变量：FIRSTCODER_MODEL")
        return ProviderConfigStatus(
            config.get_env("FIRSTCODER_PROVIDER_NAME", "openai-compatible") or "openai-compatible",
            model,
            base_url,
            True,
        )

    preset = PROVIDER_PRESETS.get(selected)
    if preset is None:
        supported = ", ".join(sorted([*PROVIDER_PRESETS.keys(), "openai-compatible", "custom"]))
        return ProviderConfigStatus(selected, "", None, False, None, f"不支持的 provider：{selected}。当前支持：{supported}")

    api_key = config.get_env(preset.api_key_env)
    if not api_key and preset.name == "ollama":
        api_key = "ollama"
    model = config.get_env(preset.model_env) or preset.default_model
    base_url = config.get_env(preset.base_url_env) if preset.base_url_env else None
    base_url = base_url or preset.default_base_url
    if not api_key:
        return ProviderConfigStatus(preset.name, model, base_url, False, preset.api_key_env, f"缺少环境变量：{preset.api_key_env}")
    return ProviderConfigStatus(preset.name, model, base_url, True)
```

- [ ] **步骤 4：运行配置测试**

运行：

```bash
.venv/bin/python -m pytest tests/test_config.py -q
```

预期：通过。

- [ ] **步骤 5：提交**

```bash
git add firstcoder/providers/factory.py tests/test_config.py
git commit -m "feat: inspect provider config status"
```

---

## 任务 1：增加 `python -m firstcoder` 启动入口

**文件：**
- 新建：`firstcoder/cli.py`
- 新建：`firstcoder/__main__.py`
- 测试：`tests/test_cli.py`

- [ ] **步骤 1：先写失败测试**

新增 `tests/test_cli.py`：

```python
from __future__ import annotations

from pathlib import Path

from firstcoder.cli import build_app, main
from firstcoder.app.tui import FirstCoderApp


def test_build_app_uses_project_root_and_data_root(tmp_path: Path) -> None:
    app = build_app(project_root=tmp_path, data_root=tmp_path / ".firstcoder-test", provider_name="ollama")

    assert isinstance(app, FirstCoderApp)
    assert app.current_session is not None
    assert app.current_session.session_id


def test_main_returns_zero_when_app_run_succeeds(monkeypatch, tmp_path: Path) -> None:
    calls = []

    class FakeApp:
        def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr("firstcoder.cli.build_app", lambda **kwargs: FakeApp())

    result = main(["--project-root", str(tmp_path)])

    assert result == 0
    assert calls == ["run"]


def test_main_returns_nonzero_for_provider_config_error(monkeypatch, capsys, tmp_path: Path) -> None:
    from firstcoder.providers.factory import ProviderConfigError

    def fail_build_app(**kwargs):
        raise ProviderConfigError("缺少环境变量：OPENAI_API_KEY")

    monkeypatch.setattr("firstcoder.cli.build_app", fail_build_app)

    result = main(["--project-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert "OPENAI_API_KEY" in captured.err
```

- [ ] **步骤 2：运行测试，确认失败原因正确**

运行：

```bash
.venv/bin/python -m pytest tests/test_cli.py -q
```

预期：失败，原因是 `firstcoder.cli` 还不存在。

- [ ] **步骤 3：实现最小 CLI 模块**

新建 `firstcoder/cli.py`：

```python
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from firstcoder.app.factory import create_firstcoder_app
from firstcoder.app.tui import FirstCoderApp
from firstcoder.providers.factory import ProviderConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firstcoder", description="Run the FirstCoder TUI.")
    parser.add_argument("--project-root", default=".", help="Project root to operate in.")
    parser.add_argument("--data-root", default=None, help="Data root for sessions and permissions.")
    parser.add_argument("--provider", default=None, help="Override FIRSTCODER_PROVIDER for this run.")
    return parser


def build_app(
    *,
    project_root: str | Path = ".",
    data_root: str | Path | None = None,
    provider_name: str | None = None,
) -> FirstCoderApp:
    from firstcoder.providers.factory import create_provider

    provider = create_provider(provider_name) if provider_name else None
    return create_firstcoder_app(project_root=project_root, data_root=data_root, provider=provider)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        app = build_app(
            project_root=args.project_root,
            data_root=args.data_root,
            provider_name=args.provider,
        )
    except ProviderConfigError as exc:
        parser.exit(2, f"FirstCoder provider config error: {exc}\n")
    app.run()
    return 0
```

新建 `firstcoder/__main__.py`：

```python
from __future__ import annotations

from firstcoder.cli import main


raise SystemExit(main())
```

- [ ] **步骤 4：运行 CLI 测试**

运行：

```bash
.venv/bin/python -m pytest tests/test_cli.py -q
```

预期：通过。

- [ ] **步骤 5：提交**

```bash
git add firstcoder/cli.py firstcoder/__main__.py tests/test_cli.py
git commit -m "feat: add firstcoder module entrypoint"
```

---

## 任务 2：增加 `/help`

**文件：**
- 新建：`firstcoder/app/help_commands.py`
- 修改：`firstcoder/app/factory.py`
- 测试：`tests/test_app_help_commands.py`、`tests/test_app_factory.py`

- [ ] **步骤 1：先写 `/help` 失败测试**

新建 `tests/test_app_help_commands.py`：

```python
from firstcoder.app.help_commands import HelpCommandHandler


def test_help_command_lists_core_commands() -> None:
    result = HelpCommandHandler().handle("/help")

    assert result.handled is True
    assert "/new" in result.output
    assert "/resume" in result.output
    assert "/permissions" in result.output
    assert "/config" in result.output


def test_help_command_ignores_non_help_commands() -> None:
    result = HelpCommandHandler().handle("/sessions")

    assert result.handled is False
```

- [ ] **步骤 2：运行测试，确认失败原因正确**

运行：

```bash
.venv/bin/python -m pytest tests/test_app_help_commands.py -q
```

预期：失败，原因是 `firstcoder.app.help_commands` 还不存在。

- [ ] **步骤 3：实现 help handler**

新建 `firstcoder/app/help_commands.py`：

```python
from __future__ import annotations

from dataclasses import dataclass

from firstcoder.app.commands import CommandResult


HELP_TEXT = """FirstCoder commands:
/help                         Show this help.
/config                       Show provider, model, project, data, session, and permission status.
/new                          Start a new session.
/sessions                     List saved sessions.
/session <session_id>          Show one session summary.
/resume                       Pick a session to resume.
/resume <session_id>           Resume a session by id.
/share [session_id]            Export a Markdown transcript.
/rename <title>                Rename the current session.
/permissions                  List persistent permission grants.
/permissions revoke <grant_id> Revoke a persistent permission grant.
/mode                         Show permission mode.
/mode <mode>                   Set permission mode: conservative, standard, aggressive.
/context                      Show context status.
/compact status               Show compaction status.
/compact                      Manually compact context.
"""


@dataclass(slots=True)
class HelpCommandHandler:
    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if command != "/help":
            return CommandResult(handled=False)
        return CommandResult(handled=True, output=HELP_TEXT.strip())
```

- [ ] **步骤 4：在 factory 中接入 help handler**

修改 `firstcoder/app/factory.py` import：

```python
from firstcoder.app.help_commands import HelpCommandHandler
```

修改 command handler 组装：

```python
command_handler = CompositeCommandHandler(
    [
        HelpCommandHandler(),
        session_handler,
        context_handler,
        permission_handler,
    ]
)
```

- [ ] **步骤 5：运行聚焦测试**

运行：

```bash
.venv/bin/python -m pytest tests/test_app_help_commands.py tests/test_app_factory.py -q
```

预期：通过。

- [ ] **步骤 6：提交**

```bash
git add firstcoder/app/help_commands.py firstcoder/app/factory.py tests/test_app_help_commands.py
git commit -m "feat: add help command"
```

---

## 任务 3：增加 `/config`

**文件：**
- 修改：`firstcoder/app/runtime.py`
- 修改：`firstcoder/app/factory.py`
- 新建：`firstcoder/app/config_commands.py`
- 测试：`tests/test_app_config_commands.py`、`tests/test_app_factory.py`

- [ ] **步骤 1：先写 `/config` 失败测试**

新建 `tests/test_app_config_commands.py`：

```python
from dataclasses import dataclass
from pathlib import Path

from firstcoder.app.config_commands import ConfigCommandHandler, RuntimeConfigView


@dataclass(slots=True)
class FakeCurrentSession:
    session_id: str = "sess_test"
    mode: str = "standard"


def test_config_command_renders_runtime_status(tmp_path: Path) -> None:
    view = RuntimeConfigView(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider_name="fake",
        provider_model="fake-model",
        provider_ready=True,
        provider_error=None,
        current_session=FakeCurrentSession(),
    )
    result = ConfigCommandHandler(view=view).handle("/config")

    assert result.handled is True
    assert "Provider: fake" in result.output
    assert "Model: fake-model" in result.output
    assert "Session: sess_test" in result.output
    assert "Permission mode: standard" in result.output
    assert f"Project root: {tmp_path}" in result.output


def test_config_command_renders_provider_error(tmp_path: Path) -> None:
    view = RuntimeConfigView(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider_name="openai",
        provider_model="gpt-4.1-mini",
        provider_ready=False,
        provider_error="缺少环境变量：OPENAI_API_KEY",
        current_session=FakeCurrentSession(),
    )

    result = ConfigCommandHandler(view=view).handle("/config")

    assert "Provider ready: no" in result.output
    assert "OPENAI_API_KEY" in result.output


def test_config_command_ignores_other_commands(tmp_path: Path) -> None:
    view = RuntimeConfigView(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider_name="fake",
        provider_model="fake-model",
        provider_ready=True,
        provider_error=None,
        current_session=FakeCurrentSession(),
    )

    assert ConfigCommandHandler(view=view).handle("/context").handled is False
```

- [ ] **步骤 2：运行测试，确认失败原因正确**

运行：

```bash
.venv/bin/python -m pytest tests/test_app_config_commands.py -q
```

预期：失败，原因是模块不存在。

- [ ] **步骤 3：实现 config command**

新建 `firstcoder/app/config_commands.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from firstcoder.app.commands import CommandResult


class CurrentSessionLike(Protocol):
    session_id: str
    mode: str


@dataclass(slots=True)
class RuntimeConfigView:
    project_root: Path
    data_root: Path
    provider_name: str
    provider_model: str
    provider_ready: bool
    provider_error: str | None
    current_session: CurrentSessionLike


@dataclass(slots=True)
class ConfigCommandHandler:
    view: RuntimeConfigView

    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if command != "/config":
            return CommandResult(handled=False)
        ready = "yes" if self.view.provider_ready else "no"
        lines = [
            f"Provider: {self.view.provider_name}",
            f"Model: {self.view.provider_model}",
            f"Provider ready: {ready}",
            f"Project root: {self.view.project_root}",
            f"Data root: {self.view.data_root}",
            f"Session: {self.view.current_session.session_id}",
            f"Permission mode: {self.view.current_session.mode}",
        ]
        if self.view.provider_error:
            lines.append(f"Provider error: {self.view.provider_error}")
        return CommandResult(handled=True, output="\n".join(lines))
```

- [ ] **步骤 4：在 factory 中接入 config view**

修改 `firstcoder/app/factory.py` import：

```python
from firstcoder.app.config_commands import ConfigCommandHandler, RuntimeConfigView
```

在 `current = CurrentSessionState(session)` 之后创建。不要硬编码 `provider_ready=True`，应使用任务 0 的配置状态：

```python
from firstcoder.config import load_config
from firstcoder.providers.factory import inspect_provider_config

provider_status = inspect_provider_config(load_config(getattr(resolved_provider, "name", None)))
config_view = RuntimeConfigView(
    project_root=project_path.resolve(),
    data_root=resolved_data_root.resolve(),
    provider_name=provider_status.provider_name,
    provider_model=provider_status.model or resolved_provider.model,
    provider_ready=provider_status.ready,
    provider_error=provider_status.error,
    current_session=current,
)
```

如果实现时发现 `load_config(resolved_provider.name)` 会影响自定义 provider 场景，则调整为让 `create_firstcoder_app()` 接收可选 `provider_config_status` 参数，由 CLI 或 factory 传入。测试必须覆盖 provider error 能显示在 `/config` 中。

在 `CompositeCommandHandler` 中把 `ConfigCommandHandler(config_view)` 放到 `HelpCommandHandler()` 后面。

- [ ] **步骤 5：运行聚焦测试**

运行：

```bash
.venv/bin/python -m pytest tests/test_app_config_commands.py tests/test_app_factory.py -q
```

预期：通过。

- [ ] **步骤 6：提交**

```bash
git add firstcoder/app/config_commands.py firstcoder/app/factory.py tests/test_app_config_commands.py
git commit -m "feat: add config command"
```

---

## 任务 4：增加 `/new`

**文件：**
- 修改：`firstcoder/app/session_commands.py`
- 修改：`firstcoder/app/factory.py`
- 测试：`tests/test_app_session_commands.py`

- [ ] **步骤 1：先写 `/new` 失败测试**

追加到 `tests/test_app_session_commands.py`：

```python
def test_new_command_creates_and_switches_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    first = AgentSession.create(store=store, session_id="sess_one", agents_md="")
    state = CurrentSessionState(first)
    switched = []
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        store=store,
        new_session_factory=lambda: AgentSession.create(store=store, session_id="sess_two", agents_md=""),
        on_resume=lambda session: (state.set_session(session), switched.append(session)),
    )

    result = handler.handle("/new")

    assert result.handled is True
    assert "New session: sess_two" in result.output
    assert state.session_id == "sess_two"
    assert switched[0].session_id == "sess_two"
```

- [ ] **步骤 2：运行测试，确认失败原因正确**

运行：

```bash
.venv/bin/python -m pytest tests/test_app_session_commands.py::test_new_command_creates_and_switches_session -q
```

预期：失败，原因是 `new_session_factory` 还不被接受，且 `/new` 未知。

- [ ] **步骤 3：在 `SessionCommandHandler` 中实现 `/new`**

修改 `firstcoder/app/session_commands.py` import：

```python
from collections.abc import Callable
```

更新 dataclass 字段：

```python
new_session_factory: Callable[[], SessionRuntimeLike] | None = None
```

在 `handle()` 中 `/sessions` 之前加入：

```python
if name == "/new":
    return CommandResult(handled=True, output=self._new(args))
```

新增方法：

```python
def _new(self, args: list[str]) -> str:
    if args:
        return "Usage: /new"
    if self.new_session_factory is None:
        return "New session unavailable: session factory is not configured"

    session = self.new_session_factory()
    self.current_session = session
    if self.on_resume is not None:
        self.on_resume(session)
    return f"New session: {session.session_id}"
```

- [ ] **步骤 4：在 app factory 中接入新 session 工厂**

在 `firstcoder/app/factory.py` 的 `resume_service` 后定义：

```python
def new_session() -> AgentSession:
    return AgentSession.from_project(
        store=store,
        session_id=new_session_id(),
        project_root=project_path,
        tools=resolved_tools,
        permission_manager=create_project_permission_manager(project_path, grants=grant_store),
    )
```

传给 `SessionCommandHandler`：

```python
new_session_factory=new_session,
```

- [ ] **步骤 5：运行聚焦测试**

运行：

```bash
.venv/bin/python -m pytest tests/test_app_session_commands.py tests/test_app_factory.py -q
```

预期：通过。

- [ ] **步骤 6：提交**

```bash
git add firstcoder/app/session_commands.py firstcoder/app/factory.py tests/test_app_session_commands.py
git commit -m "feat: add new session command"
```

---

## 任务 5：增加长期权限授权查看和撤销

**文件：**
- 修改：`firstcoder/permissions/grants.py`
- 新建：`firstcoder/app/permission_grant_commands.py`
- 修改：`firstcoder/app/factory.py`
- 测试：`tests/test_permissions_grants.py`、`tests/test_permission_commands.py`

- [ ] **步骤 1：先写 grant store 撤销失败测试**

追加到 `tests/test_permissions_grants.py`：

```python
def test_file_permission_grant_store_removes_grants(tmp_path) -> None:
    path = tmp_path / "permissions.json"
    store = FilePermissionGrantStore(path)
    store.add(_grant("grant_pytest"))

    removed = store.remove("grant_pytest")
    reloaded = FilePermissionGrantStore(path)

    assert removed is True
    assert reloaded.list() == []
```

- [ ] **步骤 2：运行测试，确认失败原因正确**

运行：

```bash
.venv/bin/python -m pytest tests/test_permissions_grants.py::test_file_permission_grant_store_removes_grants -q
```

预期：失败，原因是 `remove` 不存在。

- [ ] **步骤 3：实现 `remove()`**

在 `PermissionGrantStore` 中增加：

```python
def remove(self, grant_id: str) -> bool:
    before = len(self._grants)
    self._grants = [grant for grant in self._grants if grant.id != grant_id]
    return len(self._grants) != before
```

在 `FilePermissionGrantStore` 中增加：

```python
def remove(self, grant_id: str) -> bool:
    removed = super().remove(grant_id)
    if removed:
        self.save()
    return removed
```

- [ ] **步骤 4：先写 `/permissions` 命令失败测试**

追加到 `tests/test_permission_commands.py`：

```python
from firstcoder.app.permission_grant_commands import PermissionGrantCommandHandler
from firstcoder.permissions.grants import FilePermissionGrantStore
from firstcoder.permissions.types import PermissionAction, PermissionGrant, PermissionScopeType


def test_permissions_command_lists_persistent_grants(tmp_path) -> None:
    store = FilePermissionGrantStore(tmp_path / "permissions.json")
    store.add(
        PermissionGrant(
            id="grant_pytest",
            effect="allow",
            action=PermissionAction.EXECUTE_SHELL,
            scope_type=PermissionScopeType.COMMAND_PREFIX,
            scope_value="pytest",
            created_at="2026-06-04T00:00:00Z",
            reason="用户选择 allow always。",
        )
    )

    result = PermissionGrantCommandHandler(grants=store).handle("/permissions")

    assert result.handled is True
    assert "grant_pytest" in result.output
    assert "execute_shell" in result.output
    assert "pytest" in result.output


def test_permissions_revoke_removes_grant(tmp_path) -> None:
    store = FilePermissionGrantStore(tmp_path / "permissions.json")
    store.add(
        PermissionGrant(
            id="grant_pytest",
            effect="allow",
            action=PermissionAction.EXECUTE_SHELL,
            scope_type=PermissionScopeType.COMMAND_PREFIX,
            scope_value="pytest",
            created_at="2026-06-04T00:00:00Z",
        )
    )

    result = PermissionGrantCommandHandler(grants=store).handle("/permissions revoke grant_pytest")

    assert result.output == "Revoked permission grant: grant_pytest"
    assert store.list() == []
```

- [ ] **步骤 5：实现 permission grant command handler**

新建 `firstcoder/app/permission_grant_commands.py`：

```python
from __future__ import annotations

from dataclasses import dataclass

from firstcoder.app.commands import CommandResult
from firstcoder.permissions.grants import PermissionGrantStore
from firstcoder.permissions.types import PermissionGrant


@dataclass(slots=True)
class PermissionGrantCommandHandler:
    grants: PermissionGrantStore

    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if not command.startswith("/permissions"):
            return CommandResult(handled=False)

        parts = command.split()
        if len(parts) == 1:
            return CommandResult(handled=True, output=self._list())
        if len(parts) == 3 and parts[1] == "revoke":
            return CommandResult(handled=True, output=self._revoke(parts[2]))
        return CommandResult(handled=True, output="Usage: /permissions OR /permissions revoke <grant_id>")

    def _list(self) -> str:
        grants = self.grants.list()
        if not grants:
            return "No persistent permission grants."
        lines = ["Persistent permission grants:"]
        for grant in grants:
            lines.append(_render_grant(grant))
        return "\n".join(lines)

    def _revoke(self, grant_id: str) -> str:
        if self.grants.remove(grant_id):
            return f"Revoked permission grant: {grant_id}"
        return f"Permission grant not found: {grant_id}"


def _render_grant(grant: PermissionGrant) -> str:
    return (
        f"- {grant.id} {grant.effect} {grant.action.value} "
        f"{grant.scope_type.value} {grant.scope_value}"
    )
```

- [ ] **步骤 6：在 app factory 中接入 grant handler**

修改 `firstcoder/app/factory.py` import：

```python
from firstcoder.app.permission_grant_commands import PermissionGrantCommandHandler
```

在 composite command list 中，把 `PermissionGrantCommandHandler(grant_store)` 放到 `permission_handler` 前面。

- [ ] **步骤 7：运行聚焦测试**

运行：

```bash
.venv/bin/python -m pytest tests/test_permissions_grants.py tests/test_permission_commands.py tests/test_app_factory.py -q
```

预期：通过。

- [ ] **步骤 8：提交**

```bash
git add firstcoder/permissions/grants.py firstcoder/app/permission_grant_commands.py tests/test_permissions_grants.py tests/test_permission_commands.py firstcoder/app/factory.py
git commit -m "feat: add permission grant commands"
```

---

## 任务 6：增加 `/resume` 交互式选择器

**文件：**
- 修改：`firstcoder/app/session_commands.py`
- 修改：`firstcoder/app/tui.py`
- 测试：`tests/test_app_session_commands.py`、`tests/test_app_tui.py`

- [ ] **步骤 1：增加结构化 picker 请求**

在 `firstcoder/app/session_commands.py` 中增加：

```python
@dataclass(frozen=True, slots=True)
class ResumePickerRequest:
    records: list[SessionRecord]
```

不要改共享的 `CommandResult`。第一版先在 `SessionCommandHandler` 上增加状态：

```python
last_resume_picker: ResumePickerRequest | None = None
```

- [ ] **步骤 2：先写 `/resume` 无参数失败测试**

追加到 `tests/test_app_session_commands.py`：

```python
def test_resume_without_id_requests_picker(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _make_session(store, "sess_one", title="第一个")
    _make_session(store, "sess_two", title="第二个")
    handler = SessionCommandHandler(catalog=SessionCatalog(tmp_path))

    result = handler.handle("/resume")

    assert result.handled is True
    assert result.output == "__FIRSTCODER_RESUME_PICKER__"
    assert handler.last_resume_picker is not None
    assert [record.session_id for record in handler.last_resume_picker.records] == ["sess_two", "sess_one"]
```

- [ ] **步骤 3：实现 `/resume` 无参数 picker request**

在 `_resume()` 中加入：

```python
if len(args) == 0:
    records = [record for record in self.catalog.list_sessions() if record.status == "ok"]
    if not records:
        return "No resumable sessions."
    self.last_resume_picker = ResumePickerRequest(records=records)
    return "__FIRSTCODER_RESUME_PICKER__"
```

在 `handle()` 开始处设置：

```python
self.last_resume_picker = None
```

- [ ] **步骤 4：运行 session command 测试**

运行：

```bash
.venv/bin/python -m pytest tests/test_app_session_commands.py::test_resume_without_id_requests_picker -q
```

预期：通过。

- [ ] **步骤 5：先写 TUI picker 失败测试**

追加到 `tests/test_app_tui.py`：

```python
class PickerCommandHandler:
    def __init__(self) -> None:
        from firstcoder.app.session_commands import ResumePickerRequest
        from firstcoder.session.models import SessionRecord

        self.last_resume_picker = None
        self.resume_calls = []
        self.request = ResumePickerRequest(
            records=[
                SessionRecord(session_id="sess_one", title="第一个"),
                SessionRecord(session_id="sess_two", title="第二个"),
            ]
        )

    def handle(self, text: str) -> CommandResult:
        if text == "/resume":
            self.last_resume_picker = self.request
            return CommandResult(handled=True, output="__FIRSTCODER_RESUME_PICKER__")
        if text.startswith("/resume "):
            self.resume_calls.append(text)
            return CommandResult(handled=True, output=f"Resumed session: {text.split()[1]}")
        return CommandResult(handled=False)


@pytest.mark.anyio
async def test_resume_command_opens_picker_and_enter_resumes() -> None:
    handler = PickerCommandHandler()
    app = FirstCoderApp(command_handler=handler)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/resume")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause()

    assert handler.resume_calls == ["/resume sess_one"]


@pytest.mark.anyio
async def test_resume_picker_down_then_enter_resumes_second_session() -> None:
    handler = PickerCommandHandler()
    app = FirstCoderApp(command_handler=handler)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/resume")
        await pilot.press("enter")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

    assert handler.resume_calls == ["/resume sess_two"]


@pytest.mark.anyio
async def test_resume_picker_escape_cancels_without_resuming() -> None:
    handler = PickerCommandHandler()
    app = FirstCoderApp(command_handler=handler)

    async with app.run_test() as pilot:
        await pilot.click("#input")
        await pilot.press(*"/resume")
        await pilot.press("enter")
        await pilot.press("escape")
        await pilot.pause()

    assert handler.resume_calls == []
```

- [ ] **步骤 6：实现最小 Textual resume picker**

修改 `firstcoder/app/tui.py` imports：

```python
from textual.widgets import Footer, Header, Input, RichLog, OptionList
from textual.widgets.option_list import Option
```

在 `compose()` 中，在 `RichLog` 后加入：

```python
yield OptionList(id="resume-picker")
```

在 `on_mount()` 中隐藏 picker：

```python
picker = self.query_one("#resume-picker", OptionList)
picker.display = False
```

在 command 处理里，`result = self.command_handler.handle(text)` 后加入：

```python
picker_request = getattr(self.command_handler, "last_resume_picker", None)
if result.handled and result.output == "__FIRSTCODER_RESUME_PICKER__" and picker_request is not None:
    self._open_resume_picker(picker_request)
    return
```

新增方法：

```python
def _open_resume_picker(self, request) -> None:
    picker = self.query_one("#resume-picker", OptionList)
    picker.clear_options()
    for record in request.records:
        label = f"{record.title}  {record.session_id}"
        picker.add_option(Option(label, id=record.session_id))
    picker.display = True
    picker.focus()

def _close_resume_picker(self) -> None:
    picker = self.query_one("#resume-picker", OptionList)
    picker.display = False
```

新增事件处理：

```python
async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
    if event.option_list.id != "resume-picker":
        return
    session_id = str(event.option.id)
    self._close_resume_picker()
    output = self.query_one("#output", RichLog)
    result = self.command_handler.handle(f"/resume {session_id}")
    output.write(result.output)
    self._refresh_session_subtitle()
```

增加 Esc 取消：

```python
BINDINGS = [("ctrl+c", "quit", "Quit"), ("escape", "cancel_picker", "Cancel")]

def action_cancel_picker(self) -> None:
    picker = self.query_one("#resume-picker", OptionList)
    if picker.display:
        self._close_resume_picker()
        self.query_one("#input", Input).focus()
```

- [ ] **步骤 7：运行 TUI 和 session 测试**

运行：

```bash
.venv/bin/python -m pytest tests/test_app_session_commands.py tests/test_app_tui.py -q
```

预期：通过。

- [ ] **步骤 7.5：手动验收 picker 交互**

运行：

```bash
.venv/bin/python -m firstcoder --provider ollama
```

在 TUI 中输入：

```text
/resume
```

预期：

- 出现历史会话列表。
- `↑` / `↓` 可以移动选择。
- `Enter` 恢复当前选中会话。
- `Esc` 关闭选择器，不切换 session。

- [ ] **步骤 8：提交**

```bash
git add firstcoder/app/session_commands.py firstcoder/app/tui.py tests/test_app_session_commands.py tests/test_app_tui.py
git commit -m "feat: add resume picker"
```

---

## 任务 7：更新 README 和目标文档

**文件：**
- 修改：`README.md`
- 修改：`docs/CLI_TUI_GOAL.md`
- 测试：如影响 README provider 文档，运行 `tests/test_readme_provider_docs.py`，最后跑全量测试。

- [ ] **步骤 1：更新 README 启动说明**

在 `README.md` 的本地环境部分后加入：

```markdown
## 启动

FirstCoder 当前需要 Python 3.11+。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m firstcoder
```

如果需要指定 provider：

```bash
FIRSTCODER_PROVIDER=deepseek DEEPSEEK_API_KEY=your-key python -m firstcoder
```
```

- [ ] **步骤 2：更新目标文档状态**

在 `docs/CLI_TUI_GOAL.md` 中加入：

```markdown
## 实施状态

本文档对应的实施计划见 `docs/CLI_TUI_IMPLEMENTATION_PLAN.md`。
```

- [ ] **步骤 3：运行文档相关测试和全量测试**

运行：

```bash
.venv/bin/python -m pytest tests/test_readme_provider_docs.py -q
.venv/bin/python -m pytest -q
```

预期：通过。

- [ ] **步骤 4：提交**

```bash
git add README.md docs/CLI_TUI_GOAL.md
git commit -m "docs: document cli tui launch flow"
```

---

## 最终验收

- [ ] 运行全量测试：

```bash
.venv/bin/python -m pytest -q
```

预期：

```text
555+ passed
```

- [ ] 回归验证已有命令仍可用：

```bash
.venv/bin/python -m pytest tests/test_app_session_commands.py tests/test_permission_commands.py tests/test_app_context_commands.py -q
```

预期：

- `/sessions`、`/session <id>`、`/resume <id>`、`/share`、`/rename` 仍通过测试。
- `/mode` 仍通过测试。
- `/context`、`/compact status`、`/compact` 仍通过测试。

- [ ] smoke test：模块入口帮助：

```bash
.venv/bin/python -m firstcoder --help
```

预期：argparse help 输出包含 `--project-root`、`--data-root`、`--provider`。

- [ ] smoke test：缺少默认 OpenAI key 时有清晰错误：

```bash
env -u OPENAI_API_KEY .venv/bin/python -m firstcoder --project-root .
```

预期：退出码非 0，stderr 包含 `OPENAI_API_KEY`，不输出 Python traceback。

- [ ] smoke test：app import：

```bash
.venv/bin/python - <<'PY'
from firstcoder.cli import build_app
app = build_app(provider_name="ollama")
print(type(app).__name__)
PY
```

预期：

```text
FirstCoderApp
```

## 自检

- 覆盖目标：直接 CLI 启动、`/help`、`/config`、`/new`、`/resume` picker、`/permissions`、文档更新。
- LLM 配置覆盖：启动缺 key 时有清晰错误，`/config` 能显示 provider readiness 和 provider error。
- 既有命令覆盖：`/sessions`、`/session`、`/resume <id>`、`/share`、`/mode`、`/context`、`/compact` 都通过回归测试保留。
- 遵守非目标：不实现 `/tools enable` 或 `/tools disable`。
- 测试覆盖：每个命令都有聚焦测试；factory 和 TUI wiring 有集成测试。
- 已知风险：`/resume` picker 第一版使用 sentinel 输出字符串 `__FIRSTCODER_RESUME_PICKER__`。这能快速落地，后续如果 command router 继续增长，可以把 `CommandResult` 升级成带结构化 payload 的结果对象。
