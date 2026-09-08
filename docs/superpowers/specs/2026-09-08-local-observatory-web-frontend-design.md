# 本机 Observatory Web 前端设计（Task 6.5）

- 日期: 2026-09-08
- 状态: 已对齐，待用户审阅
- 范围: Stage 6 Web 服务之上的 Langfuse 风格只读前端与展示 DTO

## 1. 目标与非目标

Task 6.5 将当前可运行但仅用于验证接口的静态页面，升级为可长期使用的本机观测工作台。页面参考 Langfuse/LangSmith 的成熟信息架构：Trace Explorer 是高密度表格，Trace Detail 是 Observation Tree + Timeline/Waterfall + Selected Evidence 三栏工作区，Session Replay 是 branch-aware 的只读回放。

本任务不改变 journal 事实源、trace 生命周期语义、session branch reducer 或 recorder 行为。Web 仍只绑定 loopback、只提供 GET、无认证、无新依赖、无构建步骤。

本任务明确不包含：

- cost、score/evaluation、analytics/聚合图表；
- input/output 全文搜索、自然语言查询、saved views；
- 删除、编辑、重试、resume、fork、export、分享或其他 mutation；
- TUI `/observe` 与 `lanscoder observe` CLI 接线（另属 Stage 7）；
- provider 真实调用或网络访问。

## 2. 产品心智模型

页面固定采用以下层级：

```text
Primary Session
  └─ Branch
      └─ Trace
          └─ Observation
```

Observation 不是一级导航。child trace、background trace 和 rewind link 作为当前 trace 的关联节点或关系链接展示，不复制进父 trace 的 observation 树，也不出现在普通 primary session 列表。

`incomplete` 是独立的证据完整性状态，不替代 trace 的 running、waiting_for_input、completed、failed、cancelled 状态。Explorer、Detail 和对应 observation 节点都必须同时显示状态和完整性。

## 3. 视觉与导航

采用已确认的 A 工作台方案：

- 深色产品顶栏和导航壳；
- 浅色内容工作区；
- 高密度表格、细分隔线、紧凑状态 badge；
- ID、时间戳和 Raw JSON 使用等宽字体；
- 状态同时使用文字和颜色表达，不依赖颜色单独传达含义；
- 不使用营销卡片、渐变背景或大面积装饰图表。

一级导航只有：

```text
Traces | Sessions
```

固定页面路由：

```text
/traces
/traces/<trace-id>
/sessions
/sessions/<session-id>?branch=<branch-id>
```

现有 `/` 保留为 `/traces` 的兼容入口。Explorer 的过滤、排序、分页写入 URL query；刷新、浏览器返回和复制链接必须保留视图状态。Detail 和 Replay 的深链接必须在直接打开时恢复页面。

## 4. Trace Explorer

Explorer 使用表格而非卡片列表。默认列：

```text
Status | Trace / Input preview | Started | Duration |
Model / Provider | Tokens | Observations | Branch | Evidence
```

顶部提供可组合 filter chips，并映射到现有查询参数：

- project / session；
- status；
- from / to 时间范围；
- model / provider；
- tool；
- has_error；
- min/max duration、tokens、observation count；
- metadata.*、tags.*。

默认按 started_at 倒序，使用现有 cursor pagination；第一期采用显式分页，不使用无限滚动。列表行点击进入 `/traces/<trace-id>`。

Branch 列显示 active、historical 或 detached 语义；Evidence 列显示 complete 或 incomplete。running、waiting_for_input、failed、cancelled 和 incomplete 必须有文字标签与可辨识颜色。

列表请求失败显示错误状态，不得静默渲染为空。空数据必须区分：无任何 trace、当前过滤无结果、journal/index corrupt 或 incomplete 诊断。

## 5. Trace Detail

固定采用三栏工作区：

```text
左：Observation Tree
中：Timeline / Waterfall
右：Selected Observation Evidence
```

左栏默认选中 root agent observation；若不存在，则选择最早 observation。树节点统一显示 display name、observation type、outcome/status、duration，并标记 error/incomplete。推荐 display name：

- agent：`Agent turn`；
- generation：`Generation · <model>`；
- tool：`Tool · <tool_name>`；
- event：`Event · <event kind>`；
- child/background：关系标签加 linked trace id。

中栏显示从 trace 起点计算的 waterfall：每个 observation 有 start offset、duration、深度和状态；树节点与时间条同步选中。permission、child agent、background 使用专门标签；running/waiting 显示进行中状态。

右栏只显示当前选中 observation，采用标签页：

