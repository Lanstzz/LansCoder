# Shell 执行沙箱与多语言验证能力目标计划

## 背景

FirstCoder 当前已经有路径沙箱和权限系统，但 shell 工具本质上仍在宿主机执行。`PathSandbox` 能限制工具参数里的 `cwd` 和文件路径不能逃出项目根目录，却无法约束子进程内部行为，例如脚本访问项目外文件、执行网络下载、删除用户目录或读取敏感环境变量。

最近的 `harness-bench-fast` 评测暴露了一个清晰问题：FirstCoder 不是不会写 Python，也不是缺少 shell 工具，而是 shell 执行层还没有形成可靠的工程能力边界。短期硬编码命令白名单能提升一部分任务表现，但长期会变成不可维护的命令枚举，无法自然支持多语言和真实项目。

本目标计划要解决的是：让 FirstCoder 的 shell 成为安全、通用、可验证的工程执行层，而不是靠不断添加 benchmark 命令特判来获得能力。

## 总目标

建立一套原生的 shell 执行安全与验证体系，使 FirstCoder 能够在受控边界内自然处理多语言项目、数据文件、构建测试命令和 patch 工作流。

目标状态：

- shell 能在明确边界内安全执行项目本地命令。
- 权限系统基于风险分类，而不是无限增长的命令白名单。
- agent 能根据项目结构推荐和运行合适的验证命令。
- Python、JavaScript/TypeScript、Go、Rust、Java、C/C++ 等项目拥有一致的验证入口。
- SQLite、XLSX、patch stack 等工程格式有通用处理策略。
- benchmark 只作为能力回归检查，不驱动代码特判。

## 非目标

本计划不追求：

- 为某个 benchmark 题目写专门逻辑。
- 把所有可能命令都加入白名单。
- 在没有执行隔离前完全放开 shell。
- 替代用户对高风险操作的确认。
- 一次性实现完整容器级安全沙箱。

## 核心原则

### 1. 能力边界优先于命令枚举

权限系统不应该主要问“这个命令是不是在白名单里”，而应该问：

- 是否可能写项目外路径？
- 是否可能删除或修改敏感文件？
- 是否访问网络？
- 是否读取敏感环境变量？
- 是否启动长期后台进程？
- 是否使用管道下载执行代码？
- 当前执行环境是否有足够隔离？

白名单只能作为过渡和 UX 优化，不能成为核心安全模型。

### 2. shell 是工程执行层

shell 应该用于：

- 运行项目测试、构建、lint、typecheck。
- 调用项目已有脚本。
- 使用语言生态工具，例如 `npm`、`go`、`cargo`、`mvn`。
- 探查工程数据文件，例如 SQLite、XLSX、CSV、JSONL。
- 执行补丁工作流，例如 `git apply`。

普通文件阅读、搜索、编辑仍应优先使用专门工具。

### 3. 验证命令来自项目，而不是来自 benchmark

FirstCoder 应通过项目结构推断验证命令：

- `pyproject.toml` / `pytest.ini` / `requirements.txt` -> Python 测试和 lint。
- `package.json` -> npm/pnpm/yarn scripts。
- `go.mod` -> `go test ./...`。
- `Cargo.toml` -> `cargo test`。
- `pom.xml` / `build.gradle` -> Maven / Gradle。
- `Makefile` -> `make test` 或相关目标。

用户配置必须能覆盖自动探测结果。

### 4. 安全策略和验证策略分离

验证策略回答：

> 当前项目应该怎么验证？

权限策略回答：

> 这条命令是否允许执行？

两者不能混在一起。不能因为某条命令是推荐验证命令，就无条件允许；也不能因为某条命令不在推荐列表中，就默认不可执行。

## 目标架构

```text
AgentLoop
  -> ToolRegistry
  -> shell tool
  -> ShellExecutionService
      -> ShellRiskClassifier
      -> PermissionManager
      -> ExecutionSandbox
      -> CommandRunner

ProjectDetector
  -> ProjectProfile
  -> VerificationPlanner
  -> VerificationHints

PromptBuilder
  -> project profile
  -> verification hints
  -> shell safety guidance
```

## 关键能力

### ShellRiskClassifier

