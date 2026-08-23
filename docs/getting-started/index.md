# 快速开始

本指南带你完成安装、配置并首次运行 LansCoder，全程约 5 分钟。

- [系统要求](#系统要求)
- [安装](#安装)
- [初始化配置](#初始化配置)
- [配置模型提供方](#配置模型提供方)
- [启动](#启动)
- [非交互用法](#非交互用法)
- [验证配置](#验证配置)
- [常用斜杠命令](#常用斜杠命令)
- [下一步](#下一步)

---

## 系统要求

- **Python 3.11 或更高版本**（安装脚本会依次探测 `python3.13`、`python3.12`、`python3.11`、`python3`）。
- 支持 macOS、Linux 以及 Windows（Git Bash / WSL2）。

## 安装

### 方式一：一键安装脚本

macOS / Linux / Windows Git Bash：

```sh
curl -sSL https://raw.githubusercontent.com/Lanstzz/LansCoder/main/install.sh | bash
```

脚本会检查 Python 版本、安装缺失的 `pipx`、安装或升级 LansCoder，并在没有配置时自动执行 `lanscoder config init`。

### 方式二：pipx 安装

```sh
pipx install lanscoder
lanscoder config init
```

> 为什么用 pipx：LansCoder 是命令行应用，pipx 会把它的可执行文件放进 PATH，同时把依赖隔离在独立环境里，避免污染系统 Python。

### 方式三：源码安装（开发）

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

源码安装会同时拉入开发依赖（pytest、trio、ruff、black），适合需要阅读或修改代码的场景。

## 初始化配置

```sh
lanscoder config init
```

这会在 **全局配置路径** 生成一份起始配置文件：

- **macOS / Linux**：`~/.config/lanscoder/config.toml`（若设置了 `XDG_CONFIG_HOME`，则为 `$XDG_CONFIG_HOME/lanscoder/config.toml`）
- **Windows**：`C:\Users\<用户名>\.config\lanscoder\config.toml`

如果文件已存在，`config init` 会拒绝覆盖；需要强制重建时使用：

```sh
lanscoder config init --force
```

生成的起始配置内容如下：

```toml
# LansCoder global configuration. Project-level "./lanscoder.toml" can override it.
default_model = "deepseek/deepseek-v4-flash"

[providers.deepseek]
type = "openai-compatible"
base_url = "https://api.deepseek.com"
api_key = ""
parallel_tool_calls = true

[models."deepseek/deepseek-v4-flash"]
label = "DeepSeek V4 Flash"
context_window = 1000000

[permissions]
mode = "ask"

[ui]
theme = "default"
```

### 全局配置与项目配置

LansCoder 有两层配置，**项目配置会覆盖全局配置**：

| 层级 | 路径 | 作用 |
|------|------|------|
| 全局 | `~/.config/lanscoder/config.toml` | 所有项目的默认值 |
| 项目 | `<项目根>/lanscoder.toml` | 只对当前项目生效，可覆盖全局 |

想确认当前生效的配置文件路径，运行：

```sh
lanscoder config path
# global: /home/you/.config/lanscoder/config.toml
# project: /home/you/your-project/lanscoder.toml
```

## 配置模型提供方

打开全局配置文件，填入 API key，并确保 `default_model` 指向已配置的模型引用。

### 配置结构

- `default_model`：默认模型引用，格式为 `provider/model`。
- `[providers.<provider>]`：模型服务提供方，包含 `type`、`base_url`、`api_key`、`parallel_tool_calls` 等。
- `[models."<provider>/<model>"]`：具体模型的元信息，如 `label`、`context_window`。

`type` 支持两类取值：

- `openai-compatible`（或 `custom`）：任意兼容 OpenAI 接口的服务，必须提供 `base_url`。
- 预置 provider 名称：`openai`、`deepseek`、`qwen`、`moonshot`、`zhipu`、`openrouter`、`ollama`、`anthropic`。使用预置名称时可以不写 `base_url`（会自动用厂商默认地址），例如：

```toml
default_model = "qwen/qwen-plus"

[providers.qwen]
type = "qwen"
api_key = "sk-xxx"

[models."qwen/qwen-plus"]
label = "Qwen Plus"
context_window = 131072
```

预置 provider 的默认模型与地址：

| provider | 默认模型 | 默认 base_url |
|----------|---------|---------------|
| openai | `gpt-4.1-mini` | 官方 API |
| deepseek | `deepseek-v4-flash` | `https://api.deepseek.com` |
| qwen | `qwen-plus` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| moonshot | `moonshot-v1-8k` | `https://api.moonshot.cn/v1` |
| zhipu | `glm-4-flash` | `https://open.bigmodel.cn/api/paas/v4` |
| openrouter | `openai/gpt-4.1-mini` | `https://openrouter.ai/api/v1` |
| ollama | `qwen2.5-coder:7b` | `http://localhost:11434/v1` |
| anthropic | `claude-sonnet-4-5` | 官方 API |

### 权限模式

LansCoder 有三种权限模式，控制工具调用的授权方式：

- `standard`：标准模式，写文件、执行命令等敏感操作前逐一询问（默认）。
- `aggressive`：宽松模式，项目根目录内常见验证命令与普通写入自动放行。
- `bypass`：放行模式，不询问直接执行（谨慎使用）。

默认模式为 `standard`，可在会话内通过 `/mode <standard|aggressive|bypass>` 切换。

### UI 主题

起始配置中的 `[ui] theme = "default"` 段为预留配置，当前版本内置深色主题，无需修改。

## 启动

在项目目录下直接运行：

```sh
lanscoder
```

无参数且处于终端时，LansCoder 会启动 **Textual TUI**：

- 输入区位于底部，直接输入你的需求并回车。
- 顶部显示当前 provider / model 与权限模式。
- `Ctrl+C` 复制当前输出或退出；`Esc` 中断正在进行的回合。

启动后，LansCoder 会在项目目录创建 `.lanscoder/` 目录，用于存放会话数据（JSONL 转录、权限授权记录、模型选择状态等）。

### 第一个回合

输入类似下面的指令：

```
读一下这个项目的 README，然后用三句话总结它的定位。
```

LansCoder 会展示推理过程、工具调用与结果；当它需要写文件或执行命令时，会弹出权限确认，按提示选择即可。

## 非交互用法

### 交互式 REPL（不带 TUI）

```sh
lanscoder --interactive
```

逐行输入指令，适合 SSH 会话或无法渲染 TUI 的环境。`/exit` 或 `/quit` 退出。

### 单次回合

```sh
lanscoder --message "这个仓库的测试怎么跑？"
```

或通过 stdin 传入：

```sh
echo "总结一下这个项目" | lanscoder
```

单次回合会把模型回复打印到 stdout，适合脚本集成。

### 常用启动参数

| 参数 | 说明 |
|------|------|
| `--project <dir>` | 项目根目录（默认当前目录），决定工具与 AGENTS.md 的生效范围 |
| `--data-root <dir>` | 会话数据目录（默认 `<project>/.lanscoder`） |
| `--model <provider/model>` | 指定本次使用的模型 |
| `--session-id <id>` | 创建或复用指定会话 |
| `--resume-session` | 恢复指定会话而不是新建 |
| `--auto-approve` | 权限确认自动回答 allow_once |
| `--max-tool-rounds <n>` | 覆盖单回合工具调用轮次上限 |
| `--reasoning-effort <level>` | 传给模型请求的推理强度（按 provider 支持情况） |

## 验证配置

```sh
lanscoder config show
```

输出当前生效的 provider、模型、base_url、并行工具调用开关与实际加载的配置文件路径（不会打印密钥）。例如：

```
provider: deepseek
model: deepseek/deepseek-v4-flash
default_model: deepseek/deepseek-v4-flash
models:
  - deepseek/deepseek-v4-flash (DeepSeek V4 Flash)
base_url: https://api.deepseek.com
parallel_tool_calls: true
config_files:
  - /home/you/.config/lanscoder/config.toml
```

## 常用斜杠命令

在 TUI 或 REPL 输入 `/help` 可随时查看完整命令列表。常用命令：

| 命令 | 作用 |
|------|------|
| `/model <provider/model>` | 切换当前模型 |
| `/mode <standard\|aggressive\|bypass>` | 切换权限模式 |
| `/new`、`/resume`、`/sessions` | 新建、恢复、列出会话 |
| `/fork` | 复制当前会话为新分支 |
| `/recall` | 回退对话到之前的某一轮 |
| `/context`、`/compact` | 查看上下文状态、手动压缩 |
| `/memory` | 查看持久记忆 |
| `/skills`、`/skill-use <name>` | 浏览与引用本地技能 |
| `/mcp list` | 查看 MCP 服务器状态 |

## 下一步

- [能力指南](../guides/) — 权限模型、上下文压缩、会话管理
- [架构](../architecture/) — 分层设计与依赖规则
- [FAQ](../faq.md) — 常见问题
- [开发](../development.md) — 本地开发与贡献
