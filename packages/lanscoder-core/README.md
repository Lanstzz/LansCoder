# lanscoder-core

Headless SDK of [LansCoder](https://github.com/Lanstzz/LansCoder): L1 `agent_loop`、L2 `Agent`、L3 `create_agent_session`。无 TUI、最小依赖。

> **替代关系**:`lanscoder-core` 与 `LansCoder` 共用同一 import 树(`lanscoder`),二者为替代关系,**不要同时安装**。TUI 应用用户装 `LansCoder`;SDK 集成用户装 `lanscoder-core`。

## 安装

```sh
pip install lanscoder-core
```

最小必装依赖:`anyio` / `portalocker` / `PyYAML`(无 `textual` / `prompt_toolkit` / `tomlkit` / `python-dotenv`)。

真实模型 / MCP 通过 extras 可选安装:

```sh
pip install "lanscoder-core[llm]"   # openai + anthropic
pip install "lanscoder-core[mcp]"   # mcp
```

## 使用

```python
from lanscoder.core import create_agent_session
```

- L3 `create_agent_session` 完整 headless 装配(provider + session + 工具 + context 管理器 + runner)。
- L2 `Agent`(`subscribe / prompt / steer / follow_up / abort`)与 L1 `agent_loop` 亦可直接使用。
- 传输协议 `LlmTransport`(2 方法 + 3 属性)可 duck-typing 接入,不需要 `openai`/`anthropic`。

版本与 `LansCoder` 主包单一事实来源一致:`lanscoder/core/_version.py`。
