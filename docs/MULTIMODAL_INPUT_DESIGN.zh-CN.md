# 多模态输入设计

[English](MULTIMODAL_INPUT_DESIGN.md)

## 范围

FirstCoder 接受用户图片和普通文件附件，但不会把 session 日志变成 provider 专属 blob 仓库。这是一条输入与上下文投影链路，不是 OpenAI Responses API 接入：运行时仍使用 OpenAI Chat Completions-compatible adapter 和 Anthropic Messages adapter。

核心规则很简单：附件字节保存在 session store 下，JSONL 只保存相对路径和元数据，构造 provider 请求时才生成厂商协议数据。

## 用户路径

```text
粘贴路径 / file:// URI / 剪贴板图片
  -> input.attachments 解析并暂存 UserAttachment
  -> TUI 显示 chip；提交时把文本 + 附件交给 AgentLoop
  -> AgentSession 复制字节到 attachments/<session-id>/ 并追加 message part
  -> ContextBuilder 读取合法的已存图片，生成 ContentPart
  -> provider adapter 编码为各自原生图片 block
```

`resolve_paste_attachments` 识别粘贴的路径和 `file://` URI。没有解析到路径时，它可以读取系统剪贴板图片（macOS、安装了相应工具的 Linux、或 Windows PowerShell）。Textual composer 在提交聊天后清空已暂存条目；纯图片提交会补一条很短的默认指令，保证仍形成 user message。

## 限制与文件语义

| 规则 | 当前行为 |
| --- | --- |
| 图片 | 单张最多 20 MiB；识别到的图片成为 image attachment |
| 数量 | 单条消息最多 16 个附件 |
| 小型文本文件 | text-like 文件在 200 KiB 以内会以内联文本 message part 保存 |
| 其它文件 | 保留 session 相对路径和元数据给本地工具；不作为图片 block 发给模型 |
| 剪贴板文件 | 先暂存临时剪贴板文件，发送时再复制进 session 存储 |

“Text-like” 包含 `text/*` 和常见源码、配置、JSON、XML、CSV、shell、日志后缀。能附加文件不代表每个模型都能理解任意二进制格式。

## 持久化表示与安全

`prepare_attachments_for_session` 会把每个已提交附件复制到：

```text
<data-root>/attachments/<session-id>/<sha-prefix>-<safe-filename>
```

对应 user `MessagePart` 保存 kind、filename、media type、字节数、SHA-256、source 和相对 data root 的路径。JSONL 永不存图片 base64。`ContextBuilder` 解析路径前会确认它仍位于 session store root 内；缺失、被篡改或试图越界的元数据会被忽略，而不是读取。这样回放事件无法让 provider 图片输入读取任意本地文件。

## Provider 投影

`ContextBuilder` 保留旧的 `ChatMessage.content` 文本；仅在存在可见图片附件时添加 `content_parts`：

- `OpenAICompatibleProvider` 把文本映射为 `{"type": "text"}`，图片映射为 `{"type": "image_url", "image_url": {"url": "data:<media>;base64,..."}}`。
- `AnthropicProvider` 把文本映射为 text block，图片映射为 `{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}`。

adapter 负责编码图片，但视觉能力仍属于配置的模型和 provider。一次本地成功附加不代表某个远程模型必然看得懂。

## 当前限制

- 尚未实现 OpenAI Responses API。
- PDF 和其它大文件/二进制不会转换成模型原生 file content；session 会为本地工具保留它们的元数据/路径。
- 剪贴板支持依赖宿主机可用的 OS 能力。
- 20 MiB 是单图上限，不能覆盖上游 provider 更小的请求或图片限制。

## 验证

```sh
.venv/bin/python -m pytest tests/test_multimodal_input.py \
  tests/test_context_builder_new.py tests/test_providers.py tests/test_app_tui.py -q
```

重点检查 provider payload、无 base64 的持久化、路径越界拒绝、尺寸/数量限制和 composer 暂存清理。不要使用真实 API key：fake provider client 可确定性验证契约。
