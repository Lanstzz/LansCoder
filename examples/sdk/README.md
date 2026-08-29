# SDK 示例(`examples/sdk/`)

`lanscoder.core` 是稳定、可对外发布的 SDK 面(不依赖 TUI)。本目录是可运行的 headless 示例,
全程无 TUI、无网络(用 stub transport 代替真实模型)。

## 运行

```sh
# 从仓库根目录
python examples/sdk/minimal_llm_transport.py
python examples/sdk/headless_l3_session.py
```

或直接执行任意脚本(脚本内已自带仓库根目录 `sys.path` 引导,未安装包也能跑)。

## 内容

| 文件 | 演示点 |
|------|--------|
| `stub_provider.py` | 共享 stub `ChatProvider`(无网络、可预置回复,含工具调用回合) |
| `minimal_llm_transport.py` | 最小 `LlmTransport` 接入:duck-typed transport(2 方法 + 3 属性)驱动 L2 `Agent` |
| `headless_l3_session.py` | L3 `create_agent_session` 完整装配:自定义知识工具 + `set_permission_mode` + `tool_event_handler` 审计 + `resume` |

对应验收:SC-7(文档/示例 headless 可运行)由 `tests/test_sdk_examples.py` 以子进程冒烟测试锁定。
