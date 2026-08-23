# 权限执法解耦与 runtime/ 移除设计:LansCoder tools/permissions/agent 职责重组

日期:2026-08-21
状态:v6(已并入 `runtime/` 移除;按两轮架构评审复审修订;`permission_results` 归位 agent/,tools ⊥ permissions 双零隔离;待用户评审)
评审说明:
- 首版经架构评审子代理核查,三项阻断已修(18 门控集、并行批二次求值、变更清单)。
- v3 起用户拍板本轮并入 `runtime/` 移除(外沿:只删包+清引用,不改任何文件名);v4 经调用方验真,`tools/permission_results.py` 整迁 `agent/permission_results.py`,废除 "tools 保留 DTO 消费边" 条款。
- v5 按 architecture-critic 复审修复 10 项:**阻断 2**——`test_agent_tool_flow.py`/`test_session_resume_service.py` 直接 execute 带闸 session 的测试未列入 E 清单(F-1);参数缺失请求若无条件串联进 `manager.preflight` 会从 DENY 偏到 ASK(F-2)。**主要 4**——`permission_registry.py:11` 是 `tools.permission_results` 唯一 tools 侧消费者,须先删再迁(F-3);`_permission_target_for_patch` 依赖 `tools/apply_patch.py::parse_patch`,classification 私有持有实现会导致 permissions→tools 边,须把解析族迁 `utils/patch.py`(F-4);`session.py:599` 调用点改名漏点(F-5);并行批"决策复用"机制空缺,须按 `tool_call.id` 缓存 prepare(F-6)。**次要 4**——计数 16/19 修正为 17/18(F-7)、同名自定义工具按名门控的语义变化未声明(F-8)、§6-2 完备断言措辞过强(F-9)、"两次求值"低估(F-10)。
- v6 按轻量复审(逐条核对 15 项,verdict=approve-with-changes)修正两处主要 + 六处措辞:①**确认 builder 归属声明**——`make_permission_confirmation_result` 的唯一生产调用方是 `permission_registry.py:48`,而该分支在生产上不可达(ASK 的确认载体是 coordinator 产出的 `pending_input`),E 删除 registry 后它成为**test-only 协议形状锁**,spec 对此显式声明而非含糊"双向协议";②**§6-E F-1 断言形态修正**——E 后直测对象的 `request_type=="permission_confirmation"` 形态不存在,断言须按 pending UserInputRequest 键集**重新表达**,并防 `test_app_factory.py:570` 真空化;③`test_tools.py`/`test_agent_tool_flow.py` 不直接引用 parse 面(真解析行为锁在 `test_mutation_tools`/`test_prewrite_review`/`test_review_view`);④Phase 1 ③行 27 处引用措辞矛盾;⑤F-1 行号校至 write=:121-142/apply_patch=:145-175/python_exec=:178+;⑥`tools/permission_results.py:5` 补入 DTO 消费方清单、F-4 理由精确化。
- 排程因此从"B 先 E 后"两阶段,重排为**三阶段**:机械搬移 A(Phase 1)→ E 权限手术(Phase 2)→ 机械搬移 B(Phase 3)。唯一原因是 F-3:Phase 2 删除 `permission_registry.py`(它 import 着 make_*),Phase 3 才能把 `tools/permission_results.py` 迁走——否则会出现一个 commit 的 tools→agent 向上边,违反单层依赖规则。
范围:本设计是更大重构(core/shell 拆包、`create_agent_loop` 移位、runner 命名)里**两个相邻子项目的合并**:权限执法与工具纯净化 + `runtime/` 杂物袋移除。归一后依赖图收敛为 `app → agent → {tools, permissions} → utils`,**tools 与 permissions 互不知晓**,`agent/` 是唯一协调层。

---

## 1. 背景与动机

LansCoder 目前权限链路存在三个结构问题,外加一个错误分层的包:

1. **多处策略求值**。`tools/permission_registry.py` 的 `PermissionAwareToolRegistry.execute()` 自带 preflight + 门控;`agent/permission.py` 的 `PermissionCoordinator.prepare()/preflight()` 又做一次 preflight,且靠 `isinstance(registry, PermissionAwareToolRegistry)` 强转去拿请求构建;此外 `agent/tool_execution.py:457` 的 `can_execute_in_parallel()` 对每个可并行只读调用**再调一次** `coordinator.preflight`,且批执行经由带闸 registry 还会再触发一次。一次 `git_status` 单调用路径求值两次、并行批内可达**三次**。策略求值分散在多层,单看代码无法断定"一次工具调用过几次闸"。
2. **工具层背着权限的脏腑**。内置门控工具在工具定义里声明 `ToolPermissionSpec(action=...)`;MCP 工具由 `mcp/adapter.py:59` 统一盖章;`tools/permission_results.py` 组装权限结果——它们待在 `tools/` 的**全部历史原因**是原始调用者 `PermissionAwareToolRegistry` 也在 `tools/`,而后者正是本次要删除的对象。这不只是"工具知道权限存在",而是**工具层承载了一整套权限的声明、盖章与结果组装**,与单层依赖规则和可读性目标相悖。
3. **读工具的门控依赖装饰器**。`grep/glob/ls/tree/view/read_multi` 六个读工具经 `tools/path_permissions.py` 的 `with_read_permission` 盖 READ_PATH 章——这是"敏感路径/项目根外读取需 ASK"的防线,**不能被"其余工具 → None 不门控"的默认规则悄悄吞掉**。
4. **`lanscoder/runtime/` 是语义错位的杂物袋**。`cancellation.py`(取消令牌/线程本地上下文)与 `user_input.py`(用户输入 DTO + ToolResult→请求转换)两簇互不相干,却同居一个"runtime"包;cancellation 是低层机制却位于 tool 层之上,user_input DTO 被 `permissions/manager.py` 直接消费却放在 permissions 之外;补丁解析 `parse_patch` 是纯文本格式工具,却被压在 `tools/apply_patch.py` 里,以至权限侧无法复用。参照系(Claude Code)中这些是 harness 运行机制,不该伪装成与 tools 平级的一层。

