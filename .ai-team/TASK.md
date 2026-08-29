# Current Task

- ID: `TASK-003`
- Title: `对外发布:lanscoder-core 独立分发包(D1=A)+ 发布流程`
- Status: `active`
- Owner: `Lanster`
- Next owner: `Lanster`

## Goal

把 `lanscoder.core` 发布为独立分发包 `lanscoder-core`(P4 落地):SDK 用户 `pip install lanscoder-core` 后 `from lanscoder.core import ...` 即用,最小依赖、无 TUI;同时补齐发布流程(CHANGELOG、双 dist CI、tag 发布)。

- **D1=A 独立分发包**:dist 名 `lanscoder-core`,import 名保持 `lanscoder`(dist 名与 import 名解耦)。
- **依赖裁剪**:`lanscoder-core` 必装依赖仅 `anyio` / `portalocker` / `PyYAML`(实测足迹);`openai`/`anthropic`/`mcp` 走 extras;排除 `textual`/`prompt_toolkit`/`tomlkit`/`python-dotenv`。
- **发布流程**:一个 tag 同时构建并发布 `LansCoder`(TUI 应用)+ `lanscoder-core`(SDK)两个 wheel;版本单一事实来源 `_version.py`。
- **结构无冲突**:`lanscoder-core` 是 `lanscoder/` 导入树的**唯一持有者**;`LansCoder` 为薄壳(依赖 `lanscoder-core[llm,mcp]` + TUI 侧依赖,不再自带 `lanscoder/` 文件),两个 dist 文件零重叠(D7)。

## Acceptance scenarios

- [x] **SC-1 (独立 wheel 可安装可 import)**: Given 干净 venv,When `pip install lanscoder-core`,Then `from lanscoder.core import create_agent_session` 成功且 `pip show` 不含 textual。
- [x] **SC-2 (无 TUI 可验证)**: Given 最小依赖环境,When 运行契约测试 + SDK 示例冒烟 + `test_core_import_does_not_pull_tui`,Then 全绿。
- [ ] **SC-3 (双 dist 构建一致)**: When CI 分别 build 两 dist,Then 同时产出 `LansCoder` + `lanscoder-core` 两个 wheel,版本都等于 `_version.py`,`twine check` 绿。
- [ ] **SC-4 (tag 发布)**: Given push `vX.Y.Z`,When 触发发布流程,Then tag 与两个 dist 版本一致;Test PyPI 演练通过(真实 PyPI 上传人工确认)。
- [ ] **SC-5 (文档)**: Given CHANGELOG + 安装/发布文档,When 审阅,Then SDK 安装、extras、`LansCoder` 与 `lanscoder-core` 的依赖关系(薄壳,非替代关系)说明齐全。
- [ ] **SC-6 (全量门禁)**: When 运行全量 `pytest` + `ruff check .` + `node .ai-team/check.mjs --base origin/main`,Then 全绿。
- [x] **SC-7 (结构无冲突)**: Given `pip install LansCoder`,Then 自动装 `lanscoder-core` + TUI 依赖;`lanscoder` 命令可用;两个 dist 的 RECORD 文件交集为空;`pip uninstall LansCoder` 后 `import lanscoder.core` 仍可用。

## Invariants

- `core` 永不 import `app`;`agent` 永不 import `core`;不产生新环。
- 不改变 `lanscoder.core` 既有 API 与 `__all__`(契约测试继续钉死;零 core 代码语义改动;core 的 package-data 只新增 `app/*.tcss` 数据文件)。
- 不改变 TUI 行为;`LansCoder` 由全量应用包改为**薄壳**(不再自带 `lanscoder/` 树,依赖 `lanscoder-core[llm,mcp]` + TUI 侧依赖),文件零重叠。
- `lanscoder-core` 与 `LansCoder` 为**依赖关系**(后者依赖前者),不再互为替代;同 import 树(`lanscoder`)由 core 唯一持有。
- 版本单一事实来源 `lanscoder/core/_version.py`;双 dist 版本必须一致(root 薄壳的版本与 `lanscoder-core==` pin 硬编码,由 tag/CI 校验强制与 `_version.py` 相等)。

## Decisions

