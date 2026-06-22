# Agent Loop 护栏目标计划

## 背景

FirstCoder 现在的 `AgentLoop` 是一个有意保持简单的最小闭环：

```text
用户消息
-> 调用 provider
-> 如果模型请求工具，就执行工具
-> 写入工具结果
-> 再次调用 provider
-> 直到模型不再调用工具，或达到 max_tool_rounds
```

这个结构已经能工作，但最近的本地 smoke test 暴露了一个真实问题：agent 已经修好了代码，也跑通了 `pytest`，但它又尝试多调用一次工具做收尾确认，结果撞上 `max_tool_rounds`。任务事实上完成了，loop 却给出了 `tool_round_limit`，没有产出干净的最终回答。

问题不在于模型不会做题。问题在于：当前 loop 把一个很小的工具轮数上限当成主要安全刹车，同时还不会识别“验证已经通过，所以应该收工”。

## 目标

把 FirstCoder 的 agent loop 从“靠小轮数硬停”升级为“多层护栏控制”，让它能胜任更长的 coding task 和 SWE-bench Lite 这类基准测试。

目标停止方式应该分三类：

```text
正常停止：
  模型返回最终文本，不再调用工具

完成停止：
  验证命令通过后，loop 强制模型进入纯文本最终回答

安全停止：
  总耗时超限、provider 调用次数超限、工具超时、连续失败，或可选的工具轮数上限触发
```

也就是说，要给 agent 足够空间解决真实任务，同时让运行时不被无限循环、昂贵重试、测试通过后的无意义工具调用拖住。

## 非目标

- 不照搬 Claude Code 的完整 loop 架构。
- 第一阶段不做并行工具执行。
- 第一阶段不做完整美元成本统计。
- 不把 SWE-bench 专用逻辑硬编码进通用 `AgentLoop`。
- 不只依赖提示词里的“测试通过后停止”。

## 期望行为

当 agent 运行类似 `pytest -q` 的验证命令，并且命令成功退出时：

```text
shell / diagnostics 工具结果：
  ok = true
  exit_code = 0
  command 看起来是验证命令

AgentLoop：
  记录工具结果
  再调用一次 provider，但设置 tool_choice="none"
  写入最终 assistant 回复
  干净结束本轮任务
```

模型可以总结自己改了什么，但在成功验证信号出现后，不应该再继续调用工具。

当验证失败时：

```text
exit_code != 0
-> 继续正常工具循环，让模型读取失败信息并修复
```

当任务跑太久时：

```text
总耗时超限或 provider 调用次数超限
-> 用清晰的结构化失败状态停止
```

## 第一阶段护栏

按优先级先做这些：

1. 成功验证后收工
   - 识别成功的 `shell` 或 `diagnostics` 结果。
   - 第一版识别这些命令：`pytest`、`python -m pytest`、`npm test`、`pnpm test`、`yarn test`、`go test`、`cargo test`。
   - 识别成功后，下一次模型请求强制 `tool_choice="none"`。

2. Provider 调用次数上限
   - 统计一次用户任务里调用模型的次数。
   - 超过可配置上限时停止。
   - 这比单纯统计工具轮数更直观，因为每次继续都会真实消耗一次模型调用。

3. 单轮总耗时上限
   - 一次用户任务超过可配置时长后停止。
   - SWE-bench Lite 第一版可以先按每题 20-30 分钟设计。

4. 可配置工具轮数上限
   - 保留 `max_tool_rounds`，但把它降级为保险丝。
   - benchmark 默认值应该比早期骨架里的 `4` 大很多，或者允许不设置。

5. 工具超时策略
   - 保留现有工具级 timeout。
   - benchmark 模式下允许测试命令使用更长 timeout，例如 120-180 秒。

## 建议默认值

普通 TUI 使用：

```text
max_tool_rounds: 20 或 None
max_provider_calls: 40
max_turn_seconds: 600
shell timeout: 30 秒
diagnostics timeout: 120 秒
successful_verification_stop: true
```

SWE-bench Lite：

```text
max_tool_rounds: 60 或 None
max_provider_calls: 100
max_turn_seconds: 1800
测试类 shell / diagnostics timeout: 180 秒
successful_verification_stop: true
```

摘要、压缩这类子任务：

```text
max_tool_rounds: 0 或 1
max_provider_calls: 1-3
尽量禁用工具
```

## 设计方向

给 loop 加一个轻量策略层，而不是把 benchmark 行为写死在 `AgentLoop` 里。

可能的代码形状：

```text
firstcoder/agent/loop_limits.py
  AgentLoopLimits
  AgentLoopStopReason

firstcoder/agent/verification.py
  is_successful_verification_result(tool_name, result)
  is_verification_command(command)

firstcoder/agent/loop.py
  接收 loop limits
  统计 provider 调用次数
  检查单轮总耗时
  检查成功验证
  在成功验证后执行一次 tool_choice="none" 的最终模型调用
```

这样 `AgentLoop` 仍然保持通用，TUI、benchmark adapter、子任务可以选择不同预算。

## 成功标准

- 任务跑到 `pytest` 成功后，以正常 assistant 最终回答结束，而不是 `tool_round_limit`。
- 验证命令失败时，不会强制结束。
- provider 调用次数超限时，loop 能给出清晰的 finish reason。
- 单轮总耗时超限时，loop 能给出清晰的 finish reason。
- 现有权限确认行为不被破坏。
- 现有 tool_call / tool_result 消息顺序仍然合法。
- SWE-bench Lite adapter 能使用更大的预算，而不是依赖很小的工具轮数作为主要刹车。

## 为什么重要

SWE-bench 这类任务不是几轮工具调用就能稳定完成的。一个真实 coding agent 往往需要：

```text
看文件
定位代码
修改实现
跑测试
读取失败
再次修改
再次验证
总结
```

如果 `max_tool_rounds` 很小，agent 还没完成正常工作流就会被掐断。

但完全取消限制也不安全。正确方向是分层控制：

```text
给 agent 足够工作空间
识别任务什么时候已经完成
验证通过后强制最终回答
用明确限制挡住失控任务
```

这是 FirstCoder 从“能跑工具的最小 agent”走向“能跑主流 coding benchmark 的 agent”的下一步。

