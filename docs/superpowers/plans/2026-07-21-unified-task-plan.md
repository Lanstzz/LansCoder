# FirstCoder 统一任务计划系统实施计划

> **供智能体执行者使用：** 实施本计划时，必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 技能逐项执行。所有步骤使用复选框（`- [ ]`）跟踪。

**目标：** 用一个支持 `linear` 与 `dag` 两种模式的 `TaskPlan`，替换当前彼此独立、每次整体重写的 Todo 和 TaskGraph，使模型默认只按稳定任务 ID 更新必要字段，由 Python 负责合并、校验、派生状态、持久化与展示。

**架构：** 领域层以不可歧义的 `Task`/`TaskPlan` 快照为唯一权威；纯 reducer 接收带 `expected_revision` 的增量命令并原子地产生新快照；会话层只写一种 `task_plan_updated` 事件，同时保存变更与结果快照；四个模型工具只负责创建、状态/依赖更新、改写文案和读取；TUI、后台任务与策略层全部读取同一投影。新 schema 直接拒绝旧会话，不提供迁移、兼容字段或 replay fallback。

**技术栈：** Python 3.11+、pytest、Textual、dataclasses、JSONL 事件溯源

---

## 一、不可变设计约束

实施过程中若代码实现与以下约束冲突，应修改实现而不是放宽约束：

1. `TaskPlan` 是唯一计划状态，不再并存 `todos`、`task_graph` 或二者间的同步逻辑。
2. `linear` 表示显式顺序执行，最多一个任务处于 `in_progress`；`dag` 表示显式依赖关系，互不依赖的任务可以同时处于 `in_progress`。
3. 模型通过稳定 `task_id` 定位任务。任务创建后，普通进度更新不得改变 `content`。
4. `task_update` 不接受 `content`；只有 `task_revise` 能修改任务文案，避免“更新进度”退化为重写整个计划。
5. `task_update` 支持一次原子更新多个任务，例如同一次调用完成 A 并启动 B。任一 patch 非法时整批不落库。
6. 所有写操作必须携带 `expected_revision`。revision 不匹配时返回冲突及最新 revision，模型先 `task_list` 再重试。
7. 空 patch 或应用后无变化的 patch 是 no-op：不增加 revision、不写事件、不刷新 TUI。
8. 依赖只通过 `add_depends_on`、`remove_depends_on` 增量修改；模型不能提交 `ready_nodes`、拓扑层级或整张依赖图。
9. Python 负责 ID 唯一性、依赖存在性、自依赖、环、状态转换、执行就绪和拓扑投影校验。
10. 会话日志只保留 `task_plan_updated` 一种计划事件。事件同时记录规范化 changes 和变更后的完整 snapshot，replay 直接采用最新合法 snapshot。
11. 不兼容旧会话：缺少当前 `context_event_schema_version` 或版本不同的会话，在 resume/fork 前明确失败；不读取旧 `todo_updated`、`task_graph_updated`，也不从旧工具结果恢复计划。
12. 最终删除旧 Todo/TaskGraph 工具、策略、事件、字段、测试和说明，不留下 alias、deprecated wrapper 或迁移模块。

## 二、目标数据与协议

### 2.1 领域模型

```python
TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
TaskPlanMode = Literal["linear", "dag"]

@dataclass(frozen=True, slots=True)
class Task:
    id: str
    content: str
    status: TaskStatus = "pending"
    depends_on: tuple[str, ...] = ()
    owner: str | None = None
    order: int = 0

@dataclass(frozen=True, slots=True)
class TaskPlan:
    mode: TaskPlanMode
    revision: int
    tasks: tuple[Task, ...]
```

`order` 是稳定的显示顺序。`linear` 的执行前置关系由 `order` 推导，不要求模型重复填写链式依赖；`dag` 的执行关系来自 `depends_on`。序列化时 tuple 输出为 JSON array，反序列化后重新规范化为 tuple。

### 2.2 工具协议

初次创建或追加任务：

```json
{
  "mode": "linear",
  "expected_revision": 0,
  "tasks": [
    {"id": "inspect", "content": "检查现有实现"},
    {"id": "implement", "content": "实现增量更新"}
  ]
}
```

原子推进多个任务：

```json
{
  "expected_revision": 1,
  "updates": [
    {"id": "inspect", "status": "completed"},
    {"id": "implement", "status": "in_progress", "owner": "main"}
  ]
}
```