**参照系(Claude Code)**:官方架构中工具是纯净能力(schema 而已),权限是一个独立的 harness 侧轴——按"工具名 + 解析后的参数"匹配外置规则(allow/deny/ask,deny 优先),`mcp__*` 通配规则一键覆盖全部 MCP 工具;连内置只读 bash 命令集都是 harness 维护的表。执法发生在每次工具调用的边界。**harness(agent)坐在能力(tools)与执法(permissions)之间做协调者,能力与执法不互知。**

## 2. 目标与非目标

### 目标

1. **`tools/` 层纯净到极致**:对 `permissions` **零引用(含 DTO/类型消费)**——门控声明移出、MCP 不再盖章、`permission_results` 模块迁入 `agent/`,`tools` 与 `permissions` 互不知晓;`Tool` 删除 `permission` 字段,`ToolPermissionSpec` 移除。工具文件里残留的 target_builder(`_permission_target_for_patch`/`_permission_target_for_python_exec`/inline lambda/`read_*_target`)与分类表的归属冲突,由 classification.py 私有持有对应实现解决(§4.1)。
2. 单一执法点:`PermissionCoordinator` 成为唯一闸门——请求构建、preflight、ask/deny/放行、后台自动拒,全在它与其调用的 `permissions/` 内部;工具执行只走无闸的原始 `registry.execute`;**并行批判定复用 prepare 结果,不再触发第二次策略求值**;每工具调用在一次准备周期内 `manager.preflight` 恰好一次(测试认定);**参数缺失短路 DENY,不经 preflight**(§3/F-2)。
3. "工具调用 → 权限请求"构建职责整体从 `tools/permission_registry.py` 迁入 `permissions/classification.py`;权限结果的组装与还原(`make_*` + `user_input_request_from_tool_result`)从 tools/runtime 归入 `agent/permission_results.py`,成为 agent loop 回合协议的单层归属。
4. **`lanscoder/runtime/` 包移除**:`cancellation.py` → `utils/cancellation.py`;`user_input.py` 拆两半——DTO → `permissions/user_input.py`、转换函数 → `agent/permission_results.py`(与整文件迁入的 `make_*` 组装族同居);`parse_patch`/`PatchPlan`/`PatchOperation`/`PatchHunk`/marker 常量从 `tools/apply_patch.py` → `utils/patch.py`(classification 的 target 实现与工具执行**共用同一解析**,不产生 `permissions → tools` 边);删 `__init__.py`;全仓 **27 处** `lanscoder.runtime` 引用清零(外沿:只删包+清引用,不改任何文件名);`app/runtime.py`、`context/runtime_replay.py`、`context/runtime_state.py` 文件名**本轮保留**。
5. 全部行为语义不变(§5 不变式),包括敏感读的 ASK 防线与参数无效的 DENY 文案。

### 非目标

- **不改权限语义**:MCP 全 ASK、自主 ASK→DENY、BYPASS 全放、grants/allow_always,原样保留。
- **不做用户可写规则 JSON**(Claude Code settings 形态):`classification.py` 是代码内注册表,用户规则留作未来接缝。
- **不做 MCP 按 server 分级信任**:仍全 ASK;`mcp__*` 前缀规则只是把现有行为搬进分类表。
- **改名不是本轮内容**:`app/runtime.py`(装着 `create_agent_loop`/`AgentChatRunner`/`CurrentSessionState`)与 `context/runtime_replay.py`/`runtime_state.py` 的改名分别归子项目 C、D 与 compact 命名线。本轮只删 `lanscoder/runtime/` 包、不改文件名。
- **同名自定义工具不豁免门控**:按名命中分类表的工具一律门控,这是本轮外沿的**已知语义变化**(F-8),声明在 §4.2,不做例外处理。
- **历史文档不改**:`docs/superpowers/` 既往 spec/plan 里对 `lanscoder.runtime.cancellation` 的引用是历史记录,不修订。
- **不引入能力标签词表**(中间态方案 C)。
- 本设计**不覆盖**但**不阻碍**:core/shell 拆包、`create_agent_loop → agent/factory.py`、runner 命名、`docs/architecture.md` 修订——均属后续子项目(§9)。

## 3. 现状诊断(事实底稿,按 v5 复审修正)

- **多处求值确认(单向调用路径 2 次、并行批内可达 3 次)**:`PermissionAwareToolRegistry.execute`(`tools/permission_registry.py:38-53`,内含 preflight)与 `coordinator.prepare/preflight`(`agent/permission.py:105-114`,经 `isinstance` 强转)并存;`tool_execution.py:449-458` 的 `can_execute_in_parallel` 对并行只读候选再跑一次 `coordinator.preflight`,且批执行经 `session.execute_tool_call` → 带闸 `registry.execute` 再触发第三次。
- **门控工具构成(18 个内置工具名 + `mcp__*` 前缀)**:
  - 直接声明(12):write/delete/shell/edit/apply_patch/fetch/git_diff/git_status/git_log/diagnostics/python_exec/web_search
  - 读工具(6,经 `with_read_permission`,`tools/path_permissions.py:21-32`):grep/glob/ls/tree/view/read_multi
  - `tools/path_permissions.py` 是**辅助模块**(含 `with_read_permission`/`read_path_target`/`read_multi_target`),不是工具。
  - `mcp/adapter.py:32` 命名 `mcp__{server}__{tool}`;`app/runtime.py:151` 用 `name.startswith("mcp__")` 识别(`agent/mcp_activation.py` 用**构造期传入的** `mcp_tool_names` frozenset,不是前缀判定)。
