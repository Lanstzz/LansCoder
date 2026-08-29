"""Layer-boundary regression tests.

``lanscoder.tools`` must never transitively import ``lanscoder.agent``. The
delegate tool used to depend on ``lanscoder.agent.subagent_engine``, forming an
``agent -> tools -> agent`` import cycle that was survivable only because of a
lazy function-level import. These tests pin the one-way dependency in a fresh
interpreter so the assertion is not polluted by this test module's own imports.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fresh_import_leak_check(module: str, leaked_prefixes: tuple[str, ...]) -> str | None:
    """Import ``module`` in a fresh interpreter; return leaked-module names or None.

    ``leaked_prefixes`` selects which leaked modules are considered failures, so
    callers assert on exactly the boundary they care about without tripping on
    the module's own legitimate dependencies.
    """

    code = (
        "import sys\n"
        f"import {module}\n"
        f"leaked = [m for m in sys.modules if any(m.startswith(p) for p in {leaked_prefixes!r})]\n"
        "if leaked:\n"
        "    sys.stderr.write('leaked modules: %r\\n' % leaked)\n"
        "    raise SystemExit(1)\n"
    )
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + existing if existing else "")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        check=False,
    )
    if proc.returncode == 0:
        return None
    return proc.stderr


@pytest.mark.parametrize("module", ["lanscoder.tools", "lanscoder.tools.delegate", "lanscoder.tools.background"])
def test_tools_import_does_not_pull_agent(module: str) -> None:
    leaked = _fresh_import_leak_check(module, ("lanscoder.agent",))
    assert leaked is None, leaked


def test_loop_does_not_import_subagent_module() -> None:
    """AgentLoop must not pull in the subagent engine module.

    ``SubagentEngine`` depends on a ``child_runner_factory`` injected by the
    assembly root instead of importing ``AgentLoop``, so importing the loop must
    not transitively import the engine (and vice versa). This is what breaks the
    historical ``agent.loop -> agent.subagent -> agent.loop`` cycle.
    """

    leaked = _fresh_import_leak_check("lanscoder.agent.loop", ("lanscoder.agent.subagent_engine",))
    assert leaked is None, leaked


def test_core_import_does_not_pull_app() -> None:
    """``lanscoder.core`` 是 headless 装配源,必须能脱离 ``lanscoder.app`` 独立 import。

    Step 1 把装配根从 ``app/runtime.py`` 搬到 ``lanscoder/core/runtime.py``;若 core
    反向 import app 会形成环,这条约束锁死该边界。
    """

    leaked = _fresh_import_leak_check("lanscoder.core", ("lanscoder.app",))
    assert leaked is None, leaked


def test_core_runtime_import_does_not_pull_app() -> None:
    """装配根本身(``lanscoder.core.runtime``)不得把 app 拖进 import 链。"""

    leaked = _fresh_import_leak_check("lanscoder.core.runtime", ("lanscoder.app",))
    assert leaked is None, leaked


def test_agent_import_does_not_pull_core() -> None:
    """``lanscoder.agent`` 是下层引擎,不得反向依赖 core(依赖方向终态约束)。"""

    leaked = _fresh_import_leak_check("lanscoder.agent", ("lanscoder.core",))
    assert leaked is None, leaked


def test_core_import_does_not_pull_tui() -> None:
    """SDK 承诺(P1): ``lanscoder.core`` 可在完全不 import / 初始化 TUI 的情况下使用。

    ``textual`` 是 TUI 的唯一依赖;core 若把它拖进 import 链,headless 消费端
    就被迫装上整个 UI 栈。SC-6 泄漏检查的一部分。
    """

    leaked = _fresh_import_leak_check("lanscoder.core", ("textual",))
    assert leaked is None, leaked
