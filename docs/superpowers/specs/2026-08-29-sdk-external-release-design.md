# 对外发布设计(spec)

- 日期: 2026-08-29
- 状态: 待评审(Task 0 / TASK-003)
- 上游决策: `2026-08-29-sdk-hardening-design.md`(TASK-002,P0-P3 已合入)+ 2026-08-29 发布规划讨论(D1/D4 用户已定;D2/D3/D5 本 spec 推荐,评审即生效)
- 关联任务: `.ai-team/TASK.md` TASK-003

## 1. 背景与目标

TASK-002 已把 `lanscoder.core` 钉死为稳定 SDK 面:契约测试锁定 `__all__`/签名、`py.typed`、`__version__` 版本策略、`docs/architecture/03-sdk.md` 与 `examples/sdk/` headless 示例。但 P4(独立分发包)当时显式排除,现状对"对外发布"有两个缺口:

1. **SDK 消费面太厚**:`lanscoder.core` 随 `LansCoder` 主包一起安装,`install_requires` 含 TUI 侧依赖(`textual`/`prompt_toolkit`/`tomlkit`/`python-dotenv`);SDK 用户被迫装整个应用栈。
2. **无独立发布形态**:只有主包 `publish-pypi.yml`(tag 触发);无 CHANGELOG、无 `lanscoder-core` wheel、无最小依赖验证。

目标(P4 落地,范围不超 D1-D5):

- 发布独立分发包 `lanscoder-core`(dist 名 ≠ import 名;import 保持 `lanscoder`):`pip install lanscoder-core` 即用,最小依赖、无 TUI。
- 一个 tag 同时发布 `LansCoder`(TUI 应用)与 `lanscoder-core`(SDK)两个 wheel,版本一致。
- 补齐 CHANGELOG 与安装/发布文档。

## 2. 决策

| # | 决策 | 依据 |
|---|------|------|
| D1 | 独立分发包 `lanscoder-core`,import 名保持 `lanscoder` | 用户已定(A);dist 名与 import 名解耦是标准做法(如 `Pillow`/`beautifulsoup4`) |
| D2 | 依赖裁剪:必装 `anyio` / `portalocker` / `PyYAML`;extras `[llm]` = openai + anthropic、`[mcp]` = mcp;排除 `textual`/`prompt_toolkit`/`tomlkit`/`python-dotenv`;只裁剪依赖,不裁剪模块树 | 依赖审计实测(§3) |
| D3 | 单一版本号,双 dist 同步(`lanscoder/core/_version.py` 唯一来源;一个 tag 发两包) | 1.x 快速演进期,双版本会引入漂移与 `__version__` 二义性;core 稳定到可独立节拍时再拆 |
| D4 | 验收形态(第一个):`pip install lanscoder-core` 后 `from lanscoder.core import ...` 即用;无 TUI 依赖可验证;契约测试/示例/CI 全绿 | 用户已定 |
| D5 | 双 dist 构建:独立子项目 `packages/lanscoder-core/pyproject.toml`,`package-dir` 指回仓库根;CI 对两个 dist 分别 build + twine check + upload | setuptools 单 pyproject 单 dist;子项目是 monorepo 双 dist 的标准做法 |

## 3. 依赖审计证据(2026-08-29 实测)

在已装全量依赖的 venv 中,fresh interpreter `import lanscoder.core`,随后默认 L3 装配(`create_agent_session` + 内置工具)+ 一次 stub 回合,加载的第三方发行版:

| 层 | 第三方依赖 | 证据/说明 |
|----|-----------|----------|
| 必装 | `anyio`(+`sniffio` 传递)、`portalocker`、`PyYAML` | runner 用 anyio;session store 用 portalocker;`lanscoder/memory/models.py` 顶层 `import yaml` |
| extras: 真实模型 | `openai`、`anthropic` | `lanscoder/providers/base.py` 顶层不 import(仅 stdlib + providers.errors/types);适配器惰性,实例化时才拉 |
| extras: MCP | `mcp` | `lanscoder/tools/builtin.py` 顶层不 import mcp;工具接入惰性 |
| 排除(TUI/CLI/config 侧) | `textual`、`prompt_toolkit`、`tomlkit`、`python-dotenv` | core 及其依赖链不拉 `textual`(已有 `test_core_import_does_not_pull_tui` 锁定);config/CLI 属应用侧 |

> 结论:裁剪依赖可行且已被 import 足迹与泄漏测试双重支撑;`lanscoder-core` 最小安装即可 import core、装配 L3、跑 headless 回合(duck-typed `LlmTransport` 甚至不需要 openai/anthropic)。

## 4. 目标发布形态

- `lanscoder-core` wheel:包含同一 `lanscoder` 源码树(模块树不裁剪),元数据:
  - `name = "lanscoder-core"`;`version` 动态读 `lanscoder/core/_version.py`(AST 解析,不 import)。
  - `install_requires = ["anyio", "portalocker", "PyYAML"]`。
  - `[project.optional-dependencies] llm = ["openai", "anthropic"]`;`mcp = ["mcp>=1.28.1,<2"]`。
  - `package-data` 含 `py.typed` 与 `context/prompts/*.md`(与主包一致,保证类型/提示模板可用)。
- `LansCoder` wheel:现状不变(全量依赖,TUI)。
- 双 dist 为替代关系(同 `lanscoder` import 树),文档注明**不要同时安装**。
- 版本:均读 `_version.py`;tag `vX.Y.Z` 触发双 build;tag 校验强制两 dist 版本一致(D3)。

## 5. 分步改动清单与验收(每 Step 独立 PR,TDD 先行)

### Step 1 — 打包骨架 + 本地验证