- **模型可见性事实**:权限章(permission 字段)从不进入模型上下文;模型见 `ToolDefinition`(name/description/parameters),MCP 工具描述含 "调用 MCP 工具 {server}/{tool}。"。删章不影响模型认知。
- **依赖方向(现状,计数按 v5 实测修正)**:`tools → permissions` 现存 **17 文件 18 行**引用,分三类——①门控声明:`permission_registry.py` 外的 14 文件、14 行(12 门控工具的 `ToolPermissionSpec` + `path_permissions` + `tools/types.py` 的 TYPE_CHECKING);②执法:`permission_registry.py` 2 行(`PermissionManager`/`PermissionDecision`...)+ `tools/session_registry.py` 1 行(`PermissionManager`),共 2 文件 3 行;③结果组装:`tools/permission_results.py` 1 行(`PermissionDecision`/`PermissionRequest`)。`permissions → tools` 仅 `runtime/user_input.py` 对 `tools.types.ToolResult` 的 TYPE_CHECKING。E 清 ①②,B 三阶段(§4.1)清 ③ → 双零。
- **`tools/permission_results.py` 是待消融的历史残留(调用方事实)**:
  - `make_permission_confirmation_result`/`make_permission_denied_result`/`make_prewrite_review_stale_result`/`make_prewrite_review_failed_result` 的调用方仅三处:`tools/permission_registry.py:45,48`(**E 删除**)、`agent/permission.py:143,195,243`、`agent/permission_resume.py:181,189,199`。
  - `user_input_request_from_tool_result` 唯一调用方 `agent/tool_execution.py:425`;`_options_from_data` 唯一外部引用方 `agent/session.py`(import :28 + **调用点 :599** 两处,F-5)。
  - 对 `lanscoder.tools.permission_results` 的引用仅上述 agent 两文件 + `tools/permission_registry.py:11` + `tests/test_permission_results.py`;**app/、mcp/、其余 tools、其余 agent 文件零引用**(registry 的引用见下一条 F-3 依据)。
  - **`permission_registry.py:11` 是 `tools.permission_results` 唯一 tools 侧消费者**——这是"必须先删 registry、再迁模块"排程的依据(F-3);否则迁址瞬间产生一个 commit 的 tools→agent 向上边。
- **target_builder 函数事实(F-4)**:
  - `apply_patch.py:74-82` `_permission_target_for_patch` 调 `parse_patch(patch)`(定义在 `apply_patch.py:85`,纯 marker 文本解析,依赖 `BEGIN_MARKER`/`END_MARKER`/`PatchPlan`/`PatchOperation`/`PatchHunk`);**`tools/review.py:10` 也消费 `PatchPlan`/`parse_patch`**。
  - tests 侧**无直接引用 parse 面**(`test_tools.py`/`test_agent_tool_flow.py` 仅 import `create_apply_patch_tool`);解析行为锁在 `test_mutation_tools.py`/`test_prewrite_review.py`/`test_review_view.py`(经工具执行路径,迁址后自动绿)。
  - `python_exec.py:66-69` `_permission_target_for_python_exec` 纯字符串、零依赖;`git_diff.py:50` 内联 lambda 零依赖;`path_permissions.py:15-20` 两 read builder 纯字符串、零依赖。
  - 分类表要复用这些 target 实现,直接引用 tools 符号即产生 `permissions → tools` 运行时边 → 双零破功。结论:parse_patch 族迁 `utils/patch.py`(两个 tools 消费者 + classification 共用),其余 builder 逻辑由 classification 私有持有。
- **敏感读防线有测试锁死**:`tests/test_read_tools.py:115-148` 断言 `view private.key`、`read_multi [README, private.key]` 必须 ASK 且不泄漏内容。任何重构不得让这 6 个读工具失去 READ_PATH 门控。
- **参数缺失短路事实(F-2)**:`permission_registry.py:73-84` 捕获 `permission_request_for_tool` 抛的 `ValueError`,**不调 `manager.preflight`**,直接返回 `request(id=f"perm_{name}_invalid", target="", reason=str(exc))` + `decision(DENY, reason=str(exc))`。新的 coordinator 必须逐字保留该短路,否则 `shell` 缺 `command` 等场景会从 DENY 漂成 ASK。
- **`lanscoder/runtime/` 组成(27 处引用、20 个消费文件,v5 复测确认)**:
  - `cancellation.py`(4 符号):消费方 `app/runtime.py`、agent/{loop,tool_execution,background,observer,subagent_engine}、utils/{subprocess,execution_sandbox}、tests/{test_utils_subprocess,test_agent_context_loop,test_model_request_options,test_delegate_tool}。
  - `user_input.py`(2 DTO + 2 转换):DTO 消费方 `permissions/manager.py`、agent/{loop,session,tool_execution,permission,permission_resume,user_input}、**`tools/permission_results.py`(:5)**、app/runtime.py、tests/{test_permission_results,test_app_tui};转换消费方见上条。
  - `__init__.py` 仅聚合转发,删无散落(无 `import lanscoder.runtime as` 用法)。无 pyproject/路径形式引用;`docs/superpowers/` 历史引用不修订。

## 4. 设计

### 4.1 组件归属变化与**三阶段实现顺序**

冲突约束决定顺序:
- `permission_registry.py:11` 是 `tools/permission_results` 唯一 tools 侧引用 → **Phase 2(删 registry)必须先于 Phase 3(迁模块)**;
- classification 的 apply_patch target 复用 `parse_patch` → **`utils/patch.py` 迁址(Phase 1)必须先于 classification 落地(Phase 2)**。

```
Phase 1  机械搬移 A(纯搬移,逐批全绿):
         ① runtime/cancellation.py → utils/cancellation.py
         ② runtime/user_input.py 拆:DTO → permissions/user_input.py;user_input_request_from_tool_result + options_from_data → agent/permission_results.py
         ③ lanscoder/runtime/ 整体删除(含 __init__.py)——27 处引用**全部**收口(含 tools/permission_results.py:5 的 DTO 引用,一并改指 lanscoder.permissions.user_input);Phase 1 结束时 lanscoder.runtime grep 为零(§6-6),四个 make_* 尚未动,tools/permission_results.py 仍原址
         ④ apply_patch.py 的 parse_patch/PatchPlan/PatchOperation/PatchHunk/BEGIN_MARKER/END_MARKER → utils/patch.py;apply_patch.py 与 review.py 改从 utils 导入
        → tools/permission_results.py(含 make_*)仍原址,注册表依赖它,无向上边
Phase 2  E 权限手术:classification.py(用 utils.patch::parse_patch 构建私有 target 实现)、coordinator 重构、session 去 isinstance、tool_execution 并行批按 id 缓存 prepare、session_registry 纯 ToolRegistry、mcp 去章、工具去 spec、path_permissions 拆除、permission_registry.py 删除 + agent/permission.py 内 make_* 改从 tools.permission_results 改为继续可用(agent→tools 合法)
Phase 3  机械搬移 B:tools/permission_results.py(四个 make_* + _permission_request_data)→ 并入 agent/permission_results.py;agent/permission.py、agent/permission_resume.py re-point(agent→agent);tests/test_permission_results re-point;全仓依赖断言 + grep 收口
```

