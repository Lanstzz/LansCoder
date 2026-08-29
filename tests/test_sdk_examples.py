"""Step 3 (P1) SC-7: SDK headless 示例冒烟测试。

在无 TUI 环境用子进程真实运行 ``examples/sdk/*.py``,断言退出码与关键输出,
防止文档里的示例与真实 API 漂移。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = PROJECT_ROOT / "examples" / "sdk"


def _run_example(script: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / script)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        check=False,
    )


def test_minimal_llm_transport_example_runs() -> None:
    proc = _run_example("minimal_llm_transport.py")
    assert proc.returncode == 0, proc.stderr
    assert "事件序列" in proc.stdout
    assert "agent_end" in proc.stdout


def test_headless_l3_example_runs_with_audit_and_resume() -> None:
    proc = _run_example("headless_l3_session.py")
    assert proc.returncode == 0, proc.stderr
    assert "[audit] tool_event" in proc.stdout
    assert "[L3] resumed session" in proc.stdout
    assert "first turn done" in proc.stdout
