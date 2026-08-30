# LansCoder 架构健康检查报告

- 日期：2026-08-29
- 审查方式：agentops-awesome-list（只读审计 + 3 个并行深度审计）
- 目标：LansCoder（`/Users/lansterzhang/Documents/LansCoder`，约 2.9 万行 Python 的本地编程代理）

## 体检结论

- 判定：**risky**
- 适用模板：**T3 生产项目**（对外发布、写文件/执行命令/访问网络、可无人审批长跑）
- 一句话结论：LansCoder 的核心引擎（代理循环、权限单一闸门、上下文压缩管线）属于同规模项目里罕见的强实现，测试锁定严密；但在「对外发布 + autonomous 长跑」这一档上，代码执行沙箱、评估体系、可观测性、护栏与证据门明显不足，且 README 宣称的基准文档（docs/benchmark.md）不存在。
- 置信度：high（结论基于源码逐行审计 + 3 个并行深度审计 + 归档的基准运行记录）

## 用户目标与审查边界

- 我理解的目标：把这个 ~2.9 万行的本地编程代理作为一个**可对外安装使用**的产品维护，支持**无人工盯屏的长任务**。
- 已向用户确认的信息：用途 = 对外发布/被他人使用；自治度 = 可无人确认地跑长任务。
- 当前假设：单进程单用户 CLI/TUI 工具（非服务端、非多租户），因此未按 T4 要求身份/RBAC/租户隔离；它能**在 permission=bypass 模式下全速执行写入、shell、python_exec**。
- 不在本次审查范围内：不评估 TUI 视觉体验；不评估 `.ai-team/` 开发治理流程本身（那套证据门在 CI/开发时有效、运行时无涉）。

## 完整架构基线

