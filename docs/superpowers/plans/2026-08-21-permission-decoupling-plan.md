# 权限执法解耦与 runtime/ 移除 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一次工具调用只过一次策略闸门；`tools/` 与 `permissions/` 双向零引用；`lanscoder/runtime/` 包从代码库消失。

**Architecture:** 三阶段——Phase 1 纯机械搬移（cancellation→utils、user_input DTO→permissions、converter→agent、parse_patch 族→utils/patch，`runtime/` 删除）；Phase 2 权限手术（`permissions/classification.py` 外置分类表 + `PermissionCoordinator` 单闸门 + 工具纯净化 + `permission_registry.py` 删除）；Phase 3 `tools/permission_results.py` 整并入 `agent/permission_results.py`。终态依赖图 `app → agent → {tools, permissions} → utils`，任一 commit 无 tools→agent 或 permissions→tools 边。

**Tech Stack:** Pure Python（`.venv/bin/python`）、pytest、ruff。无需新增依赖。

**Spec:** `docs/superpowers/specs/2026-08-21-permission-decoupling-design.md`（v6）。实现者必须全程可同时打开 spec；本计划从 spec 论证，spec 里的事实底稿/行号/不变式是本计划的权威依据，两者冲突以 spec 为准并报告。

## Global Constraints

- 开发命令一律用 `.venv/bin/python -m pytest ...`；改过的文件全部跑 `ruff`（项目规约）。
- **单向依赖焊死**：任一 commit 不得出现 `tools → agent`、`permissions → tools`（含 TYPE_CHECKING）、`mcp → permissions` 边。tools/permissions 互相零引用是终态验收。
- **行为漂移零容忍**（测试锁死，见 spec §6-1）：`reason` 文案逐字、`allow_*` 显式 False 照抄、`_permission_request_id` 的 sha256 payload 逐字、`web_search` target 字面量勿顺手修、`UserInputRequest` 的 data 键协议逐字、`AgentCancelledError` 类名语义不变。
- 阶段纪律：每阶段内每个 commit 全量 pytest 绿；不允许"留到后面一起绿"。
- commit 需**先经用户批准**（CLAUDE.md/记忆）；message 格式 `{feat,fix,docs}: <imperative verb ...>`；只 `git add` 本任务明确列出的文件（逐路径，禁 `-A`）。
- 测试命名描述行为（如 `test_classify_unknown_tool_returns_none`）；fakes/fixtures 代替真实网络/密钥。
- 同名自定义工具按名命中分类表即门控（spec §4.2 规则 3 的已知语义变化），不豁免。

---

## Phase 1 — 机械搬移（纯搬移，逐批全绿）

### Task 1: `runtime/` 三件套迁出 + 27 处引用全部改指新家

**Files:**
- Move: `lanscoder/runtime/cancellation.py` → `lanscoder/utils/cancellation.py`（内容逐字，一行不改）
- Move: `lanscoder/runtime/user_input.py` 两段 → `lanscoder/permissions/user_input.py`（DTO 段）+ `lanscoder/agent/permission_results.py`（转换段，新建）
- Modify: 19 个消费文件 + `lanscoder/runtime/__init__.py`（改从新家转发，Task 2 才删包）

**Interfaces:**
- Produces（后续任务依赖的符号已就位）:
  - `lanscoder.utils.cancellation`：`CancellationToken`、`AgentCancelledError`、`current_cancellation_token`、`cancellation_context`（逐字）
  - `lanscoder.permissions.user_input`：`UserInputOption(id,label,description)`、`UserInputRequest(id,kind,question,options,payload)`
  - `lanscoder.agent.permission_results`：`user_input_request_from_tool_result(result, *, tool_call_id, tool_name) -> UserInputRequest | None`、`options_from_data(raw_options) -> list[UserInputOption]`（`_options_from_data` 改名公开）

