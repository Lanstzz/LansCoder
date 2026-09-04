"""D7 结构性消除:双 dist 元数据契约。

- `lanscoder-core` 是 `lanscoder/` 导入树的唯一持有者(必装依赖仅最小集,
  package-data 含 `py.typed`/prompts/`app/*.tcss`)。
- `LansCoder` 是薄壳:wheel 不打包任何 `lanscoder/` 文件(`packages = []`),
  依赖 `lanscoder-core[llm,mcp]==<version>` + TUI 侧依赖,文件零重叠。
- 版本一致性:root 薄壳 `version` 与 `lanscoder-core` pin 都必须等于
  `lanscoder/core/_version.py` 的 `__version__`(D3/D7a,漂移即红)。
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_PYPROJECT = PROJECT_ROOT / "packages" / "lanscoder-core" / "pyproject.toml"


def _core_version() -> str:
    tree = ast.parse((PROJECT_ROOT / "lanscoder" / "core" / "_version.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("__version__ not found in _version.py")


def _root_pyproject() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _core_pyproject() -> dict:
    return tomllib.loads(CORE_PYPROJECT.read_text(encoding="utf-8"))


def test_root_version_matches_core_version_single_source() -> None:
    assert _root_pyproject()["project"]["version"] == _core_version()


def test_root_shell_depends_on_core_with_same_version_pin() -> None:
    deps = _root_pyproject()["project"]["dependencies"]
    core_pins = [d for d in deps if d.startswith("lanscoder-core")]
    assert len(core_pins) == 1
    assert core_pins[0] == f"lanscoder-core[llm,mcp]=={_core_version()}"


def test_root_shell_has_no_tui_runtime_deps_directly() -> None:
    """openai/anthropic/mcp/anyio/PyYAML/portalocker 由 core(及其 extras)提供。"""

    deps = _root_pyproject()["project"]["dependencies"]
    for name in ("openai", "anthropic", "mcp", "anyio", "PyYAML", "portalocker"):
        assert not any(d.startswith(name) for d in deps), f"root 不应直接依赖 {name}"


def test_root_shell_keeps_tui_side_deps_and_entrypoint() -> None:
    deps = _root_pyproject()["project"]["dependencies"]
    for name in ("textual", "prompt_toolkit", "tomlkit", "python-dotenv"):
        assert any(d.startswith(name) for d in deps), f"root 应直接依赖 {name}"
    assert _root_pyproject()["project"]["scripts"]["lanscoder"] == "lanscoder.cli:main"


def test_root_shell_ships_no_packages() -> None:
    """`LansCoder` wheel 不含任何 `lanscoder/` 文件 → 与 core 文件零重叠。"""

    setuptools_cfg = _root_pyproject().get("tool", {}).get("setuptools", {})
    assert setuptools_cfg.get("packages") == []
    assert "packages.find" not in _root_pyproject().get("tool", {}).get("setuptools", {})


def test_core_deps_are_minimal_and_no_tui() -> None:
    deps = _core_pyproject()["project"]["dependencies"]
    assert deps == ["anyio>=4,<4.15", "portalocker", "PyYAML"]
    for name in ("textual", "prompt_toolkit", "tomlkit", "python-dotenv", "openai", "anthropic", "mcp"):
        assert not any(d.startswith(name) for d in deps)


def test_core_extras_and_package_data() -> None:
    project = _core_pyproject()["project"]
    assert project["optional-dependencies"] == {
        "llm": ["openai", "anthropic"],
        "mcp": ["mcp>=1.28.1,<2"],
    }
    package_data = _core_pyproject()["tool"]["setuptools"]["package-data"]["lanscoder"]
    assert "py.typed" in package_data
    assert "context/prompts/*.md" in package_data
    assert "app/*.tcss" in package_data


def test_core_version_is_dynamic_via_setup_py() -> None:
    assert "version" in _core_pyproject()["project"]["dynamic"]
    assert (PROJECT_ROOT / "packages" / "lanscoder-core" / "setup.py").is_file()
