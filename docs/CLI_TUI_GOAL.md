# FirstCoder CLI/TUI 体验增强目标

## 背景

FirstCoder 当前已经具备 agent 主循环、provider、tool calling、session、context、权限系统和 Textual TUI 外壳，但用户还缺少一个可以直接上手体验的 CLI/TUI 入口。

下一阶段目标不是继续堆底层能力，而是把已有能力组织成一个自然、可理解、接近真实 coding agent 的交互体验。

## 总目标

让用户可以通过一条命令启动 FirstCoder，并在 TUI 中完成基础 coding agent 工作流：

- 新建会话
- 查看和恢复历史会话
- 配置并确认当前 LLM 状态
- 输入任务并获得 agent 回复
- 在需要时处理权限确认
- 查看或撤销长期授权
- 导出会话 transcript

## 用户启动方式

第一阶段支持：

```bash
python -m firstcoder
```

后续再支持：

```bash
firstcoder
```

## 命令设计原则

命令应符合 coding agent 用户心智，不暴露过多内部实现细节。

不做“工具工作集管理”作为主命令，例如：

```text
/tools enable write
/tools disable shell
```

工具集合应作为内部能力，由权限系统控制风险。用户主要理解和操作的是：

- 当前配置
- 当前 session
- 权限模式
- 长期授权
- 历史会话

## 第一批命令

### /help

显示当前支持的命令、用途和示例。

### /config

显示当前运行状态：

- provider
- model
- project root
- data root
- session id
- permission mode
- LLM 配置是否完整

### /new

创建一个新的 session，并切换到新 session。

### /sessions

列出历史 session 摘要。

### /session <session_id>

查看某个 session 的详细摘要。

### /resume

无参数时打开交互式历史会话选择器。

支持：

- 上下选择
- Enter 恢复
- Esc 取消

继续兼容：

```text
/resume <session_id>
```

### /permissions

查看长期授权记录。

### /permissions revoke <grant_id>

撤销某条长期授权。

### /mode

查看或切换权限模式：

```text
/mode
/mode conservative
/mode standard
/mode aggressive
```

### /share

导出当前或指定 session 的 Markdown transcript。

## 权限设计目标

permission 是 agent 的安全闸门。

模型可以请求执行工具，但真正执行前必须经过权限系统判断：

```text
ALLOW：直接执行
ASK：暂停并询问用户
DENY：拒绝执行
```

长期授权来自用户选择 `Allow always`，保存到：

```text
.firstcoder/permissions.json
```

`/permissions` 用于查看和撤销这些授权。

## 非目标

本阶段不做：

- 完整插件系统
- 用户手动选择工具工作集
- 图形化设置页
- 云端同步
- Web UI
- 复杂多 workspace 管理
- session 删除功能
- 自动上传分享链接

## 验收标准

完成后，用户应该可以：

1. 运行 `python -m firstcoder` 启动应用。
2. 输入 `/help` 看到完整命令说明。
3. 输入 `/config` 知道当前 LLM 和工作区状态。
4. 输入 `/new` 创建新会话。
5. 输入 `/resume` 通过列表选择历史会话。
6. 输入 `/permissions` 查看长期授权。
7. 正常输入任务，让 agent 进入已有 AgentLoop。
8. 全量测试通过。

## 推荐实施顺序

1. 增加 CLI 入口：`firstcoder/__main__.py` 和 `firstcoder/cli.py`
2. 增加 `/help`
3. 增加 `/config`
4. 增加 `/new`
5. 增强 `/resume` 为交互式 picker
6. 增加 `/permissions`
7. 补 README 启动说明
8. 补测试