- [ ] **Step 1: 读 `lanscoder/runtime/user_input.py` 全文，按行一分为二。** DTO 段（`UserInputOption`/`UserInputRequest` 两个 dataclass，字段/默认值逐字）→ 新建 `permissions/user_input.py`，头部仅保留 `from __future__ import annotations`、`dataclasses`、`typing` 导入，**去掉对 `lanscoder.tools.types` 的 TYPE_CHECKING**（DTO 不需要它）。转换段（`user_input_request_from_tool_result` 与 `_options_from_data` 函数体逐字）→ 新建 `agent/permission_results.py`：`_options_from_data` 改名为 `options_from_data`（公开，因被 `agent/session.py` 消费），函数体内 `_options_from_data(` 自引用同步改；该文件头导入：`from lanscoder.permissions.user_input import UserInputOption, UserInputRequest`、`from lanscoder.tools.types import ToolResult`、`if TYPE_CHECKING:` 仅当需要。

- [ ] **Step 2: 重拼 `lanscoder/runtime/__init__.py` 的 import 为从新家转发**（cancellation→utils、DTO→permissions.user_input、converter→agent.permission_results），`__all__` 不变。此时删除 `runtime/cancellation.py`、`runtime/user_input.py`，`runtime/` 目录只留这个空壳 `__init__.py`。

- [ ] **Step 3: 一批一个文件地 re-point 引用，每个文件改完立即跑它对应的窄测。**

| 文件 | 改 import | 窄测 |
|---|---|---|
| `app/runtime.py:18` | `from lanscoder.utils.cancellation import CancellationToken` | test_app_runtime |
| `app/runtime.py:32` | `from lanscoder.permissions.user_input import UserInputRequest` | test_app_runtime |
| `utils/subprocess.py:10` | `from lanscoder.utils.cancellation import CancellationToken` | test_utils_subprocess |
| `utils/execution_sandbox.py:6` | `from lanscoder.utils.cancellation import current_cancellation_token` | test_execution_sandbox / test_sandbox 相关 |
| `agent/loop.py:12-13` | cancellation→utils；`UserInputRequest`→permissions.user_input | test_agent_context_loop |
| `agent/tool_execution.py:16` | cancellation→utils | test_execution_tools |
| `agent/tool_execution.py:30` | converter→`from lanscoder.agent.permission_results import UserInputRequest, user_input_request_from_tool_result` | test_execution_tools |
| `agent/background.py:14` | cancellation→utils | test_background 相关 |
| `agent/observer.py:6` | cancellation→utils | test_observer 相关 |
| `agent/subagent_engine.py:27` | cancellation→utils | test_subagent / test_delegate_tool |
| `agent/session.py:28` | `options_from_data` 改从 `lanscoder.agent.permission_results` 取；**调用点 :599 `_options_from_data(` 同步改 `options_from_data(`** | test_session_* |
| `agent/permission.py:24` | DTO→permissions.user_input | test_permission_* |
| `agent/permission_resume.py:16` | DTO→permissions.user_input | test_permission_resume 相关 |
| `agent/user_input.py:7` | DTO→permissions.user_input | test_app 相关 |
| `permissions/manager.py:10` | `from lanscoder.permissions.user_input import ...`（同包） | test_permission_* |
| `tools/permission_results.py:5` | `from lanscoder.permissions.user_input import UserInputRequest` | test_permission_results |
| `tests/test_utils_subprocess.py:13`、`tests/test_agent_context_loop.py:19`、`tests/test_model_request_options.py:19`、`tests/test_delegate_tool.py:383/:441/:942` | cancellation→`lanscoder.utils.cancellation` | 各文件 |
| `tests/test_app_tui.py:21` | DTO→permissions.user_input | test_app_tui |
| `tests/test_permission_results.py:1` | DTO→permissions.user_input；converter→`lanscoder.agent.permission_results` | test_permission_results |

- [ ] **Step 4: 全量测试。** `cd 仓库根 && .venv/bin/python -m pytest` —— 必须全绿。改过的文件跑 `ruff`。
- [ ] **Step 5: 提交审批点。** `git status` 确认只有本任务文件；向用户展示改动清单请求批准后 `git add <逐个路径>` + `git commit -m "refactor: relocate runtime cancellation/user_input modules"`（用户批准前不 commit）。

### Task 2: 删除 `lanscoder/runtime/` 包 + 引用清零闸

**Files:**
- Delete: `lanscoder/runtime/__init__.py`（Task 1 后已是空壳，无消费方）
- Create: `tests/test_runtime_gone.py`