三阶段各自保持全绿、单向依赖;**tools→agent 边在任一 commit 都不出现**。

---

**(以下表格是终态归属,不是执行顺序)**

**E 部分——删除(无替换)**

| 对象 | 处置 |
|---|---|
| `tools/permission_registry.py` | 整文件删除(`PermissionAwareToolRegistry`、`permission_request_for_tool`/`_target_from_arguments`/`_cwd_from_arguments`/`_permission_request_id`) |
| `tools/types.py` 的 `ToolPermissionSpec` + `Tool.permission` 字段 | 移除,`Tool` 回归纯 schema |
| `tools/path_permissions.py` | 整体拆除:`with_read_permission` 移除,`read_path_target`/`read_multi_target` 迁入 classification.py |
| `apply_patch.py`/`python_exec.py` 的 `_permission_target_for_patch`/`_permission_target_for_python_exec`、`git_diff.py` 的 inline lambda | 工具文件删除 spec 时同步删除本函数;target 逻辑由 classification.py **私有持有**相应实现(见下) |

**E 部分——迁移与新增**

| 文件 | 动作 |
|---|---|
| `tools/permission_registry.py` 的请求构建逻辑(allocation)| → `permissions/classification.py`(新) |
| 18 个内置门控工具的 `ToolPermissionSpec` 声明 + `PermissionAction` import | → `permissions/classification.py` 对应条目,逐项转写(含 `target_arg`/`target_value`/`target_builder`/`cwd_arg`/`reason`/`allow_*` 全字段,以及 `request_id` 生成算法 `_permission_request_id` 的 sha256 payload 逐字一致,保住挂起/恢复的 request_id 匹配) |
| `mcp/adapter.py` | 去掉盖章与 `from lanscoder.permissions.types import PermissionAction` |
| `tools/session_registry.py` | `create_session_tool_registry` 去掉 `PermissionAwareToolRegistry` 分支,恒返回纯 `ToolRegistry`(随之清掉对 `PermissionManager` 的引用) |
| `agent/permission.py`(coordinator) | `preflight()` 改为 `classification.classify(...) → build_request(...) → [invalid 短路] → manager.preflight(...)`;删 `isinstance` 与 import;`make_*` 引用指向 `agent/permission_results` |
| `agent/session.py` | `tool_registry` 变纯 `ToolRegistry`;`isinstance` 分支(:450-455,语义=确认/后台后用不带闸执行)简化为直接 execute——调用方均已先过 coordinator,不丢闸 |
| `agent/tool_execution.py` | `can_execute_in_parallel` 不再自调 `preflight`;**并行批判定改为按 `tool_call.id` 缓存本回合 prepare 决策(新增),批组装扫描复用缓存("无门控 或 ALLOW"才并入批)**;执行一律走 coordinator 之后的原始 `registry.execute` |

**B 部分——`runtime/` 移除、`permission_results` 归位、parse 面下放 `utils/`（终态）**

| 对象 | 处置 |
|---|---|
| `lanscoder/runtime/cancellation.py` | 逐字迁 → `utils/cancellation.py`(4 符号;`AgentCancelledError` 类名、`raise_if_cancelled` 行为、`_LOCAL` 线程本地语义全不变) |
| `runtime/user_input.py` 的 DTO 半段 | 逐字迁 → `permissions/user_input.py`(`UserInputOption`/`UserInputRequest`;converter 迁走后**连 `tools.types` 的 TYPE_CHECKING 都不需要**) |
| `runtime/user_input.py` 的转换半段 | 迁 → `agent/permission_results.py`:`user_input_request_from_tool_result`(逻辑逐字)+ `_options_from_data` **改名 `options_from_data`**(被 `agent/session.py` 引用,模块私有名在跨模块引用下不成立;且 import :28 与调用点 :599 **双点同步**,F-5) |
| `tools/permission_results.py` | 整文件迁 → `agent/permission_results.py`(四个 `make_*` + `_permission_request_data` 逐字)并入转换半段;`tools/permission_results.py` 删除。该文件成为 **agent loop 回合协议的归属**:还原向(`user_input_request_from_tool_result`)是生产路径;组装向除确认 builder 外(DENY/预写审查三函数)是生产路径——**确认 builder 的归属见下注** |
| `apply_patch.py` 的 `parse_patch`/`PatchPlan`/`PatchOperation`/`PatchHunk`/`BEGIN_MARKER`/`END_MARKER` | 逐字迁 → `utils/patch.py`;`tools/apply_patch.py`(保留 `create_apply_patch_tool`/`_apply_plan`)、`tools/review.py` 改从 `utils.patch` 导入 |
| `lanscoder/runtime/__init__.py` | 删除(无重新导出残留) |
| 27 处 `from lanscoder.runtime.* import ...` | 全部改写指向新家:utils.cancellation / permissions.user_input / agent.permission_results |

**确认 builder 的归属(v6 复审修正)**:
- `make_permission_confirmation_result` 的唯一**生产**调用方是 `permission_registry.py:48`,而该分支在生产上**不可达**——`ToolExecutor` 的确认请求由 `coordinator.prepare` 直接产出 `pending_input`(UserInputRequest),执行期到达 registry 时决策已是 ALLOW/未门控,从不产 confirmation 形态的 ToolResult。
- 因此 E 删除 registry 后,该函数成为 **test-only 协议形状锁**:由 `test_permission_results.py` 与 F-1 迁移测试钉住 data 键协议(`requires_user_input`/`request_type`/`permission_request_id`/`options`);§4.3 数据流中确认的载体是 coordinator 的 `pending_input`,**与现状一致,非本轮新增或移除行为**。
- spec 不为其指定新生产调用者("双向协议"表述降级为此注);若未来需要,接缝在 `agent/permission_results.py`。

