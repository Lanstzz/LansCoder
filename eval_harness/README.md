# LansCoder evaluation harness

`eval_harness/` is the offline-first evaluation layer for LansCoder. It is an
observer and driver of the public runtime API: `lanscoder/` never imports it.
An executable case is separate from the fresh trace it produces, so one
portable case can support repeatable golden checks and later live-model runs.

## First offline run

Install the repository's development dependencies, then choose a **new**,
untracked output directory outside the repository. This first case uses a
scripted provider, disables process-level sockets, and never calls a model or
external service.

```sh
venv/bin/python -m eval_harness run \
  --case eval_harness/cases/offline/write_greeting.json \
  --output /tmp/lanscoder-eval-write-greeting
```

The command writes:

- `trace.jsonl`: ordered, versioned facts from the runtime, provider tape,
  tool lifecycle, artifact diff, final delivery, and an integrity footer.
- `scorecard.json`: five deterministic hard gates (`trace`, `artifact`,
  `recovery`, `security`, and `delivery`) plus independent metrics.
- `artifacts/`: the copied fixture workspace after the agent run.

The command exits non-zero when a hard gate fails. It refuses an existing
output directory so each invocation creates an auditable fresh trace.

## Fault and recovery probes

Offline manifests may add deterministic probes without changing runtime turn
semantics:

```json
{
  "provider_tape": [{"fault": "timeout"}, {"content": "Recovered.", "tool_calls": []}],
  "tool_faults": {"call-id": "timeout"},
  "interrupt_after_tool_calls": 1,
  "enable_compaction": true
}
```

Supported provider faults are `malformed_response`, `timeout`,
`prompt_too_long`, and `network_error`; tool faults are `timeout`, `failure`,
and `interrupt`;
`failure`. Faults are recorded as portable `provider_error`, tool result, or
`context_compaction` facts. The recovery gate checks that tool starts are
closed by an end or an interruption and reports provider/tool/compaction
categories in the scorecard. The compaction option is an explicitly labelled
deterministic no-op probe in the harness; it is not a substitute for a real
model compaction run.

## Golden replay

Timestamps, elapsed time, provider request IDs, and trace digests are not
semantic golden inputs. Produce stable projections before comparing two fresh
runs:

```sh
venv/bin/python -m eval_harness canonicalize \
  --trace /tmp/lanscoder-eval-write-greeting/trace.jsonl \
  --output /tmp/lanscoder-eval-write-greeting/canonical.json
```

`interaction_replay` is the implemented deterministic mode: an explicit
provider tape drives the current `lanscoder.core.agent_loop`. The case schema
also reserves `fresh_model` for the later canary/statistical mode; it is not an
offline gate and is intentionally not executed by this first runner.

Compare a fresh scorecard with a baseline; a lost passing hard gate is a
regression and numeric metrics are reported as deltas:

```sh
venv/bin/python -m eval_harness compare \
  --baseline /tmp/baseline/scorecard.json \
  --current /tmp/current/scorecard.json
```

`run` also accepts `--baseline` and embeds the comparison in its scorecard.

## Case and trace boundary

`cases/` contains portable JSON manifests and `fixtures/` contains small,
reviewable seed workspaces. A manifest has an ID, schema version, mode, prompt,
fixture path, provider tape, expected artifacts, and expected final delivery.
`trace.jsonl` records facts for one run and must not be reused as a case.

Before writing a portable trace, the recorder redacts registered private
values, API-key-like values, bearer tokens, and absolute paths using stable
hash placeholders. System prompts, tool descriptions, tool arguments, and tool
result bodies are represented only by redacted placeholders, field summaries,
and fingerprints. Do not put real user input, API keys, private source, or
unredacted tool output in a manifest, fixture, trace, or scorecard. The future
history extractor will keep any original material only in a repository-external
encrypted capsule.

## Harbor

Harbor remains an optional external regression/canary adapter, now located in
[`eval_harness/harbor/`](harbor/README.md). It is not used by the offline
smoke or golden gate and therefore does not add a Harbor dependency to the
runtime.