改动:
- 新增 `packages/lanscoder-core/pyproject.toml`(D5):name/version/extras 如上;`[tool.setuptools] package-dir = {"" = "../.."}`,`packages.find` include `lanscoder*`;`[tool.setuptools.package-data]` 同步主包。
- 本地 `python -m build` 产出双 wheel;干净 venv(仅装 `lanscoder-core`)冒烟:`from lanscoder.core import create_agent_session` + 运行 `examples/sdk/minimal_llm_transport.py`。

验收:
- SC-1 独立 wheel 可安装可 import(干净 venv,`pip show` 无 textual)。
- SC-2 前半:最小依赖环境跑 `test_core_contract.py` + `test_sdk_examples.py` + 泄漏检查。

### Step 2 — CI 双 dist 发布流程

改动:
- `publish-pypi.yml` 扩展:对 `LansCoder` 与 `lanscoder-core` 分别 `python -m build` + `twine check` + 上传;tag 校验同时约束两个 dist 的版本(都读 `_version.py`)。
- 新增最小依赖验证 job(仅装 `lanscoder-core` 依赖)跑契约 + 示例冒烟 + 泄漏检查,锁死 D2 最小集。

验收:
- SC-2 后半 / SC-3 双 wheel 版本一致 + twine check 绿 / SC-4 Test PyPI 演练通过(真实 PyPI 上传人工确认)。

### Step 3 — 文档与发布检查

改动:
- `CHANGELOG.md` 建立(keep-a-changelog 风格)。
- `docs/architecture/03-sdk.md` 与 `examples/sdk/README.md` 补:安装命令、extras(`pip install "lanscoder-core[llm]"`)、与 `LansCoder` 的替代关系。
- README 补 SDK 安装段;发布检查文档(Test PyPI → 真实 PyPI 操作清单)。

验收:
- SC-5 文档齐全 / SC-6 全量门禁绿。

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 双 dist 文件冲突(同 import 树) | 文档声明替代关系;pip 元数据天然冲突提示;不支持同时安装 |
| 最小依赖集漏依赖(运行时惰性 import 踩空) | Step 1 干净 venv 冒烟 + Step 2 最小依赖 CI job 锁定 |
| 版本漂移 | D3 单一版本号;tag 校验强制双 dist 一致 |
| 分发包改动破坏 core API | 本任务零 core 代码语义改动;既有契约测试继续钉死 |
| PyPI 发布事故 | Test PyPI 演练先行;twine check;真实上传人工确认 |

## 7. 验收清单(对应 TASK.md SC-1..SC-6)

- [ ] SC-1 独立 wheel 可安装可 import(无 textual)
- [ ] SC-2 最小依赖环境:契约 + 示例冒烟 + 泄漏检查全绿
- [ ] SC-3 双 dist 构建一致(版本 == `_version.py`,`twine check` 绿)
- [ ] SC-4 tag 发布 + Test PyPI 演练通过
- [ ] SC-5 CHANGELOG + 安装/发布文档齐全
- [ ] SC-6 全量 `pytest` / `ruff check .` 绿

## 8. D7 修订(2026-08-29,结构性消除,用户拍板)

### 背景(实测证据)

Step 1 双装实验(`/tmp/lanscoder-dual` venv)确证:两个 dist 约 215 个 `lanscoder/` 文件重叠;
pip 安装第二个 dist **零警告**、`pip check` 报 `No broken requirements`;`pip uninstall LansCoder`
会删除共享 `lanscoder/` 文件,`lanscoder-core` 仍注册但 `import lanscoder.core` → `ModuleNotFoundError`。
"文档 + Conflicts 元数据 + import 守卫"均只能缓解;**文件只有一份**才是真解。

### 决策

- **D7(已定)** `lanscoder-core` 是 `lanscoder/` 导入树**唯一持有者**;`LansCoder` 改**薄壳**:
  - `dependencies = ["lanscoder-core[llm,mcp]==<version>", "textual", "prompt_toolkit", "tomlkit", "python-dotenv"]`;
  - `[tool.setuptools] packages = []`(wheel 不含任何 `lanscoder/` 文件);
  - 保留 `[project.scripts] lanscoder = "lanscoder.cli:main"`(模块由 core 提供)。
  - 冲突从根上消除:两 dist 文件零重叠;`pip install LansCoder` 自动拉 core;卸载任一不影响另一。
- **D7a(已定)** root 薄壳 `version` 与 `lanscoder-core==` pin 硬编码,一致性由两层门禁强制:
  `tests/test_dist_metadata.py`(本地,漂移即红)+ Step 2/3 tag 校验(发布期)。
- **D7b(已定)** core package-data 增加 `app/*.tcss`(`lanscoder/app/tui.py` 的 `CSS_PATH`,
  是 TUI 唯一包内数据文件;core 是文件唯一持有者故必须带上)。
- **D7c(已定)** 开发/CI 安装流改双 editable:`pip install -e packages/lanscoder-core` +
  `pip install -e ".[dev]"`(README、ci.yml、publish-pypi.yml 同步;install.sh 走 pipx 装发布版,不改)。

### 影响

- 关系由"替代关系(不得同时安装)"改为"依赖关系(`LansCoder` → `lanscoder-core`)"。
- core wheel 内含 `lanscoder.app` 等 TUI 模块(惰性,顶层 import textual 但无人 import),
  层边界泄漏测试继续锁定 `import lanscoder.core` 不拉 app/textual;`app/*.tcss` 随之进入 core wheel。
- 新增验收 **SC-7**:`pip install LansCoder` 自动装 core + TUI 依赖;`lanscoder` 命令可用;
  两 dist RECORD 文件交集为空;`pip uninstall LansCoder` 后 `import lanscoder.core` 仍可用。
