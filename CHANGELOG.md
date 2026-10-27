# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and Semantic Versioning.

## [Unreleased]

### Added

- Streaming chat over Ollama, OpenAI-compatible APIs (including OpenRouter) and Vertex AI through shared stream events.
- `llm-harness --plain`: line-based chat that works over pipes.
- Terminal UI (`llm-harness`) with a model selector, collapsible thinking and Escape to cancel.
- Web GUI (`llm-harness --gui`) served on 127.0.0.1:8080; it opens the browser and uses the full window width.
- Configuration files `config.toml` (providers) and `models.yaml` (models, tags, priority, price); example files are offered on first run.
- Per-model `thinking` setting, `reasoning_style` for OpenAI-compatible providers, and `--think` / `--no-think` flags.
- Display of the model's thinking text (never kept in the chat history) and a usage line (model, tokens, cost, time and session total; see below).
- Safety features: API keys masked in output, config file permission warnings, sanitized model output (replies rendered as markdown, raw HTML shown as text), GUI Host and Origin check, and ambient SDK environment variables ignored.
- `/quit` command in the chat input (GUI, terminal UI, `--plain`) and clean Ctrl-C shutdown of the GUI (exit code 130, no traceback).
- Model fallback: when a model fails before any output (`connection`, `rate_limit`, `not_found`, `unknown` or `auth`), the next `chat`-tagged model, then the `default`-tagged models, are tried; a failing model is skipped for 1, 2, 3, 5, 10, 15, 30, then 60 minutes, persisted across restarts; a manual `--model` or selector pick overrides the backoff; a note shows each switch.
- SQLite ledger of every model call (tokens, cost from the `models.yaml` prices, time, outcome) at `~/.local/share/llm-harness/harness.db`, and a usage line with the cost and the session total after each reply.
- Markdown rendering of assistant replies in the GUI and the terminal UI (tables, strikethrough, task lists, footnotes, highlighted code, links), with raw HTML shown as text and images never loaded.