增量修改 DAG 依赖：

```json
{
  "expected_revision": 3,
  "updates": [
    {"id": "test", "add_depends_on": ["implement"]},
    {"id": "docs", "remove_depends_on": ["inspect"]}
  ]
}
```

唯一允许的文案修改：

```json
{
  "expected_revision": 4,
  "revisions": [
    {"id": "docs", "content": "更新中英文架构文档"}
  ]
}
```

四个模型可见工具：

| 工具 | 职责 | 禁止行为 |
|---|---|---|
| `task_create` | 初始化计划或向现有计划追加任务 | 覆盖已有任务、隐式改文案 |
| `task_update` | 按 ID 原子更新 status、owner、依赖 | 接收 content、整体替换 tasks |
| `task_revise` | 按 ID 原子修改 content | 修改状态、owner、依赖 |
| `task_list` | 返回权威 snapshot 及派生 projection | 写入状态 |

### 2.3 事件协议

```json
{
  "type": "task_plan_updated",
  "payload": {
    "previous_revision": 2,
    "revision": 3,
    "operation": "update",
    "changes": [
      {"id": "inspect", "status": "completed"},
      {"id": "implement", "status": "in_progress"}
    ],
    "snapshot": {
      "mode": "linear",
      "revision": 3,
      "tasks": []
    }
  }
}
```

首次创建也是 `task_plan_updated`，其 `previous_revision` 为 `0`、结果 `revision` 为 `1`、`operation` 为 `create`。追加任务仍为 `create` 操作。no-op 不产生此事件。

### 2.4 目标文件布局

```text
firstcoder/planning/
├── models.py
├── validation.py
├── reducer.py
├── projection.py
└── service.py

firstcoder/tools/
├── task_create.py
├── task_update.py
├── task_revise.py
└── task_list.py

firstcoder/agent/
└── task_plan_policy.py
```

`reducer.py` 保持纯函数，`service.py` 负责读取 SessionView、调用 reducer 和写事件。不要把二者合并：这样既避免工具各自复制 merge 逻辑，也能在不构造 session 的情况下完整测试原子性。

---

## 三、实施任务

### 任务 1：建立统一领域模型与稳定序列化

**文件：**

- 新建：`firstcoder/planning/models.py`
- 新建：`tests/test_task_plan_models.py`

- [ ] 先在 `tests/test_task_plan_models.py` 写失败测试，覆盖 `Task`/`TaskPlan` 默认值、JSON 往返、输入列表规范化为 tuple、重复 ID 拒绝、空白 ID/content 拒绝、未知 mode/status 拒绝。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_models.py -q`，确认因模块不存在或类型未实现而失败。
- [ ] 在 `firstcoder/planning/models.py` 实现冻结 dataclass、`to_dict()`、`from_dict()` 和 `TaskPlanError`；不加入旧 `done` 状态别名。
- [ ] 使用下列构造边界，确保所有后续模块只处理规范化对象：

```python
@classmethod
def from_dict(cls, payload: Mapping[str, object]) -> "TaskPlan":
    mode = require_mode(payload.get("mode"))
    revision = require_non_negative_int(payload.get("revision"))
    tasks = tuple(Task.from_dict(item) for item in require_object_list(payload.get("tasks")))
    require_unique_task_ids(tasks)
    return cls(mode=mode, revision=revision, tasks=tasks)
```

- [ ] 再运行 `.venv/bin/python -m pytest tests/test_task_plan_models.py -q`，确认全部通过。
- [ ] 提交：`git add firstcoder/planning/models.py tests/test_task_plan_models.py && git commit -m "Add unified task plan models"`

### 任务 2：实现模式无关校验和派生投影

**文件：**

- 新建：`firstcoder/planning/validation.py`
- 新建：`firstcoder/planning/projection.py`
- 新建：`tests/test_task_plan_validation.py`
- 新建：`tests/test_task_plan_projection.py`
- 参考并最终取代：`firstcoder/planning/dag.py`

- [ ] 在两个新测试文件中写失败测试：悬空依赖、自依赖、重复依赖、DAG 环、linear 多个 `in_progress`、未满足依赖却启动、合法并行 DAG、ready/blocked 节点、稳定拓扑层级和 linear 顺序。
- [ ] 明确状态规则：`completed`/`cancelled` 视为终态；任务只有在所有依赖为 `completed` 时才 ready，依赖为 `cancelled` 时保持 blocked，不能自动跳过。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_validation.py tests/test_task_plan_projection.py -q`，确认缺少实现而失败。
- [ ] 从 `firstcoder/planning/dag.py` 提取并泛化环检测/拓扑算法，不复制 `TaskGraph` 数据模型；实现以下纯函数：

