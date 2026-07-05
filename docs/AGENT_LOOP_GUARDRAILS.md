# Agent Loop Guardrails

## 概述

Agent Loop Guardrails 是一套安全护栏，防止 agent 在执行过程中失控。它覆盖三个维度：

1. **工具轮数上限** — 防止无限循环调用工具
2. **运行时上限** — 防止长时间运行
3. **单次消息验证上限** — 防止一次用户消息触发过多验证轮次

## 核心组件

### AgentLoopLimits

```python
class AgentLoopLimits:
    max_tool_rounds: int = 30
    max_runtime_seconds: float = 300.0
    max_verifications: int = 5
    age: Age = Age()
    cancellation: CancellationToken | None
```

**职责**：集中管理所有循环限制参数，提供统一的检查入口。

**设计决策**：
- 所有限制值都是可配置的，通过 CLI 参数 `--max-tool-rounds` 和配置文件的 `loop_limits` 覆盖
- 默认值（30 轮工具调用、300 秒运行时间、5 次验证）基于实际测试确定，平衡了复杂任务和安全性

### Age

```python
@dataclass
class Age:
    start_time: float = time.time()
    last_tool_start: float = 0.0
    last_tool_end: float = 0.0
    total_tool_time: float = 0.0
```

**职责**：跟踪 agent 的运行时间线和工具调用耗时。

**关键行为**：
- `start_time` 在 agent 启动时设置，之后只增不减
- `last_tool_start/end` 记录上一次工具调用的起止时间
- `total_tool_time` 累计所有工具调用的耗时
- 每次调用 `record_tool_call()` 时更新这些字段

### 检查流程

```
用户消息到达
    |
    v
检查 cancellation（是否被取消）
    |
    v
检查 max_tool_rounds（是否达到工具轮数上限）
    |
    v
检查 max_runtime_seconds（是否超过运行时间上限）
    |
    v
检查 max_verifications（是否超过验证次数上限）
    |
    v
检查 Age.total_tool_time（工具总耗时是否过长）
    |
    v
通过 → 执行下一轮
```

## 与 Agent Loop 的集成

### 在 `AgentLoop.run_once()` 中的位置

```python
async def run_once(self, user_message: str) -> AgentResult:
    # 1. 检查限制
    limits_check = await self._check_loop_limits()
    if limits_check.should_stop:
        return AgentResult(stopping_reason=limits_check.stopping_reason)
    
    # 2. 构建 prompt（含 skills、context 等）
    prompt_inputs = await self._build_prompt(user_message)
    
    # 3. 调用 provider
    response = await self.provider.chat_completion(prompt_inputs.messages)
    
    # 4. 处理工具调用
    if response.tool_calls:
        await self.age.record_tool_call()
        # 执行工具...
    
    # 5. 更新 session
    await self.session.add_assistant_response(response)
    
    # 6. 返回结果
    return AgentResult(...)
```

### 在 `AgentChatRunner.run()` 中的位置

```python
async def run(self, user_message: str) -> AgentResult:
    while True:
        # 1. 检查 cancellation
        if self.cancellation.is_cancelled:
            return AgentResult(stopping_reason="cancelled")
        
        # 2. 检查 loop limits
        limits_check = await self.loop.check_limits()
        if limits_check.should_stop:
            return AgentResult(stopping_reason=limits_check.stopping_reason)
        
        # 3. 执行一轮 agent loop
        result = await self.loop.run_once(user_message)
        
        # 4. 如果结果包含 tool_calls，继续下一轮
        if result.tool_calls:
            user_message = self._format_tool_result(result)
            continue
        
        # 5. 正常结束
        return result
```

## 停止原因

| 停止原因 | 触发条件 | 用户可见信息 |
|---------|---------|-------------|
| `max_tool_rounds` | 工具调用轮数 ≥ 30 | "已达到最大工具调用轮数" |
| `max_runtime` | 运行时间 ≥ 300 秒 | "运行时间已达上限" |
| `max_verifications` | 验证次数 ≥ 5 | "验证次数已达上限" |
| `total_tool_time` | 工具总耗时 ≥ 120 秒 | "工具执行总耗时过长" |
| `cancelled` | 用户主动取消 | "已取消" |

## 配置方式

### CLI 参数

```sh
# 覆盖最大工具轮数
firstcoder --max-tool-rounds 50

# 覆盖运行时间（秒）
firstcoder --max-runtime 600
```

### 配置文件

```toml
[loop_limits]
max_tool_rounds = 30
max_runtime_seconds = 300
max_verifications = 5
max_total_tool_time = 120
```

### 默认值

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_tool_rounds` | 30 | 单条消息最多调用工具的轮数 |
| `max_runtime_seconds` | 300 | 单条消息最长运行时间 |
| `max_verifications` | 5 | 单条消息最多验证次数 |
| `max_total_tool_time` | 120 | 单条消息工具总耗时上限 |

## 扩展性

### 添加新的限制

1. 在 `AgentLoopLimits` 中添加新字段
2. 在 `Age` 中添加相应的跟踪逻辑
3. 在 `check_limits()` 中添加检查条件
4. 在 `StoppingReason` 枚举中添加新的停止原因
5. 更新配置文件的 schema

### 动态调整

限制值可以在运行时动态调整，通过 `AgentLoopLimits.update()` 方法：

```python
await self.loop_limits.update(max_tool_rounds=50)
```

调整后，新的限制值立即生效，不影响当前的运行状态。

## 与上下文压缩的关系

Agent Loop Guardrails 和上下文压缩是两个独立但相关的机制：

- **Guardrails** 防止 agent 失控（时间/轮数维度）
- **上下文压缩** 防止 token 溢出（内存维度）

当 guardrails 触发时，agent 会停止执行，但当前的 session 状态（包括上下文压缩的历史）会保存到磁盘，下次 resume 时可以恢复。

## 设计决策记录

| 决策 | 理由 |
|------|------|
| 工具轮数默认 30 | 实测发现大多数任务在 10-20 轮内完成，30 轮给复杂任务留有余地 |
| 运行时间默认 300 秒 | 防止网络请求卡死等异常情况，同时允许足够的工具执行时间 |
| 工具总耗时单独限制 | 有些任务可能轮数不多但单个工具耗时很长（如文件搜索） |
| 限制值可配置 | 不同场景需求不同，教育用途可能需要更严格的限制 |