- [ ] **Step 1: 验证无残留引用。** 运行 `grep -rn "lanscoder\.runtime" lanscoder tests`，输出为空（排除 `docs/superpowers/`）。若非空，回到 Task 1 补齐 re-point 再继续。
- [ ] **Step 2: 删除目录。** `rm lanscoder/runtime/__init__.py && rmdir lanscoder/runtime`。
- [ ] **Step 3: 写 §6-6 的 CI 闸测试（防回归）。** 新建 `tests/test_runtime_gone.py`，用 `pathlib` 递归扫描（不依赖系统 grep）：

```python
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

def test_no_lanscoder_runtime_references():
    hits = []
    for base in (REPO / "lanscoder", REPO / "tests"):
        for path in base.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "lanscoder.runtime" in text:
                hits.append(str(path.relative_to(REPO)))
    assert hits == []
```
- [ ] **Step 4:** `.venv/bin/python -m pytest tests/test_runtime_gone.py -v` PASS；全量 pytest 绿；ruff。
- [ ] **Step 5: 提交审批点。**（`git add tests/test_runtime_gone.py`；批准后 `git commit -m "refactor: remove lanscoder.runtime package"`）

### Task 3: `parse_patch` 族 → `utils/patch.py`（F-4 前置）

**Files:**
- Create: `lanscoder/utils/patch.py`
- Modify: `lanscoder/tools/apply_patch.py`（import + 删定义）、`lanscoder/tools/review.py:10`

**Interfaces:**
- Produces: `lanscoder.utils.patch` 导出 `parse_patch(patch: str) -> PatchPlan`、`PatchPlan`、`PatchOperation`、`PatchHunk`、`BEGIN_MARKER`、`END_MARKER` —— 全部逐字。
- Consumes（维持既有行为）: `apply_patch.py` 的 `_apply_plan` 与 `create_apply_patch_tool` 从 `utils.patch` 导入 `parse_patch`/`PatchPlan`；`review.py` 从 `utils.patch` 导入 `PatchPlan`/`parse_patch`、仍从 `tools.apply_patch` 导入 `_apply_plan`。

- [ ] **Step 1: 读 `tools/apply_patch.py:13-90`**（marker 常量 + 三个 dataclass + `parse_patch` 函数体），逐字搬进新文件 `utils/patch.py`，头部只加 `from __future__ import annotations`、`dataclasses`、不再需要 `pathlib`（仅若原函数体用到才保留）。
- [ ] **Step 2: 改写 `tools/apply_patch.py`：** 删除 `BEGIN_MARKER`/`END_MARKER`/`PatchHunk`/`PatchOperation`/`PatchPlan`/`parse_patch` 定义，改 `from lanscoder.utils.patch import BEGIN_MARKER, END_MARKER, parse_patch, PatchPlan`（`_apply_plan` 需要的 `PatchOperation` 也一并导入）。函数体引用不变。
- [ ] **Step 3: 改写 `tools/review.py:10`：** `from lanscoder.utils.patch import PatchPlan, parse_patch`，`_apply_plan` 保留 `from lanscoder.tools.apply_patch import _apply_plan`。
- [ ] **Step 4: 行为锁测试全绿。** `test_tools.py`、`test_agent_tool_flow.py`（经工具执行路径）、`test_mutation_tools.py`、`test_prewrite_review.py`、`test_review_view.py` —— 全绿即 parse 面迁址无误（这些测试不直接 import parse 面，自动锁行为）。全量 pytest + ruff。
- [ ] **Step 5: 提交审批点。**（`git commit -m "refactor: move patch parsing to utils.patch"`）

---

## Phase 2 — E 权限手术

### Task 4: `permissions/classification.py`（外置分类表 + request 构建）

**Files:**
- Create: `lanscoder/permissions/classification.py`
- Create: `tests/test_classification.py`

**Interfaces:**
- Consumes: `lanscoder.utils.patch.parse_patch`（Task 3）、`lanscoder.permissions.types.PermissionAction/PermissionRequest`。
- Produces:
  - `ClassificationSpec`（dataclass，字段见 spec §4.2）
  - `classify(tool_name, arguments) -> ClassificationSpec | None`
  - `build_request(tool_name, arguments) -> PermissionRequest`（缺参抛 `ValueError`，消息与今日逐字一致）

