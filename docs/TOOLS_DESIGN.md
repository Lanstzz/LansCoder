# Tools Design

## 概述

工具系统是 FirstCoder 的核心能力之一，允许 agent 与外部环境交互（文件系统、shell、git、网络等）。所有工具都通过 `ToolRegistry` 统一管理，并在执行前经过权限检查。

## 核心组件

### Tool 协议

```python
class Tool(Protocol):
    """所有工具必须实现的统一接口"""
    
    @property
    def name(self) -> str:
        """工具的唯一名称"""
    
    @property
    def description(self) -> str:
        """工具的功能描述，用于模型理解"""
    
    @property
    def parameters(self) -> Dict[str, Any]:
        """工具的参数 schema（JSON Schema 格式）"""
    
    async def execute(self, arguments: Dict[str, Any]) -> ToolResult:
        """执行工具调用，返回结果"""
```

### ToolRegistry

```python
class ToolRegistry:
    """工具注册表，管理所有可用工具"""
    
    def __init__(self):
        self._tools: Dict[str, Tool] = {}
    
    def register(self, tool: Tool):
        """注册一个新工具"""
        self._tools[tool.name] = tool
    
    def get_tool(self, name: str) -> Optional[Tool]:
        """根据名称获取工具"""
        return self._tools.get(name)
    
    def list_tools(self) -> List[ToolDefinition]:
        """返回所有工具的定義列表（用于 provider 请求）"""
        return [ToolDefinition.from_tool(tool) for tool in self._tools.values()]
```

### ToolResult

```python
@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    metadata: Dict[str, Any] = None
```

## 内置工具

### 文件操作工具

| 工具 | 说明 | 权限要求 |
|------|------|---------|
| `view` | 读取文件内容 | `read_path` |
| `write` | 写入文件内容 | `write_path` |
| `edit` | 编辑文件特定内容 | `write_path` |
| `delete` | 删除文件或目录 | `delete_path` |
| `ls` | 列出目录内容 | `read_path` |
| `glob` | 文件模式匹配 | `read_path` |

### 代码执行工具

| 工具 | 说明 | 权限要求 |
|------|------|---------|
| `python_exec` | 执行 Python 代码 | `execute_shell` |
| `shell` | 执行 Shell 命令 | `execute_shell` |
| `diagnostics` | 运行项目验证命令 | `execute_shell` |

### Git 工具

| 工具 | 说明 | 权限要求 |
|------|------|---------|
| `git_status` | 查看 git 状态 | `git_operation` |
| `git_diff` | 查看 git 差异 | `git_operation` |
| `git_log` | 查看 git 日志 | `git_operation` |

### 搜索工具

| 工具 | 说明 | 权限要求 |
|------|------|---------|
| `grep` | 文本内容搜索 | `read_path` |
| `web_search` | 网络搜索 | `network_request` |
| `fetch` | 获取 URL 内容 | `network_request` |

### 交互工具

| 工具 | 说明 | 权限要求 |
|------|------|---------|
| `ask_user` | 向用户提问 | 无 |
| `todo` | 管理待办事项 | 无 |
| `think` | 记录推理过程 | 无 |
| `task_boundary` | 检测任务边界 | 无 |

## 权限集成

### PermissionRegistry

```python
class PermissionRegistry:
    """工具权限注册表"""
    
    TOOL_PERMISSION_MAP = {
        "view": "read_path",
        "write": "write_path",
        "shell": "execute_shell",
        "git_diff": "git_operation",
        # ... 其他映射
    }
    
    def check_permission(tool_name: str) -> Optional[PermissionAction]:
        """检查工具执行所需的权限类型"""
        return cls.TOOL_PERMISSION_MAP.get(tool_name)
```

### 权限检查流程

```
工具调用请求
    |
    v
PermissionRegistry 检查所需权限
    |
    v
权限系统评估（策略 + 用户决策）
    |
    v
允许执行 --> 工具执行
    |
拒绝 --> 返回错误结果
```

## 工具执行流程

### 在 Agent Loop 中的位置

```python
class AgentLoop:
    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """1. 从 registry 获取工具
           2. 检查权限
           3. 验证参数
           4. 执行工具
           5. 返回结果"""
        
        tool = self.registry.get_tool(tool_call.name)
        if not tool:
            return ToolResult(content=f"Unknown tool: {tool_call.name}", is_error=True)
        
        # 权限检查
        permission_action = PermissionRegistry.check_permission(tool_call.name)
        if permission_action and not await self.permissions.check(permission_action):
            return ToolResult(content="Permission denied", is_error=True)
        
        # 执行工具
        try:
            result = await tool.execute(tool_call.arguments)
            return ToolResult(content=result.content, is_error=result.is_error)
        except Exception as e:
            return ToolResult(content=f"Tool execution error: {str(e)}", is_error=True)
```

## 错误处理

### 工具错误类型

| 错误类型 | 说明 | 处理策略 |
|---------|------|---------|
| `PermissionDenied` | 权限被拒绝 | 返回错误结果，记录审计日志 |
| `ValidationError` | 参数验证失败 | 返回格式错误的提示 |
| `ExecutionError` | 工具执行失败 | 返回具体错误信息 |
| `TimeoutError` | 执行超时 | 中断执行，返回超时提示 |

### 错误响应格式

```json
{
  "content": "Error: permission denied for write_path",
  "is_error": true,
  "metadata": {
    "tool_name": "write",
    "error_type": "PermissionDenied",
    "timestamp": "2024-01-01T00:00:00Z"
  }
}
```

## 扩展性

### 添加新工具

1. 实现 `Tool` 协议
2. 在 `ToolRegistry` 中注册
3. 添加权限映射到 `PermissionRegistry`
4. 编写工具描述供模型理解

### 自定义工具行为

1. 继承现有工具实现
2. 覆盖特定的方法
3. 添加自定义的参数验证

### 工具监控

```python
class ToolMonitor:
    def record_execution(tool_name: str, duration: float, success: bool):
        """记录工具执行统计信息"""
        # 用于性能分析和故障排查
```

## 设计决策记录

| 决策 | 理由 |
|------|------|
| 统一 Tool 协议 | 简化工具开发和集成 |
| 权限系统集成 | 确保安全执行，防止越权操作 |
| 集中错误处理 | 提供一致的用户体验 |
| 可扩展的注册机制 | 支持自定义工具和第三方集成 |
| 工具描述标准化 | 帮助模型理解工具用途和参数 |
