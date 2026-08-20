"""Layer-boundary regression tests.

``lanscoder.tools`` must never transitively import ``lanscoder.agent``. The
delegate tool used to depend on ``lanscoder.agent.subagent``, forming an
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


@pytest.mark.parametrize("module", ["lanscoder.tools", "lanscoder.tools.delegate"])
def test_tools_import_does_not_pull_agent(module: str) -> None:
    code = (
        "import sys\n"
        f"import {module}\n"
        "leaked = [m for m in sys.modules if m.startswith('lanscoder.agent')]\n"
        "if leaked:\n"
        "    sys.stderr.write('agent modules leaked: %r\\n' % leaked)\n"
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
    assert proc.returncode == 0, proc.stderr
