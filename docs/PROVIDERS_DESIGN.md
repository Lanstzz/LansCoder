# Providers Design

## 概述

Provider 抽象层隔离了 agent 主循环与具体的 LLM 厂商实现。所有模型 provider 都实现 `ChatProvider` 协议，使得切换模型或厂商时只需替换实现，无需修改核心逻辑。

## 核心组件

### ChatProvider 协议

```python
class ChatProvider(Protocol):
    """所有 provider 必须实现的统一接口"""
    
    async def chat_completion(self, request: ChatRequest) -> ChatResponse:
        """发送聊天请求并返回完整响应"""
    
    async def stream_completion(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        """发送聊天请求并返回流式事件迭代器"""
    
    def get_model_info(self) -> ModelInfo:
        """返回 provider 支持的模型信息"""
```

### ChatRequest

```python
@dataclass
class ChatRequest:
    messages: List[Message]
    model: str
    temperature: float = 0.7
    max_tokens: int = 4096
    tools: List[ToolSpec] = None
    tool_choice: str = "auto"
    stream: bool = False
    metadata: Dict[str, Any] = None
```

### ChatResponse

```python
@dataclass
class ChatResponse:
    content: str
    model: str
    finish_reason: str
    usage: UsageStats
    tool_calls: List[ToolCall] = None
    reasoning_content: str = None
    metadata: Dict[str, Any] = None
```

## 支持的 Provider

### OpenAI Compatible

```python
class OpenAICompatibleProvider(ChatProvider):
    """兼容 OpenAI API 的 provider 实现"""
    
    def __init__(self, base_url, api_key, model):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
```

**支持的特性**：
- 标准聊天完成 API
- 流式输出
- 工具调用（function calling）
- 多模态输入（如果 API 支持）

### Anthropic

```python
class AnthropicProvider(ChatProvider):
    """Anthropic Claude provider 实现（实验性）"""
    
    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model
```

**支持的特性**：
- Claude 消息 API
- 流式输出
- Thinking 和 Cache 行为（部分支持）
- 原生工具调用

## Provider Factory

```python
class ProviderFactory:
    """创建和缓存 provider 实例"""
    
    def create_provider(config: ProviderConfig) -> ChatProvider:
        """根据配置创建对应的 provider 实例"""
        if config.type == "openai-compatible":
            return OpenAICompatibleProvider(...)
        elif config.type == "anthropic":
            return AnthropicProvider(...)
        else:
            raise UnsupportedProviderError(config.type)
```

**设计特点**：
- 支持动态 provider 注册
- 缓存已创建的实例以避免重复初始化
- 提供 provider 健康检查接口

## 错误处理

### ProviderError

```python
class ProviderError(Exception):
    """Provider 相关异常的基类"""
    
    def __init__(self, kind: ProviderErrorKind, message: str, details: Dict = None):
        self.kind = kind
        self.message = message
        self.details = details or {}
```

### 错误类型

| 错误类型 | 说明 | 处理策略 |
|---------|------|---------|
| `RATE_LIMIT` | 请求频率限制 | 指数退避重试 |
| `AUTH_FAILED` | 认证失败 | 立即失败，提示用户 |
| `MODEL_NOT_FOUND` | 模型不存在 | 列出可用模型 |
| `TOKEN_LIMIT` | Token 超出限制 | 触发上下文压缩 |
| `NETWORK_ERROR` | 网络连接问题 | 重试或降级 |

## 集成到 Agent Loop

### 在 AgentLoop 中的使用

```python
class AgentLoop:
    def __init__(self, provider: ChatProvider, ...):
        self.provider = provider
    
    async def run_once(self, user_message: str) -> AgentResult:
        # 1. 构建请求
        request = self._build_request(user_message)
        
        # 2. 调用 provider
        response = await self.provider.chat_completion(request)
        
        # 3. 处理响应
        return self._process_response(response)
```

### 流式输出处理

```python
async def handle_stream(provider: ChatProvider, request: ChatRequest):
    """处理来自 provider 的流式响应"""
    async for event in provider.stream_completion(request):
        if event.type == "content_delta":
            yield event.delta  # 发送到 TUI 显示
        elif event.type == "tool_call_delta":
            yield event.tool_call  # 累积工具调用
        elif event.type == "usage":
            update_token_usage(event.usage)  # 更新 token 统计
```

## 配置

### Provider 配置

```toml
[provider]
type = "openai-compatible"
name = "yurenapi"
base_url = "https://example.com/v1"
api_key_env = "FIRSTCODER_API_KEY"
model = "gpt-4.1-mini"
```

### 环境变量

| 变量 | 说明 | 示例值 |
|------|------|--------|
| `FIRSTCODER_PROVIDER` | 默认 provider 类型 | "openai-compatible" |
| `FIRSTCODER_API_KEY` | API 密钥 | "sk-..." |
| `FIRSTCODER_BASE_URL` | API 基础 URL | "https://api.openai.com/v1" |
| `FIRSTCODER_MODEL` | 默认模型 | "gpt-4.1-mini" |

## 扩展性

### 添加新的 Provider

1. 实现 `ChatProvider` 协议
2. 在 `ProviderFactory` 中注册新类型
3. 添加相应的配置和环境变量支持
4. 实现错误处理和重试逻辑

### 自定义 Provider 行为

1. 继承现有 provider 实现
2. 覆盖特定的方法（如 `chat_completion`）
3. 添加自定义的配置选项

### Provider 健康检查

```python
class ProviderHealthChecker:
    def check_health(provider: ChatProvider) -> HealthStatus:
        """检查 provider 是否正常工作"""
        try:
            # 发送一个简单的测试请求
            response = await provider.chat_completion(TestRequest())
            return HealthStatus.HEALTHY
        except Exception as e:
            return HealthStatus.UNHEALTHY(error=e)
```

## 设计决策记录

| 决策 | 理由 |
|------|------|
| 抽象接口隔离 | 使切换模型或厂商无需修改核心逻辑 |
| 支持流式和完整响应 | 满足不同场景需求（实时显示 vs 批量处理） |
| 集中错误处理 | 统一处理各种 provider 相关的异常情况 |
| 配置驱动 | 支持动态切换 provider 和模型 |
| 健康检查机制 | 及时发现 provider 问题，提升用户体验 |
