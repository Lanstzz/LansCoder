# Archived: Async Tools, Subagents, and DAG Planning Implementation Plan

This historical implementation plan proposed a separate Todo and TaskGraph
protocol for asynchronous tools and subagents. It has been superseded by the
unified TaskPlan design: one revisioned plan supports both `linear` and `dag`
execution, while ordinary progress updates address stable task IDs incrementally.

For the current protocol and implementation scope, see the
[Unified TaskPlan implementation plan](superpowers/plans/2026-07-21-unified-task-plan.md).

This document is retained only as historical background and must not be used as
a current runtime, tool, or migration reference.
