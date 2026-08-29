# SDK 示例(`examples/sdk/`)

`lanscoder.core` 是稳定、可对外发布的 SDK 面(不依赖 TUI)。本目录是可运行的 headless 示例,
全程无 TUI、无网络(用 stub transport 代替真实模型)。

## 安装(`lanscoder-core`)

SDK 以独立分发包 **`lanscoder-core`** 发布(dist 名 ≠ import 名,import 保持 `lanscoder`):

```sh
pip install lanscoder-core          # 最小集,无 TUI(anyio / portalocker / PyYAML)
pip install "lanscoder-core[llm]"   # + openai / anthropic
pip install "lanscoder-core[mcp]"   # + mcp
```

`lanscoder-core` 是 headless SDK;完整 TUI 应用 `LansCoder` 是**薄壳**,依赖 `lanscoder-core[llm,mcp]`
+ TUI 侧依赖(**依赖关系而非替代关系**,装 `LansCoder` 自动带上 core),关系说明见
`docs/architecture/03-sdk.md` §2。

## 运行

示例支持两种运行方式:

1. **仓库内开发(未安装)**:从仓库根直接运行,脚本自带仓库根 `sys.path` 引导。
2. **已安装 SDK**:`pip install lanscoder-core` 后把示例文件复制到本地运行
   (示例不在 wheel 内),此时 import 解析到 site-packages 里的已安装包。

```sh
# 从仓库根目录
python examples/sdk/minimal_llm_transport.py
python examples/sdk/headless_l3_session.py
```

## 内容

| 文件 | 演示点 |
|------|--------|
| `stub_provider.py` | 共享 stub `ChatProvider`(无网络、可预置回复,含工具调用回合) |
| `minimal_llm_transport.py` | 最小 `LlmTransport` 接入:duck-typed transport(2 方法 + 3 属性)驱动 L2 `Agent` |
| `headless_l3_session.py` | L3 `create_agent_session` 完整装配:自定义知识工具 + `set_permission_mode` + `tool_event_handler` 审计 + `resume` |

对应验收:SC-7(文档/示例 headless 可运行)由 `tests/test_sdk_examples.py` 以子进程冒烟测试锁定;
SC-2(最小依赖环境全绿)由 `publish-pypi.yml` 的 `minimal-core-deps` job 在发布期锁定。
