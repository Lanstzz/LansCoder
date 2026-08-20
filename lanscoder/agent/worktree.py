from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from lanscoder.utils.execution_sandbox import ExecutionSandbox

WORKTREE_DIRNAME = "fc-worktrees"
_DIFF_STAT_LIMIT = 8000


class WorktreeError(RuntimeError):
    pass


@dataclass(slots=True)
class Worktree:

    name: str
    path: Path
    branch: str
    base_ref: str


@dataclass(slots=True)
class WorktreeDiff:

    stat: str
    files_changed: list[str]
    has_changes: bool

    def render(self) -> str:
        if not self.has_changes:
            return "(worktree has no changes)"
        return self.stat


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:

    env = ExecutionSandbox(cwd).build_env()
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(["git", *args], returncode=1, stdout="", stderr=str(exc))


def is_git_repo(path: str | Path) -> bool:

    root = Path(path)
    if not root.exists():
        return False
    result = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


class WorktreeManager:

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def available(self, *, base_ref: str = "HEAD") -> bool:

        if not is_git_repo(self.project_root):
            return False
        result = _run_git(self.project_root, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"])
        return result.returncode == 0

    def _common_git_dir(self) -> Path:
        result = _run_git(self.project_root, ["rev-parse", "--git-common-dir"])
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "无法定位 git 目录；当前不是 git 仓库。")
        raw = result.stdout.strip() or ".git"
        common = Path(raw)
        if not common.is_absolute():
            common = (self.project_root / common).resolve()
        return common

    def worktrees_root(self) -> Path:
        return self._common_git_dir() / WORKTREE_DIRNAME

    def create(self, name: str, *, base_ref: str = "HEAD") -> Worktree:

        safe_name = _sanitize_name(name)
        if not safe_name:
            raise WorktreeError("worktree 名称不能为空。")
        if not is_git_repo(self.project_root):
            raise WorktreeError("当前项目不是 git 仓库，无法创建隔离 worktree。")
        if not self.available(base_ref=base_ref):
            raise WorktreeError(f"无法解析 worktree 基准提交：{base_ref}")

        target = self.worktrees_root() / safe_name
        if target.exists():
            raise WorktreeError(f"worktree 路径已存在：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)

        branch = f"fc/subagent/{safe_name}"
        result = _run_git(
            self.project_root,
            ["worktree", "add", "-q", "-b", branch, str(target), base_ref],
        )
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "git worktree add 失败。")
        return Worktree(name=safe_name, path=target.resolve(), branch=branch, base_ref=base_ref)

    def diff(self, worktree: Worktree) -> WorktreeDiff:

        add_result = _run_git(worktree.path, ["add", "-A", "-N"])
        if add_result.returncode != 0:
            raise WorktreeError(add_result.stderr.strip() or "git add -N 失败。")
        stat_result = _run_git(worktree.path, ["diff", "--stat", "HEAD"])
        names_result = _run_git(worktree.path, ["diff", "--name-status", "HEAD"])
        if stat_result.returncode != 0:
            raise WorktreeError(stat_result.stderr.strip() or "git diff --stat 失败。")
        if names_result.returncode != 0:
            raise WorktreeError(names_result.stderr.strip() or "git diff --name-status 失败。")
        stat = stat_result.stdout.strip()
        files = _parse_name_status(names_result.stdout)
        has_changes = bool(files) or bool(stat)
        if len(stat) > _DIFF_STAT_LIMIT:
            stat = stat[:_DIFF_STAT_LIMIT] + "\n…(diff stat truncated)"
        return WorktreeDiff(stat=stat, files_changed=files, has_changes=has_changes)

    def is_dirty(self, worktree: Worktree) -> bool:
        result = _run_git(worktree.path, ["status", "--porcelain"])
        return bool(result.stdout.strip())

    def remove(self, worktree: Worktree, *, force: bool = False) -> None:

        if not force and self.is_dirty(worktree):
            raise WorktreeError("worktree 有未提交改动；请先审查/保存，或用 force=True 显式丢弃。")
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree.path))
        result = _run_git(self.project_root, args)
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "git worktree remove 失败。")
        _run_git(self.project_root, ["worktree", "prune"])


def _sanitize_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(name).strip())
    return cleaned.strip("-")


def _parse_name_status(output: str) -> list[str]:
    files: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        files.append(parts[-1])
    return files
