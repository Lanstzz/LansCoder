# Harbor Evaluation

## What Harbor is

Harbor is an external evaluation runtime for coding agents. A Harbor dataset is
a collection of tasks. Each task provides an instruction, an isolated execution
environment, and a verifier. Harbor resolves the dataset, starts the task
environment, runs the selected agent, invokes the verifier after the agent
exits, and records job and trial artifacts.

LansCoder deliberately does not implement dataset-specific runners. Harbor is
the only benchmark integration maintained by this repository.

## How LansCoder participates

`benchmark.harbor.lanscoder_agent:LansCoderHarborAgent` is an installed-agent
adapter. For each task it stages only `pyproject.toml`, `README.md`, and
`lanscoder/`, creates an isolated agent virtual environment, and runs one
non-interactive `lanscoder --benchmark` turn in Harbor's task directory.

The adapter does not copy `.git`, `venv`, local sessions, `.env`, or other
workspace files. It receives the task instruction but does not inspect verifier
files or inject hidden-test information into the prompt.

## Aider Polyglot feedback mode

The upstream Aider Polyglot benchmark permits one repair turn after the first
test run fails. For an Aider-comparable local run, opt in to the benchmark-only
plugin below. It keeps the first agent turn blind to verifier files, and only
after a real `reward=0` sends the verifier's test output back through the same
LansCoder session. Timeouts, missing reward files, and provider failures do
not receive a repair turn.

For a long-running local suite, classify infrastructure failures separately from `reward=0`: network/provider errors, Docker environment failures, timeouts, and a verifier that fails before writing `reward.txt` do not carry the same interpretation as an implementation failing its tests. The run report documents examples and recovery commands.

```sh
PYTHONPATH="$PWD" venv/bin/harbor run \
  -p .local/harbor-datasets/aider-polyglot \
  -a benchmark.harbor.lanscoder_agent:LansCoderHarborAgent \
  --plugin benchmark.harbor.aider_feedback_plugin:AiderFeedbackPlugin \
  -m gpt-5.6-luna -n 2 -k 1 \
  --ak max_tool_rounds=120 --ak reasoning_effort=high \
  -o benchmark/runs/harbor/aider-polyglot-feedback -y
```

Do not use this plugin for Terminal-Bench or any benchmark whose official
protocol does not explicitly allow test-feedback repair rounds.

## Install Harbor

Install Harbor in LansCoder's development environment:

```sh
venv/bin/python -m pip install 'harbor==0.18.0'
```

Verify the CLI and Docker daemon before running a task:

```sh
venv/bin/harbor --version
docker version
```

## Datasets

