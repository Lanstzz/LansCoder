# 本机 Observatory Web 前端设计（Task 6.5）

- 日期: 2026-09-08
- 状态: 已修订，待用户批准
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

固定页面路由仅接受以下精确路径：

```text
/traces
/traces/<trace-id>
/sessions
/sessions/<session-id>?branch=<branch-id>
```

`/` 保留为 `/traces` 的兼容入口。`/traces`、`/traces/<trace-id>`、`/sessions` 与 `/sessions/<session-id>` 以外的同前缀路径必须返回 404，不能把任意路径交给静态入口。Detail 使用 `?observation=<observation-id>&tab=<overview|input|output|metadata|raw>` 恢复选中节点和证据标签；Replay 使用 `?branch=<branch-id>` 恢复所选 branch。Explorer 的过滤和分页写入 URL query；刷新、浏览器返回和复制链接必须保留视图状态。

## 4. Trace Explorer

Explorer 使用表格而非卡片列表。默认列：

```text
Status | Trace / Input preview | Started | Duration |
Model / Provider | Tokens | Observations | Branch | Evidence
```

顶部提供可组合 filter chips。规范 query schema 为：

- 字符串过滤为 `project`、`session_id`、`status`、`model`、`provider`、`tool` 和重复的 `tag`；同名重复值为 OR，不同字段为 AND；
- `from`、`to` 是包含边界的 RFC 3339 UTC 时间；
- `has_error` 为 `true` 或 `false`；数值范围字段固定为 `min_duration_ms`、`max_duration_ms`、`min_tokens`、`max_tokens`、`min_observation_count` 和 `max_observation_count`；指标缺失时不匹配相应范围；
- `metadata.<key>` 的值是 URL 编码后的 JSON scalar，例如字符串 `"production"` 或数字 `3`；服务端解析后按 JSON 值精确比较；
- 不支持 `session`、`tags.*` 或同义时间参数；未知参数、非法布尔/数值/时间/metadata scalar 均为 `invalid_query` problem；
- 第一期固定 `started_at:desc`，不提供用户可选排序参数。

所有字符串、`tag` 和相同 `metadata.<key>` 可重复；`from`、`to`、`has_error`、各数值范围字段、`limit`、`cursor` 只能出现一次。`limit` 默认 50，范围为 1–200。`cursor` 绑定规范化 filters 和固定排序，但不绑定 `limit`；其格式错误、与当前 query 不匹配或失效时返回 `invalid_cursor` problem，不得静默重置为首页。使用显式 opaque cursor pagination，不使用无限滚动。其内部可以编码 offset，因而不是快照分页；实时新增 trace 可能使后续页重复或跳过，UI 必须明确说明。列表行点击进入 `/traces/<trace-id>`。

Branch 列显示 `active` 或 `historical`，并可额外显示独立 `detached` 标签。QueryService 必须在同 session 内以 `job_id` 将 `background.scheduled.parent_trace_id` 与 `background.completed`、`background.failed` 或 `background.cancelled` 的 `background_trace_id` 关联。completion 的 `detached_from_active_branch=true` 只将对应 background trace 标为 `detached=true`；父 trace 仅在 relation 中显示该子 trace 的 detached completion。无法完成该 job 关联时 `detached=false`。它可以和 `historical` 同时出现。Evidence 列显示 complete 或 incomplete。running、waiting_for_input、failed、cancelled 和 incomplete 必须有文字标签与可辨识颜色。

列表成功响应除 `items`、`total`、`next_cursor` 外，必须返回未过滤健康 trace 的 `unfiltered_total`、`empty_reason`（`no_traces`、`filtered_empty`、`corrupt` 或 `null`）和 `diagnostics`。一个损坏 journal 或 trace index 不得阻塞健康 trace：查询层先尝试物化索引，失败时逐 session 从 journal 投影健康 trace，并将跳过的 corrupt session 写入 diagnostics。列表请求失败显示错误状态，不得静默渲染为空；`incomplete` 是 trace evidence 状态和诊断，不是空列表理由。

## 5. Trace Detail

固定采用三栏工作区：

```text
左：Observation Tree
中：Timeline / Waterfall
右：Selected Observation Evidence
```