**`permissions/classification.py` 对 target 实现的持有方式(F-4 锁死)**:
- 私有持有:仿 `read_path_target`/`read_multi_target`,classification.py 实现 `_patch_files_target`(调 `utils.patch.parse_patch`,与工具执行**共用同一解析**,逐字等值)、`_python_exec_target`、`_git_diff_target`(`"diff --cached" if staged else "diff"`)。
- **禁止** `permissions/` 内任何 `from lanscoder.tools...`(含 TYPE_CHECKING);parse_patch 只经 `utils.patch` 中转。
- §6-4 依赖断言锁住该约束;§6-1 对 18 门控的 target 字面量等价断言兜住漂移。

**E∩B 双重改动文件(阶段归属见三阶段图)**:

| 文件 | E 改什么(Phase 2) | B 改什么(Phase 1/3) |
|---|---|---|
| `agent/permission.py` | coordinator 请求构建/分类接线、删 isinstance | Phase 1:DTO import 改 permissions.user_input;Phase 3:`make_*` 改从 `agent/permission_results` 取 |
| `agent/session.py` | isinstance 分支简化、tool_registry 纯化 | Phase 1:`_options_from_data`→`options_from_data` 双点(:28 import + :599 调用) |
| `agent/tool_execution.py` | 并行批判定去 preflight + 按 id 缓存 prepare | Phase 1:2 处 import(cancellation 迁址 + converter 从 `agent/permission_results` 取) |
| `app/runtime.py` | 零改动 | Phase 1:2 行 import(:18/:32) |

### 4.2 分类表数据形态(`permissions/classification.py`)

```python
@dataclass(frozen=True, slots=True)
class ClassificationSpec:
    action: PermissionAction
    target_arg: str | None = None          # arguments[target_arg] 作 target
    target_value: str | None = None        # 固定 target(如 git_status="status --short")
    target_builder: Callable | None = None # 一等成员:apply_patch/git_diff 与 6 读工具在用(非逃生门);实现由 classification 私有持有
    cwd_arg: str | None = None
    reason: str = ""
    allow_always: bool = True
    allow_auto: bool = True                # python_exec/apply_patch/MCP 条目显式 False
```

匹配规则(按序):

1. **精确名(18)**:上表 12 个直接声明 + 6 个读工具。读工具共用 READ_PATH 组约定:`grep/glob/ls/tree/view` → `target_builder=read_path_target`,`read_multi` → `target_builder=read_multi_target`。
2. **前缀规则** `mcp__*`: `server, tool = tool_name.removeprefix("mcp__").rsplit("__", 1)` → `ClassificationSpec(action=MCP_TOOL, target_value=f"{server}/{tool}", reason=f"调用 MCP 工具 {server}/{tool}。", allow_auto=False)` —— reason 逐字沿用 `mcp/adapter.py:63` 的文案（用户可见，不得退化为通用文案）。
3. **其余一切工具 → `None`(不门控)**,对应今日 `tool.permission is None` → 直接执行。**读工具必须显式落在规则 1**,默认 None 对它们不适用。**同名自定义工具(F-8)**:按名命中规则 1/2 即门控——用户注册的 `shell` 工具不再因"对象无 permission"而逃过门控,这是本轮外沿的已知语义变化,不做豁免;测试对该变化加断言。

公共 API:

```python
def classify(tool_name: str, arguments: dict) -> ClassificationSpec | None: ...  # None = 不门控
def build_request(tool_name: str, arguments: dict) -> PermissionRequest: ...      # 参数缺失抛 ValueError(等价今日 permission_request_for_tool)
```

`build_request` 承接今日 `permission_request_for_tool` 的 target/cwd/request_id 生成全部逻辑;**参数缺失时抛 `ValueError`(与今日逐字一致),由 coordinator 捕获并短路 DENY**(见 §4.3 [invalid] 分支)——不把无效请求喂进 `manager.preflight`(F-2)。

**转写注意(逐项锁死,防止漂移)**:
- `reason` 文案逐字一致;`allow_*` 默认值相反,凡显式 `False` 的条目必须照抄(python_exec/apply_patch/MCP)。
- `request_id` = `_permission_request_id` 的 sha256 payload 逐字一致(挂起/恢复依赖);**无效参数 id 固定为 `perm_{name}_invalid`**,reason = 原 ValueError 文案(§3 短路事实)。
- `web_search` 的 `target_value` 保留工具侧 URL 字面量**原样**(`f"{EXA_MCP_URL},{PARALLEL_MCP_URL}"`,勿顺手修好)。
- target_builder 一律由 classification 私有持有(读二、patch、python_exec、git_diff),**禁止引用 tools 符号**;apply_patch 的解析经 `utils.patch.parse_patch` 复用。
- B 期间 DTO/组装/转换函数**零行为改动**:`UserInputRequest.payload` 键集、`request_id` fallback 链(`data.request_id` → `data.permission_request_id` → `tool_call_id`)、`options_from_data` 的 label 缺省规则、四个 `make_*` 的 data 键集逐字保留(测试锁死,见 §6)。

### 4.3 数据流(一次工具调用的完整旅程)

所有闸门收敛到 coordinator,`[GATE]` 为**唯一**策略求值点:

```
模型发出 ToolCall
   → ToolExecutor.execute_interactive
   → [GATE] coordinator.prepare(tool_call, deferred)     ← 每工具调用一次
         ① classification.classify(name, args) → None=不门控,直接放行
         ② build_request(name, args)
             ├─ ValueError(参数缺失,id=perm_{name}_invalid) → 短路 DENY 回填(不调 preflight)
             └─ PermissionRequest
                → ③ manager.preflight(request) → grants 命中? → policy.decide → 自主 ASK→DENY
     ├─ DENY → agent/permission_results.make_permission_denied_result 回填(本轮结束)
     ├─ ASK / review_only → store_pending_request → UserInputRequest(permission_confirmation)
     │                      → 回合挂起,TUI/REPL 弹出确认
     └─ ALLOW → bypass_mutation(仅 BYPASS 预写审查)
                → 并行批判定(复用本批 prepare 决策,不二次 preflight)
                → 原始 registry.execute(name, args) ← 无闸执行
                → 前台直跑 或 run_in_background 交 BackgroundJobManager
                → ToolResult 回填
```

