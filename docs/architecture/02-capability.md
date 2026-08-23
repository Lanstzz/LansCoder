# 02-能力层 `tools/` + `permissions/`

> 能力层是 LansCoder 最核心的设计：**工具与权限互不知晓**。工具只关心"如何执行"，权限只关心"能否执行"，二者通过编排层的 `PermissionCoordinator` 桥接。本文件讲清楚这一层如何组织、如何裁决、以及解耦是怎么做到的。

- [职责：为什么存在](#职责为什么存在)
- [工具侧：33 个内置工具如何组织](#工具侧33-个内置工具如何组织)
- [权限侧：策略如何裁决](#权限侧策略如何裁决)
- [权限裁决流程](#权限裁决流程)
- [解耦是如何做到的](#解耦是如何做到的)
- [依赖关系](#依赖关系)
- [设计要点与取舍](#设计要点与取舍)

---

## 职责：为什么存在

能力层回答两个正交的问题：

- **`tools/`**：模型可以调用哪些能力？每个能力**如何执行**？
- **`permissions/`**：这个操作**能否执行**？放行、拒绝还是询问用户？

关键在"正交"：工具不声明自己的权限，权限不依赖工具的实现。如果把两者耦合，每加一个工具都要想权限、每改一次权限策略都可能碰坏工具。LansCoder 用一张**外置分类表**把它们拆开。

## 工具侧：33 个内置工具如何组织

### 声明形态：函数即工具

工具是 `Tool`（`tools/types.py`）：一个 `ToolDefinition`（名称、描述、JSON Schema 参数）加一个 `executor` 可调用对象。绝大多数工具用 `tool_from_function`（`utils/introspection.py`）从一个**普通带类型注解的函数**自动生成：

```python
# tools/view.py 的简化示意
def view(path: str, offset: int = 0, limit: int = 200) -> ToolResult:
    """按行读取项目内 UTF-8 文本文件；支持分页。"""
    ...

return tool_from_function(view)  # 自动生成 ToolDefinition
```

`function_to_parameters` 用 `inspect` 把函数签名与类型注解转换为 JSON Schema（`str` → string、`int` → integer、`list[T]` → array）。好处：工具作者只写普通 Python 函数，模型看到的工具定义自动生成，不会漂移。

### 注册：按能力分组

`create_builtin_registry`（`tools/builtin.py`）按四组装配：

| 组 | 工具 |
|----|------|
| 基础（始终启用） | `ls`、`view`、`grep`、`glob`、`tree`、`git_status`、`git_diff`、`git_log`、`diagnostics`、`think`、`read_multi`、`ask_user` |
| 变更（mutation） | `write`、`edit`、`delete`、`apply_patch` |
| 执行（execution） | `shell`、`python_exec` |
| 网络（network） | `fetch`、`web_search` |

`include_mutation_tools` / `include_execution_tools` / `include_network_tools` 三个开关让调用方（装配根）按场景裁剪工具集。`ToolRegistry`（`tools/registry.py`）按名登记、查询、执行，并把一切异常统一转换为 `ToolResult`。

### 结果：统一形态

所有工具返回 `ToolResult`（`ok` / `content` / `data` / `error`），成功用 `make_text_result`，失败用 `make_error_result`。模型拿到的永远是结构化结果，而非裸异常。

## 权限侧：策略如何裁决

### 外置分类表

`permissions/classification.py` 的 `_CLASSIFICATION` 把**工具名 → 权限声明**映射起来，例如：

- `write` / `edit` / `apply_patch` → `WRITE_PATH`
- `delete` → `DELETE_PATH`
- `shell` / `python_exec` / `diagnostics` → `EXECUTE_SHELL`
- `fetch` / `web_search` → `NETWORK_REQUEST`
- `git_diff` / `git_status` / `git_log` → `GIT_OPERATION`
- `grep` / `glob` / `ls` / `tree` / `view` / `read_multi` → `READ_PATH`
- `mcp__<server>__<tool>` → `MCP_TOOL`（动态生成）

每条声明还定义**目标如何提取**：`target_arg`（从参数取，如 `path`）、`target_value`（固定值，如 `status --short`）、`target_builder`（从参数计算，如 `apply_patch` 解析补丁里的文件列表）。

### 裁决路径

`PermissionManager.preflight`（`permissions/manager.py`）是核心：

```
PermissionRequest
   │
   ├─ ① 授权存储命中？ ── 是 → 直接返回存储的 allow/deny（持久授权）
   │
   ├─ ② 策略裁决：DefaultPermissionPolicy.decide(request, mode)
   │     ├─ standard  → 敏感操作（写文件、执行命令、网络、项目外路径）→ ASK
   │     ├─ aggressive→ 项目内普通写入与常见验证命令自动 ALLOW，高风险仍 ASK
   │     └─ bypass    → 全部 ALLOW
   │
   └─ ③ 后台子代理（autonomous）且结果是 ASK？
         └─ 是 → 转为 DENY（后台无法交互确认，自动拒绝）
```

策略细节（`permissions/policy.py`）：

- **路径**：项目根内普通读取 → ALLOW；项目根外 / 敏感路径（`.git`、`.env`、私钥）→ ASK；项目根外删除 → DENY。
- **git**：项目内只读命令（`status`、`diff`、`log`）→ ALLOW；含 shell 控制符 → ASK。
- **shell**：standard 一律 ASK；aggressive 下项目内常见验证命令 ALLOW、高风险命令（`rm -rf` 等）仍 ASK。
- **网络 / MCP 工具**：一律 ASK。

### 确认与授权

需要 ASK 时，`build_confirmation` 构造 `UserInputRequest`，选项固定为：

- **Deny**（拒绝）
- **Allow once**（放行一次）
- **Allow always**（同作用域长期授权，如 `path_tree: /path/to/project`）——视工具是否允许长期授权而定（`apply_patch`、`python_exec` 等 `allow_always=False`）

`prewrite review`（写前审查）走 `build_prewrite_review_confirmation`：只有 Deny / Apply reviewed change 两个选项，且不允许长期授权——因为审查的是"这一份具体的 diff"。

## 权限裁决流程

```
模型请求调用 write_file(path=...)
   │
   ▼
ToolExecutor（编排层）─ 把 tool_call 交给 PermissionCoordinator.prepare
   │
   ▼
PermissionCoordinator.preflight
   │
   ├─ classify(name, args) ── 外置分类表 → ClassificationSpec（WRITE_PATH）
   │
   ├─ build_request ── 提取 target（path）、cwd、request_id
   │
   └─ PermissionManager.preflight
        ├─ grants.matching_decision ── 持久授权命中？→ 直接返回
        └─ policy.decide ── 按模式裁决
             ├─ ALLOW → 返回放行
             ├─ DENY  → 返回拒绝结果（make_permission_denied_result）
             └─ ASK   → 挂起：store_pending_request
                        └─ 回合进入 WAITING_FOR_USER_INPUT
                           └─ 用户选择 → PermissionResumeHandler
                              ├─ allow_once → 执行工具
                              ├─ allow_always → 记录授权 + 执行
                              └─ deny → 拒绝结果，反馈带回模型
```

关键：**工具执行函数本身从不检查权限**。`session.execute_tool_call` 只负责调用；权限检查全部发生在执行前的 `PermissionCoordinator.prepare`。

## 解耦是如何做到的

1. **权限声明外置**：工具侧没有任何权限声明对象（Task 8 已删除）。`classification.py` 顶部明令"禁止 import `lanscoder.tools / agent / app`"，分类表是纯字面量。
2. **防漂移锁**：`tests/test_classification.py` 用**属性测试**断言分类表与工具侧的关键字面量一致（如 `web_search` 的 MCP URL 与 `tools/web_search.py` 里的常量），防止表与工具实现悄悄分叉。
3. **依赖方向测试**：`tests/test_layer_boundaries.py` 锁定 `lanscoder.tools` 永不 import `lanscoder.agent`——工具无法"偷偷"拿到权限实现。
4. **桥接在编排层**：`PermissionCoordinator`（`agent/permission.py`）同时认识工具调用与权限策略，是唯一的桥。工具不认识权限，权限不认识工具，只有协调者两者都认识。

## 依赖关系

```
[agent/] PermissionCoordinator（编排层，唯一桥）
   │
   ├─▶ tools/        Tool 定义与 ToolRegistry（只读）
   │                   工具从不 import agent / permissions
   │
   └─▶ permissions/  PermissionManager、DefaultPermissionPolicy、分类表
                       permissions 从不 import tools / agent / app
```

能力层内部：

- `tools/` 依赖 `providers/types.py`（`ToolDefinition`）与 `utils/`（sandbox、introspection），不依赖编排层。
- `permissions/` 依赖 `permissions/types.py`、`utils/patch.py`（解析补丁目标），不依赖工具实现。
- 两包之间**互不依赖**。

## 设计要点与取舍

- **函数即工具**：工具开发成本极低（写一个带注解的函数），Schema 自动生成。代价：复杂参数校验需要工具内自行处理。
- **分类表外置**：权限逻辑集中、可审计，新增工具只需在表里加一行（或依赖默认不拦截）。代价：表与工具可能漂移，用属性测试锁住。
- **单一裁决入口**：`PermissionManager.preflight` 是唯一裁决点，授权存储 → 策略 → 后台自动拒绝的优先级清晰。
- **后台子代理自动拒绝**：无法交互确认的场景（后台子代理）直接把 ASK 转 DENY，而不是卡死等待——这是"不可用就明确失败"的设计。
- **预写审查独立路径**：`prewrite_review` 与普通权限确认分开，因为审查对象是具体 diff，不允许"长期授权这份 diff"。

下一篇：[03-模型与上下文](03-model-context.md)