- [ ] **Step 1: 先读 `tools/permission_registry.py:98-151`**（`permission_request_for_tool`/`_target_from_arguments`/`_cwd_from_arguments`/`_permission_request_id`）——下面每个函数体与它逐字等值。
- [ ] **Step 2: 逐字转录 target/cwd/request_id 算法到新文件**（schema 从 `ToolPermissionSpec` 换成 `ClassificationSpec`）：

```python
    def _spec_target(spec: ClassificationSpec, arguments: dict) -> str:
        if spec.target_builder is not None:
            return spec.target_builder(arguments)
        if spec.target_value is not None:
            return spec.target_value
        if spec.target_arg is None:
            return ""
        if spec.target_arg not in arguments:
            raise ValueError(f"权限声明缺少目标参数：{spec.target_arg}")
        return str(arguments[spec.target_arg])

    def _spec_cwd(spec, arguments):
        if spec.cwd_arg is None:
            return None
        raw = arguments.get(spec.cwd_arg)
        if raw in (None, ""):
            return None
        return Path(str(raw))

    def _permission_request_id(tool_name, arguments):
        payload = json.dumps({"tool": tool_name, "arguments": arguments},
                             ensure_ascii=False, separators=(",", ":"),
                             sort_keys=True, default=str)
        return f"perm_{tool_name}_{sha256(payload.encode('utf-8')).hexdigest()[:12]}"
```
（`json`/`sha256`/`Path` 对应 import。）

- [ ] **Step 4: build_request：**

```python
def build_request(tool_name: str, arguments: dict) -> PermissionRequest:
    spec = _lookup(tool_name)          # 与 classify 同一张表,含 mcp__* 前缀
    if spec is None:
        raise ValueError(f"工具没有权限声明：{tool_name}")
    target = _spec_target(spec, arguments)
    cwd = _spec_cwd(spec, arguments)
    request_id = _permission_request_id(tool_name, arguments)
    return PermissionRequest(
        id=request_id, action=spec.action, target=target,
        reason=spec.reason or f"工具 {tool_name} 请求 {spec.action.value} 权限。",
        cwd=cwd,
        metadata={"tool_name": tool_name, "arguments": dict(arguments),
                  "allow_always": spec.allow_always, "allow_auto": spec.allow_auto},
    )
```

- [ ] **Step 5: 建表（私有 target 实现全部内联，禁止 import tools）：** `_read_path_target`/`_read_multi_target`（照抄 `path_permissions.py:15-20` 逐字）、`_patch_files_target`（照抄 `apply_patch.py:74-82`，但用 `utils.patch.parse_patch`）、`_python_exec_target`（照抄 `python_exec.py:66-69`）、`_git_diff_target`（`"diff --cached" if bool(arguments.get("staged")) else "diff"`）。然后对 **12 个直接声明工具**逐一读其工具文件里现存的 `ToolPermissionSpec`（Task 8 才删），`action`/`target_arg`/`target_value`/`target_builder`/`cwd_arg`/`reason`/`allow_always`/`allow_auto` **逐字段转写**成 `_CLASSIFICATION[tool_name] = ClassificationSpec(...)`。6 个读工具按 spec §4.2 组：`grep/glob/ls/tree/view` → `action=READ_PATH, target_builder=_read_path_target`，`read_multi` → `target_builder=_read_multi_target`；reason 逐字照抄工具文件。`classify` 先查表，再 `mcp__` 前缀（`server, _, tool = name.removeprefix("mcp__").rsplit("__", 1)` → `ClassificationSpec(action=MCP_TOOL, target_value=f"{server}/{tool}", allow_auto=False)`），其余 `None`。
- [ ] **Step 6: 先写失败测试 `tests/test_classification.py`（§6-1 全组）：**