挂起/恢复:用户选择 → `runner.aresume_with_user_input(request_id, answer)` → `loop.resume_with_user_input` → `coordinator.pending_get(request_id)` → `manager.resolve_confirmation(choice)` → DENY 用 `agent/permission_results.make_permission_denied_result` 回填;ALLOW 用 trusted `pending_tool_call` 走原始 `registry.execute` → 回填 → loop 继续。ask_user 分支(模型主动问用户)路径不变——DTO 已迁址 `permissions/user_input.py`,还原转换住在 `agent/permission_results.py`,数据流外形不因此变化。**确认请求的生产载体是 coordinator 产出的 `pending_input`(UserInputRequest),不是 permission_confirmation ToolResult**——`make_permission_confirmation_result` 仅为 test-only 协议锁(见 §4.1 注)。

错误处理:`classify`/`build_request` 参数缺失 → **短路 DENY(带原因、id=`perm_{name}_invalid`),不抛未捕获异常、不调 preflight**;挂起态存 `pending_permission_execution`,恢复靠 `request_id` 匹配,不匹配视为无效恢复。

**与现状的差异**:①③ 从"registry 内嵌的第二次 preflight + can_execute_in_parallel 的第三次"收敛为唯一一次;并行批判定复用结论而非重跑;工具执行从带闸 `registry.execute` 改为原始 `registry.execute`;权限结果组装从 `tools/permission_results.py` 迁至 `agent/permission_results.py`(调用方本就在 agent/,纯位移)。

### 4.4 移除对象与收尾

见 §4.1。三阶段完成后 grep 清零(必须为空):
- E:`from lanscoder.tools.permission_registry`、`ToolPermissionSpec`、`with_read_permission`、**`tools/` 内对 `permissions` 的任何引用(含 DTO/类型)**、**`permissions/` 内任何 `from lanscoder.tools`(含 TYPE_CHECKING;parse_patch 只经 `utils.patch` 中转)**。
- B:`lanscoder.runtime`(全仓,排除 `docs/superpowers/` 历史文档)、`from lanscoder.tools.permission_results`。

## 5. 行为不变式(改动前后逐条成立)