| 基线组件 | 难度要求 | 状态 | 当前证据 | 缺口/理由 |
|---|---|---|---|---|
| 系统边界 | required | present | README.md 项目声明、docs/architecture/index.md | 边界清晰（coding agent、不做规模化市场验证）；但 docs/benchmark.md 缺失使风险声明不可核验 |
| 任务接收 | required | weak | agent/loop.py、context/system_prompt.py | 意图分类/拒绝规则/成功标准全靠提示词，无显式分类与拒绝路径 |
| 身份/会话作用域 | required | adequate | session/models.py、memory/manager.py | session_id/workspace/项目级记忆隔离齐全；无 auth 上下文（本地单用户可接受） |
| Agent 循环 | required | strong | agent/loop.py(757行)、loop_limits.py、tests/test_agent_loop_limits.py | observe→think→act→结算显式化，含恢复路径 |
| 规划器 | required | adequate | planning/（linear/dag、revision 守卫）、agent/task_plan_policy.py | 计划实体+投影强；但结束对齐只是提示词指令，非强制 |
| 路由器 | optional | adequate | agent/subagent_engine.py、tool_execution.py:280-291 | 模型按 4 个固定角色委托，有角色/工具过滤；无置信度与 fallback（单父代理形态可接受） |
| 执行器 | required | adequate | agent/tool_execution.py、tools/ | 超时/取消/类型化错误强；**工具级重试缺失、回滚缺失** |
| 反思器 | required | weak | agent/loop.py:374-409 | 仅「可重试错误重试一次」；无步骤批判、无错误诊断与下一步策略 |
| 终止器 | required | strong | agent/guardrails.py、loop_limits.py | 200 轮/400 次/3600s 硬上限，超限/中断响应明确 |
| 类型化消息 | required | strong | providers/types.py、core/events.py、subagent/types.py | 契约测试钉死（test_core_contract.py 16 项） |
| 状态 schema | required | strong | context/runtime_state.py、context/store.py:266-283 | 事件溯源+版本化，replay 校验 revision 链，损坏抛错 |
| 工具 schema | required | weak | tools/types.py:24-32、utils/introspection.py | 输入类型化；输出为无结构文本；工具对象上无副作用/idempotency 声明（分类外置在 permissions/classification.py） |
| 工件 schema | required | adequate | context/archive.py（sha256 内容寻址+archive_id） | 版本/校验和/溯源有；无保留期与删除策略 |
| 交接 schema | optional | adequate | subagent/types.py（SubagentRequest/Result） | task packet 有；accept/reject/terminal 生命周期弱（单父委托够用） |
| 模型层 | required | weak | providers/factory.py、presets.py | 8 个 provider 预设、提示词版本 v19；**无按风险选模型、无 fallback 链、无结构化输出**（supports_json_mode 声明了但无实现） |
| 上下文装配 | required | strong | context/ L1-L3 流水线、token_budget.py、manager.py、runtime_state.py 熔断 | 预算水位/去重/归档/序列校验/熔断全有，~2k 行测试锁定 |
| 工作记忆 | required | strong | agent/session.py、context/runtime_replay.py | 会话事件+运行时状态，断点处可重建 |
| 短期记忆 | required | strong | session/resume.py、fork.py | 恢复/分支/分享齐全，中断工具调用尾修复有 |
| 长期记忆 | optional | weak | memory/ | **纯文件+子串/扫描检索，无向量/语义召回**；写记忆无审批门、无来源链接、无保留/大小限制 |
| 工具层（最小权限/审批/重试/回滚） | required | adequate | agent/permission.py 单一闸门、grants.py、tools/review.py 预写审查 | 审批/最小权限极强且经 test_single_gate.py 锁定；**重试与回滚缺失**；dry-run 仅 apply_patch |
| 代码/工作区沙箱 | required | **weak（第一风险）** | utils/execution_sandbox.py、utils/sandbox.py、utils/subprocess.py | shell/python_exec 是**同宿主裸进程**：无容器/网络策略/rlimit/chroot；仅 cwd 圈定+敏感 env 剥离+超时。pyproject 无 sandbox 层 |
| 项目台账 | required | weak | planning/（仅任务记账）；.ai-team/ 为开发期、运行时不读 | 运行时无目标/决策/操作/证据/版本单一台账 |
| 证据系统 | required | weak | session/ JSONL（追踪日志）、subagent/types.py evidence 字段 | 无 claim→evidence 索引、无出处链接、无过期/冲突处理 |
| 门禁系统 | required | adequate-弱 | 权限闸门强；结束对齐是提示词 | 无运行时证据门/发布门/后置条件检查；BYPASS 下写入不再人工审批（按自治度设定为有意，护栏须覆盖） |
| 工作区/工件 | required | adequate | agent/worktree.py worktree 隔离、archive | 有隔离；无独立工件仓库（版本/所有权/exports） |
| Agent 注册表 | optional | missing | — | 单进程本地工具，无需控制面（按形态判定 not-needed） |
| 角色矩阵 | optional | present | subagent/types.py 4 角色+工具白名单 | 角色、非目标、工具、数据访问清晰，coder 后台强制 worktree |
| 任务路由 | optional | weak | delegate 工具+角色过滤 | 模型自由选择角色，无置信度阈值/拒绝/升级（单父可接受） |
| 协调状态 | optional | missing | — | 子代理相互隔离、无共享状态（单父形态 not-needed） |
| 冲突仲裁 | optional | missing | — | tools/review.py 是人工审查门而非代理仲裁；按形态 not-needed |
| 交接生命周期 | optional | adequate | subagent_engine.py、background.py | 无暂停/恢复/审查交接周期；子会话跑完即删 |
| A2A 边界 | not-needed | — | — | 无 agent-to-agent 协议 |
| MCP 边界 | optional | adequate | mcp/config.py 严格校验+allowed_tools 白名单+超时+bearer | 校验严格；**无首次连接的交互式信任确认**，信任模型=配置文件即可`信 |
| 可观测性 | required | weak | agent/observer.py、JSONL 事件日志、usage_summary | 无结构化追踪/cost/latency/state-diff 指标；全仓库无文件日志（仅一个 logger） |
| 评估 | required | weak | benchmark/harbor/（外部 Harbor 适配器）、cli.py --benchmark；归档 run 显示 mean 0.94667 | 仅外部 Harbor Aider Polyglot；**无离线/golden/回归/red-team/canary**；docs/benchmark.md 不存在 |
| 护栏/安全 | required | weak | tools/hidden.py 空、agent/guardrails.py、session/redaction.py | 循环上限+权限闸门强；但**防注入薄**：hidden 工具列表空、无输出围栏标记、fork 不脱敏、无 jailbreak 检测 |
| 部署/运行时 | required | adequate | cli.py（TUI/REPL/单跑/benchmark）、background.py 后台任务、流式、并行工具 | 无服务端/健康检查/调度（本地工具不需要）；swe-bench-fast.toml 为孤儿配置 |
| 运维/runbook | required | adequate | AbandonSince、中断修复、resume | 无 runbook 文档 |
| 自演化 | not-needed | — | — | 无运行时自修改；开发期变更由 .ai-team check.mjs 约束 |

> 每条 required + weak/missing 组件均导致判定不得为 ready。

## 缺失组件清单

| 优先级 | 缺失组件 | 为什么重要 | 建议补齐方式 |
|---|---|---|---|
| P0 | 代码执行沙箱 | 选了「可无人确认长跑」，shell/python_exec 同宿主全权执行，一旦被诱导即全盘损失 | 至少：进程级资源限制（rlimit/线程数）+ 网络策略（默认禁网）；期望：OS 沙箱或容器，并以测试锁定 |
| P0 | 运行时护栏（防注入） | 无人盯屏时没有护栏回退 | 填 tools/hidden.py 可隐藏工具位；工具输出加定界标记并对用户提示词与不可信工具结果分类；BYPASS 模式的白名单 |
| P1 | 记忆写门+脱敏 | 记忆可被模型无审批写个人数据，fork.py 逐字复制不脱敏 | remember 走权限分类（当前未分类→绕过闸门）；记忆记录来源链接；共享/分支沿用 redaction |
| P1 | 回滚/补偿 | 长跑中断后部分执行状态只能「结果未知」上报 | 写工具补两阶段+旧内容存档回滚；至少写日志式补偿 |
| P1 | 评估体系 | 只有一个外部基准，无法防回归/防注入 | 建立离线 golden 集 + 轨迹评估 + 关键回归用例沉淀为 tests/evals/；补 docs/benchmark.md 复现口径 |
| P2 | 可观测性 | 成本/延迟/状态差异不可追溯，出问题无法归因 | usage_summary 扩展 cost 估算；工具 span 落 JSONL；加 --log 文件日志 |
| P2 | 运行时证据门 | 完成状态靠提示词「别谎报」 | TaskPlanPolicy.final_reconciliation_instruction 加后置校验（要求列出验证证据）；超时未完成不得标 done |
| P2 | 模型兜底 | 单一 provider 挂了长跑直接死 | ErrorKind 已分类，接 fallback 链到次选 provider |

## 架构地图

| 模块 | 当前证据 | 评分 | 说明 |
|---|---|---|---|
| app/（TUI） | app/tui.py（1360 行）转录/权限/子代理面板 | adequate | 只订阅不持有业务，架构上正确地薄 |
| agent/（编排） | loop/tool_execution/session/permission/subagent_engine | strong | 单闸门、恢复、worktree 隔离都落在编排层；重试/反思/护栏弱项也在此 |
| tools/+permissions/ | 33 工具、18+1 分类规则、grants、预写审查 | strong | 工具与权限互不知晓、单一执法点，test_single_gate.py 锁定 |
| providers/ | OpenAI 兼容+Anthropic、流式、错误分类 11 类 | adequate | 无结构化输出/无 fallback |
| context/ | L1-L3 压缩、熔断、归档、预算 | strong | 全项目工程质量最高的部分 |
| session/ | JSONL 事件溯源、resume/fork/share/redaction | strong | redaction 仅导出时生效 |
| memory/ | 文件+索引，项目/用户作用域 | weak | 无语义召回、无写门、无来源 |
| planning/ | TaskPlan linear/dag、revision、文件锁 | adequate | 运行时任务记账，非证据台账 |
| mcp/ | 严格配置校验、allowed_tools、timeout | adequate | 无首次信任确认 |
| benchmark/ | Harbor 适配器、Aider feedback 插件、归档运行 | adequate | 唯一评估；无内置 golden/回归 |

## 功能审查报告

| 功能 | 是否存在 | 完整度 | 主要问题 | 建议 |
|---|---|---|---|---|
| 代理循环+上限 | ✓ | 高 | 反思器弱 | 加错误分类→下一步策略钩子 |
| 权限裁决 | ✓ | 高 | 分类表未覆盖 12 个工具→绕过闸门（含 remember/forget） | 全工具入分类表或显式标注 not-classified |
| 预写审查 | ✓ | 高 | BYPASS 下仅展示不拦截（符合自治度设定） | 保持，但需配上 P0 护栏 |
| 上下文压缩 | ✓ | 高 | token 估算 heuristic（chars/4） | 接入真实分词或回退保护 |
| 会话持久化/恢复 | ✓ | 高 | 无限期保留、无用户删除命令 | 加保留策略与删除 |
| 子代理 | ✓ | 中 | 无注册表/仲裁/交接生命周期（按形态 ok） | 如需更强协作再加 |
| 记忆 | ✓ | 低 | 子串召回+无写门 | 先补写门/来源，再看语义召回 |
| 评估 | ✓（外部） | 低 | 无内置回归/red-team；docs/benchmark.md 缺失 | 建 tests/evals/+补文档 |
| 可观测 | ✓（事件日志） | 低 | 无成本/延迟/文件日志 | 扩 usage 落盘 |

## 关键问题

| 优先级 | 问题 | 为什么重要 | 建议修复 |
|---|---|---|---|
| P0 | 代码执行无 OS 沙箱，且可在无人确认下运行 | 泄露/破坏宿主是「无人确认长跑」设定下的首要风险 | 见缺失组件 P0 |
| P0 | 防注入护栏薄弱（hidden 工具空、无输出围栏、无 jailbreak 检测） | 无人盯屏 = 无第二道防线 | 填 hidden 位、定界工具输出、检测对抗输入 |
| P1 | README 宣称的 docs/benchmark.md 及各指南文档不存在；README「96.38%(213/221)」与归档 run mean 0.94667（221 试）口径需对账 | 对外物料的证据与声明不一致 | 补文档或用归档 run 更新声明 |
| P1 | 记忆写入无审批、fork 不脱敏 | 隐私面 | 记忆入分类表；fork 复用 redaction |
| P1 | 工具无重试/回滚 | 长跑不可靠 | 工具级重试策略+写补偿 |

## 优化建议

| 优先级 | 建议 | 预期收益 | 实施成本 | 验收方式 |
|---|---|---|---|---|
| P0 | shell/python_exec 默认禁网+进程限额（保留显式开启） | 未确认执行被极大收敛 | 中 | tests/test_execution_sandbox.py 增加网络阻断用例 |
| P0 | 护栏：填 HIDDEN_TOOL_STATUS_NAMES + 工具输出定界 + 敏感提示词检测 | 注入攻击面收敛 | 中 | 新增 antijailbreak 单测 |
| P1 | 记忆写走权限门 + RedactionOptions 应用于 fork | 隐私合规 | 低 | test_memory_tools + test_session_fork 新增用例 |
| P1 | 补 docs/benchmark.md（口径/复现/分数与归档 run 对账） | 对外可信度 | 低 | 文档+链接可访问 |
| P2 | 建离线评估集（golden task + 回归 + 注入对抗） | 可防回归 | 中 | pytest tests/evals/ 绿 + 门禁接入 |
| P2 | --log <file> + cost 估算落盘 | 事故可归因 | 低 | 日志与 session JSONL 时间线可对齐 |

## 下一步

1. 若认可 P0 与 P1，落一个最小修复批次（沙箱网络策略 + 记忆写门 + 护栏/脱敏），每项配套测试——这是当前判定 risky 的主要缺口。
2. 对账 README 基准声明：要么补 docs/benchmark.md，要么更新声明指向 benchmark/runs/ 归档。
3. 把离线评估集建起来并接入 .ai-team 的开发门禁，让每次发布都有除 Harbor 外的本地回归证据。