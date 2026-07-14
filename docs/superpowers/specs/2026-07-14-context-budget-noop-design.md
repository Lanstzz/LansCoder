# Context Budget and No-Op Compaction Design

## Scope

Improve the existing context pipeline without changing checkpoint persistence or adding a user-input artifact tool.

## Design

FirstCoder will calculate automatic-compaction pressure from the provider-facing request: system prefix, projected messages, tool definitions, and reserved output tokens. The existing tail-only triggers remain available for large tool-result and message-count guards.

When L1-L3 reports an unchanged, previously seen context fingerprint, the manager will return `skipped_no_effect` and will not persist a `compaction_completed` event. A materially changed session produces a new fingerprint and may be compacted again.

After L4 writes a checkpoint, the manager will evaluate the rebuilt effective context against the target. If it remains over budget, it returns `still_over_budget` rather than success, allowing the existing prompt-too-long recovery path to stop safely.

## Non-goals

- No input-artifact storage or `read_user_input` tool.
- No change to JSONL message persistence or checkpoint/tail projection.
- No automatic multi-pass L4 loop in this phase.
