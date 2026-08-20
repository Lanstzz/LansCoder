from __future__ import annotations

import subprocess

from lanscoder.utils.sandbox import PathSandbox
from lanscoder.utils.execution_sandbox import ExecutionSandbox


def run_git(sandbox: PathSandbox, args: list[str]) -> subprocess.CompletedProcess[str]:

    execution_sandbox = ExecutionSandbox(sandbox.root)
    try:
        return subprocess.run(
            ["git", *args],
            cwd=sandbox.root,
            env=execution_sandbox.build_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(["git", *args], returncode=1, stdout="", stderr=str(exc))
