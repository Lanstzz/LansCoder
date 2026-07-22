# FirstCoder LLM 主动加载 Skill 重构设计

日期：2026-07-22

## 背景

FirstCoder 当前会在调用模型前由本地 `SkillRouter` 根据用户文本自动匹配 skill，随后读取完整 `SKILL.md` 及必读文件，并把它们永久加入 session 的 system prompt。与此同时，system prompt 每轮还携带所有 skill 的路径、root、来源和完整 description。

当前运行环境发现 147 条 skill 记录，仅 skill 目录和协议就约占 12,693 个估算 token，约为完整 system prompt 的 90%。同名 skill 还可能从项目目录、`~/.agents/skills`、`~/.codex/skills` 等来源重复暴露。已加载 skill 会持续累积，并在 resume 时重新读取磁盘后再次注入 system prompt。

本次重构把 skill 使用统一为一条明确路径：模型根据精简目录自主决定是否调用 `load_skill`；FirstCoder 不再在模型请求前自动匹配或永久注入 skill 正文。

## 目标

1. 让 LLM 根据用户任务和精简目录自主选择 skill。
2. 只在调用 `load_skill` 时把完整 `SKILL.md` 放入当前会话。
3. 保留 FirstCoder 现有的 session-scoped 工具、append-only 事件、权限注册表、工具消息投影和 checkpoint 架构。
4. 显著降低 system prompt 中 skill 目录的固定开销。
5. 统一 skill 加载和审计路径，避免本地自动路由与模型工具调用并存。
6. 不增加旧会话兼容层，保持实现干净。

## 非目标

- 本次不实现 `context: fork`、skill 专属模型、skill hooks 或 `allowed-tools` 自动授权。
- 本次不新增专用 skill 消息类型。
- 本次不自动递归加载 `references/`、`scripts/`、`assets/` 等资源。
- 本次不调整通用工具权限、task plan 或 context compaction 阈值。
- 本次不迁移、重写或删除旧 JSONL 会话事件。

## 总体架构

```text
启动/恢复 session
    -> 发现项目和全局 skills
    -> 按名称解析唯一有效 skill
    -> system prompt 注入受限的名称+短描述目录

用户消息
    -> LLM 判断是否需要某个 skill
    -> LLM 调用 load_skill(name, args?)
    -> session-scoped tool 从已解析 catalog 安全查找
    -> 读取 SKILL.md
    -> 成功后写 skill_selected / skill_loaded 审计事件
    -> 完整正文作为普通 tool_result 写入 append-only session
    -> LLM 按正文继续调用现有工具
```

`load_skill` 返回普通工具结果，而不是修改 system prompt。这样 skill 正文天然参与现有 provider tool-call 序列、resume 投影、工具结果生命周期和 checkpoint，不需要扩展消息协议。

## Skill 发现与同名解析

现有发现目录继续保留：

- `<project>/.agents/skills/*/SKILL.md`
- `<project>/skills/*.md`
- `~/.agents/skills`
- `~/.codex/skills`
- `~/.firstcoder/skills`

对模型和 `load_skill` 暴露的 catalog 必须按 skill `name` 唯一化。优先级保持：

1. 项目 agent skill
2. 项目 markdown skill
3. 全局 agent skill
4. 全局 markdown skill

同一优先级存在重名时，使用稳定的 root/path 排序选择第一条，保证启动、测试和 resume 行为可重复。底层 discovery 可以保留来源记录，但所有运行时消费者都必须使用解析后的有效 catalog，不能各自实现不同的去重规则。

## System Prompt 目录

system prompt 只暴露：

```text
- content-strategy: Plan content strategy, topics, and editorial calendars.
```

不暴露：

- filesystem path
- root
- source enum
- 同名的被覆盖版本
- 完整 `SKILL.md`

目录总文本设 8,000 字符硬上限。条目按稳定顺序加入；放不下的条目不做半截输出。目录末尾明确告诉模型：需要使用 skill 时，先调用 `load_skill` 获取完整指令；未加载前不能声称遵循了该 skill。

description 使用发现阶段解析到的 description，规范化换行和多余空白后作为单行输出。单条 description 需要限制长度，避免少数超长 frontmatter 消耗整个目录预算。具体字符上限由实施计划中的测试固定，但必须保证名称、分隔符和截断标记可读。

## `load_skill` 工具

工具接口：

```text
load_skill(name: string, args?: string)
```

语义：

- `name` 只接受有效 catalog 中的 skill 名称，不接受文件路径。
- `args` 是可选的用户任务参数，用于显式 slash command 场景；它不参与文件寻址。
- 工具通过 catalog 中已验证的 root/path 调用现有 `SkillLoader`。
- 加载成功后返回完整 `SKILL.md`，并在结果中标明 skill 名称和可选 args。
- `SKILL.md` 中引用的其他文件不由工具自动读取。模型按正文要求使用现有 `view`、`read_multi` 或 `shell` 按需访问。
- 工具通过 session-scoped registry 注入，与 `task_list`、`retrieve_archive` 使用相同生命周期。
- `load_skill` 是保留名称，外部 supplied tools 不得覆盖。

错误行为：