Browse published datasets at [Harbor Hub](https://hub.harborframework.com/datasets).
Download a dataset into Harbor's local cache when you want to inspect its task
names and environment definitions:

```sh
venv/bin/harbor dataset download DATASET_NAME --cache
```

The dataset name, task filter, image architecture, and resource requirements are
part of a reproducible run. Inspect them before starting a large job.

## Run one task

Keep the provider key in a host environment variable. The example below maps
that host value into Harbor's agent environment without writing the value into
the repository. Replace the dataset, task, provider, model, and endpoint with
your own values:

```sh
zsh -lic 'export PYTHONPATH="$PWD"; venv/bin/harbor run \
  -d DATASET_NAME \
  -i TASK_NAME \
  -a benchmark.harbor.lanscoder_agent:LansCoderHarborAgent \
  -m Yuren/gpt-5.6-terra \
  -n 1 -k 1 --ak max_tool_rounds=120 --ak reasoning_effort=medium \
  --agent-setup-timeout-multiplier 3 \
  --ae LANSCODER_PROVIDER_NAME=PROVIDER \
  --ae LANSCODER_MODEL=gpt-5.6-terra \
  --ae LANSCODER_BASE_URL=https://provider.example/v1 \
  --ae "LANSCODER_API_KEY=\${LANSCODER_API_KEY}" \
  --ae LANSCODER_DISABLE_GLOBAL_SKILLS=1 \
  -o benchmark/runs/harbor/smoke -y'
```

`-m` records model metadata in Harbor. The `LANSCODER_*` variables configure
the LansCoder process inside the task. Do not add `--upload` unless publishing
results is explicitly intended.

`reasoning_effort` is optional and is passed to LansCoder as a provider-specific
model request field. Whether values such as `low`, `medium`, or `high` are
accepted depends on the selected provider/model.

## Reuse dependencies across trials

By default Harbor gives every trial a fresh container, so LansCoder's Python
dependencies are downloaded again for each task. The adapter installs into a
shared pip/uv cache at `/opt/lanscoder-cache`. Bind-mount a host directory
there with Harbor's `--mounts` so wheels download once and are reused across
trials and concurrent containers:

```sh
mkdir -p "$HOME/.cache/lanscoder-harbor"
venv/bin/harbor run \
  ... \
  --mounts '[{"type":"bind","source":"'"$HOME"'/.cache/lanscoder-harbor","target":"/opt/lanscoder-cache"}]' \
  ...
```

The mount stores downloaded archives only, not the virtual environment: each
trial rebuilds its own venv (`--clear`) so concurrent trials never corrupt a
shared environment. The install step retries the download up to three times
with backoff, so a single flaky fetch does not error the trial. Without the
mount the adapter still runs correctly, using a per-container cache that is
discarded when the container is removed.

## Results

Harbor stores the resolved configuration, trial status, agent logs, verifier
logs, rewards, and timing under the selected jobs directory. Inspect a completed
local run with:

```sh
venv/bin/harbor view benchmark/runs/harbor/smoke
```

A successful dataset download or container start is not a passing result. Use
the trial reward and verifier logs as the completion evidence.

## Windows

Use Docker Desktop in Linux containers mode for normal Harbor task images. Run
the commands from a shell whose working directory is the LansCoder repository,
keep `PYTHONPATH` pointed at that checkout, and start with one task and `-n 1`.
Verify the agent log and verifier result before increasing concurrency.

## SWE-bench 接入注意事项(2026-08-29 冒烟实测)

- **平台**:swebench 基础镜像是 amd64-only,Apple Silicon 上运行需 `export DOCKER_DEFAULT_PLATFORM=linux/amd64`(Docker Desktop Rosetta 模拟)。
- **基础镜像**:本机 daemon 配了 registry mirror(如 daocloud),构建偶发 `failed to fetch anonymous token: EOF`;先 `docker pull swebench/sweb.eval.*:<task>` 本地化再跑。
- **Harbor 0.18.0 汇总表 bug**:task 名含 `__`(如 `psf__requests-1142`)时 `harbor run` 打印汇总表崩溃(`_format_group_title` 拆分 eval key 出错);任务本身已完成,直接读 `jobs_dir/<ts>/result.json` 与 trial `result.json`。
- **会话/压缩数据**:适配器把 session 导出到容器 `/logs/agent/lanscoder-session.jsonl`(含工具调用/CompactionEvent);运行加 `--agent-include-logs '*.jsonl'` 即可抓取(比 `--artifact` 简单)。
- **工作区源码注入(重要)**:容器默认装的是 PyPI `lanscoder-core`,不是工作区代码——必须让 staging 生成 `lanscoder-core` pyproject(打包工作区 `lanscoder/`),否则 `--context-window/--compaction-strategy` 等新参数不生效。已修:适配器 `_stage_local_source` 写 staged pyproject(依赖含 anyio/portalocker/PyYAML/openai/anthropic/mcp/textual/prompt_toolkit/tomlkit/python-dotenv)。
- **最小冒烟命令**:`harbor run -p ~/.cache/harbor/tasks/packages/swe-bench/<task> -a benchmark.harbor.lanscoder_agent:LansCoderHarborAgent -m deepseek/deepseek-v4-flash -n 1 -k 1 --ak max_tool_rounds=120 --agent-setup-timeout-multiplier 3 --ae LANSCODER_PROVIDER_NAME=deepseek --ae LANSCODER_MODEL=deepseek-v4-flash --ae LANSCODER_BASE_URL=https://api.deepseek.com --ae "LANSCODER_API_KEY=\${LANSCODER_API_KEY}" --ae LANSCODER_DISABLE_GLOBAL_SKILLS=1 --mounts '[{"type":"bind","source":"'$HOME'/.cache/lanscoder-harbor","target":"/opt/lanscoder-cache"}]' -o benchmark/runs/harbor/<run> -y`
