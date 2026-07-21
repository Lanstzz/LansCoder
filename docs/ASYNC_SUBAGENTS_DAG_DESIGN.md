# Archived: Async Tools, Subagents, and Task DAG

[中文](ASYNC_SUBAGENTS_DAG_DESIGN.zh-CN.md)

This historical design described a separate TaskGraph protocol and whole-graph
updates for asynchronous work. It has been replaced by the unified TaskPlan
design: one revisioned plan supports both `linear` and `dag` execution, and
ordinary progress changes address stable task IDs incrementally.

For the current protocol and migration scope, see the
[Unified TaskPlan implementation plan](superpowers/plans/2026-07-21-unified-task-plan.md).

Do not use this document as a current runtime or tool reference.
