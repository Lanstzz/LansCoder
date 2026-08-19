# LansCoder

> 一个你能从头读到尾的本地编程代理。

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

![LansCoder TUI 界面截图](./assets/tui-screenshot.png)

## 简介

LansCoder 是一个本地运行的 Python 编程代理。它像 Claude Code 或 Aider 一样，能理解你的代码库、编辑文件、运行命令——但它的核心设计目标不是功能堆砌，而是**可理解**。

约 30,000 行 Python 代码，清晰的模块边界，完整的测试覆盖。整个系统足够真实可用，也足够小到你能从头读到尾。

在 Harbor Aider Polyglot 基准测试中达到 96.38% 通过率。

## 核心亮点

- **小，所以可读** — ~30k 行 Python，不是 570k 行 TypeScript。每个模块的职责一目了然。
- **为学习而生** — 架构分层严格，依赖规则明确。适合学习编程代理的内部原理、二次开发、面试准备。
- **真实可用** — 40+ 内置工具、多模型提供商支持、MCP 集成、会话持久化、上下文压缩，不是玩具。
- **写前预览** — 每次文件修改前显示语法高亮 diff，即使在高权限模式下也不跳过。

## 快速上手

```sh
pipx install lanscoder
lanscoder config init
```

编辑配置文件，填写你的 API 密钥：

- **macOS / Linux**：`~/.config/lanscoder/config.toml`
- **Windows**：`C:\Users\<用户名>\.config\lanscoder\config.toml`

示例配置：

```toml
default_model = "openai/gpt-4o"

[providers.openai]
type = "openai-compatible"
base_url = "https://api.openai.com/v1"
api_key = "sk-xxx"
api_key_env = "OPENAI_API_KEY"
```

然后在工作目录启动：

```sh
lanscoder
```

开发环境：

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## 功能概览

| 能力 | 说明 |
|------|------|
| 编程代理 | 理解代码、编辑文件、运行 Shell 命令，支持 40+ 内置工具 |
| 多模型 | 兼容 OpenAI 和 Anthropic，模型可在会话中热切换 |
| 权限控制 | 标准/宽松/放行三种模式，每次文件操作前展示 diff |
| TUI 界面 | Textual 构建的终端界面，实时展示推理、工具调用、结果 |
| 会话管理 | 创建、恢复、分支、分享会话，JSONL 持久化 |
| 上下文压缩 | 四级压缩管线（L1–L4），控制长对话的 token 消耗 |
| 后台子代理 | 子代理可独立在后台运行，完成后通知 |
| MCP 集成 | 连接外部工具服务器 |

## 架构速览

```
lanscoder/
├── app/           Textual TUI 界面
├── agent/         代理循环与编排
├── providers/     模型提供商适配器
├── tools/         工具注册与执行（40+ 工具）
├── permissions/   权限策略与授权
├── context/       事件日志与上下文管理
├── session/       会话生命周期
├── mcp/           MCP 协议集成
├── memory/        跨会话持久记忆
├── skills/        本地技能文件系统
├── config/        TOML 配置加载
└── utils/         工具函数
```

## 适合谁

- 想**深入理解编程代理内部原理**的开发者
- 想基于 Python 编程代理**做二次开发**的人
- 准备面试、需要**能讲清楚架构**的项目
- 想在本地**实验不同模型**的 AI 爱好者

## 参与贡献

欢迎提交 Issue 和 PR。测试覆盖大部分核心模块，修改前请确保测试通过。

## 许可证

MIT