```python
GATED = ["write","delete","shell","edit","apply_patch","fetch","git_diff",
         "git_status","git_log","diagnostics","python_exec","web_search",
         "grep","glob","ls","tree","view","read_multi"]
# 1) 18 名 → 非 None,action 正确;reason/allow_auto/allow_always 与工具文件 ToolPermissionSpec 逐字一致(每名断言)
# 2) apply_patch 缺 patch → ValueError("patch 必须以 *** Begin Patch 开头" 或 target 缺失文案)
# 3) shell 缺 command → ValueError("权限声明缺少目标参数：command"),build_request 抛出不吞
# 4) git_status → target_value == "status --short"
# 5) mcp__server__tool → action MCP_TOOL, target_value "server/tool";mcp__a__b__c → rsplit 出 server="a__b"
# 6) 未知名 "no_such_tool" → classify None
# 7) F-8: 同名自定义工具名("shell") 也按名命中(不因对象无 permission 而豁免)
# 8) build_request("write", {"path":"a.txt"}) 顺序断言 id 前缀 perm_write_、metadata 键集
# 9) web_search target_value == f"{EXA_MCP_URL},{PARALLEL_MCP_URL}" 字面量(读 tools/web_search.py 常量)
```
（reason/action 等值断言值来自 Step 5 你转写的表与工具文件，作为防漂移锁——写断言时从工具文件复核一次。）

- [ ] **Step 7:** `.venv/bin/python -m pytest tests/test_classification.py -v` FAIL（模块不存在）→ 实现 → PASS。再全量 pytest + ruff。
- [ ] **Step 8: 提交审批点。**（`git commit -m "feat: add permissions classification table"`）

### Task 5: Coordinator 单闸门 + 无效短路

**Files:**
- Modify: `lanscoder/agent/permission.py`（coordinator）
- Modify: `tests/test_permission_registry.py`（本轮起改写：把基于 `PermissionAwareToolRegistry` 的用例迁到 coordinator 语义）

**Interfaces:**
- Consumes: `classification.classify/build_request`（Task 4）。
- Produces: `coordinator.prepare/preflight` 行为契约——**唯一**策略求值点；缺参短路不进 preflight。

- [ ] **Step 1: 读 `agent/permission.py` 全文**，定位 `prepare()`/`preflight()`（含 :105-114 的 `isinstance` 强转与 :143/:195/:243 的 make_* 调用点）。
- [ ] **Step 2: 重构 preflight 主干**为（形状对齐现状返回值，务实填入现有结构，错误处理与现文件一致）：

```python
# preflight(tool_call) 内:
spec = classify(name, arguments)
if spec is None:
    return None
try:
    request = build_request(name, arguments)
except ValueError as exc:
    return _deny_invalid(name, arguments, spec, exc)   # id=f"perm_{name}_invalid", target="", reason=str(exc), DENY
request = self.permission_manager.normalize_request(request)
decision = self.permission_manager.preflight(request)
```
- [ ] **Step 3: 删除 `isinstance(registry, PermissionAwareToolRegistry)` 强转逻辑**（coordinator 不再依赖 registry 类型；`make_*` 引用**暂保留**走 `lanscoder.tools.permission_results`，Phase 3 再改）。
- [ ] **Step 4: 迁移「无效参数→短路 DENY」的等价断言进 `tests/test_permission_registry.py`（改写其夹具指向 coordinator-prepare 路径）**：缺参请求 → `decision.kind is PermissionDecisionKind.DENY`、`request.id == f"perm_{name}_invalid"`、`reason` == 原文案、**且 `manager.preflight` 未被调用**（用替换型 fake manager 钉住）。GRANT 先于 policy、BYPASS、自主 ASK→DENY 原有用例保持语义。
- [ ] **Step 5:** `.venv/bin/python -m pytest tests/test_permission_registry.py tests/test_classification.py -v` PASS；全量 + ruff。
- [ ] **Step 6: 提交审批点。**（`git commit -m "refactor: make coordinator the single enforcement gate"`）

### Task 6: session/session_registry 纯化 + F-1 测试迁移

**Files:**
- Modify: `lanscoder/agent/session.py`(:442-455)、`lanscoder/tools/session_registry.py`
- Modify: `tests/test_agent_tool_flow.py`、`tests/test_session_resume_service.py`、`tests/test_app_factory.py:570`（F-1）