1. **MCP 一律 ASK**:`mcp__*` → MCP_TOOL → policy ASK;例外仅 BYPASS 与事前 grants。
2. **敏感读防线**:grep/glob/ls/tree/view/read_multi 对项目根外/敏感路径(.git/.env/*.key)仍 ASK;`test_read_tools` 全绿。
3. **后台/自主子代理 ASK→DENY**:`manager.autonomous` 逻辑不动。
4. **BYPASS 全放行** + BYPASS 下预写审查(`requires_bypass_review`)不动。
5. **grants 先于 policy** 的求值顺序保留(`manager.preflight`)。
6. **预写审查路径**(`requires_review`/`store_pending_request`)保留在 coordinator。
7. **未门控工具直接执行**(等价今日 `permission=None`)。
8. **恢复语义**:`request_id` 匹配;`pending_tool_call` 深度拷贝防篡改。
9. **子代理网络自动放行 grant**(`child_permission_manager`)不动。
10. **模式切换同步 sandbox**(`_sync_sandbox_access_with_mode`)不动。
11. **参数无效 → 短路 DENY 带原因,id=`perm_{name}_invalid`,不经 preflight**(与 `permission_registry.py:73-84` 逐字一致,不漂成 ASK)。
12. **并行只读批不触发第二次策略求值**(`can_execute_in_parallel` 不再自调 preflight;决策按 id 缓存复用)。
13. **B 纯搬移零行为变化**:cancellation 线程本地语义、DTO 字段与排序、转换与四个 `make_*` 的判空/缺省/request_id fallback/data 键集、**`parse_patch` 的 marker 校验与异常文案**逐字保留;仅 import 目标与模块路径改变。`AgentCancelledError` 类名与 `raise` 语义不变。
14. **`tools` ⊥ `permissions` 双零**:`tools` 对 `permissions` 零引用(含 DTO);`permissions` 对 `tools` 零引用(含 TYPE_CHECKING;classification 的 parse_patch 经 `utils.patch` 中转)。`agent/` 是唯一同时知晓两者的层。

## 6. 测试策略

**E 部分——新增**

1. **分类表单测(含 F-2 短路)**:18 个门控工具名 → 正确 action/target/cwd/reason/allow 标志(与现 spec 逐条等价断言,含 reason 文案与 `_permission_request_id` 指纹);**18 个门控工具缺参 → `build_request` 抛 ValueError → coordinator 短路:decision=DENY、id=`perm_{name}_invalid`、reason=原文案**(逐条锁);`mcp__*` → MCP_TOOL + rsplit 解析(server 名含 `__` 仍正确);未知工具 → None;F-8:**自定义同名 `shell` 工具 → 按名命中 → ASK,不因对象无 permission 豁免**。
2. **门控完备性断言(双向,防漂移不防新增)**:静态硬编码集合 `{18 个门控名 ∪ mcp__*}` ⊆ 分类表条目,且**分类表条目 ⊆ builtin 注册表名集合 ∪ {mcp__*}**——后者防"表里引用不存在的工具名";"新增门控工具忘登记"仍靠人工同步静态集合,注明局限。
3. **单一闸门断言**:单批工具调用中 `manager.preflight` 恰好一次;并行只读批一次准备周期内**每成员恰好一次**(N 成员批 = N 次,锁 F-6 的 prepare 缓存机制)。
4. **依赖方向断言**(AST/pytest 扫描,进 CI):`permissions/` 不得**运行时** import `tools`/`agent`/`app`,且 **`tools`/`permissions` 双向零引用(含 TYPE_CHECKING)**;`permission_registry`/`with_read_permission`/`tools.permission_results` 引用数为零。
5. MCP 前缀不变量:任意 `mcp__` 名 → classify → ASK。

**B 部分——新增与迁移**

6. **引用清零断言(进 CI)**:`grep -rn "lanscoder\.runtime" lanscoder tests` 引用数为零(排除 `docs/superpowers/`)。Phase 1 结束时达成。
7. import 行改写、断言不变:Phase 1 组——`tests/test_utils_subprocess.py`、`tests/test_agent_context_loop.py`、`tests/test_model_request_options.py`、`tests/test_delegate_tool.py`(:383/:441/:942,→ `lanscoder.utils.cancellation`)、`tests/test_app_tui.py`、`tests/test_permission_results.py`(DTO → `lanscoder.permissions.user_input`;转换 → `lanscoder.agent.permission_results`);**`tests/test_tools.py`/`tests/test_agent_tool_flow.py` 无需改动**(仅 import `create_apply_patch_tool`,不直接引用 parse 面;解析行为锁在 `test_mutation_tools.py`/`test_prewrite_review.py`/`test_review_view.py`,经工具执行路径、迁址后自动绿);Phase 3 组——`tests/test_permission_results.py` 的 `make_*` import → `lanscoder.agent.permission_results`,整个文件作为迁移行为锁必须全绿。

**E 部分——迁移与保持绿(v5 补 F-1)**

- `tests/test_permission_registry.py`(整文件,基于 `PermissionAwareToolRegistry`):改写为 classification + coordinator 语义。
- `tests/test_execution_tools.py` / `tests/test_read_tools.py`:夹具从带闸 registry 换为 coordinator-prepare 路径重新表达,断言不变。
- **`tests/test_agent_tool_flow.py`(F-1,行号 v6 校至 write=:121-142 / apply_patch=:145-175 / python_exec=:178+)**:`test_project_session_permissioned_write_pauses_without_writing` 等对带闸 session 直接 `execute_tool_call` 断言 confirmation 形态的 ToolResult(`result.data["request_type"]=="permission_confirmation"`、`result.data["permission_request"]["action"]`、`result.data["options"]`)——E 后 `session.tool_registry` 为纯 `ToolRegistry`,该断言形态**不存在**。**测试意图不变(要求确认 + 未真写入),断言对象重表达**:改走 `coordinator.prepare` 路径,断言产出为 pending `UserInputRequest`(kind=`permission_confirmation`/payload/question/options 键集)且文件未写;再补 DENY/ALLOW 恢复分支断言(pending_tool_call 走原始 execute)。
- **`tests/test_session_resume_service.py`(F-1)**(:219-229 经 `ResumeService` 恢复出的会话直接 execute 断言同上):同样改走 coordinator 路径,断言形态改 pending UserInputRequest。
- **`tests/test_app_factory.py:570`(F-1 附)**:现断言 `request_type != "permission_confirmation"`——E 后纯 registry 下该字段恒 `None`,断言**真空化**(恒真)。须改为断言确认路径语义(如 pending_input 缺失时 execute 直跑),禁止靠空值恒真。
- `tests/test_mcp_adapter.py` / `tests/test_mcp_integration.py`:去章后的工具构造与 classify 行为。
- 其余权限相关测试保持绿(含 `test_permission_results` 的 Phase 3 全绿)。

## 7. 依赖方向保证

迁移后(三阶段终态)全仓依赖断言:

- `tools/` 与 `permissions/` **双向零引用(含 TYPE_CHECKING)**:tools 不 import 任何 permissions 符号;permissions 不 import 任何 tools 符号(classification 的 parse_patch 经 `utils.patch` 中转)。
- `agent/` 是唯一同时知晓两者的层:`agent → tools`(ToolRegistry/ToolResult 等既有边)、`agent → permissions`(coordinator/classification/manager 与 `agent/permission_results.py` 的 DTO 消费)。`agent/permission_results.py` 自身零 agent/ 内部引用,无环。
- `mcp/` 零 `permissions` 引用。
- `utils/` 新增 `cancellation.py` 与 `patch.py`,位于最低层,无新环(仅 stdlib/typing/dataclasses;`utils/subprocess.py`/`execution_sandbox.py` 变同包导入;tools 与 permissions 均向下引用 utils)。
- 全链 `app → agent → {tools, permissions} → utils`,严格单向,无 `lanscoder.runtime`。

## 8. 风险与对策

1. **门控完备性同步风险**(工具名在两处字符串引用)。对策:§6-2 双向断言(防漂移,不能防新增——注明局限)+ 注释式中心列表。**读工具为最高危缺口**(漏即敏感文件直读)。
2. **并行批决策复用机制缺失(F-6)**:已收敛为 §4.3 + `agent/tool_execution.py` 按 `tool_call.id` 缓存 prepare;实现时确保批准备先于并行判定。§6-3 每成员恰好一次锁机制。
3. **执行入口兜底消失**:今日直调 `session.execute_tool_call` 会被带闸 registry 兜底;重构后兜底消失(直接后果是 F-1 的两个测试文件 + `test_app_factory.py:570`)。对策:E 清单显式迁移这仨文件,并在 CI 声明"工具执行必须经 coordinator prepare"。
4. **转写漂移**(reason/allow_*/request_id 指纹/target_builder)。对策:§4.2 转写注意 + §6-1 等价断言逐字段锁。
5. **web_search target 字面量复制**:注释交叉引用 `tools/web_search.py` + §6-1 断言锁一致,勿顺手修行为。
6. **`agent/session.py:450-455` 与 `tools/session_registry.py` 分支语义**:实现时先读再简化(语义=确认/后台后不带闸执行),防止误删后台直通。
7. **B 漏改 import → ImportError**:纯搬移的典型失败;`permission_results` 归位非纯搬移,改模块归属——引用面(registry E 删、agent/permission.py、agent/permission_resume.py、tests/test_permission_results.py)必须**按三阶段顺序**收口。对策:每阶段全量 pytest(ImportError 收集期即暴露)+ §4.4/§6-6 grep 闸。
8. **`_options_from_data` 私有名跨模块引用**:改名 `options_from_data`,`agent/session.py` **import(:28)+ 调用点(:599)双点**同步;行为由 `test_permission_results` 锁死。
9. **E∩B 阶段顺序错乱(F-3/F-4)**:registry 删除必须先于 permission_results 迁址、utils/patch.py 迁址必须早于 classification 落地;否则向上边/边双击穿。对策:三阶段顺序是规格强约束(§4.1),实现计划不得合批跨阶段文件。
10. **`parse_patch` 迁址漂移(F-4)**:target 字符串喂 **grant/policy 匹配与恢复存储**(request_id 指纹只取原始 arguments,不涉 target),复制实现会漂。对策:解析族整体搬 `utils/patch.py` 共用(非复制),`tools/apply_patch.py`、`tools/review.py`、classification 三处同一实现;§6-1 target 字面量断言兜底。
11. **参数缺失短路语义(F-2)**:若误把无效请求喂进 preflight,`shell` 缺参会从 DENY 漂成 ASK。对策:§4.3 [invalid] 短路分支 + §6-1 逐条断言锁死。

