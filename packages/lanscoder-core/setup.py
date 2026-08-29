"""lanscoder-core 打包入口。

版本单一事实来源是仓库根的 ``lanscoder/core/_version.py``;这里用 AST 静态解析
(不 import 任何包),保证双 dist 版本一致(D3)。
"""

from __future__ import annotations

import ast
from pathlib import Path

from setuptools import setup

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERSION_FILE = PROJECT_ROOT / "lanscoder" / "core" / "_version.py"

tree = ast.parse(VERSION_FILE.read_text(encoding="utf-8"))
version: str | None = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
    ):
        version = ast.literal_eval(node.value)
        break
if version is None:
    raise RuntimeError(f"cannot find __version__ in {VERSION_FILE}")

setup(version=version)
