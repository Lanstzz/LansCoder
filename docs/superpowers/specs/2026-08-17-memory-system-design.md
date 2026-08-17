# FirstCoder 记忆系统设计

- 日期：2026-08-17
- 状态：已评审，待实现

## 背景与目标

FirstCoder 目前只有单会话记忆（JSONL 会话事件 + AGENTS.md 项目指令），缺少跨会话的持久记忆。本设计为项目引入记忆系统，设计思路对齐 Claude Code 的文件式记忆：**一个记忆一个文件、带 YAML frontmatter、MEMORY.md 索引常驻系统提示词、完整内容按需读取**。

### 目标

- 跨会话持久记忆，分为**项目级**（随项目走）与**用户级**（跨项目全局）两级作用域。
- 双写入：模型通过工具自主记/删记忆；用户通过 `/memory` 命令显式管理。
- 召回模型：`MEMORY.md` 索引每次构建 system prompt 时进入前缀（参与现有指纹缓存），完整记忆内容由模型用 `read_memory` / `search_memory` 按需读取。
- 完全采用 Claude Code 的记忆文件格式：一个记忆一个文件、YAML frontmatter（`name` / `description` / `metadata.type`）、`**Why:**` / `**How to apply:**` 约定、`[[name]]` 链接。
- 记忆操作记入会话 JSONL 事件，可审计、可恢复。

### 非目标（YAGNI）

- 不做语义 embedding / top-k 自动召回，留作将来扩展。
- 不做目录级 `CLAUDE.md` 继承（那是指令层级；本项目已有项目级 `AGENTS.md` 指令，保持不变）。
- 不做记忆合并、版本历史或跨会话自动摘要。

## 存储布局

两个记忆根目录均位于 `git` 之外，只写入各自专属目录：

```
用户级  ~/.firstcoder/memory/
          ├── MEMORY.md              ← 索引
          └── <name>.md              ← 每条记忆一个文件

项目级  {data_root}/memory/projects/{project_hash}/
          ├── MEMORY.md
          └── <name>.md
```

- `project_hash` 由项目根目录的绝对路径经 `content_fingerprint(...)` 计算得出（`firstcoder/context/identity.py`），与 Claude Code `~/.claude/projects/<encoded-path>/memory/` 的思路一致。
- `data_root` 复用 `SessionBootstrap.resolved_data_root()`（默认 `store.root`，即 `.firstcoder/`），保证项目记忆天然被 git-ignore、不污染仓库。

## 记忆文件格式

一个文件一个事实。文件名 = `<name>.md`，`name` 为 kebab-case slug，同名写入即覆盖（upsert）。

```markdown
---
name: build-commands
description: How to build and test FirstCoder locally
metadata:
  type: project
---

Create the local environment with .venv, run tests with pytest.
**How to apply:** run the narrowest test first, then the full suite.
```

- `metadata.type` ∈ `user | feedback | project | reference`。
- `feedback` / `project` 类型正文约定带 `**Why:**` / `**How to apply:**` 行；写入时不强校验，但工具 schema 描述会引导模型遵守。
- 正文中的 `[[name]]` 作为链接标记，索引渲染时原样保留。

## MEMORY.md 索引

每次记忆写入/删除后由 `MemoryStore` 重写对应记忆根目录下的 `MEMORY.md`，一行一条，无 frontmatter：

```markdown
- [build-commands](build-commands.md) — How to build and test FirstCoder locally
```

索引在每次 `build_system_prefix()` 时由 `MemoryIndex.render(records)` 动态渲染，保证系统提示词拿到最新快照；同时落盘成真实文件，便于用户人工浏览与手改。

## 模块划分 `firstcoder/memory/`

| 文件 | 职责 |
| --- | --- |
| `models.py` | `MemoryScope`（`USER` / `PROJECT`）、`MemoryRecord`（`name` / `description` / `type` / `body` / `file_path`）、frontmatter 解析与序列化。 |
| `store.py` | `MemoryStore(root)`：`list()` / `get()` / `write()` / `delete()` / `exists()`；负责文件名清洗、`<name>.md` 落盘（临时文件 + rename）、刷新 `MEMORY.md`、跳过坏 frontmatter 文件。 |
| `index.py` | `MemoryIndex.render(records) -> str`，渲染一行一条的索引文本。 |
| `manager.py` | `MemoryManager(user_root, project_root)`：解析作用域，委托给两个 `MemoryStore`，对外 `list_all()` / `write(scope, record)` / `delete(scope, name)` / `get(scope, name)`；提供 `render_index_text()` 供系统提示词使用。 |

## 工具集 `firstcoder/tools/memory_tools.py`

