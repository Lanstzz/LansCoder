# AtCoder Benchmark

This runner evaluates FirstCoder on AtCoder tasks with official judging.

It does not download hidden tests. It uses `atcoder-cli` (`acc`) to create task folders with official samples, runs `oj test` locally, and optionally calls `oj submit` so AtCoder returns the real verdict from hidden tests.

## Setup

```sh
.venv/bin/python -m pip install online-judge-tools atcoder-cli
oj login https://atcoder.jp/
```

Configure FirstCoder provider environment variables before running.

## Dry Run

```sh
.venv/bin/python benchmark/atcoder/runner.py \
  --tasks benchmark/atcoder/tasks.sample.jsonl \
  --workdir runs/atcoder-smoke \
  --summary-out runs/atcoder-smoke-summary.json \
  --max-tasks 1
```

## Submit For Real Verdict

This submits code to AtCoder.

```sh
.venv/bin/python benchmark/atcoder/runner.py \
  --tasks benchmark/atcoder/tasks.sample.jsonl \
  --workdir runs/atcoder-smoke \
  --summary-out runs/atcoder-smoke-summary.json \
  --max-tasks 1 \
  --submit
```