```python
def validate_plan(plan: TaskPlan) -> None: ...
def ready_task_ids(plan: TaskPlan) -> tuple[str, ...]: ...
def blocked_task_ids(plan: TaskPlan) -> tuple[str, ...]: ...
def topological_levels(plan: TaskPlan) -> tuple[tuple[str, ...], ...]: ...
def project_plan(plan: TaskPlan) -> dict[str, object]: ...
```

- [ ] 对 `linear` 使用 `order` 排序并把前一个未完成任务视为后续任务的隐式阻塞；对 `dag` 只读取显式 `depends_on`。两个模式都禁止非 ready 任务进入 `in_progress`。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_validation.py tests/test_task_plan_projection.py -q`，确认全部通过。
- [ ] 提交：`git add firstcoder/planning/validation.py firstcoder/planning/projection.py tests/test_task_plan_validation.py tests/test_task_plan_projection.py && git commit -m "Add task plan validation and projection"`

### 任务 3：实现带 revision 的原子增量 reducer

**文件：**

- 新建：`firstcoder/planning/reducer.py`
- 新建：`tests/test_task_plan_reducer.py`

- [ ] 写失败测试，覆盖首次创建、追加任务、批量状态推进、owner 设置/清除、依赖增删、专用文案 revision、revision 冲突、未知 ID、整批回滚、no-op 和每次有效写只增加一次 revision。
- [ ] 加一条协议守卫测试：进度更新 dataclass/schema 没有 `content` 字段，传入 `content` 必须明确失败。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_reducer.py -q`，确认失败原因来自 reducer 尚未实现。
- [ ] 实现命令类型与结果类型：

```python
@dataclass(frozen=True, slots=True)
class TaskPatch:
    id: str
    status: TaskStatus | None = None
    owner: str | None | Unset = UNSET
    add_depends_on: tuple[str, ...] = ()
    remove_depends_on: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class ReductionResult:
    plan: TaskPlan
    changes: tuple[dict[str, object], ...]
    changed: bool
```

- [ ] 实现 `create_tasks()`、`update_tasks()`、`revise_tasks()`：先在临时集合应用完整批次，再构造候选 plan 并运行 `validate_plan()`；只有全部成功才返回候选 plan。
- [ ] revision 冲突抛出专用 `TaskPlanRevisionConflict(expected, actual)`；其他非法命令抛出 `TaskPlanCommandError`。no-op 返回原对象、`changed=False`。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_reducer.py -q`，确认全部通过。
- [ ] 提交：`git add firstcoder/planning/reducer.py tests/test_task_plan_reducer.py && git commit -m "Add atomic task plan reducer"`

### 任务 4：接入唯一计划事件和 SessionView replay

**文件：**

- 修改：`firstcoder/context/models.py`
- 修改：`firstcoder/context/writer.py`
- 修改：`firstcoder/context/store.py`
- 修改：`tests/test_context_writer.py`
- 修改：`tests/test_context_store.py`

- [ ] 先改测试，要求 `SessionView.task_plan: TaskPlan | None`，并验证连续两个 `task_plan_updated` 事件以后 replay 得到第二个 snapshot。
- [ ] 测试事件包含 `previous_revision`、`revision`、`operation`、`changes`、`snapshot`；无效 snapshot 使 replay 明确报损坏错误，不能静默忽略。
- [ ] 删除/改写测试中对 `todos`、`todo_initialized`、`todo_task_hash`、`task_graph`、`task_graph_ready_nodes` 和 legacy Todo tool result fallback 的期待。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_context_writer.py tests/test_context_store.py -q`，确认新断言失败。
- [ ] 将 `SessionView` 的五个旧字段替换为单一字段：

```python
task_plan: TaskPlan | None = None
```

