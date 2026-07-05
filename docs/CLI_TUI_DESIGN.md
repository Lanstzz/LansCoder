# CLI / TUI Design

## 概述

FirstCoder 提供两种交互方式：
1. **Textual TUI** — 交互式终端界面，展示完整的 agent 状态
2. **CLI** — 命令行接口，适合脚本化和自动化

TUI 和 CLI 共享相同的底层 agent 运行时，区别仅在于用户界面和交互模式。

## 核心组件

### App Factory

```python
class AppFactory:
    def create_app(config, runner, session_manager, skill_loader):
        """创建 Textual TUI 应用实例"""
        return FirstCoderApp(...)
    
    def create_cli(config, runner, session_manager, skill_loader):
        """创建 CLI 入口"""
        return FirstCoderCLI(...)
```

**职责**：集中创建应用实例，解耦配置和运行时依赖。

### Runtime Assembly

```python
class RuntimeAssembly:
    def assemble(config) -> AgentChatRunner:
        """组装完整的 agent 运行时"""
        provider = self._create_provider(config)
        tools = self._create_tools(config)
        permissions = self._create_permissions(config)
        skills = self._create_skills(config)
        session = self._create_session(config)
        context = self._create_context(session)
        loop = self._create_agent_loop(provider, tools, permissions, skills, context, session)
        return AgentChatRunner(loop)
```

**关键设计**：
- 所有组件通过依赖注入组装
- 配置优先级：CLI 参数 > 环境变量 > 项目配置 > 全局配置 > 默认值
- 每个组件都有对应的 factory 方法，便于测试和扩展

### TUI State Management

```python
class TUIState:
    session_id: str
    provider_name: str
    permission_mode: str
    activity_state: ActivityState
    current_tool_calls: List[ToolCall]
    permission_requests: List[PermissionRequest]
    stream_buffer: str
```

**Activity 状态机**：
```
IDLE -> THINKING -> STREAMING -> TOOL_CALL -> WAITING_PERMISSION -> TOOL_RESULT -> STREAMING -> IDLE
```

## TUI 架构

### 界面布局

```
┌─────────────────────────────────────────────────────────────┐
│ Session: default    Provider: openai    Mode: ask           │
├─────────────────────────────────────────────────────────────┤
│ Activity: [● THINKING]                                      │
├─────────────────────────────────────────────────────────────┤
│ User: 帮我修复这个 bug                                       │
│                                                             │
│ Assistant:                                                  │
│   🔧 正在读取文件...                                        │
│   🛠️  工具调用: read_path(path="bug.py")                   │
│   💭 分析中...                                              │
│   ✅ 已修复 (2 处更改)                                     │
│                                                             │
│ User: 看起来不错，提交吧                                     │
├─────────────────────────────────────────────────────────────┤
│ /sessions  /resume  /compact  /permission  /help            │
└─────────────────────────────────────────────────────────────┘
```

### 关键 Widget

| Widget | 职责 | 位置 |
|--------|------|------|
| `SessionHeader` | 显示当前 session、provider、权限模式 | 顶部 |
| `ActivityIndicator` | 显示 agent 当前活动状态 | 顶部 |
| `ConversationLog` | 展示对话历史和工具调用 | 中央 |
| `PermissionPrompt` | 暂停 agent 等待用户决策 | 底部 |
| `CommandInput` | 接收用户输入或 slash commands | 底部 |
| `StreamBuffer` | 缓冲流式输出，避免频繁刷新 | 内部 |

### 流式输出处理

```python
class StreamHandler:
    def handle_stream(chunk: str):
        """处理来自 provider 的流式响应"""
        # 1. 累积到 buffer
        self.stream_buffer += chunk
        
        # 2. 定期刷新 UI（避免每毫秒刷新）
        if time.time() - self.last_refresh > 0.1:
            self._refresh_ui()
            self.last_refresh = time.time()
    
    def _refresh_ui():
        """刷新对话日志显示"""
        # 使用 Rich 的 Markup 处理工具调用和权限请求的格式化
        conversation_log.update(self._format_conversation())
```