- [ ] **Step 1: `agent/session.py:442-455`：** `execute_tool_call` 与 `execute_tool_call_after_permission_confirmation` 直接 `return self.tool_registry.execute(tool_call.name, tool_call.arguments)`（两方法同体）；删 `isinstance` 分支与 `PermissionAwareToolRegistry` 引用。
- [ ] **Step 2: `tools/session_registry.py`：** `create_session_tool_registry` 去掉 `if permission_manager is not None: return PermissionAwareToolRegistry(...)` 分支，恒返回纯 `ToolRegistry`；删对应 import（`PermissionManager`、`PermissionAwareToolRegistry`）。
- [ ] **Step 3: F-1 断言形态迁移（核心，语义不变断言形态换）。** 对 `test_agent_tool_flow.py:121-142/145-175/178+` 与 `test_session_resume_service.py:219-229`，用 coordinator 路径重建场景：

```python
# 现在:result = session.execute_tool_call(tool_call); 断言 data["request_type"]=="permission_confirmation"
# 改为:
prepared = session.permission_coordinator.prepare(tool_call, deferred=False)
assert prepared is not None
assert prepared.decision.kind is PermissionDecisionKind.ASK      # 或按当前 PreparedPermission 字段名
assert not README.exists()                                        # 未真写入
# 恢复分支:决策 DENY → 文件仍未写;决策 ALLOW → execute_tool_call_after_permission_confirmation 写入成功
```
（`PreparedPermission` 实际字段名以读 `agent/permission.py` 为准，实现时先读后套。）
- [ ] **Step 4: `tests/test_app_factory.py:570` 防真空化：** 现断言 `request_type != "permission_confirmation"` 在纯 registry 下恒真。改为断言确认路径语义（无 coordinator.prepare 时 execute 直跑、无 pending_input 副作用）。
- [ ] **Step 5:** `.venv/bin/python -m pytest tests/test_agent_tool_flow.py tests/test_session_resume_service.py tests/test_app_factory.py -v` PASS；全量 + ruff。
- [ ] **Step 6: 提交审批点。**（`git commit -m "refactor: plain registry in sessions and resume path"`）

### Task 7: 并行只读批按 id 缓存 prepare（§6-3）

**Files:**
- Modify: `lanscoder/agent/tool_execution.py`

- [ ] **Step 1: 读 `tool_execution.py` 的 `execute_interactive`（:173-191）与 `can_execute_in_parallel`/`parallel_readonly_batch_end`（:440-460）全文**，确认批组装的调用面。
- [ ] **Step 2: 引入按 `tool_call.id` 的 prepare 决策缓存：** 在 `ToolExecutor` 上加 `_prepare_cache: dict[str, PreparedPermission]`，`execute_interactive` 每次 `coordinator.prepare` 后写入 `self._prepare_cache[tool_call.id] = prepared`；回合结束清理。改 `can_execute_in_parallel`（:457）：不再调 `coordinator.preflight`，改为读缓存——`prepared = self._prepare_cache.get(tool_call.id)`；`prepared is None or prepared.decision.kind is PermissionDecisionKind.ALLOW`（或无门控/未求值语义按现状读代码对齐）才并入并行批。
- [ ] **Step 3: 先写失败测试 `tests/test_single_gate.py`（§6-3）：** 用 recording fake `PermissionManager`（记录 `preflight` 调用次数），一次含 3 个只读工具的并行批（如 `view`/`ls`/`git_status` 组合，按现状可并行集选几个只读工具）——断言准备周期内 **每个成员 `manager.preflight` 恰好一次**、总次数 = 成员数（而非 2×或 3×）；再补单调用场景恰好一次。
- [ ] **Step 4:** 实现至 PASS；`.venv/bin/python -m pytest tests/test_single_gate.py tests/test_execution_tools.py -v`；全量 + ruff。
- [ ] **Step 5: 提交审批点。**（`git commit -m "perf: reuse prepared permission decisions for parallel batch"`）

### Task 8: 工具纯净化（去章 + 去装饰器 + mcp 去章）

**Files:**
- Modify: 12 门控工具文件（write/delete/shell/edit/apply_patch/fetch/git_diff/git_status/git_log/diagnostics/python_exec/web_search）、6 读工具文件（grep/glob/ls/tree/view/read_multi）、`tools/path_permissions.py`、`tools/types.py`、`mcp/adapter.py`
- Modify: `tests/test_read_tools.py`、`tests/test_execution_tools.py`、`tests/test_mcp_adapter.py`、`tests/test_mcp_integration.py`