- [ ] 在 writer 实现唯一入口 `append_task_plan_updated(...)`；store 只处理 `task_plan_updated` 并用 `TaskPlan.from_dict(snapshot)` 重建权威状态。
- [ ] 删除 `append_todo_updated()`、`append_task_graph_updated()`、`_apply_todo_payload()`、`_apply_task_graph_payload()`、`_apply_legacy_todo_result()` 及其调用。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_context_writer.py tests/test_context_store.py -q`，确认全部通过。
- [ ] 提交：`git add firstcoder/context/models.py firstcoder/context/writer.py firstcoder/context/store.py tests/test_context_writer.py tests/test_context_store.py && git commit -m "Persist unified task plan events"`

### 任务 5：严格切换会话 schema，不兼容旧会话

**文件：**

- 修改：`firstcoder/context/versions.py`
- 修改：`firstcoder/context/writer.py`
- 修改：`firstcoder/session/errors.py`
- 修改：`firstcoder/session/resume.py`
- 修改：`firstcoder/session/fork.py`
- 修改：`tests/test_context_writer.py`
- 修改：`tests/test_session_resume_service.py`
- 新建：`tests/test_session_fork.py`

- [ ] 写失败测试：新 session 的 `session_created` 含当前 `context_event_schema_version`；缺字段、`v1` 或未知版本的已有 session 在 resume 和 fork 时均抛 `SessionUnsupportedSchemaError`，消息包含 session ID、实际版本和期望版本。
- [ ] 写测试确认拒绝发生在复制 fork 事件、恢复 pending permission 或调用 provider 之前，因此失败不产生半成品 fork 会话。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_context_writer.py tests/test_session_resume_service.py tests/test_session_fork.py -q`，确认失败。
- [ ] 将 `CONTEXT_EVENT_SCHEMA_VERSION` 升为 `"v2"`，并由 `append_session_created()` 自动写入，调用者不能覆盖该保留字段。
- [ ] 新建 `SessionUnsupportedSchemaError(SessionError)`；实现一个共享校验函数读取第一条 `session_created`，由 resume 和 fork 在任何副作用之前调用。
- [ ] 不添加 migration、版本映射或 missing-as-v1 分支；缺失版本直接以 `actual="missing"` 拒绝。
- [ ] 运行上述 focused tests，确认全部通过。
- [ ] 提交：`git add firstcoder/context/versions.py firstcoder/context/writer.py firstcoder/session/errors.py firstcoder/session/resume.py firstcoder/session/fork.py tests/test_context_writer.py tests/test_session_resume_service.py tests/test_session_fork.py && git commit -m "Reject legacy session schemas"`

### 任务 6：建立事件写入服务，统一工具与后台调用边界

**文件：**

- 新建：`firstcoder/planning/service.py`
- 新建：`tests/test_task_plan_service.py`

- [ ] 写失败测试：服务从 `SessionView.task_plan` 读取当前状态，调用 reducer，有效变更恰好写一个事件，no-op 不写，revision 冲突不写，非法批次不写。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_service.py -q`，确认失败。
- [ ] 实现窄服务接口，避免四个工具重复读写逻辑：

```python
class TaskPlanService:
    def create(self, *, mode: str, expected_revision: int, tasks: object) -> TaskPlanMutation: ...
    def update(self, *, expected_revision: int, updates: object) -> TaskPlanMutation: ...
    def revise(self, *, expected_revision: int, revisions: object) -> TaskPlanMutation: ...
    def current(self) -> TaskPlan | None: ...
```

- [ ] `TaskPlanMutation` 返回 `plan`、`projection`、`changed`，供工具与后台通知共享；只有 service 能调用 `append_task_plan_updated()`。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_service.py -q`，确认全部通过。
- [ ] 提交：`git add firstcoder/planning/service.py tests/test_task_plan_service.py && git commit -m "Add task plan mutation service"`

### 任务 7：实现四个最小模型工具

**文件：**

- 新建：`firstcoder/tools/task_create.py`
- 新建：`firstcoder/tools/task_update.py`
- 新建：`firstcoder/tools/task_revise.py`
- 新建：`firstcoder/tools/task_list.py`
- 新建：`tests/test_task_plan_tools.py`