注册进 `create_session_tool_registry`（新增 `memory_manager` 参数；为 `None` 时不注册这些工具，保持向后兼容）。沿用内部会话工具的无 `ToolPermissionSpec` 模式——工具只写专属记忆目录，风险低，可审计性交由会话事件承担：

- `remember` — 参数 `name`、`description`、`body`、`type`、`scope`（默认 `project`）。按 `name` upsert。
- `forget` — 参数 `name`、`scope`（默认 `project`）。删除该记忆。
- `read_memory` — 参数 `name`、`scope`（默认 `project`）。返回记忆全文。
- `search_memory` — 参数 `query`（子串匹配）、`scope`（`project` | `user` | `all`，默认 `project`）。返回命中的 `name` + `description` + 正文预览。

## 系统提示词接线

- `SystemPromptInputs`（`firstcoder/context/system_prompt.py`，frozen dataclass）新增字段 `memory_index: str = ""`，并纳入 `fingerprint()` 参与计算。
- `SystemPromptBuilder.build()` 新增 **Memory** section，内容为：
  1. 一段协议文本：何时使用 `project` vs `user` 作用域、行动前先 `read_memory` 读全文、何时调用 `remember` 保存持久事实；
  2. 用户级索引；
  3. 项目级索引。
- `AgentSession` 新增 `memory_manager` 字段；`build_system_prefix()` 从它取 `render_index_text()` 传入 inputs。
- 记忆写入 → 索引文本变化 → 指纹变化 → 前缀自动重建。该行为与现有 `PromptPrefixCache` 天然契合，无需额外失效逻辑。

## TUI `/memory` 命令 `firstcoder/app/memory_commands.py`

- `/memory` — 列出用户级 + 项目级两级索引。
- `/memory remember <name>: <body>` — 新增/更新记忆（description / type 自动推导或简式交互）。
- `/memory forget <name>` — 删除记忆（默认项目级，`user:` 前缀切到用户级）。
- 接入 `app/commands.py` 路由；复用现有 picker 展示列表。

## 会话事件（可审计）

工具与 `/memory` 命令共用一条写路径，均调用 `SessionEventWriter.append_event("memory_updated", {scope, name, action, ...})`。泛用 `append_event` 已存在，无需 schema 版本升级。

## 错误处理

- 坏 frontmatter：读取时跳过该文件并写 debug 日志，绝不使系统提示词构建失败。
- 非法 `name`（含 `/`、`\`、`..`、保留名 `MEMORY`）：写入时拒绝；文件名一律经 `<name>.md` 白名单字符清洗。
- 非法 `type`、空 `body`：写入时拒绝。
- `read_memory` / `forget` 未命中：返回友好提示文本，不算错误。
- 记忆目录不存在：`mkdir(parents=True)` 创建；写入用临时文件 + rename 原子替换，避免半写文件。

## 测试

- `tests/memory/test_store.py` — CRUD、frontmatter 往返、文件名清洗、`MEMORY.md` 刷新、坏文件跳过。
- `tests/memory/test_index.py` — 索引行渲染、空目录 → 空 section。
- `tests/memory/test_manager.py` — 作用域解析、项目哈希隔离（两个项目互不可见）、用户/项目合并渲染。
- `tests/memory/test_tools.py` — 四个工具的增删查改、`scope` 参数、缺参报错。
- `tests/context/test_system_prompt_memory.py` — `memory_index` 进指纹；记忆写入后指纹变化、前缀重建。
- `tests/session/test_memory_events.py` — 工具写记忆 → 追加 `memory_updated` 事件。

## 改动文件清单

**新增**
- `firstcoder/memory/__init__.py`
- `firstcoder/memory/models.py`
- `firstcoder/memory/store.py`
- `firstcoder/memory/index.py`
- `firstcoder/memory/manager.py`
- `firstcoder/tools/memory_tools.py`
- `firstcoder/app/memory_commands.py`
- `tests/memory/…`（对应测试）

**修改**
- `firstcoder/context/system_prompt.py` — `SystemPromptInputs` 增字段 + Memory section。
- `firstcoder/agent/prompt_inputs.py` — `build_system_prompt_inputs()` 透传 `memory_index`。
- `firstcoder/agent/session.py` — `AgentSession.memory_manager` 字段；`build_system_prefix()` 注入索引；`create_session_tool_registry` 传 manager。
- `firstcoder/session/bootstrap.py` — 构造 `MemoryManager` 并注入 AgentSession。
- `firstcoder/tools/session_registry.py` — `create_session_tool_registry` 增 `memory_manager` 参数并注册记忆工具。
- `firstcoder/app/factory.py` — bootstrap 装配时提供记忆根目录。
- `firstcoder/app/commands.py` — 注册 `/memory` 命令。
