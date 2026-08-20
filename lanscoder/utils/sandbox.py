from __future__ import annotations

from pathlib import Path

from lanscoder.utils.sandbox_access import SandboxAccess


class PathSandbox:

    def __init__(self, root: str | Path, *, access: SandboxAccess | None = None) -> None:
        self.root = Path(root).resolve()
        self.access = access or SandboxAccess()

    def resolve(self, path: str | Path | None = None) -> Path:

        if path in (None, ""):
            target = self.root
        else:
            raw = Path(path)
            target = (raw if raw.is_absolute() else self.root / raw).resolve()

        if not self.access.unrestricted and target != self.root and self.root not in target.parents:
            raise ValueError(f"路径超出项目目录：{path}")
        return target

    def resolve_validated(
        self,
        path: str | Path | None = None,
        *,
        expect: str = "any",
    ) -> Path:

        target = self.resolve(path)

        if not target.exists():
            raise ValueError(f"路径不存在：{path}")
        if expect == "file" and not target.is_file():
            raise ValueError(f"路径不是文件：{path}")
        if expect == "dir" and not target.is_dir():
            raise ValueError(f"路径不是目录：{path}")

        return target

    def relative(self, path: str | Path) -> str:

        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            if self.access.unrestricted:
                return str(resolved)
            raise