- [ ] 先写 schema 测试：四个工具名称和 required 字段准确；三个写工具都有 `expected_revision`；`task_update` properties 中不存在 `content`；工具不接收完整 `task_plan` 或 `ready_nodes`。
- [ ] 写行为测试：成功结果返回 revision、规范化 changes、snapshot/projection；冲突与校验错误返回可恢复的错误结果；`task_list` 无计划时明确返回 `revision=0` 和 `plan=null`。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_tools.py -q`，确认模块缺失而失败。
- [ ] 每个工具只解析参数并调用注入的 `TaskPlanService`，不在工具内实现 merge、环检测或事件写入。
- [ ] 错误信息给模型可执行的下一步，例如 revision 冲突返回 `actual_revision` 并提示先调用 `task_list`；不要在错误后自动重试写入。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_tools.py -q`，确认全部通过。
- [ ] 提交：`git add firstcoder/tools/task_create.py firstcoder/tools/task_update.py firstcoder/tools/task_revise.py firstcoder/tools/task_list.py tests/test_task_plan_tools.py && git commit -m "Add incremental task plan tools"`

### 任务 8：注册新工具并删除旧工具入口

**文件：**

- 修改：`firstcoder/tools/builtin.py`
- 修改：`firstcoder/tools/session_registry.py`
- 修改：`firstcoder/tools/__init__.py`
- 修改：`firstcoder/tools/descriptions.py`
- 修改：`tests/test_tools.py`
- 删除：`firstcoder/tools/todo.py`
- 删除：`firstcoder/tools/task_graph.py`
- 删除：`tests/test_todo.py`
- 删除：`tests/test_task_graph_tool.py`

- [ ] 先改 `tests/test_tools.py`：默认/会话工具集合包含四个新工具，且不包含 `todo`、`task_graph`；description 强调“按 ID 局部更新”和 `task_revise` 的独占职责。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_tools.py tests/test_task_plan_tools.py -q`，确认注册断言失败。
- [ ] 让 session registry 为当前会话构造一个 `TaskPlanService`，再注入四个工具；保证 reserved tool name 检查覆盖四个名称，避免用户 supplied tool 覆盖权威实现。
- [ ] 从 builtin、exports、description 映射删除旧工具，删除两个旧实现及专属测试；不要保留 import alias。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_tools.py tests/test_task_plan_tools.py -q`，确认全部通过。
- [ ] 运行 `rg -n 'create_todo_tool|create_task_graph_tool|tools\.todo|tools\.task_graph' firstcoder tests`，本任务结束时应无命中。
- [ ] 提交：`git add -A firstcoder/tools tests/test_tools.py tests/test_task_plan_tools.py tests/test_todo.py tests/test_task_graph_tool.py && git commit -m "Replace legacy planning tools"`

### 任务 9：用单一 TaskPlan 策略替换双策略

**文件：**

- 新建：`firstcoder/agent/task_plan_policy.py`
- 新建：`tests/test_task_plan_policy.py`
- 删除：`firstcoder/agent/todo_policy.py`
- 删除：`firstcoder/agent/task_graph_policy.py`
- 删除：`tests/test_task_graph_policy.py`

- [ ] 写失败测试：复杂编码任务未创建计划时提示一次；已有未完成任务但最终回复前状态未对齐时提示一次；计划已全部终态或本轮没有需要计划的工作时不提示；提示要求用 `task_update` 局部更新而非重建计划。
- [ ] 为 `linear` 与 `dag` 各写一组 reconciliation 测试，但共用同一个 policy 类与同一种返回协议。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_policy.py -q`，确认失败。
- [ ] 实现只读取 `SessionView.task_plan` 和 projection 的 `TaskPlanPolicy`；不要在 policy 内修改计划或猜测 ready 节点。
- [ ] 删除两个旧策略及其专属测试，保留仍有价值的行为断言到新测试中。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_policy.py -q`，确认全部通过。
- [ ] 提交：`git add -A firstcoder/agent/task_plan_policy.py firstcoder/agent/todo_policy.py firstcoder/agent/task_graph_policy.py tests/test_task_plan_policy.py tests/test_task_graph_policy.py && git commit -m "Unify task plan reconciliation policy"`

### 任务 10：简化 AgentSession 与 AgentLoop 的计划路径

**文件：**

- 修改：`firstcoder/agent/session.py`
- 修改：`firstcoder/agent/loop.py`
- 修改：`tests/test_agent_context_loop.py`
- 修改：`tests/test_agent_tool_flow.py`

