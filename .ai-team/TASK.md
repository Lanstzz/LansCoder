# Current Task

- ID: `TASK-003`
- Title: `对外发布:lanscoder-core 独立分发包(D1=A)+ 发布流程`
- Status: `planning`
- Owner: `Lanster`
- Next owner: `Lanster`

## Goal

把 `lanscoder.core` 发布为独立分发包 `lanscoder-core`(P4 落地):SDK 用户 `pip install lanscoder-core` 后 `from lanscoder.core import ...` 即用,最小依赖、无 TUI;同时补齐发布流程(CHANGELOG、双 dist CI、tag 发布)。

- **D1=A 独立分发包**:dist 名 `lanscoder-core`,import 名保持 `lanscoder`(dist 名与 import 名解耦)。
- **依赖裁剪**:`lanscoder-core` 必装依赖仅 `anyio` / `portalocker` / `PyYAML`(实测足迹);`openai`/`anthropic`/`mcp` 走 extras;排除 `textual`/`prompt_toolkit`/`tomlkit`/`python-dotenv`。
- **发布流程**:一个 tag 同时构建并发布 `LansCoder`(TUI 应用)+ `lanscoder-core`(SDK)两个 wheel;版本单一事实来源 `_version.py`。

## Acceptance scenarios

- [ ] **SC-1 (独立 wheel 可安装可 import)**: Given 干净 venv,When `pip install lanscoder-core`,Then `from lanscoder.core import create_agent_session` 成功且 `pip show` 不含 textual。
- [ ] **SC-2 (无 TUI 可验证)**: Given 最小依赖环境,When 运行契约测试 + SDK 示例冒烟 + `test_core_import_does_not_pull_tui`,Then 全绿。
- [ ] **SC-3 (双 dist 构建一致)**: When CI `python -m build`,Then 同时产出 `LansCoder` + `lanscoder-core` 两个 wheel,版本都等于 `_version.py`,`twine check` 绿。
- [ ] **SC-4 (tag 发布)**: Given push `vX.Y.Z`,When 触发发布流程,Then tag 与两个 dist 版本一致;Test PyPI 演练通过(真实 PyPI 上传人工确认)。
- [ ] **SC-5 (文档)**: Given CHANGELOG + 安装/发布文档,When 审阅,Then SDK 安装、extras、替代关系(不与 `LansCoder` 同时安装)说明齐全。
- [ ] **SC-6 (全量门禁)**: When 运行全量 `pytest` + `ruff check .` + `node .ai-team/check.mjs --base origin/main`,Then 全绿。

## Invariants

- `core` 永不 import `app`;`agent` 永不 import `core`;不产生新环。
- 不改变 `lanscoder.core` 既有 API 与 `__all__`(契约测试继续钉死;本任务零 core 代码语义改动)。
- 不改变 TUI 行为;主包 `LansCoder` 发布形态不变。
- `lanscoder-core` 与 `LansCoder` 同 import 树(`lanscoder`),为替代关系,不得同时安装。
- 版本单一事实来源 `lanscoder/core/_version.py`;双 dist 版本必须一致。

## Decisions

- **D1(已定,用户)** 独立分发包 `lanscoder-core`;import 名保持 `lanscoder`。
- **D2(本 spec 推荐,待评审)** 依赖裁剪:必装 `anyio` / `portalocker` / `PyYAML`;extras `[llm]` = openai + anthropic、`[mcp]` = mcp;排除 `textual`/`prompt_toolkit`/`tomlkit`/`python-dotenv`;只裁剪依赖,不裁剪模块树(同一 `lanscoder` 包)。
- **D3(本 spec 推荐,待评审)** 单一版本号,双 dist 同步(`_version.py` 唯一来源;一个 tag 发两包;core 稳定到可独立节拍时再拆)。
- **D4(已定,用户)** 验收形态取第一个:`pip install lanscoder-core` 后即用 + 无 TUI 可验证 + 契约/示例/CI 全绿。
- **D5(本 spec 推荐,待评审)** 双 dist 构建:独立子项目 `packages/lanscoder-core/pyproject.toml`(`package-dir` 指回仓库根);CI 对两个 dist 分别 build + twine check + upload。

## Completed

- 2026-08-29 发布规划讨论拍板 D1/D4(记录于本 TASK 与 `docs/superpowers/specs/2026-08-29-sdk-external-release-design.md`)。
- 依赖审计(2026-08-29 实测):`import lanscoder.core` 与默认 L3 装配+一次回合,第三方足迹仅 `anyio`/`portalocker`/`PyYAML`;`providers/base.py` 与 `tools/builtin.py` 惰性 import openai/anthropic/mcp;`memory/models.py` 硬 import yaml。

## Pending

- [ ] Task 0(spec + TASK,本 PR):只写文档,不动代码;评审通过后 `planning → active`。
- [ ] Step 1:`packages/lanscoder-core` 打包骨架 + 本地 build 双 wheel + 干净 venv 安装冒烟(SC-1、SC-2 前半)。
- [ ] Step 2:CI 双 dist 发布流程 + 最小依赖验证 job(SC-2 后半、SC-3、SC-4)。
- [ ] Step 3:CHANGELOG + 安装/发布文档 + Test PyPI 演练(SC-5、SC-6)。

## Next step

评审并合并本 Task 0 spec PR;通过后 Status `planning → active`,进入 Step 1(打包骨架,每 Step 独立 PR)。

## Verification

- planning 阶段只写文档,无代码验证;依赖审计证据见 spec §3(2026-08-29 实测命令)。
- [ ] SC-1..SC-6 以真实命令退出码记录(实现 Step 中逐项勾选)。

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: TASK-003 启动(planning);D1=独立分发包、D4=第一个验收形态已定;D2(依赖裁剪)/D3(单一版本号)/D5(子项目双 dist)为本 spec 推荐项;依赖审计已完成(core 足迹仅 anyio/portalocker/PyYAML);Task 0 只写文档,评审通过后进入 Step 1。
