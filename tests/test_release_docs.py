"""SC-5 文档契约:CHANGELOG + 安装/发布文档。

- ``CHANGELOG.md`` 存在且含 ``[Unreleased]`` 与当前版本段(keep-a-changelog)。
- SDK 安装与 extras(``pip install lanscoder-core[llm]``)在 README / 03-sdk.md / examples/sdk/README.md
  均有说明。
- ``LansCoder`` 与 ``lanscoder-core`` 的薄壳依赖关系在文档中说明(依赖,非替代)。
- 发布检查文档存在(Test PyPI → 真实 PyPI 清单)。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _core_version() -> str:
    tree = ast.parse((ROOT / "lanscoder" / "core" / "_version.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("__version__ not found in _version.py")


def test_changelog_has_unreleased_and_tracks_current_version() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text
    assert f"## [{_core_version()}]" in text, "CHANGELOG 缺少当前版本段(版本 bump 时必须同步)"

    released = text.split("## [Unreleased]", 1)[1]
    assert "### Added" in released or "### Changed" in released, "Unreleased 应为空或有分类条目"


def test_sdk_install_docs_cover_core_extras_and_shell_relationship() -> None:
    docs = [
        (ROOT / "README.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "architecture" / "03-sdk.md").read_text(encoding="utf-8"),
        (ROOT / "examples" / "sdk" / "README.md").read_text(encoding="utf-8"),
    ]
    for text in docs:
        assert "pip install lanscoder-core" in text
        assert "lanscoder-core[llm]" in text
        assert "薄壳" in text, "文档应说明 LansCoder 是薄壳(依赖关系,非替代)"


def test_publishing_checklist_doc_exists() -> None:
    text = (ROOT / "docs" / "publishing.md").read_text(encoding="utf-8")
    assert "testpypi" in text or "Test PyPI" in text
    assert "twine" in text