- [ ] 把旧 Todo/TaskGraph 测试改为新协议，覆盖工具调用后立即写唯一事件、同轮连续增量更新、no-op 不触发事件、最终回复前最多一次 reconciliation。
- [ ] 写回归测试确保 loop 只有 `_task_plan_reconciliation_attempted`，新用户轮次会重置它，但单轮多个 tool round 不重复提示。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_agent_context_loop.py tests/test_agent_tool_flow.py -q`，确认新测试失败。
- [ ] 删除 `AgentSession._append_todo_updated_if_present()` 与 `_append_task_graph_updated_if_present()`；计划工具已通过 service 写入事件，session 不再从 ToolResult 猜状态。
- [ ] 将 `TodoPolicy`/`TaskGraphPolicy`、两个 reconciliation flags 和两个 instruction 分支合并成 `TaskPlanPolicy`、一个 flag、一个 instruction path。
- [ ] 删除任何根据工具名复制完整结果到 context 的逻辑，避免 service 和 session 双写事件。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_agent_context_loop.py tests/test_agent_tool_flow.py -q`，确认全部通过。
- [ ] 提交：`git add firstcoder/agent/session.py firstcoder/agent/loop.py tests/test_agent_context_loop.py tests/test_agent_tool_flow.py && git commit -m "Simplify agent task plan flow"`

### 任务 11：把后台任务和子智能体绑定到统一 task ID

**文件：**

- 修改：`firstcoder/agent/background.py`
- 修改：`firstcoder/agent/subagent.py`
- 修改：`firstcoder/tools/background.py`
- 修改：`firstcoder/tools/delegate.py`
- 修改：`firstcoder/context/writer.py`
- 修改：`tests/test_background_jobs.py`
- 修改：`tests/test_delegate_tool.py`

- [ ] 写失败测试，将 `background_graph_id`/`background_node_id` 输入和 metadata 替换为单一 `task_id`；无 task ID 的普通后台命令仍可运行，有 task ID 时必须在当前 TaskPlan 中存在。
- [ ] 写完成通知测试：后台/子智能体成功只通过 `TaskPlanService.update()` 把对应任务置为 `completed`；失败不得谎报完成，可保留 `in_progress` 并把失败事实写入通知；revision 冲突不覆盖主智能体较新的状态。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_background_jobs.py tests/test_delegate_tool.py -q`，确认旧字段导致失败。
- [ ] 将控制参数统一为 `task_id`，删除 graph-specific 常量和 metadata；后台管理器保存启动时关联的 task ID 与观察到的 revision。
- [ ] 对完成竞态使用显式冲突处理：重新读取当前计划；若任务已经是终态则 no-op，若仍是同一任务且允许完成则用最新 revision 更新，否则只发通知交给主智能体判断。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_background_jobs.py tests/test_delegate_tool.py -q`，确认全部通过。
- [ ] 运行 `rg -n 'background_graph_id|background_node_id' firstcoder tests`，应无命中。
- [ ] 提交：`git add firstcoder/agent/background.py firstcoder/agent/subagent.py firstcoder/tools/background.py firstcoder/tools/delegate.py firstcoder/context/writer.py tests/test_background_jobs.py tests/test_delegate_tool.py && git commit -m "Link background work to task plans"`

### 任务 12：把 TUI 改为同一 TaskPlan 的两种投影

**文件：**

- 修改：`firstcoder/app/tui_view.py`
- 修改：`firstcoder/app/tui_state.py`
- 修改：`firstcoder/app/tui_widgets.py`
- 修改：`firstcoder/app/activity_view.py`
- 修改：`tests/test_app_tui.py`

- [ ] 写失败测试：无计划不显示面板；linear 按 order 显示单列进度；dag 按拓扑层级/依赖显示并标记 ready、blocked、in-progress；同一 revision 的 no-op 不刷新；revision 增加只更新同一个组件。
- [ ] 删除测试 fixture 中的旧 `todos`、`todo_initialized`、`task_graph` 字段，只构造 `TaskPlan`。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_app_tui.py -q`，确认旧分支无法满足新断言。
- [ ] TUI 只调用 `project_plan(view.task_plan)`；允许按 `projection["mode"]` 选择 linear/DAG 排版，但禁止分别维护状态、读取两套 SessionView 字段或互相 fallback。
- [ ] 将最近渲染 revision 存在单一 TUI state 中，revision 未变直接跳过计划组件刷新。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_app_tui.py -q`，确认全部通过。
- [ ] 提交：`git add firstcoder/app/tui_view.py firstcoder/app/tui_state.py firstcoder/app/tui_widgets.py firstcoder/app/activity_view.py tests/test_app_tui.py && git commit -m "Render unified task plans in TUI"`

