"""终态依赖方向断言(spec §6-4 / §6-2,Task 11 收口).

约束:
- `permissions/` 不得引用 `tools//agent//app/`,parse_patch 只经 `utils.patch`。
- `tools/`、`mcp/` 不得引用 `permissions/`。
- 全仓(lanscoder + tests)不得再出现 runtime 模块路径(lanscoder 与 runtime
  的点分拼接字面量)。
- 分类表键与 18 名门控集合、builtin 注册表已知名双向一致。

用 AST 而非 grep,是为了覆盖 `if TYPE_CHECKING:` 内的 import,同时排除
仅描述约束的 docstring 字面量造成的误报。
"""

from __future__ import annotations

import ast
from pathlib import Path

from lanscoder.permissions.classification import classify
from lanscoder.tools import create_builtin_registry

ROOT = Path(__file__).resolve().parents[1]
LANSCODER = ROOT / "lanscoder"
TESTS = ROOT / "tests"

GATED = [
    "write",
    "delete",
    "shell",
    "edit",
    "apply_patch",
    "fetch",
    "git_diff",
    "git_status",
    "git_log",
    "diagnostics",
    "python_exec",
    "web_search",
    "grep",
    "glob",
    "ls",
    "tree",
    "view",
    "read_multi",
]


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """收集 Module/Class/Function 文档字符串的 Constant 节点 id,扫描时跳过。"""

    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                ids.add(id(value))
    return ids


def _unexpected_module_refs(
    package: Path, forbidden: tuple[str, ...]
) -> list[tuple[str, str]]:
    """返回 package 内 import(含 TYPE_CHECKING)与非 docstring 字符串里的越界模块引用。

    返回 (相对路径, 引用原文) 列表,空表示干净。
    """

    leaf_names = tuple(prefix.removeprefix("lanscoder.") for prefix in forbidden)
    hits: list[tuple[str, str]] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_constant_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden):
                        hits.append((str(path.relative_to(ROOT)), alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden):
                    hits.append((str(path.relative_to(ROOT)), module))
                elif module == "lanscoder" and any(
                    alias.name in leaf_names for alias in node.names
                ):
                    name = next(
                        alias.name for alias in node.names if alias.name in leaf_names
                    )
                    hits.append((str(path.relative_to(ROOT)), f"lanscoder.{name}"))
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                for prefix in forbidden:
                    if prefix in node.value:
                        hits.append((str(path.relative_to(ROOT)), node.value))
                        break
    return hits


def test_permissions_never_references_tools_agent_or_app() -> None:
    forbidden = ("lanscoder.tools", "lanscoder.agent", "lanscoder.app")
    assert _unexpected_module_refs(LANSCODER / "permissions", forbidden) == []


def test_tools_never_references_permissions() -> None:
    assert (
        _unexpected_module_refs(LANSCODER / "tools", ("lanscoder.permissions",)) == []
    )


def test_mcp_never_references_permissions() -> None:
    assert _unexpected_module_refs(LANSCODER / "mcp", ("lanscoder.permissions",)) == []


def test_permissions_parse_patch_only_via_utils_patch() -> None:
    for path in sorted((LANSCODER / "permissions").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name == "parse_patch":
                    assert node.module == "lanscoder.utils.patch", (
                        f"{path.relative_to(ROOT)}: parse_patch 只能来自 lanscoder.utils.patch,"
                        f"实际来自 {node.module}"
                    )


def test_no_lanscoder_runtime_anywhere() -> None:
    needle = "lanscoder." + "runtime"
    hits = []
    for base in (LANSCODER, TESTS):
        for path in base.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if needle in text:
                hits.append(str(path.relative_to(ROOT)))
    assert hits == []


def _classification_table_keys() -> list[str]:
    source = (LANSCODER / "permissions" / "classification.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(
        source, filename=str(LANSCODER / "permissions" / "classification.py")
    )
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.target if isinstance(node, ast.Assign) else node.target
        if (
            isinstance(target, ast.Name)
            and target.id == "_CLASSIFICATION"
            and isinstance(node.value, ast.Dict)
        ):
            return [
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
    raise AssertionError("classification.py 未找到 _CLASSIFICATION 表定义")


def _builtin_known_names(root: Path) -> set[str]:
    registry = create_builtin_registry(
        root,
        include_mutation_tools=True,
        include_execution_tools=True,
        include_network_tools=True,
    )
    return set(registry.names())


def test_gated_tool_set_is_subset_of_classification_table() -> None:
    assert sorted(set(GATED) - set(_classification_table_keys())) == []


def test_classification_table_keys_are_known_builtin_tools(tmp_path) -> None:
    known = _builtin_known_names(tmp_path)
    unknown = sorted(
        key
        for key in _classification_table_keys()
        if key not in known and not key.startswith("mcp__")
    )
    assert unknown == []


def test_every_classification_table_key_classifies() -> None:
    for name in _classification_table_keys():
        assert classify(name, {}) is not None, name


def test_gated_tool_set_all_classify() -> None:
    for name in GATED:
        assert classify(name, {}) is not None, name
