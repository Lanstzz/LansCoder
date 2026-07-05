# Permissions Design

## 概述

权限系统分离了"模型想做什么"和"程序允许做什么"。它在工具执行前拦截请求，根据策略和用户决策决定是否允许执行。

## 核心组件

### PermissionAction

```python
class PermissionAction(Enum):
    READ_PATH = "read_path"
    WRITE_PATH = "write_path"
    DELETE_PATH = "delete_path"
    EXECUTE_SHELL = "execute_shell"
    NETWORK_REQUEST = "network_request"
    GIT_OPERATION = "git_operation"
    READ_ENV = "read_env"
```

**职责**：定义所有可能的权限请求类型。

### PermissionDecision

```python
class PermissionDecision(Enum):
    ALLOW = "allow"    # 直接执行
    ASK = "ask"        # 暂停并询问用户
    DENY = "deny"      # 阻止动作
```

### PermissionDecisionKind

```python
class PermissionDecisionKind(Enum):
    ONCE = "allow_once"       # 本次允许
    ALWAYS_SAME_SCOPE = "allow_always_same_scope"  # 长期授权
```

## 权限策略

### PermissionMode

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| `conservative` | 更多确认，更谨慎的默认值 | 生产环境、敏感项目 |
| `standard` | 默认平衡模式 | 日常开发 |
| `aggressive` | 更愿意执行常见项目内操作 | 熟悉的项目、快速迭代 |
| `bypass` | 跳过策略检查，用于受控实验 | 测试、调试 |

### 策略引擎

```python
class PermissionPolicy:
    def evaluate(request: PermissionRequest) -> PermissionDecision:
        """1. 检查是否有匹配的显式 grant
           2. 如果没有，应用默认策略
           3. 返回 ALLOW / ASK / DENY"""
```

**关键设计**：
- 默认策略提供安全底线
- 激进模式可以减少普通项目内写入确认，但不能绕过硬边界
- 敏感操作（如删除、环境变量读取）始终需要确认

## 长期授权

### Grant 结构

```python
@dataclass
class Grant:
    action: PermissionAction
    scope: GrantScope  # 路径、命令、模式等
    created_at: datetime
    expires_at: Optional[datetime]
    is_active: bool = True
```

### 授权存储

```python
class PermissionGrants:
    def save_grant(grant: Grant):
        """保存授权到 permissions.json"""
    
    def load_grants() -> List[Grant]:
        """从 permissions.json 加载所有授权"""
    
    def revoke_grant(grant_id: str):
        """撤销特定授权"""
```

**存储位置**：`<project-root>/.firstcoder/permissions.json`

## 权限请求流程

### 在 Agent Loop 中的位置

```python
async def execute_tool_action(request: PermissionRequest):
    """1. 检查权限策略
       2. 如果有匹配的 grant，直接允许
       3. 如果没有，评估策略
       4. 如果需要用户决策，暂停并等待
       5. 执行或拒绝工具调用"""
```

### 用户交互

```
工具调用请求
    |
    v
权限系统检查
    |
    v
需要用户决策？
    |
    +-- 是 --> 显示 PermissionPrompt Widget
    |               |
    |               v
    |          用户选择 ALLOW / DENY
    |               |
    |               v
    |          是否选择长期授权？
    |               |
    |               v
    |          保存 Grant
    |
    +-- 否 --> 根据策略直接执行
```

## 权限命令

### CLI 命令

| 命令 | 说明 |
|------|------|
| `/permission` | 查看当前权限模式 |
| `/mode conservative` | 使用更谨慎的权限策略 |
| `/mode standard` | 使用默认平衡策略 |
| `/mode aggressive` | 更主动允许常见项目内操作 |
| `/mode bypass` | 跳过策略检查 |

### TUI 交互

权限请求会暂停 agent，等待用户决定：

```
┌─────────────────────────────────────────────────────────────┐
│ ⚠️  Permission Request                                      │
│                                                             │
│ Action: write_path                                          │
│ Path: ./src/main.py                                         │
│ Mode: aggressive                                            │
│                                                             │
│ [Allow] [Deny] [Always Allow]                               │
└─────────────────────────────────────────────────────────────┘
```

## 扩展性

### 添加新的 PermissionAction

1. 在 `PermissionAction` 枚举中添加新类型
2. 更新策略引擎以支持新类型的请求
3. 在 TUI 中添加相应的交互组件

### 自定义策略

1. 实现 `PermissionPolicy` 协议
2. 注册新的策略到配置系统
3. 测试策略行为

### 审计日志

权限决策会被记录到 session 事件中：

```json
{"type": "permission_requested", "action": "write_path", "path": "./src/main.py"}
{"type": "permission_decided", "action": "write_path", "decision": "allow", "grant": null}
```

## 设计决策记录

| 决策 | 理由 |
|------|------|
| 分离模型意图和程序许可 | 防止模型越权操作，提供安全底线 |
| 支持长期授权 | 减少重复确认，提升用户体验 |
| 多模式策略 | 适应不同场景需求（安全 vs 效率） |
| 审计日志 | 便于调试和追踪权限相关行为 |
| TUI 集成 | 提供直观的用户交互界面 |
