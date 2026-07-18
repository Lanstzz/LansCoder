# Multimodal Input Design

[中文版本](MULTIMODAL_INPUT_DESIGN.zh-CN.md)

## Scope

FirstCoder accepts user images and ordinary file attachments without turning the
session log into a provider-specific blob store. The feature is an input and
context-projection path, not an OpenAI Responses API integration: the runtime
continues to use OpenAI Chat Completions-compatible adapters and the Anthropic
Messages adapter.

The central rule is simple: persist attachment bytes under the session store,
persist only relative paths and metadata in JSONL, and produce provider wire
data only while building a request.

## User Path

```text
paste path / file:// URI / clipboard image
  -> input.attachments resolves and stages UserAttachment
  -> TUI shows a chip; submit sends text + attachments to AgentLoop
  -> AgentSession copies bytes to attachments/<session-id>/ and appends message parts
  -> ContextBuilder reads valid stored images as ContentPart
  -> provider adapter encodes its native image blocks
```

`resolve_paste_attachments` recognizes pasted paths and `file://` URIs. If no
path is resolved, it can read an image from the OS clipboard (macOS, supported
Linux clipboard utilities, or Windows PowerShell). The Textual composer removes
staged items after it submits the chat turn. A textless image submission uses a
small default instruction so it still forms a user message.

## Limits and File Semantics

| Rule | Current behavior |
| --- | --- |
| Images | one image is at most 20 MiB; recognized images are image attachments |
| Count | at most 16 attachments per message |
| Small text files | text-like files through 200 KiB are inlined as text message parts |
| Other files | retained as a session-relative path and metadata for local tools; not sent as an image block |
| Clipboard files | a temporary clipboard file is staged first, then copied into session storage on send |

“Text-like” includes `text/*` plus common source, configuration, JSON, XML,
CSV, shell, and log suffixes. File acceptance does not claim that every model
can consume every binary format.

## Durable Representation and Safety

`prepare_attachments_for_session` copies every submitted item below:

```text
<data-root>/attachments/<session-id>/<sha-prefix>-<safe-filename>
```

The corresponding user `MessagePart` records kind, filename, media type, byte
size, SHA-256, source, and a path relative to the data root. JSONL never stores
image base64. `ContextBuilder` resolves a path only after checking that it stays
below the session store root; missing, changed, or traversal-like metadata is
ignored rather than read. This makes a replayed event incapable of requesting
an arbitrary local file as provider image input.

## Provider Projection

`ContextBuilder` preserves the legacy `ChatMessage.content` text and adds
`content_parts` only when a visible image attachment is available:

- `OpenAICompatibleProvider` maps text to `{"type": "text"}` and images to
  `{"type": "image_url", "image_url": {"url": "data:<media>;base64,..."}}`.
- `AnthropicProvider` maps text to text blocks and images to
  `{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}`.

The adapters encode images, but vision remains a capability of the configured
model and provider. A successful local attachment operation is not evidence
that a particular remote model will understand it.

## Current Limitations

- OpenAI Responses API is not implemented.
- PDFs and other large/binary files are not transformed into model-native file
  content; the session retains their metadata/path for local tools.
- Clipboard support depends on OS facilities available on the host.
- The 20 MiB limit is per image and does not override an upstream provider's
  smaller request or image limit.

## Verification

```sh
.venv/bin/python -m pytest tests/test_multimodal_input.py \
  tests/test_context_builder_new.py tests/test_providers.py tests/test_app_tui.py -q
```

Focus on provider payload assertions, persistence without base64, traversal
rejection, size/count limits, and staged-composer cleanup. Do not use a real API
key: fake provider clients verify the contract deterministically.