左栏默认选中 root agent observation；若不存在，则选择最早 observation。树节点统一显示 display name、observation type、outcome/status、duration，并标记 error/incomplete。`?observation=` 指向仍存在的节点时优先保留；否则按此前规则回退。推荐 display name：

- agent：`Agent turn`；
- generation：`Generation · <model>`；
- tool：`Tool · <tool_name>`；
- event：`Event · <event category>`；
- child/background：作为 relation 标签加 linked trace id，而不是伪造新的 observation type。

中栏显示从 trace 起点计算的 waterfall：每个 observation 有 start offset、duration、深度和状态；树节点与时间条同步选中。`depth` 从 parent 链计算；未知 parent 或 cycle 节点降为 root，并携带 `orphan_parent` 或 `parent_cycle` diagnostic。不能解析 trace/observation 时间时 `start_offset_ms` 为 `null`；未结束 observation 的 `status=running`、`duration_ms=null`。permission 使用 event category 标签；child/background 使用 relation 标签；running/waiting 显示进行中状态。

右栏只显示当前选中 observation，采用标签页：

```text
Overview | Input | Output | Metadata | Raw
```

Overview 按真实 observation type 与 event category/relations 显示：

- generation：model、provider、parameters、usage、usage_details、stream_summary；
- tool：tool name、arguments、result、outcome、error；
- event category 为 permission：requested、waiting、decision、resumed；
- trace relation 为 child/background：relation、linked trace、dispatch/completion 状态。

Trace 级别的 input、final output、metadata 和 evidence completeness 在 Detail 顶部固定可见。Detail 不得内联完整 payload 内容：所有大型证据只返回 payload descriptor，用户显式请求 payload endpoint 后才能显示 JSON/text preview。descriptor 的 `availability` 为 `available`、`missing` 或 `corrupt`；`preview` 为 `json`、`text` 或 `metadata_only`，后者用于有效但不可内联预览的二进制。读取失败必须显示证据不可用；Raw 默认折叠，且不得把 provider raw response 标为 normalized response，也不得提供下载或导出控件。

父子、后台和 rewind 关系使用可点击关系条，跳转到关联 trace，不把关联 trace 的所有 observation 复制进当前页面。

## 6. Session Replay

`/sessions` 只列可验证为 primary 且不是 `corrupt` 的 sessions，使用紧凑表格。损坏 journal 的 kind 无法验证，不得按默认值伪装为 primary；服务端将其排除并在列表 `diagnostics` 中报告。Session Detail 固定采用：

```text
左：Branch Tree
中：Conversation Replay
右：Selected Trace Summary
```

默认打开 active branch；`?branch=<branch-id>` 选择只读历史 branch。不存在的 branch 返回 `not_found` problem。active branch 明显高亮，historical branch 标记只读，detached/background 不得伪装成 active。

Conversation Replay 使用展示投影，不让前端理解全部 journal event。所选 branch 必须通过既有 branch projection 计算，因此包含其祖先在 `base_sequence` 前可见的事件，而不是仅返回 branch id 相等的原始事件。每个 item 显示 role、content、status、branch、可空 trace link 和 linked trace ids；普通 user/assistant message 的 `trace_id` 必须为 `null`，除非 journal 已有明确关联，禁止按时间、内容或 sequence 推断。投影中不能安全映射为 conversation item 的事件进入可展开 Raw events，不能静默丢失，也不是默认主视图。

子代理 session 不出现在 session 列表，只能从父 trace 的 linked trace 进入。

## 7. Web 展示 DTO

Task 6.5 在现有 `ObservatoryQueryService` 与静态 JavaScript 之间增加稳定展示字段；不改变 journal schema。展示 API 不再返回未标准化的 `data`；`raw` 只用于通用 Raw JSON 显示，前端不得据此推断字段或类型。每个 observation 至少提供：

```json
{
  "observation_id": "obs_...",
  "type": "generation",
  "display_name": "Generation · gpt-4.1",
  "status": "succeeded",
  "outcome": "succeeded",
  "event_category": null,
  "started_at": "...",
  "ended_at": "...",
  "start_offset_ms": 120,
  "duration_ms": 7200,
  "depth": 1,
  "parent_observation_id": "obs_...",
  "incomplete": false,
  "diagnostics": [],
  "error": null,
  "input": {},
  "output": {},
  "metadata": {},
  "overview": {},
  "relations": [],
  "raw": {}
}
```