## CLI 架构

### 命令路由

```python
class CLIRouter:
    def route_command(command: str, args: list):
        """路由 CLI 命令到对应的 handler"""
        if command == "config":
            return self._handle_config(args)
        elif command == "session":
            return self._handle_session(args)
        elif command == "permission":
            return self._handle_permission(args)
        elif command == "compact":
            return self._handle_compact(args)
        else:
            # 默认：作为用户消息发送给 agent
            return self._send_to_agent(command)
```

### CLI 命令分类

| 类别 | 命令 | 说明 |
|------|------|------|
| 启动 | `firstcoder` | 启动 TUI |
| 启动 | `firstcoder --tui` | 显式启动 TUI |
| 启动 | `firstcoder --interactive` | 启动行式 REPL |
| 消息 | `firstcoder --message "..."` | 跑一轮用户消息 |
| 项目 | `firstcoder --project <path>` | 指定项目根目录 |
| 数据 | `firstcoder --data-root <path>` | 指定数据目录 |
| Session | `firstcoder --session-id <id>` | 创建或复用 session |
| Provider | `firstcoder --provider <name>` | 覆盖 provider |
| 权限 | `firstcoder --auto-approve` | 自动批准权限请求 |
| 限制 | `firstcoder --max-tool-rounds <n>` | 覆盖工具轮数限制 |

## 配置系统

### 配置层级

```
CLI --provider
> 环境变量 / .env
> 项目 firstcoder.toml
> 全局 ~/.config/firstcoder/config.toml
> 默认值
```

### 配置文件结构

```toml
# 项目级配置 (firstcoder.toml)
model = "yurenapi/gpt-5.5"

[provider]
type = "openai-compatible"
name = "yurenapi"
base_url = "https://example.com/v1"
api_key_env = "FIRSTCODER_API_KEY"

[permissions]
mode = "ask"

[ui]
theme = "default"

[loop_limits]
max_tool_rounds = 30
max_runtime_seconds = 300
max_verifications = 5
```

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FIRSTCODER_API_KEY` | Provider API 密钥 | - |
| `FIRSTCODER_PROVIDER` | Provider 名称 | "openai-compatible" |
| `FIRSTCODER_BASE_URL` | Provider 基础 URL | - |
| `FIRSTCODER_MODEL` | 模型名称 | - |
| `OPENAI_API_KEY` | OpenAI 密钥 | - |
| `DEEPSEEK_API_KEY` | DeepSeek 密钥 | - |
| `ANTHROPIC_API_KEY` | Anthropic 密钥 | - |

## 扩展性

### 添加新的 TUI Widget

1. 继承 `textual.widget.Widget`
2. 实现 `compose()` 方法定义子组件
3. 实现 `on_mount()` 处理初始化
4. 在 `ConversationLog` 中注册新的显示类型

### 添加新的 CLI 命令

1. 在 `CLIRouter` 中添加新的路由分支
2. 实现对应的 handler 方法
3. 更新 `--help` 输出
4. 添加相应的 argparse 参数

### 添加新的 Provider

1. 实现 `ChatProvider` 协议
2. 在 `ProviderFactory` 中注册
3. 添加相应的环境变量和配置选项
4. 测试流式输出和工具调用支持

## 设计决策记录

| 决策 | 理由 |
|------|------|
| TUI 和 CLI 共享运行时 | 避免代码重复，确保行为一致 |
| 流式输出使用 buffer | 减少 UI 刷新频率，提升性能 |
| Activity 状态机 | 明确展示 agent 当前工作状态，增强透明度 |
| 配置分层 | 支持项目级个性化，同时保持全局默认值 |
| Rich Markup 格式化 | 利用 Rich 的富文本支持，提供更好的视觉效果 |