## 9. 与后续子项目的关系

- 本轮结束后剩余的更大重构:core/shell 拆包(A)、`create_agent_loop → agent/factory.py`(C)、runner 命名 + `app/runtime.py` 改名(D)、`docs/architecture.md` 修订;`context/runtime_replay.py`/`runtime_state.py` 命名属 D 及 compact 命名线。
- **B 先于 A 的收益**:拆包时可安装/嵌 core 所需的 `utils.cancellation`/`utils.patch`/`permissions.user_input`/`agent.permission_results` 均在最终归属地,`lanscoder.runtime` 不在打包范围;补丁解析位于 utils,核心工具箱与权限分类共用一份。
- `permissions/classification.py`(E)、`permissions/user_input.py`(B)与 `agent/permission_results.py`(B)互不依赖,各自归属 permissions/ 与 agent/ 新层;拆包(C/D)不需要回改本轮产物。
- 子代理停止面板(2026-08-20 已实现)消费的 cancellation 符号本轮迁址但语义不变,面板代码零改动。

## 10. 接缝文件清单(v5 合并版)

**删**
- `tools/permission_registry.py`(Phase 2)
- `tools/permission_results.py`(Phase 3,整文件迁至 `agent/permission_results.py`,并入组装逻辑)
- `lanscoder/runtime/__init__.py`、`lanscoder/runtime/cancellation.py`、`lanscoder/runtime/user_input.py`(Phase 1,`runtime/` 目录整体消失)

**新**
- `permissions/classification.py`(Phase 2,E)
- `permissions/user_input.py`(Phase 1,B:DTO 半段逐字迁入,converter 迁走后无 tools 引用)
- `utils/cancellation.py`(Phase 1,B)
- `utils/patch.py`(Phase 1,F-4:`parse_patch`/`PatchPlan`/`PatchOperation`/`PatchHunk`/`BEGIN_MARKER`/`END_MARKER` 逐字迁入)
- `agent/permission_results.py`(Phase 1 建文件承接转换半段;Phase 3 并入四个 `make_*` + `_permission_request_data`)

**改(E,Phase 2)**
- `tools/types.py`、`tools/session_registry.py`、12 个直接门控工具(write/delete/shell/edit/apply_patch/fetch/git_diff/git_status/git_log/diagnostics/python_exec/web_search)、6 个读工具(grep/glob/ls/tree/view/read_multi)与 `tools/path_permissions.py`(整体拆除)、`mcp/adapter.py`、`agent/permission.py`、`agent/session.py`、`agent/tool_execution.py`——其中 apply_patch.py/python_exec.py 连带删除 target_builder 函数,apply_patch.py 兼 Phase 1 的 parse 面 import 迁移

**改(B,Phase 1 import 路径迁移)**
- `permissions/manager.py`(`lanscoder.permissions.user_input`)
- `agent/permission.py`、`agent/loop.py`、`agent/session.py`、`agent/tool_execution.py`、`agent/background.py`、`agent/observer.py`、`agent/subagent_engine.py`、`agent/user_input.py`
- `utils/subprocess.py`、`utils/execution_sandbox.py`(同包 `utils.cancellation`)
- `app/runtime.py`(仅 :18/:32 两行)

**改(B,Phase 3 import 迁移)**
- `agent/permission.py`、`agent/permission_resume.py`(四个 `make_*` 引用改从 `lanscoder.agent.permission_results` 取)

**改(F-4 连带,Phase 1)**
- `tools/apply_patch.py`(parse 面改从 `utils.patch` 导入,保留 create/_apply_plan)、`tools/review.py`(parse 面改从 `utils.patch` 导入)

**E∩B 双重改动**:`agent/permission.py`、`agent/session.py`、`agent/tool_execution.py`、`tools/apply_patch.py`(见 §4.1 表;`app/runtime.py` 仅 B)

**测试**
- E 改写: `tests/test_permission_registry.py`;迁移 `tests/test_execution_tools.py`、`tests/test_read_tools.py`、`tests/test_mcp_adapter.py`、`tests/test_mcp_integration.py`、**`tests/test_agent_tool_flow.py`、`tests/test_session_resume_service.py`(F-1);审视 `tests/test_app_factory.py:570`**;新增 §6-1~5。
- B 迁移: `tests/test_utils_subprocess.py`、`tests/test_agent_context_loop.py`、`tests/test_model_request_options.py`、`tests/test_delegate_tool.py`、`tests/test_app_tui.py`、`tests/test_permission_results.py`(双源 import:permissions.user_input + agent.permission_results,整体作为迁移行为锁);`tests/test_tools.py`/`test_agent_tool_flow.py` 无需改动(parse 行为锁在 test_mutation_tools/test_prewrite_review/test_review_view);新增 §6-6。

**本轮不动**:`app/runtime.py` 本体(C/D 才移)、`context/runtime_state.py`、`context/runtime_replay.py`、`docs/superpowers/` 历史文档、pyproject(无 runtime 引用,无依赖变化)。