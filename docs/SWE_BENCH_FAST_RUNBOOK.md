# SWE-bench-fast Runbook

`swe-bench-fast` is an ARM64-friendly SWE-bench evaluator. It scores existing
prediction JSONL files; it does not generate patches.

## Local Binary

This workspace keeps a compiled local binary at:

```bash
.tools/swe-bench-fast/swe-bench-fast
```

The binary was built from `greynewell/swe-bench-fast` and is native `darwin/arm64`.

## Docker

Use Colima's Docker socket:

```bash
export DOCKER_HOST=unix:///Users/x/.colima/default/docker.sock
```

The workspace config is `swe-bench-fast.toml`. The ARM64 registry is:

```toml
arm64_registry = "docker.io/greynewell/swe-bench-arm64"
```

## Dataset

Use `swe-bench-fast`'s ARM64-compatible dataset rows when possible. Example for
the first two smoke instances:

```bash
data/swebench_fast_arm64_two.jsonl
```

## Run

Evaluate two existing FirstCoder predictions:

```bash
DOCKER_HOST=unix:///Users/x/.colima/default/docker.sock \
  .tools/swe-bench-fast/swe-bench-fast run \
  --dataset data/swebench_fast_arm64_two.jsonl \
  --predictions runs/firstcoder_swe_lite_two_predictions.jsonl \
  --workers 1 \
  --timeout 900 \
  --run-id firstcoder-fast-two \
  --output runs/swe_bench_fast_two_report.json
```

## Observed Smoke Result

On this Mac, the first smoke instance resolved in about 66 seconds on the first
run and about 10 seconds after the image was cached.

Two-instance smoke:

```text
astropy__astropy-12907  RESOLVED_FULL
astropy__astropy-14182  RESOLVED_NO
```