- [ ] **Step 1: `tools/types.py`：** 删 `ToolPermissionSpec` 类与 `Tool.permission` 字段；清 `permissions.types` 的 TYPE_CHECKING 引用。
- [ ] **Step 2: 12 个门控工具：** 逐个删文件内的 `ToolPermissionSpec(...)` 声明块和 `from lanscoder.permissions.types import PermissionAction`（Task 4 已转写，删时无功能残留）。`apply_patch.py` 删 `_permission_target_for_patch` 定义；`python_exec.py` 删 `_permission_target_for_python_exec` 定义；`git_diff.py` 删 inline lambda。
- [ ] **Step 3: 6 个读工具：** 把 `with_read_permission(tool, reason=..., target_builder=...)` 包裹改为直接 `tool_from_function(...)` 构造（无 permission 赋值）；`tools/path_permissions.py` 整体拆除（`with_read_permission` 删除；两个 read builder 已在 Task 4）。
- [ ] **Step 4: `mcp/adapter.py`：** 删 :59 盖章赋值与 `from lanscoder.permissions.types import PermissionAction`。
- [ ] **Step 5: 迁移测试（断言不变、通道换 coordinator-path）：** `test_read_tools.py`（关键断言 `view private.key`/`read_multi [README, private.key]` → **必须仍 ASK 且不泄漏**，经 `coordinator.prepare` 而非带闸 registry 表达）、`test_execution_tools.py`（夹具改 coordinator-prepare）、`test_mcp_adapter.py`/`test_mcp_integration.py`（工具构造去章后断言 + classify 行为）。
- [ ] **Step 6:** 全量 pytest（重点 `test_read_tools.py`）+ ruff。`grep -rn "ToolPermissionSpec\|with_read_permission" lanscoder` 为空。
- [ ] **Step 7: 提交审批点。**（`git commit -m "refactor: make tools permission-free schemas"`）

### Task 9: 删除 `tools/permission_registry.py` + 改写 `test_permission_registry.py`

**Files:**
- Delete: `lanscoder/tools/permission_registry.py`
- Modify: `tests/test_permission_registry.py`（改写为 classification + coordinator 语义，Task 5 已起头）

- [ ] **Step 1: 确认无引用：** `grep -rn "permission_registry" lanscoder tests` 与 `grep -rn "PermissionAwareToolRegistry" lanscoder` 为空（Task 5/6/8 应已清尽；残留则回补）。
- [ ] **Step 2: 收尾改写 `tests/test_permission_registry.py`：** 全部用例迁至 `classification.classify/build_request` + `coordinator.prepare` 路径（含 grants 优先、BYPASS、自主拒、恢复 request_id 匹配、`pending_tool_call` 深拷贝不变式，不变量见 spec §5）。
- [ ] **Step 3:** `.venv/bin/python -m pytest tests/test_permission_registry.py -v` PASS；全量 + ruff；四类 grep 清零：`ToolPermissionSpec`/`with_read_permission`/`from lanscoder.tools.permission_registry`/`permissions/ 内 from lanscoder.tools`。
- [ ] **Step 4: 提交审批点。**（`git commit -m "refactor: delete permission-aware tool registry"`）

---

## Phase 3 — `permission_results` 归位 agent/

### Task 10: `tools/permission_results.py` → `agent/permission_results.py` 并入

**Files:**
- Move: `tools/permission_results.py` 的四个 `make_*` + `_permission_request_data` → 追加进 `agent/permission_results.py`（Task 1 已建的 converter 文件）
- Delete: `tools/permission_results.py`
- Modify: `agent/permission.py`、`agent/permission_resume.py`（make_* import 收口）、`tests/test_permission_results.py:4`

**Interfaces:**
- Produces（终态）: `lanscoder.agent.permission_results` 导出 `user_input_request_from_tool_result`、`options_from_data`、`make_permission_confirmation_result`、`make_permission_denied_result`、`make_prewrite_review_stale_result`、`make_prewrite_review_failed_result`、`_permission_request_data`。该模块依赖 tools.types/providers.types/permissions.types/permissions.user_input，零 agent/ 内部引用。

