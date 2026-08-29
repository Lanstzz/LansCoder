# Changelog

本项目所有值得记录的变更，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本遵循[语义化版本](https://semver.org/lang/zh-CN/)。版本号单一事实来源
`lanscoder/core/_version.py`（与 root `pyproject.toml` 的 `[project].version`、root 对
`lanscoder-core` 的版本 pin 一致；由 `tests/test_dist_metadata.py` 与发布 tag 校验强制，漂移即红）。

## [Unreleased]

### Added

- **独立 SDK 分发包 `lanscoder-core`**：`pip install lanscoder-core` 即得 headless SDK
  （`from lanscoder.core import create_agent_session`），必装依赖仅 `anyio` / `portalocker` / `PyYAML`，
  无 TUI；`[llm]`（openai + anthropic）与 `[mcp]` 为可选 extras（D2）。
- **双 dist 发布流程**：一个 tag 同时构建并发布 `LansCoder`（TUI 应用）与 `lanscoder-core`（SDK）
  两个 wheel，版本一致；CI 含最小依赖验证 job（契约 + SDK 示例 + 层边界泄漏 + 安装包冒烟）与
  tag 校验（root pyproject 版本 == core `_version.py` == tag；D3/D5/D6）。

### Changed

- **`LansCoder` 改为薄壳**：不再自带 `lanscoder/` 源码树，依赖 `lanscoder-core[llm,mcp]` + TUI 侧
  依赖；与 `lanscoder-core` 是**依赖关系**（后者被前者依赖）而非替代关系，两个 dist 文件零重叠（D7）。

## [1.2.1] - 2026-08-23

- **TUI 品牌化**：统一深色配色、建议框与活动行对齐。
- **转录重构**：嵌套可折叠回合模型，live 与重放渲染顺序一致。
- **权限体验**：修复暂停/恢复错序，权限提示移入瞬时按钮区，恢复严格 1/2/3 输入。
- **后台通知**：持久化通知标签与错误信息，退出前冲刷待发通知。
- **权限执法解耦**：`PermissionCoordinator` 成为单一执法闸门，tools 层对 permissions 零引用，移除 `runtime/` 包。
- **架构门禁**：依赖方向端到端测试锁定包边界。

## [1.1.0] - 2026-08-20

- **同步/异步统一**：非流式回合收敛到统一异步核心，删除旧同步分支。
- **子代理面板**：后台子代理选择、高亮与停止交互。
- **worktree 隔离**：子代理可在隔离 git worktree 中运行且可取消。
- **子代理可观测**：delegate 结果上报 token 用量与耗时。
- **工程化**：ruff / black 纳入 dev 依赖。

## [1.0.1] - 2026-08-20

- **压缩管线 v3/v4**：LLM 摘要压缩（保留最近 N 轮原文）、hard-truncate 兜底、压缩策略版本化。

## [1.0.0] - 2026-08-19

- 首个稳定版本：核心代理循环、Textual TUI、工具系统、会话持久化、上下文管理。
