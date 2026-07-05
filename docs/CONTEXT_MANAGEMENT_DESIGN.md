# Context Management Design

## 概述

上下文管理系统负责管理 agent 与 LLM 交互时的 token 窗口。它提供**多层压缩策略**，从简单的截断到基于任务的智能摘要，确保 agent 能在有限的 token 预算内保持有效的对话历史。

## 核心组件

### ContextManager

```python
class ContextManager:
    """上下文窗口管理器，协调所有压缩操作"""
    def __init__(self, session, config):
        self.session = session
        self.config = config
        self.pipeline = CompactionPipeline()
```

**职责**：
- 跟踪当前 token 使用情况
- 决定何时触发压缩
- 协调不同压缩级别的操作

### CompactionPipeline

```python
class CompactionPipeline:
    """压缩流水线，按顺序执行 L1-L3 压缩"""
    def execute(request: CompactionRequest) -> CompactionResponse:
        """1. 尝试 L1 压缩（简单截断）
           2. 如果不够，尝试 L2 压缩（摘要）
           3. 如果还不够，尝试 L3 压缩（更智能的摘要）
           4. 返回压缩结果"""
```

## 压缩级别

### L1: 简单截断

```python
def l1_compact(messages):
    """移除最早的对话轮次，保留最近的 N 条消息"""
    return messages[-N:]
```

**适用场景**：
- Token 轻微超标
- 需要快速响应
- 对话历史不太重要

### L2: 基础摘要

```python
def l2_compact(messages):
    """对早期消息进行 LLM 摘要，保留摘要和最近消息"""
    early_messages = messages[:-RECENT_COUNT]
    summary = llm.summarize(early_messages)
    return [summary] + messages[-RECENT_COUNT:]
```

**适用场景**：
- Token 中度超标
- 需要保留对话概要
- 早期消息的具体内容不那么重要

### L3: 智能压缩

```python
def l3_compact(messages):
    """基于任务边界的智能压缩，保留任务上下文"""
    task_groups = group_by_task_boundary(messages)
    compressed = []
    for group in task_groups:
        if group.is_completed:
            compressed.append(llm.summarize(group.messages))
        else:
            compressed.extend(group.messages)
    return compressed
```

**适用场景**：
- Token 严重超标
- 需要保留任务相关的上下文
- 对话包含多个独立任务

### L4: Checkpoint 摘要

```python
def l4_checkpoint(messages):
    """创建会话 checkpoint，保存完整历史到磁盘"""
    checkpoint = Checkpoint(
        messages=messages,
        timestamp=datetime.now(),
        summary=llm.summarize(messages)
    )
    store.save_checkpoint(checkpoint)
    return [checkpoint.summary]
```

**适用场景**：
- 需要长时间保存会话状态
- 后续可能需要恢复完整历史
- 作为其他压缩策略的后备

## 任务边界检测

### TaskBoundaryDetector

```python
class TaskBoundaryDetector:
    def detect_boundary(user_message: str, context: Context) -> TaskBoundarySignal:
        """检测用户消息是否表示任务切换"""
```

**检测信号**：
```json
{
  "decision": "same | new | uncertain",
  "basis_message_id": "msg_xxx"
}
```

### 任务边界处理流程

```
用户消息到达
    |
    v
模型调用 task_boundary(decision, basis_message_id)
    |
    v
程序生成 candidate task_hash
    |
    v
稳定窗口确认任务切换
    |
    v
TASK_HASH_CHANGED 触发压缩
    |
    v
旧任务内容被 micro-compact
    |
    v
session event 保留这次切换，方便 resume
```

**关键设计**：
- 模型不直接生成 task hash，只提交结构化信号
- 程序根据 session id、basis message id 和策略版本生成稳定 hash
- 稳定窗口防止模型误判导致过早压缩

## 压缩触发条件

### 自动触发

| 条件 | 压缩级别 | 说明 |
|------|----------|------|
| token 使用量 > 80% | L1 | 预防性压缩 |
| token 使用量 > 90% | L2 | 紧急压缩 |
| token 使用量 > 95% | L3 | 深度压缩 |
| 任务边界检测 | L3 | 基于语义的压缩 |

### 手动触发

用户可以通过以下方式手动触发压缩：
- CLI 命令：`/compact`
- TUI 命令：点击压缩按钮
- API 调用：`context.compact()`

## Session 集成

### Append-Only 事件日志

所有压缩操作都会记录到 session 事件中：

```json
{"type": "compaction_started", "level": "L2", "reason": "token_budget"}
{"type": "compaction_completed", "level": "L2", "messages_removed": 15, "tokens_saved": 2048}
{"type": "task_boundary_detected", "decision": "new", "basis_message_id": "msg_123"}
```

### Checkpoint 和 Archive

```python
class Checkpoint:
    """会话检查点，保存完整状态"""
    message_history: List[Message]
    context_state: ContextState
    timestamp: datetime
    task_hash: str

class Archive:
    """归档存储，保存旧的压缩结果"""
    checkpoints: List[Checkpoint]
    compressed_segments: List[CompressedSegment]
```

## 配置

### 压缩策略配置

```toml
[context]
# 压缩触发阈值（百分比）
compression_threshold = 80
emergency_threshold = 90
critical_threshold = 95

# 压缩级别偏好
preferred_level = "L2"  # L1, L2, L3, L4

# 任务边界检测
task_boundary_detection = true
stable_window_size = 3  # 稳定窗口大小
```

### Token 预算

```python
class TokenBudget:
    total_limit: int = 128000  # 总 token 限制
    safety_margin: float = 0.1  # 安全边际 10%
    
    def calculate_available(self, current_usage: int) -> int:
        return int(self.total_limit * (1 - self.safety_margin)) - current_usage
```

## 扩展性

### 添加新的压缩算法

1. 实现 `CompressionAlgorithm` 协议
2. 注册到 CompactionPipeline
3. 更新配置以支持新算法

### 自定义触发条件

1. 实现 `TriggerCondition` 协议
2. 添加到触发条件检查列表
3. 测试条件评估逻辑

### 集成新的存储后端

1. 实现 `StorageBackend` 协议
2. 更新 Checkpoint 和 Archive 的存储逻辑
3. 测试数据持久化和恢复

## 设计决策记录

| 决策 | 理由 |
|------|------|
| 多层压缩策略 | 平衡压缩效果和性能，不同场景使用不同级别 |
| 任务边界检测 | 语义感知的压缩比单纯基于 token 数量更有效 |
| Append-Only 日志 | 保证会话历史的完整性和可追溯性 |
| 稳定窗口机制 | 防止模型误判导致的上下文丢失 |
| 可配置的触发阈值 | 适应不同模型和场景的需求 |