### 任务 13：重写模型指令与用户文档，删除旧概念

**文件：**

- 修改：`firstcoder/context/prompts/agent_instructions.md`
- 修改：`docs/ARCHITECTURE.md`
- 修改：`docs/TOOLS_DESIGN.md`
- 修改：`docs/CLI_TUI_DESIGN.md`
- 修改：`docs/CLI_TUI_DESIGN.zh-CN.md`
- 修改：`docs/README.md`
- 修改：`docs/README.zh-CN.md`
- 修改或删除：`docs/ASYNC_SUBAGENTS_DAG_DESIGN.md`
- 修改或删除：`docs/ASYNC_SUBAGENTS_DAG_DESIGN.zh-CN.md`
- 修改：`tests/test_agent_prompt_inputs.py`

- [ ] 先写 prompt 测试，要求模型说明遵循：先 `task_list` 获取 revision；创建才用 `task_create`；推进只用 `task_update`；只有语义确实变化才用 `task_revise`；禁止为了更新一个状态重写全部任务。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_agent_prompt_inputs.py -q`，确认旧提示不符合。
- [ ] 更新中英文文档为统一 TaskPlan 架构，清楚解释 linear 与 dag 的差别、稳定 ID、revision 冲突和无旧会话兼容的版本边界。
- [ ] 旧 DAG 设计文档若保留历史价值，改成明确标注“已被统一 TaskPlan 取代”的短归档并链接本计划；若只是未落地重复设计则直接删除。不要保留仍像当前行为的说明。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_agent_prompt_inputs.py -q`，确认全部通过。
- [ ] 运行 `rg -n '\b(todo_updated|task_graph_updated|create_todo_tool|create_task_graph_tool)\b' firstcoder docs tests`，除本实施计划对删除对象的说明外应无命中。
- [ ] 提交：`git add -A firstcoder/context/prompts/agent_instructions.md docs tests/test_agent_prompt_inputs.py && git commit -m "Document incremental task planning"`

### 任务 14：删除被取代的 DAG 实现并完成跨模块回归

**文件：**

- 删除：`firstcoder/planning/dag.py`
- 删除或并入：`firstcoder/planning/scheduler.py`
- 删除：`tests/test_task_graph.py`
- 修改：所有仍引用旧类型/字段的生产与测试文件