- **D1(已定,用户)** 独立分发包 `lanscoder-core`;import 名保持 `lanscoder`。
- **D2(已定,PR #11 合入)** 依赖裁剪:必装 `anyio` / `portalocker` / `PyYAML`;extras `[llm]` = openai + anthropic、`[mcp]` = mcp;排除 `textual`/`prompt_toolkit`/`tomlkit`/`python-dotenv`;只裁剪依赖,不裁剪模块树(同一 `lanscoder` 包)。
- **D3(已定,PR #11 合入)** 单一版本号,双 dist 同步(`_version.py` 唯一来源;一个 tag 发两包;core 稳定到可独立节拍时再拆)。
- **D4(已定,用户)** 验收形态取第一个:`pip install lanscoder-core` 后即用 + 无 TUI 可验证 + 契约/示例/CI 全绿。
- **D5(已定,PR #11 合入,实现细节被 D6/D7 修订)** 双 dist 构建:独立子项目 `packages/lanscoder-core/pyproject.toml`(`package-dir` 指回仓库根);CI 对两个 dist 分别 build + twine check + upload。
- **D6(已定,Step 1 实测发现)** core 子项目 **wheel-only** 构建:`python -m build --wheel`。setuptools sdist 不收录项目根(`packages/lanscoder-core`)之外的文件,`python -m build`(sdist+wheel)产出的 sdist 缺 `lanscoder/` 源码、wheel-from-sdist 阶段失败;SC-3 只要求两个 wheel,故 CI 对 core 子项目用 `--wheel`,root 保持全量 `python -m build`。
- **D7(已定,用户,结构性消除)** `lanscoder-core` 是 `lanscoder/` 树唯一持有者;`LansCoder` 改薄壳:不再自带 `lanscoder/` 文件,`dependencies = ["lanscoder-core[llm,mcp]", "textual", "prompt_toolkit", "tomlkit", "python-dotenv"]`,`[tool.setuptools] packages = []`,保留 `[project.scripts] lanscoder = "lanscoder.cli:main"`。理由:实测 pip 不检测跨 dist 文件重叠(`LansCoder` 与 `lanscoder-core` 约 215 个 `lanscoder/` 文件重叠),`pip uninstall` 任一 dist 会删共享文件使另一个静默损坏;文件只有一份即从根上消除。
- **D7a(已定,Step 2 落地)** root 薄壳版本与 `lanscoder-core==<version>` pin 硬编码在 pyproject,由 Step 2 tag/CI 校验强制与 `_version.py` 相等(避免薄壳 sdist 读外部 `_version.py` 的 D6 类问题;单一事实来源靠门禁保证)。
- **D7b(已定,Step 2 落地)** core package-data 增加 `app/*.tcss`(`lanscoder/app/tui.py` 的 `CSS_PATH = "tui.tcss"` 是 TUI 唯一包内数据文件,Textual 相对模块目录解析;core 是文件唯一持有者故必须带上)。
- **D7c(已定,Step 2 落地)** 开发/CI 安装流改为双 editable:`pip install -e packages/lanscoder-core` + `pip install -e ".[dev]"`(README、install.sh、ci.yml、publish-pypi.yml 同步)。

## Completed

- 2026-08-29 发布规划讨论拍板 D1/D4(记录于本 TASK 与 `docs/superpowers/specs/2026-08-29-sdk-external-release-design.md`)。
- 依赖审计(2026-08-29 实测):`import lanscoder.core` 与默认 L3 装配+一次回合,第三方足迹仅 `anyio`/`portalocker`/`PyYAML`;`providers/base.py` 与 `tools/builtin.py` 惰性 import openai/anthropic/mcp;`memory/models.py` 硬 import yaml。
- 2026-08-29 Task 0(spec + TASK)经 PR #11 合入 main(`ae7fea0`);D2/D3/D5 随 spec 评审生效;TASK-003 由 `planning` 转 `active`。
- 2026-08-29 Step 1(打包骨架 + 本地双 wheel + 干净 venv 冒烟)完成,PR #12(`codex/task-003-step1`):
  - 新增 `packages/lanscoder-core/{pyproject.toml, setup.py, README.md}`;版本经 `setup.py` AST 读 `lanscoder/core/_version.py`(不 import,满足 D3)。
  - 本地构建:root `dist/lanscoder-1.2.1-py3-none-any.whl` + core `packages/lanscoder-core/dist/lanscoder_core-1.2.1-py3-none-any.whl`,版本均 1.2.1 == `_version.py`。
  - 干净 venv(Python 3.13)装 core wheel:仅 anyio/portalocker/PyYAML(+传递 idna),无 textual/openai/anthropic/mcp/tomlkit/prompt_toolkit/python-dotenv。
  - 最小依赖环境:契约 + SDK 示例 + 层边界泄漏测试 26 passed;安装包端到端冒烟(stub transport 驱动 L2 Agent 一轮)通过。
- 2026-08-29 D7 冲突实测与结构性消除决策(用户拍板):双装实验确证 pip 零警告、`pip check` 不报、`pip uninstall LansCoder` 后 `import lanscoder.core` 挂;决定 `LansCoder` 薄壳化,文件零重叠。
- 2026-08-29 Step 2(D7 结构性消除落地)完成,PR #13(`codex/task-003-step2`):
  - root `pyproject.toml` 改薄壳:`dependencies = ["lanscoder-core[llm,mcp]==1.2.1", "textual", "prompt_toolkit", "tomlkit", "python-dotenv"]`、`[tool.setuptools] packages = []`、保留 CLI 入口;core `package-data` 增加 `app/*.tcss`。
  - 开发/CI 双 editable:`pip install -e packages/lanscoder-core` + `pip install -e ".[dev]"`(README、ci.yml、publish-pypi.yml)。
  - 新增 `tests/test_dist_metadata.py`(8 项元数据契约:版本单一来源、root 无包、core 最小集/extras/package-data);修订 `test_cleanup_contracts.py::test_pyproject_is_the_single_production_dependency_manifest`(mcp 移入 core extra)。
  - spec 追加 §8 D7 修订记录。

## Pending

- [x] Step 1:`packages/lanscoder-core` 打包骨架 + 本地 build 双 wheel + 干净 venv 安装冒烟(SC-1、SC-2 前半;PR #12)。
- [x] Step 2(D7 结构性消除落地):root 薄壳化(pyproject 改 deps/scripts/`packages=[]`;core 补 `app/*.tcss`);双 editable 开发流(README/install.sh/ci.yml/publish-pypi.yml);冲突消除验证 SC-7(双装 RECORD 零重叠、卸载不破坏)。
- [ ] Step 3(原 Step 2):CI 双 dist 发布流程 + 最小依赖验证 job(SC-2 后半、SC-3、SC-4;tag 校验同时约束 root pyproject 版本 + core `_version.py`)。
- [ ] Step 4(原 Step 3):CHANGELOG + 安装/发布文档 + Test PyPI 演练(SC-5、SC-6)。

## Next step

Step 3(独立 PR):CI 双 dist 发布流程——`publish-pypi.yml` 对 root(全量 `python -m build`)与 core(`python -m build --wheel`)分别构建 + `twine check` + 上传;tag 校验同时约束 root pyproject 版本与 core `_version.py`;新增最小依赖验证 job(仅装 core wheel 依赖)跑契约 + 示例冒烟 + 泄漏检查(SC-2 后半、SC-3、SC-4)。

## Verification

- planning 阶段只写文档,无代码验证;依赖审计证据见 spec §3(2026-08-29 实测命令)。
- [x] SC-1(2026-08-29 实测,exit 0):
  - `packages/lanscoder-core: python -m build --wheel` → `lanscoder_core-1.2.1-py3-none-any.whl`(含 `lanscoder/` 全树 + `py.typed` + `context/prompts/*.md`)。
  - 干净 venv `/tmp/lanscoder-core-smoke`(py3.13.9):`pip install <wheel>` → exit 0;`pip list` 仅 anyio/idna/lanscoder-core/portalocker/PyYAML;`grep -i textual` 无输出。
  - `pip show lanscoder-core` → `Requires: anyio, portalocker, PyYAML`。
  - `cd /tmp && python -c "from lanscoder.core import create_agent_session; ..."` → 成功,`__version__=1.2.1`,`'textual' not in sys.modules`。
- [x] SC-2(2026-08-29 实测,最小依赖环境):
  - 干净 venv 装 pytest 后:`python -m pytest -q tests/test_core_contract.py tests/test_sdk_examples.py tests/test_layer_boundaries.py` → **26 passed**,exit 0(含 `test_core_import_does_not_pull_tui`)。
  - 安装包端到端冒烟 `smoke_installed.py`(site-packages 导入,stub transport 驱动 L2 Agent):事件序列 `agent_start→…→agent_end`,exit 0。
- [x] D7 冲突实证(2026-08-29,临时 venv `/tmp/lanscoder-dual`):双装 pip 零警告、`pip check` 报 No broken requirements;`pip uninstall lanscoder` 后 `lanscoder-core` 仍在注册但 `import lanscoder.core` → `ModuleNotFoundError`。
- [x] SC-7(2026-08-29 实测,exit 0):
  - 两 wheel RECORD 交集为空(root wheel 仅 6 个 dist-info 文件,零 `lanscoder/`;core wheel 220 文件)。
  - 干净 venv `/tmp/lanscoder-shell`:先装 core wheel 再装 root 薄壳 wheel → 自动解析 `lanscoder-core[llm,mcp]==1.2.1` + TUI 依赖;`lanscoder --help` 可用;`import lanscoder.app.tui` 成功且 `tui.tcss` 在 site-packages;`pip uninstall LansCoder` 后 `import lanscoder.core` 仍可用。
  - D7c 开发流:项目 venv 双 editable 安装 `pip check` → No broken requirements;全量 `pytest` 1722 passed、`ruff check .` 通过、`check.mjs` valid。
  - 注:本机 macOS 沙箱会给 `.pth` 打 `UF_HIDDEN` 标志导致 site.py 跳过 editable `.pth`(环境怪癖,Linux CI 无此行为);wheel 级验证不受影响。

## Handoff note

- From: `Lanster`
- To: `Lanster`
- Summary: TASK-003 进行中(active);Step 1(PR #12)core 子项目骨架完成;Step 2(PR #13)D7 结构性消除落地:`LansCoder` 薄壳化 + core 补 `app/*.tcss` + 双 editable 开发流 + SC-7 实测通过(两 wheel RECORD 零交集、卸载不破坏);下一步 Step 3(CI 双 dist 发布流程 + 最小依赖 job),随后 Step 4 文档。