- [ ] **Step 1: 追加组装函数到 `agent/permission_results.py`：** 四个 `make_*` 与 `_permission_request_data` 函数体逐字迁入；文件头补 `from lanscoder.permissions.types import PermissionDecision, PermissionRequest`、`from lanscoder.providers.types import ToolCall`、`from dataclasses import asdict`（保持 `tools.types` 的 `ToolResult/make_error_result/make_text_result` 引用）。
- [ ] **Step 2: 收口消费方：** `agent/permission.py`（:26-28）与 `agent/permission_resume.py`（:17-21）的 `make_*` 引用从 `lanscoder.tools.permission_results` 改 `lanscoder.agent.permission_results`（agent→agent）；删除 `tools/permission_results.py`。
- [ ] **Step 3: `tests/test_permission_results.py:4`：** make_* import → `lanscoder.agent.permission_results`（:1 已在本轮 Task 1 指向 permissions.user_input）：该文件是全迁移的行为锁，**整文件必须原样全绿**（含往返测试：make_confirmation → 转换 → 断言对象形状/request_id fallback）。
- [ ] **Step 4:** 全量 pytest + ruff。grep 清零：`lanscoder.runtime`、`from lanscoder.tools.permission_results`。
- [ ] **Step 5: 提交审批点。**（`git commit -m "refactor: relocate permission result assembly to agent"`）

### Task 11: 双零依赖断言收口 + 全量验收

**Files:**
- Create: `tests/test_dependency_directions.py`（§6-4 AST 扫描断言）

- [ ] **Step 1: 写依赖方向断言测试（终态闸）：** 用 `ast` 解析每个 `lanscoder/` 包内文件的 import：
  - `permissions/` 下任何文件不得出现 `lanscoder.tools`、`lanscoder.agent`、`lanscoder.app`（含 TYPE_CHECKING）；`parse_patch` 必须经 `utils.patch` 出现。
  - `tools/` 下任何文件不得出现 `lanscoder.permissions`。
  - `mcp/` 下不得出现 `lanscoder.permissions`。
  - 全仓（lanscoder, tests）不得出现 `lanscoder.runtime`。
  - 分类表 18 名硬编码集合 ⊆ `classify` 表；且表内井号键 ⊆ `tools` 注册表已知名 ∪ `mcp__*`（§6-2 双向）。
- [ ] **Step 2:** 全量 `.venv/bin/python -m pytest` 全绿；全仓 `ruff check .`；`grep -rn "permission_results\|permission_registry\|Permissions\|lanscoder.runtime" lanscoder` 输出应符合终态（无 runtime、无 permission_registry、无 tools.permission_results）。
- [ ] **Step 3: 手测冒烟：** `.venv/bin/python -c "import lanscoder.lanscoder"`（或项目入口）；跑一个真实读工具（如 `view`）确认 ASK 弹出、批准后执行、拒绝后不执行。
- [ ] **Step 4: 更新 `handoff.md`**（§0 一句话状态、"下一步"队列、任务板）为本轮完成后状态。这是文档任务，不 commit（gitignored）。
- [ ] **Step 5: 提交审批点。**（`git commit -m "test: lock dependency directions end-to-end"`）

---

## Self-Review 摘要（写计划时的自检记录）

- **Spec 覆盖**：§4.1 三阶段（T1-T10）、§4.2 分类表（T4）、§4.3 数据流与短路（T5 第二步、T7）、§5 不变式（各任务等价断言，重点 11/12 在 T5/T7）、§6-1~6（T4/T5/T7/T8/T10/T2/T8/T11）、§6-E F-1（T6）、F-2~F-6（T4/T5/T9/T3）、§7（T11 断言）、§10 清单（各任务 Files）。无缺口任务；`test_app_factory.py:570` 真空化由 T6 Step 4 钉住。
- **占位检查**：所有 re-point 步骤都给出具体 from→to 行；target 转写的"逐字段照抄"以"读工具文件现存 ToolPermissionSpec"为明确来源（等价断言在 T4 Step 6 钉死），非模糊占位。
- **类型一致**：`classify(tool_name, arguments)->Spec|None`、`build_request(tool_name, arguments)->PermissionRequest`、`options_from_data`、`_patch_files_target` 等在 T4/T5/T10 跨任务同名同构。