```text
Overview | Input | Output | Metadata | Raw
```

Overview 按类型显示：

- generation：model、provider、parameters、usage、usage_details、stream_summary；
- tool：tool name、arguments、result、outcome、error；
- permission：requested、waiting、decision、resumed；
- child/background：relation、linked trace、dispatch/completion 状态。

Trace 级别的 input、final output、metadata 和 evidence completeness 在 Detail 顶部固定可见。大型 payload 默认显示摘要与 payload link，读取失败必须显示证据不可用；Raw 默认折叠，且不得把 provider raw response 标为 normalized response。

父子、后台和 rewind 关系使用可点击关系条，跳转到关联 trace，不把关联 trace 的所有 observation 复制进当前页面。

## 6. Session Replay

`/sessions` 只列 primary sessions，使用紧凑表格。Session Detail 固定采用：

```text
左：Branch Tree
中：Conversation Replay
右：Selected Trace Summary
```

默认打开 active branch；`?branch=<branch-id>` 选择只读历史 branch。active branch 明显高亮，historical branch 标记只读，detached/background 不得伪装成 active。

Conversation Replay 使用展示投影，不让前端理解全部 journal event。每个 item 显示 role、content、status、branch、trace link 和 linked trace ids；可展开 Raw events，但 Raw events 不是默认主视图。

子代理 session 不出现在 session 列表，只能从父 trace 的 linked trace 进入。

## 7. Web 展示 DTO

Task 6.5 在现有 `ObservatoryQueryService` 与静态 JavaScript 之间增加稳定展示字段；不改变 journal schema。每个 observation 至少提供：

```json
{
  "observation_id": "obs_...",
  "type": "generation",
  "display_name": "Generation · gpt-4.1",
  "status": "succeeded",
  "started_at": "...",
  "ended_at": "...",
  "start_offset_ms": 120,
  "duration_ms": 7200,
  "depth": 1,
  "parent_observation_id": "obs_...",
  "error": null,
  "input": {},
  "output": {},
  "metadata": {},
  "raw": {}
}
```

`input`、`output`、`metadata` 是展示层标准化字段；原始未标准化内容保留在 `raw` 或 payload reference 中。原有 `data` 字段可保留作为兼容字段，但前端不得依赖内部事件键名猜测显示名称。

Session Replay 至少提供：

```json
{
  "items": [
    {
      "sequence": 12,
      "role": "user",
      "content": "...",
      "trace_id": "trc_...",
      "branch_id": "brn_...",
      "status": "completed",
      "linked_trace_ids": []
    }
  ]
}
```

若当前 journal 事件无法安全投影为 conversation item，必须返回可诊断的 Raw event，而不是静默丢失。

## 8. 状态与实时性

- 仅 `/traces/<id>` 对 running 或 waiting_for_input trace 以约 2 秒间隔轮询；Explorer、Sessions 和 Replay 不轮询。
- 页面请求失败保留 URL 和当前选择，并显示 inline error state。
- 没有 trace、过滤无结果、session 不存在、payload 缺失、journal corrupt 和 evidence incomplete 使用不同状态文案。
- 所有页面只读，不显示 mutation 控件。

## 9. 工程边界

继续使用标准 HTML/CSS/JavaScript，无前端框架和构建系统。若代码量增加，按职责拆分静态模块：

```text
lanscoder/observability/web/static/
  index.html
  app.js
  app.css
  components/
    explorer.js
    trace-detail.js
    session-replay.js
    format.js
```

可保留现有单文件入口，但模块边界必须保持清晰。Web 不得被 `lanscoder.agent` 或 `lanscoder.core` import。

## 10. 验收标准

1. `/traces`、`/traces/<id>`、`/sessions` 和 `/sessions/<id>?branch=...` 可直接打开并恢复深链状态。
2. Explorer 表格正确映射现有 filters、cursor、status、error、incomplete、model/provider、tokens、duration 和 observation count。
3. Detail 三栏树、waterfall 和证据面板使用同一选中 observation；generation、tool、permission、child/background 各显示对应字段。
4. Input/output、metadata、usage details、stream summary、payload 和 Raw evidence 可查看，读取失败明确显示。
5. Session Replay 只展示 primary sessions，active/historical branch 语义正确，关联 trace 可进入 Detail。
6. running/waiting 仅 Detail 轮询；其他页面不轮询。
7. API/DTO、页面路由、过滤 URL、深链接、空/错/损坏/不完整状态均有测试。
8. 相关 pytest、Ruff、format check、compileall 和 `git diff --check` 通过；不新增依赖，不创建提交。
