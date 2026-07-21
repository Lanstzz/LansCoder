# 已归档：异步工具、子代理与任务 DAG

[English](ASYNC_SUBAGENTS_DAG_DESIGN.md)

这份历史设计描述了独立的 TaskGraph 协议和整图更新方式。它已被统一 TaskPlan
设计取代：同一份带 revision 的计划同时支持 `linear` 与 `dag`，日常进度只按稳定
任务 ID 做增量更新。

当前协议与改造范围请看[统一 TaskPlan 实施计划](superpowers/plans/2026-07-21-unified-task-plan.md)。

请勿把本文当作当前运行时或工具说明。