- 未知名称：返回结构化失败，包含简短错误和可用 skill 名称提示；不写加载成功事件。
- 文件不存在、越界或读取失败：返回结构化失败；不写加载成功事件。
- `args` 不改变所加载的 skill，也不能被解释为路径。

## 会话状态与审计

成功调用 `load_skill` 后写入：

- `skill_selected`：记录由模型工具调用选择、skill identity 和当前 turn。
- `skill_loaded`：记录 content hash、字节数和当前 turn。

审计事件只在读取成功后写入，避免“选中但未加载”的成功假象。完整正文由正常 `tool_result` 事件持久化，因此 resume 直接重放当时实际返回给模型的内容，不重新读取当前磁盘文件。

删除运行时 `session.loaded_skills` 的永久 system-prompt 注入职责，并移除 `replay_loaded_skills` 恢复路径。历史 JSONL 中已有的 `skill_selected`、`skill_loaded` 事件继续作为旧事实保留，但新代码不把它们恢复为 system prompt 内容，也不增加兼容转换。

同一 skill 可以被模型再次调用。普通工具结果生命周期与 compaction 负责后续去重或归档，本次不另建常驻的“已加载集合”。这保证模型可以在 skill 文件发生变化后明确重新加载，同时每次实际使用都有对应工具事实。

## TUI 命令

- `/skills`：继续展示有效、去重后的 skill 目录和选择器。
- `/skill <name>`：继续展示 skill 详情，供用户检查；详情界面可以显示内部路径和来源，因为它不进入模型 system prompt。
- `/skill-use <name-or-picker-id>`：作为选择器内部动作，构造明确的用户指令，不直接读取正文。
- `/<skill-name> <instruction>`：提交一条用户消息，明确要求模型先调用 `load_skill`，并把 instruction 作为 `args` 使用。

slash command 最终仍进入正常聊天流程，不能绕过模型工具调用或直接修改 session skill 状态。

## 删除与保留

删除：

- AgentLoop 中调用 `SkillRouter` 的自动预加载流程。
- `SkillRouter` 生产实现及只服务于自动匹配的测试。
- `AgentSession.loaded_skills` 常驻集合。
- `replay_loaded_skills` 和恢复时重新读取 skill 文件的逻辑。
- system prompt 的路径/root/source 目录格式。

保留并调整：

- skill discovery 和 frontmatter 解析。
- `SkillDefinition`、`SkillCatalog`、`SkillLoader` 的安全路径边界。
- skill 审计事件 helper。
- TUI skill 浏览和显式调用入口。
- append-only session、普通工具调用、权限和 context 投影架构。

## 数据流与边界

`SkillCatalog` 是发现结果；需要新增一个集中式“有效 catalog”解析边界，负责按名称和来源优先级生成唯一视图。system prompt、TUI 和 `load_skill` 必须共享这个视图。

`SkillLoader` 只负责安全读取，不负责选择或持久化。`load_skill` tool executor 负责把 catalog lookup、loader 和审计 writer 组合起来。AgentLoop 不再知道 skill 的选择和读取细节，只像其他工具一样执行 `load_skill`。

## 安全性

- 模型只提交 name，无法传入任意路径。
- 实际 path 来自 discovery catalog，仍经过 `SkillLoader` 的 root-relative 校验。
- 同名解析在程序侧完成，模型不能选择被覆盖的低优先级副本。
- 工具失败不泄露无关绝对路径或文件内容。
- skill 不能覆盖项目指令、权限或 sandbox；现有 system prompt 协议继续声明该边界。

## 测试策略

实施按 TDD 进行，至少覆盖：

1. 有效 catalog 按 name 去重且项目优先。
2. system prompt 只包含 name 和单行短描述，不包含 root/path/source。
3. system prompt skill 目录不超过 8,000 字符且不输出半条记录。
4. 普通用户消息不再触发 Python 自动加载或 skill 事件。
5. session registry 注册 `load_skill` 并拒绝同名 supplied tool。
6. `load_skill` 成功返回完整正文，并写入正确的审计事件和 tool result。
7. 未知名称、文件缺失和路径越界返回失败且不写成功事件。
8. `args` 被安全回显但不参与寻址。
9. resume 从原有 tool result 恢复内容，不重新读取 skill 文件。
10. checkpoint/ContextBuilder 能按普通 tool result 处理已加载正文。
11. `/skills`、`/skill`、picker 和 `/<skill-name>` 使用有效 catalog，并引导模型调用工具。
12. 删除旧 router/replay 路径后无残留导入或死代码。

先运行 skill、session registry、agent tool flow、system prompt 和 TUI command 的聚焦测试，再运行：

```sh
.venv/bin/python -m pytest tests
```

## 验收标准

- 模型可以从精简目录发现 skill，并通过 `load_skill` 获取完整正文。
- 未调用 `load_skill` 时，任何 skill 正文都不进入请求。
- system prompt 不再暴露 skill filesystem 元数据或重复名称。
- skill 正文只作为普通会话工具结果存在，可 resume、可 checkpoint、可归档。
- FirstCoder 不再执行基于用户文本的本地自动 skill 匹配。
- 旧的永久 loaded-skill system prompt 状态和恢复代码被删除。
- 聚焦测试和完整 `tests` 测试集通过。