- [ ] 先运行 `rg -n 'TaskGraph|TaskNode|task_graph|todo_initialized|todo_task_hash|task_graph_ready_nodes|view\.todos' firstcoder tests`，逐条分类为应迁移的新引用或应删除的旧测试。
- [ ] 对 scheduler 仍需要的“可调度任务/完成回写”行为先在 `tests/test_task_plan_projection.py`、`tests/test_background_jobs.py` 添加失败回归测试，再迁入 `projection.py`/`service.py`；若无独立职责则删除整个 scheduler，不留薄包装。
- [ ] 运行新增的 focused tests，确认迁移前失败。
- [ ] 删除 `dag.py`、旧 task graph 测试和无职责 scheduler；修正所有 imports、fixture 和类型标注。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_task_plan_models.py tests/test_task_plan_validation.py tests/test_task_plan_projection.py tests/test_task_plan_reducer.py tests/test_task_plan_service.py tests/test_task_plan_tools.py tests/test_task_plan_policy.py tests/test_background_jobs.py -q`，确认统一路径全部通过。
- [ ] 再运行前述 `rg`，生产代码与普通测试中应无旧状态类型/字段命中；本计划文档可以保留用于说明删除范围的文字。
- [ ] 提交：`git add -A firstcoder/planning tests firstcoder && git commit -m "Remove legacy Todo and DAG state"`

### 任务 15：全量验证、代码减量与最终审查

**文件：**

- 修改：仅限前面验证发现的真实缺陷
- 审查：本计划涉及的全部文件

- [ ] 运行 `git diff --check`，修复空白错误。
- [ ] 运行 `.venv/bin/python -m pytest tests -q`；若失败，先判断是否由本改动引入，再用最窄测试重现和修复，最后重新运行整套 `tests`。
- [ ] 运行以下残留扫描，所有命中都必须逐条解释或清除：

```sh
rg -n 'todo_updated|task_graph_updated|create_todo_tool|create_task_graph_tool' firstcoder tests docs
rg -n 'background_graph_id|background_node_id|todo_initialized|todo_task_hash|task_graph_ready_nodes' firstcoder tests
rg -n 'LEGACY|legacy|migration|migrate|兼容旧|旧会话回退' firstcoder
```

- [ ] 检查工具 schema：`task_update` 不含 content，三个写工具含 expected_revision，模型不能写 ready/blocked/topological 字段。
- [ ] 检查事件不变量：一次有效 mutation 恰好一个 `task_plan_updated`；no-op 为零事件；日志 replay 后 snapshot/revision 与写入后完全一致。
- [ ] 检查会话边界：新会话可 resume/fork；旧/缺版本会话在副作用前失败；没有 migration 或 tool-result fallback。
- [ ] 记录改造前后行数，验证“统一后整体代码变少”而不是仅移动代码：

```sh
git diff --stat
git diff --numstat
find firstcoder -name '*.py' -print0 | xargs -0 wc -l | tail -1
find tests -name 'test_*.py' -print0 | xargs -0 wc -l | tail -1
```

- [ ] 预期范围是生产代码和测试合计净减少约 300–600 行。若反而明显增加，重点审查重复 schema 解析、工具层 merge、双重投影、兼容 wrapper 和过度抽象；不要为了达成数字删除必要测试。
- [ ] 使用 `git diff -- firstcoder tests docs` 做最终人工审查，确认没有修改无关功能，没有占位文本，没有注释掉的旧实现，命名统一使用 TaskPlan/Task ID。
- [ ] 最终提交：`git add -A && git commit -m "Complete unified task plan migration"`

---

## 四、执行顺序与提交策略

必须按任务 1 → 15 的顺序推进。1–3 建立纯领域核心；4–6 建立持久化边界；7–10 切换模型与 agent 入口；11–12 接入异步执行和 UI；13–15 删除旧体系并做全量验证。不要先删旧文件再补新核心，否则中间状态难以用 focused tests 证明。

每个任务的提交命令是建议的最小提交边界。若当前工作树已有用户改动，执行者必须先确认哪些 diff 属于本计划，并只暂存本任务实际修改的文件或精确 hunk；不得使用 `git add -A` 把无关用户改动混入提交。任务 14、15 中出现的 `git add -A` 仅适用于已在独立干净分支/worktree 执行本计划的情况。

## 五、完成定义

只有同时满足以下条件才算完成：

- [ ] 模型能用稳定 ID 局部推进任务，更新一个状态不需要重发整个列表或整张图。
- [ ] `linear` 与 `dag` 共用同一数据、reducer、事件、工具、策略和 TUI 状态来源。
- [ ] Python 拒绝 revision 冲突、非法状态、悬空/自依赖、环和不满足前置条件的启动。
- [ ] no-op 不增加 revision、不写事件、不触发计划面板刷新。
- [ ] 新会话事件可完整 replay；旧或无 schema 版本会话明确拒绝 resume/fork。
- [ ] 仓库中不存在旧 Todo/TaskGraph 的生产实现、兼容入口、replay fallback 或双策略。
- [ ] `.venv/bin/python -m pytest tests -q` 通过。
- [ ] 代码总量相对旧双系统净减少，并且减量来自消除重复状态与兼容逻辑，而非削弱验证和测试。

## 六、执行前自检

- [ ] 所有行为任务都按“失败测试 → 确认失败 → 最小实现 → 确认通过 → 提交”排列。
- [ ] 所有新增、修改、删除文件均给出精确路径。
- [ ] 所有协议字段在本文中有确定名称和语义，没有待决定项。
- [ ] `TaskPatch.owner` 使用显式 `UNSET` 区分“不修改 owner”和“把 owner 清为 null”，类型与 JSON 解析保持一致。
- [ ] 初始 revision 固定为 0，首次有效创建固定生成 revision 1。
- [ ] 文档没有要求兼容旧 session、旧事件、`done` alias 或旧工具名。
- [ ] 执行者已确认当前工作树是否干净，并制定了不覆盖用户现有改动的暂存方案。
