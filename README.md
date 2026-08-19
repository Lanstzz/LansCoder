# LansCoder

> A local coding agent you can read end to end.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

![LansCoder TUI screenshot](./assets/tui-screenshot.png)

## What is it?

LansCoder is a locally-runnable Python coding agent. It understands your codebase, edits files, and runs shell commands — like Claude Code or Aider. But its core design goal isn't feature volume; it's **understandability.**

~30,000 lines of Python, clean module boundaries, solid test coverage. Real enough to use daily, small enough to read from end to end.

96.38% reward pass@1 on the Harbor Aider Polyglot benchmark.

## Highlights

- **Small, so you can read it** — ~30k lines of Python, not 570k lines of TypeScript. Every module's job is obvious.
- **Built to learn from** — strict layering, clear dependency rules. Great for studying how coding agents work, for hacking on, or for interview prep.
- **Actually usable** — 40+ built-in tools, multi-provider support, MCP integration, session persistence, context compression. Not a toy.
- **Preview before you write** — syntax-highlighted diffs shown before every file change, even in high-permission mode.

## Quick start

```sh
pipx install lanscoder
lanscoder config init
```

Edit the config file with your API key:

- **macOS / Linux**: `~/.config/lanscoder/config.toml`
- **Windows**: `C:\Users\<username>\.config\lanscoder\config.toml`

Example config:

```toml
default_model = "openai/gpt-4o"

[providers.openai]
type = "openai-compatible"
base_url = "https://api.openai.com/v1"
api_key = "sk-xxx"
api_key_env = "OPENAI_API_KEY"
```

Then launch from your project directory:

```sh
lanscoder
```

Development setup:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Features at a glance

| Capability | What it does |
|-----------|--------------|
| Coding agent | Understands code, edits files, runs shell commands, 40+ built-in tools |
| Multi-model | OpenAI-compatible and Anthropic providers, hot-switchable mid-session |
| Permissions | Standard / aggressive / bypass modes, diff preview before every mutation |
| TUI | Textual-based terminal UI, streams reasoning, tool calls, and results in real time |
| Sessions | Create, resume, fork, share — persisted as JSONL |
| Context compression | 4-level pipeline (L1–L4) to manage token usage in long conversations |
| Background subagents | Subagents run independently in the background, notify on completion |
| MCP integration | Connect external tool servers via Model Context Protocol |

## Architecture

```
lanscoder/
├── app/           Textual TUI
├── agent/         Agent loop and orchestration
├── providers/     Model provider adapters
├── tools/         Tool registration and execution (40+ tools)
├── permissions/   Policy and grant management
├── context/       Event log and context management
├── session/       Session lifecycle
├── mcp/           MCP protocol integration
├── memory/        Cross-session persistent memory
├── skills/        Local skill discovery and loading
├── config/        TOML configuration
└── utils/         Shared utilities
```

## Who is it for?

- Developers who want to **deeply understand how coding agents work**
- People looking to **extend or modify** a Python-based coding agent
- Anyone who needs **a project they can explain architecturally** for interviews or portfolios
- AI enthusiasts who want to **experiment with different models** locally

## Contributing

Issues and PRs are welcome. Tests cover most core modules — please make sure they pass before submitting.

## License

MIT