`type` 固定为 journal 的真实 `agent`、`generation`、`tool` 或 `event`。`event_category` 仅从 event observation 的显式事件名投影；permission、child 与 background 不是新的 type。`status` 为 `running`、`succeeded`、`failed`、`cancelled`、`skipped`、`scheduled` 或 `unknown`；`outcome` 保留原始 outcome 或为 `null`。`waiting_for_input` 只属于 trace，不能投影为 observation status。缺失的展示值使用 `null`，不能以猜测值填充。

`display_name` 的优先级固定为 generation 的 model、tool 的 tool name、event 的 event category、agent 的 `Agent turn`、最后为 type 名。所有 type 的 `metadata` 仅来自显式 observation metadata map，否则为 `{}`。下表规定其余展示字段的唯一映射；任何未列出的值为 `null` 或 `{}`，不得从关联 event、相邻时间或其它 observation 猜测。

| 真实 type | `input` | `output` | `overview` | `event_category` |
| --- | --- | --- | --- | --- |
| agent | `null` | `null` | `turn`、`limits` | `null` |
| generation | `normalized_request` 的 payload descriptor，或 `null` | `normalized_response` 的 payload descriptor，或 `null` | `model`、`provider`、`parameters`、`usage`、`usage_details`、`stream_summary`、`finish_reason` | `null` |
| tool | `arguments`，或 `null` | 仅已有 `result` / `ok` / `result_type` 证据，否则 `null` | `tool_name`、`arguments`、`result`、`outcome`、`error` | `null` |
| event | `null` | `null` | `event`、`tool_call_id`、`tool_name`、`permission_request_id`、`permission_decision`、`prewrite_review`、`request_id`、`job_id`、`status` 中已有字段 | event data 的 `event` 字段，或 `null` |

所有内容寻址的 payload reference，无论它出现于 `input`、`output` 还是 `overview`，一律包装为下列 descriptor；原始 payload 内容绝不进入 Detail 响应。表中列出的 `tool.arguments`、`tool.result`、`ok`、`result_type` 等受控结构化 journal 事实可直接作为标准 DTO 字段返回，但不得透传未标准化 `data`。每个 descriptor 的 `availability` 初始基于引用存在性为 `available` 或 `missing`；payload endpoint 验证后可以确认 `corrupt`。`preview` 只由 media type 决定，不能将二进制转换为文本。

```json
{
  "sha256": "...",
  "media_type": "application/json",
  "size_bytes": 18420,
  "url": "/api/v1/payloads/<sha256>?size_bytes=18420",
  "availability": "available",
  "preview": "json"
}
```

descriptor 的 `url` 必须含唯一的 `size_bytes` 查询参数，其值等于 descriptor 的 `size_bytes`。payload endpoint 使用该期望值读取相应引用：文件不存在为 `payload_missing` 404；SHA-256 或该引用的大小校验失败为 `payload_corrupt` 409。只按 digest 读取无法判断某一引用的大小是否错误，因此不以其它同 digest 引用的大小替代它。

`relations` 的固定对象形状为 `{ "relation": "...", "linked_trace_id": "...", "parent_observation_id": null, "job_id": null, "dispatch_status": null, "completion_status": null, "detached": false }`。字段仅从明确 `trace.linked` 或按 job id 配对的 background lifecycle facts 投影；缺失字段为 `null`。有 `parent_observation_id` 的 relation 附到对应 observation；其余 relation 保持 trace 级别。

observation 的 `incomplete` 为 `true` 当且仅当它未结束、任一对应 started/ended event 含 `evidence_incomplete=true`，或其明确 payload reference 已知 missing/corrupt。`diagnostics` 使用相应的 `unfinished`、`evidence_incomplete`、`payload_missing`、`payload_corrupt`、`orphan_parent` 或 `parent_cycle` code；没有诊断时为 `[]`。`raw` 的固定形状为 `{ "started_event": { "event_id": "...", "sequence": 0, "occurred_at": "...", "data": {} }, "ended_event": null }`；每个 event object 只包含这四个字段，且其中的 payload references 保持 reference、绝不展开 payload 内容。前端只能原样显示 Raw，不能依赖 `raw.data` 的键。

