# LansCoder Agent Development Rules

## Conversational Style

- Keep answers short and concise.
- No emojis in commits, issues, PR comments, or code.
- No fluff or cheerful filler text. Use direct technical prose.
- When the user asks a question, answer it first before making edits or running implementation commands.
- When responding to user feedback or an analysis, explicitly state whether you agree or disagree before describing what you changed.
- Define unavoidable jargon before using it. Prefer concrete examples over abstract summaries.

## Code Quality

- Read files in full before wide-ranging changes, before editing files you have not fully inspected, and when asked to investigate or audit. Do not rely on search snippets for broad changes.
- Use standard Python style with 4-space indentation, explicit names, and small functions that stay close to their module's responsibility.
- Prefer dataclasses for structured data already represented that way.
- Keep provider-specific fields inside provider adapters, and keep UI concerns inside `lanscoder/app`.
- Keep dependency direction one-way: upper layers may depend on lower layers, but lower layers must not depend on upper layers; same-layer modules interact through abstractions. Before adding a dependency, confirm it will not create a circular import.
- Inline single-use helpers that have only one call site.
- Write comments explaining why, not what; skip comments when the code is self-explanatory; do not keep commented-out dead code.
- Always ask before removing functionality or code that appears intentional.
- Do not preserve backward compatibility unless the user explicitly asks for it.

## Commands and Verification

- After code changes, run the linter and formatter (e.g., `ruff`) on the modified files. Fix all issues before proceeding.
- Create or modify test files for new features and bug fixes. After writing or editing a test file, run it and iterate until it passes.
- Run the narrowest relevant tests first, then the full suite for broad or cross-module changes.
- Use fakes and fixtures instead of real API keys or network access in tests.
- For ad-hoc scripts, write them to a temp file (e.g., `/tmp`), run, edit if needed, remove when done. Do not embed multi-line scripts in bash commands.
- Never commit unless the user asks.

## Dependency Management

- Adding a new dependency requires explicit user approval. Before asking, provide a brief justification explaining why the existing dependencies cannot satisfy the requirement.
- When approved, use `pip install <package>` and update `pyproject.toml` accordingly. Regenerate lock files if the project uses them.
- Do not modify `requirements.txt` or `pyproject.toml` without user confirmation.

## Git Operations

Since this is a single-developer project, Git rules are relaxed but still require discipline:

- Commit only files you changed in this session. Stage explicit paths (`git add <path1> <path2>`); never `git add -A` or `git add ..`.
- Before committing, run `git status` and verify you are only staging your files.
- Message format: `{feat,fix,docs}: <concise imperative message describing the behavior change>`. Example: `feat: add permission confirmation protocol`.
- Never run `git reset --hard`, `git checkout .`, `git clean -fd`, `git stash`, or `git commit --no-verify`.

## Testing Guidelines

- This repository uses `pytest`. Add or update tests for behavior changes, especially around agent loops, context recovery, permissions, providers, and tool execution.
- Test files use `test_*.py`; test functions should describe behavior, for example `test_resume_without_id_requests_picker`.
- If you create or modify a test file, run it and iterate on test or implementation until it passes.

## Versioning and Release

This project uses versioned releases. When preparing a release:

1. Update the version number in `pyproject.toml`.
2. Run the full test suite and linter to ensure everything is clean.
3. Create a git tag for the version.
4. Ask the user before executing the release steps.

## Security and Configuration

- Provider and model selection is configured through TOML, not environment variables: a `default_model` plus `[providers]` and `[models]` sections in `~/.config/lanscoder/config.toml` (global) and/or `./lanscoder.toml` (project, overrides global).
- Only API keys come from the environment, resolved via each provider's `api_key_env` (e.g., `DEEPSEEK_API_KEY`) with a fallback to `LANSCODER_API_KEY`; `.env` files are loaded automatically.
- Do not commit secrets, local session data, virtual environments, or machine-specific configuration.

## User Override

- If the user's instructions conflict with any rule in this document, ask for explicit confirmation before overriding. Only then execute their instructions.
