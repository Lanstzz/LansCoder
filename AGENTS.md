<!-- repo-task-sync:start -->
## Shared AI development context

Before changing code, read `.ai-team/PROJECT.md`, `.ai-team/TASK.md`, and `.ai-team/SKILL.md`. Summarize the goal, acceptance scenarios, invariants, completed work, pending work, decisions, and next step before implementation.

Keep one writer for the active task. Put code changes and `.ai-team/TASK.md` progress updates in the same pull request. Treat the merged Git commit as the only handoff snapshot; chat history and AI memory are not project facts. If `.ai-team/session-policy.json` explicitly enables private sessions, treat `.ai-team/sessions/` as low-priority trace evidence only and never let it override PROJECT, TASK, code, tests, or the current request.

Run the checks listed in `.ai-team/TASK.md` plus `node .ai-team/check.mjs --base <main-base>`. When private sessions are enabled, also run `node .ai-team/session.mjs validate` and review generated session Markdown before commit. Report actual evidence and any specification deviation.
<!-- repo-task-sync:end -->