Explorer summary 另提供最大 240 个 Unicode code point 的 `input_preview` 和 `input_preview_truncated`。它、branch state 与 detached 都由 QueryService 从 trace/session facts 按需投影，不改变 journal 或 index schema。

Session Replay 至少提供：

```json
{
  "items": [
    {
      "sequence": 12,
      "role": "user",
      "content": "...",
      "trace_id": null,
      "branch_id": "brn_...",
      "status": "recorded",
      "linked_trace_ids": []
    }
  ]
}
```

Replay 响应还必须提供 `selected_branch_id`、带 parent/base/active 的 branch tree，以及所选 projection 的 `raw_events`。message item 的 status 为 `recorded`，或在已有 background notification evidence 时为 `scheduled`、`completed`、`failed` 或 `cancelled`。若当前 journal 事件无法安全投影为 conversation item，必须返回可诊断的 Raw event，而不是静默丢失。

## 8. 状态与实时性

- 仅 `/traces/<id>` 对 running 或 waiting_for_input trace 以约 2 秒间隔轮询；Explorer、Sessions 和 Replay 不轮询。
- Detail 使用单次 `setTimeout`，不能混用 interval；发起新请求、路由切换或页面卸载时必须以 `AbortController` 取消旧请求并清除 timer。
- 轮询刷新后保留仍存在的 observation/tab；选中 observation 消失时回退 root agent，再回退最早 observation。
- 页面请求失败保留 URL 和当前选择，并显示 inline error state。
- API problem 使用 JSON `{ "error": { "code": "...", "message": "...", "resource": {} } }`。非法 query/cursor 为 400，not found 为 404，已知 journal 或 payload integrity corrupt 为 409。payload endpoint 使用 descriptor URL 内的唯一 `size_bytes` 期望值：将 `FileNotFoundError` 映射为 `payload_missing` 404，将 `PayloadIntegrityError` 或该引用的大小不匹配映射为 `payload_corrupt` 409；有效的二进制 payload 仍是成功响应，但 `metadata_only` UI 不请求或渲染它。
- 没有 trace、过滤无结果、session 不存在、payload 缺失、journal corrupt 和 evidence incomplete 使用不同状态文案。Detail 或 Replay 目标 session 已损坏时返回 `journal_corrupt` problem；健康列表数据必须继续可用。
- 所有页面只读，不显示 mutation 控件。

## 9. 工程边界

继续使用标准 HTML/CSS/JavaScript，无前端框架和构建系统。Task 6.5 保持 `index.html`、`app.js` 和 `app.css` 的一级资源，避免修改 package-data；可用文件内的清晰函数边界替代嵌套静态模块。Web 不得被 `lanscoder.agent` 或 `lanscoder.core` import。

## 10. 验收标准

1. 精确路由 `/traces`、`/traces/<id>`、`/sessions` 和 `/sessions/<id>?branch=...` 可直接打开并恢复深链状态；非法同前缀路径为 404。
2. Explorer 表格正确映射规范 filters、opaque cursor、status、error、incomplete、model/provider、tokens、duration、observation count、截断 input preview、branch state 和 detached。
3. Detail 三栏树、waterfall 和证据面板使用同一选中 observation；generation、tool、permission event、child/background relation 依据稳定 DTO 显示对应字段，且覆盖 orphan/cycle/时间缺失状态。
4. Input/output、metadata、usage details、stream summary、payload descriptor 和 Raw evidence 可查看；payload 不在 Detail 内联，missing/corrupt/metadata-only 明确显示。
5. Session Replay 只展示 primary sessions，使用 branch projection 呈现 active/historical branch；普通 message 不伪造 trace link，关联 trace 可进入 Detail。
6. running/waiting 仅 Detail 轮询；其他页面不轮询；轮询取消、URL selection/tab 恢复和回退行为按第 8 节执行。
7. pytest 覆盖 API/DTO、route、filter/cursor、branch projection、payload/error、corrupt-session isolation 与静态资源契约。无 DOM 浏览器测试工具，因此 tree/waterfall 同步选择、URL 恢复和 polling 的浏览器交互使用明确人工检查，不得声称由 pytest 覆盖。
8. 相关 pytest、Ruff、format check、compileall 和 `git diff --check` 通过；不新增依赖，不创建提交。
