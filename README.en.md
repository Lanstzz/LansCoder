# LansCoder

[中文](README.md) · [PyPI](https://pypi.org/project/lanscoder/) · [Changelog](#changelog)

> **An AI coding agent you can read from start to finish** — the goal is not to pile on features next to a bigger agent, but to keep the system **genuinely usable** while being **small enough to read end to end** and understand why every subsystem exists.

![LansCoder TUI](docs/images/lanscoder-demo.gif)

---

## What this is

LansCoder is an AI coding agent that runs in your terminal. Like Claude Code or Aider, it understands your codebase, edits files, and runs commands — but it is only **about 29k lines of Python** spread across 15 clearly separated modules. From the entry point to the agent loop to the tool system, you can read the whole thing and understand what every layer does.

It is not a toy: **33 built-in tools**, OpenAI-compatible + Anthropic dual model adapters, MCP integration, session resume and forking, an L1–L3 context compaction pipeline, and **96.38% reward pass@1** on the Harbor Aider Polyglot benchmark (213/221, locally pinned configuration; [methodology in the benchmark doc](docs/benchmark.md)).

**Core promise**: the goal is not to have more features than a bigger agent, but to keep the system genuinely usable while small enough to read end to end — and to understand why every subsystem exists.

---

## Why it's worth reading

### Compared with large production-grade agents

| Dimension | LansCoder | Large production-grade agents (e.g. Claude Code / OpenCode) |
|------|-----------|----------------------------------------------|
| Primary goal | Keep the agent's internals **readable and teachable** | Deliver a more complete production-grade agent platform |
| Code size | ~29k lines of Python (204 files · 15 modules) | ~570k lines (Claude Code ≈ 570k lines TypeScript; OpenCode ≈ 575k lines TS/JS) |
| Engineering trade-offs | Deliberately cut parts of the platform surface to stay **inspectable** | Accept higher complexity to support a broader product surface |
| Best for | Learning, building on top, interview prep, local experiments | Users who need a larger, more complete agent environment |

---

## Quick start

**One-line install (macOS / Linux / Windows Git Bash):**

```sh
curl -sSL https://raw.githubusercontent.com/Lanstzz/LansCoder/main/install.sh | bash
```

**Or via pipx:**

```sh
pipx install lanscoder
lanscoder config init
```

Edit the config file and add your API key:

- **macOS / Linux**: `~/.config/lanscoder/config.toml`
- **Windows**: `C:\Users\<username>\.config\lanscoder\config.toml`

```toml
[providers.deepseek]
type = "openai-compatible"
base_url = "https://api.deepseek.com"
api_key = "your API key"
parallel_tool_calls = true

[models."deepseek/deepseek-v4-flash"]
label = "DeepSeek V4 Flash"
context_window = 1000000

[permissions]
mode = "ask"

[ui]
theme = "default"
```

Then start it in your project directory:

```sh
lanscoder
```

---

## Feature overview

| Capability | Description |
|------|------|
| Coding agent | Understands code, edits files, runs commands — 33 built-in tools organized into permission tiers |
| Multiple models | OpenAI-compatible + Anthropic adapters, hot model switching in-session |
| Permission control | Standard / relaxed / bypass modes, diff preview before writes, sensitive-path prompts, persistent grants |
| TUI | Textual-based terminal UI showing reasoning, tool calls, and results in real time |
| Session management | Create, resume, fork, and share sessions; JSONL persistence; resume from breakpoints |
| Context compaction | L1–L3 four-level compaction pipeline to control token usage in long sessions |
| Background subagents | Subagents run independently in the background with completion notifications; supports parallel tasks and worktree isolation |
| Persistent memory | Cross-session recall with project-level / user-level scopes |
| Skills | Local skill file discovery and loading |
| MCP integration | Connect external MCP tool servers (stdio / SSE) |

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│          Presentation layer · app/ (Textual TUI)            │
│    Transcript view · Permission view · Subagent panel ·     │
│             Model switching · Slash commands                │
└──────────────────────────┬─────────────────────────────────┘
                           │
┌──────────────────────────┴─────────────────────────────────┐
│               Orchestration layer · agent/                  │
│   Agent loop · Turn protocol · Tool flows · Subagent        │
│          engine · Permission coordination · Guardrails      │
└───────┬──────────────────────────────┬─────────────────────┘
        │                              │
┌───────┴──────────┐        ┌──────────┴──────────────┐
│ Capability layer │        │   Capability layer      │
│      tools/      │        │      permissions/       │
│  33 built-in     │        │  Policies · single      │
│      tools       │        │  enforcement gate       │
│  (tools and      │        │                         │
│  permissions     │        │                         │
│  know nothing    │        │                         │
│  about each      │        │                         │
│  other; the      │        │                         │
│  agent is the    │        │                         │
│  sole            │        │                         │
│  coordinator)    │        │                         │
└───────┬──────────┘        └──────────┬──────────────┘
        └──────────────┬───────────────┘
                       │
┌──────────────────────┴──────────────────────────────────┐
│        Cross-cutting layer · Infrastructure              │
│  providers/  Model adapters (OpenAI · Anthropic)         │
│  context/    Event log · Context building · L1–L3        │
│  session/    Session lifecycle · JSONL persistence ·     │
│              Resume/fork                                 │
│  mcp/ memory/ skills/ planning/ config/ input/ subagent/ │
└─────────────────────────────────────────────────────────┘
```

### Tech stack

| Layer | Technology |
|------|------|
| Runtime | Python 3.11+ · anyio async |
| Terminal UI | Textual |
| Model adapters | OpenAI SDK (compatible interface) · Anthropic SDK |
| Tool integration | MCP (stdio / SSE) |
| Persistence | JSONL session storage · TOML config (tomlkit) · portalocker |
| Quality | pytest (127 files / 37k lines of tests) · trio · ruff · black · dependency-direction AST gate |
| Release | PyPI · pipx · one-line install script |

---

## Documentation

Full documentation lives in [docs/](docs/):

- [Getting started](docs/getting-started/) — install, configure, run in 5 minutes
- [Guides](docs/guides/) — permission model, context compaction, session management
- [Architecture](docs/architecture/) — layered design and dependency rules
- [Benchmark](docs/benchmark.md) — methodology and reproduction of the 96.38% score
- [FAQ](docs/faq.md) — frequently asked questions

---

## Who it's for

- Developers who want to **deeply understand how coding agents work** under the hood
- People who want to **build on top of an agent harness**
- Engineers preparing for interviews who need a project **they can explain clearly**

---

## Development

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check lanscoder tests
```

See [docs/development.md](docs/development.md) for contribution guidelines.

---

## Changelog

### v1.2.0 (2026-08)

- **TUI branding**: unified dark color scheme, suggestion boxes aligned with active rows
- **Transcript refactor**: nested collapsible turn model, live and replay render in the same order
- **Permission UX**: fixed pause/resume ordering, permission prompts moved to the transient button area, strict 1/2/3 input restored
- **Background notifications**: persisted notification labels and error messages, pending notifications flushed before exit
- **Permission enforcement decoupling**: PermissionCoordinator is now the single enforcement gate, the tools layer has zero references to permissions, and the `runtime/` package was removed
- **Architecture gate**: end-to-end dependency-direction test locks package boundaries

### v1.1.0 (2026-08)

- **Sync/async unification**: non-streaming turns converge on a unified async core, old sync branch removed
- **Subagent panel**: background subagent selection, highlighting, and stop interactions
- **Worktree isolation**: subagents can run in isolated git worktrees and be cancelled
- **Subagent observability**: delegate results report token usage and latency
- **Engineering**: ruff / black added to dev dependencies

### v1.0.1 (2026-08)

- **Compaction pipeline v3/v4**: LLM summary compaction (keeping the most recent N turns verbatim), hard-truncate fallback, versioned compaction strategies

### v1.0.0

- First stable release: core agent loop, Textual TUI, tool system, session persistence, context management

---