增加命令风险分类层。

建议分类：

- `safe_readonly`: 只读检查，如 `git status`、`git diff`、查看版本、schema 探查。
- `project_validation`: 项目内测试、lint、typecheck、build。
- `project_mutation`: 项目内生成文件、应用 patch、运行本地脚本。
- `network`: 访问网络。
- `destructive`: 删除、改权限、覆盖敏感路径。
- `secret_access`: 读取敏感环境或敏感文件。
- `unknown`: 无法判断。

分类结果供权限策略使用，而不是直接决定执行。

### ExecutionSandbox

现有 `PathSandbox` 只保护工具参数路径。新目标是引入 shell 执行层边界。

分阶段目标：

- 阶段 1：进程级约束  
  统一 timeout、输出截断、cwd 限制、环境变量过滤、禁止继承敏感环境变量。

- 阶段 2：命令执行约束  
  使用非 shell 模式执行可解析命令；减少 `shell=True` 的使用；明确 shell 控制符风险。

- 阶段 3：可选系统沙箱  
  探索 macOS `sandbox-exec`、容器、轻量隔离或其他可用机制，限制文件系统和网络。

### ProjectDetector

根据项目文件生成 `ProjectProfile`。

示例字段：

```text
languages: python, typescript, go, rust
package_managers: pip, uv, npm, pnpm, cargo
test_commands: [...]
lint_commands: [...]
build_commands: [...]
confidence: high / medium / low
```

### VerificationPlanner

根据 `ProjectProfile` 和当前任务生成验证建议。

它不直接执行命令，只提供 hints 给模型和 diagnostics/verification 层。

### 多格式工程策略

在原生提示和工具说明中建立通用策略：

- SQLite：使用 `sqlite3` 或 Python `sqlite3` 探查 schema、查询、导出。
- XLSX：使用 Python `openpyxl` 读取/写出。
- Patch stack：按顺序应用 patch，失败时读取 patch 内容并手动恢复。
- JSON/YAML/TOML：优先使用解析器，不做脆弱字符串替换。
- CSV/JSONL：使用标准库或项目依赖处理，并检查目标文件存在。

## 成功标准

### 功能标准

- FirstCoder 能在不新增 benchmark 特判的情况下处理 SQLite、XLSX、patch stack 等任务。
- 多语言项目能获得合理验证命令建议。
- shell 命令执行前有结构化风险分类。
- 高风险命令仍需要确认或被拒绝。
- 普通项目内测试和数据探查不会被过度阻塞。

### 安全标准

- 默认不允许读取敏感环境变量明文。
- 默认不允许写 `.env`、密钥文件、`.git` 内部文件。
- 默认不允许项目外删除。
- 网络和下载执行仍需确认。
- shell 超时、输出截断、错误信息统一。

### 回归标准

使用以下任务做能力回归：

- `harness-bench-fast` 中 SQLite/XLSX/patch stack 相关任务。
- 本地 pytest benchmark。
- SWE Lite 小样本。
- 至少一个非 Python 项目 smoke，例如 JS/TS 或 Go。

## 风险

- 过度放松 shell 可能带来真实本机风险。
- 过度依赖命令分类可能误判复杂 shell。
- 项目探测可能给出错误验证命令。
- LLM 可能滥用 shell 代替更安全的文件工具。

对应缓解：

- 保留用户确认。
- 优先减少 `shell=True`。
- 对 unknown 命令保持 ask。
- 在提示词中要求普通读写优先使用专门工具。
- 增加审计日志，记录命令、cwd、风险分类、权限决策。

## 目标完成后的体验

用户不需要告诉 FirstCoder “这是 Python 项目还是 Go 项目”。FirstCoder 会先读项目结构，选择合适验证路径，并在安全边界内运行命令。

当遇到 `.db`、`.xlsx`、`.patch` 这类工程文件时，FirstCoder 不会凭空猜测或放弃，而是使用合适的本地工具进行探查、修改和验证。

当命令风险较高时，FirstCoder 会清楚说明风险并请求确认，而不是静默执行。

最终，FirstCoder 的多语言能力来自通用工程执行层，而不是来自不断堆积命令白名单。
