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
- Model fallback: when a model fails before any output (`connection`, `rate_limit`, `not_found`, `unknown` or `auth`), the next `orchestrator`-tagged model, then the `default`-tagged models, are tried; a failing model is skipped for 1, 2, 3, 5, 10, 15, 30, then 60 minutes, persisted across restarts; a manual `--model` or selector pick overrides the backoff; a note shows each switch.
- SQLite ledger of every model call (tokens, cost from the `models.yaml` prices, time, outcome) at `~/.local/share/llm-harness/harness.db`, and a usage line with the cost and the session total after each reply.
- Markdown rendering of assistant replies in the GUI and the terminal UI (tables, strikethrough, task lists, footnotes, highlighted code, links), with raw HTML shown as text and images never loaded.
- Orchestrator: every message now runs through a step loop over a small typed state (goal, goal history, sourced findings, plan, environment) with actions `respond`, `ask_user`, `call_tool` and `delegate`; the model reply is validated and a rejected reply is retried up to 2 times, with a cap of 8 steps per turn.
- Model tag `orchestrator` in `models.yaml`: the highest-priority model with this tag runs the orchestrator; `--model <id>` overrides it.
- Status line for each orchestrator step: `[step N: ...]` on stderr in `--plain`, a note line in the terminal UI and the GUI.
- On Ollama the reply schema is sent to the model (`format`), so replies follow the JSON contract more reliably.
- Context inspector in the GUI: tabs `State` (goal, findings, plan, environment, next-request preview with character counts) and `Trace` (span tree of the last turn with full details, secrets masked).
- Export and import of the orchestrator state as YAML in the GUI (`llm-harness-state.yaml`, 1,000,000 byte cap, aliases and Python tags rejected).
- Context inspector in the terminal UI: tabs `Chat`, `State` and `Trace` with the same content as the GUI (collapsible sections, nested trace spans, full untruncated text).
- `/export [path]` and `/import <path>` in the terminal UI: save and load the orchestrator state as YAML (no overwrite, mode 0600, 1,000,000 byte cap, aliases and Python tags rejected, not sent to the model).
- `user_id` key in `config.toml`, carried in the orchestrator state's environment block, shown to the model and in the State tab of the GUI and TUI inspectors.
- `[storage]` table in `config.toml` (`relational`, `vector`, `files`) that chooses where data lives, and `llm-harness --storage-status`, which shows the state of the stores without changing anything; the local vector store (qdrant-edge) and file store behind it are internal for now.
- Embedding models in `models.yaml`: the tag `embedding`, an optional `dimensions`, an `Embedder` with `embed` on the Ollama, OpenAI-compatible and Vertex adapters, and an opt-in live test (`uv run pytest -m live tests/test_live_ollama.py -k embed -rs`); nothing calls them yet.
- `agents.yaml` next to `models.yaml`: agents with `id`, `name`, `description`, `tags`, `model_tags` and optional `examples`, `tools` and `deny`; exactly one agent carries the tag `orchestrator`, and the orchestrator takes its id and tags from it. The file is required: when it is missing the program stops and offers to write an example. The State tab of the GUI and the terminal UI lists the agents.
- Typed tools and a tool registry: a tool has a name, a one-line description, tags and a typed input; an agent may use the tools whose tags meet its own plus those in its `tools`, minus its `deny`; an unknown tool name in `agents.yaml` stops startup. The orchestrator gets the tool `tool_search`; every request lists the orchestrator's tools by name and description, and `tool_search` returns their argument schemas. A `call_tool` outside the agent's tools, or with arguments that do not fit the schema, is not run and the model is told why. The State tab of the GUI and the terminal UI shows the tools and an estimate of the listing size against the full schemas.
- Subagents: the orchestrator's `delegate` action now runs one child agent from `agents.yaml` (depth 1, at most 5 steps and 2 retries per reply, its own typed state with the goal preset from the brief, only its own tools, its model chosen by its `model_tags`); the result comes back as one observation (its answer plus findings with sources, `needs: <question>` when it asks, or a note that it hit its step limit or failed), and every request lists the available subagents under `## Subagents`.
- Example agent `researcher` in the `agents.yaml` the program offers to write (tag `research`, `model_tags: [chat]`, tool `tool_search`, two examples).
- Trace tab: a `subagent` span that holds the subagent's own steps and calls and its final state; the State tab's request preview shows the `## Subagents` section.
- Ask-before-run gate for tools: a tool declares a `risk` (`read`, `write` or `exec`) and the program asks you before it runs a `write` or `exec` tool, in `--plain` (on a terminal), the terminal UI and the web GUI. The prompt shows the exact arguments the tool will receive, with control and non-ASCII characters escaped as `\uXXXX`; answer `y` or `yes` to allow, anything else denies; a call with more than 20000 characters of arguments is denied without asking. With piped input `--plain` cannot approve, so such calls are denied; subagents cannot approve either.
- `[tools] dev_echo = true` in `config.toml` registers `dev_echo`, a harmless write-risk tool for trying the approval prompt (default: off).
- Tools `web_fetch` (fetches one public web page and returns its main text) and `web_search` (DuckDuckGo, no API key); both run without an approval prompt, behind an SSRF guard, size and time caps, and an `<untrusted-web-content>` block that marks the text as data.
- `[tools] web_search`, `[tools] web_fetch` and `[tools] web_relax_tls_strict` keys in `config.toml` (all default: off); behind a corporate TLS-inspection proxy, put the proxy's root CA in a bundle and set `SSL_CERT_FILE`, and set `web_relax_tls_strict = true` only if Python's strict certificate check still rejects it.
- Tool calls that run without a prompt (all `read` tools except `tool_search`) now show one line, `tool <name> <arguments>`, in `--plain`, the terminal UI and the GUI when the call starts.
- Tool `run_command` (off by default; switch on with `[tools] run_command = true`) runs one local program from an `argv` list, with no shell; every call needs your approval and shows the exact arguments. `[tools] command_allow` and `command_deny` lists plus a built-in deny list (shells, `sudo`, `env`, `curl`, ...) refuse programs before you are asked; they guard against mistakes and are not a sandbox. `command_timeout_seconds` (default 30, 1 to 120) limits the run time; a timeout kills the whole process group. The child gets a scrubbed environment and no stdin, and its output is capped.
- Optional agent memory tools `memory_search`, `memory_save`, `memory_update` and `memory_forget` (off by default; switch on with `[tools] memory = true` and a `user_id`): short English notes kept per user with 1 to 8 keywords and created/updated times, searched by keyword; the saves run without an approval prompt and show one `tool <name> <arguments>` line.
- Automatic memory recall: when memory is on, the orchestrator searches memory with your message at the start of each turn and puts the top 5 matching notes (at most 6000 characters, in a `<memory-entries>` data block under `## Memory`) into every model request of that turn; a failed or empty recall changes nothing, and the Trace tab shows a `recall` span. Common English words are ignored in memory searches, for recall and for `memory_search`.
- Memory reflection: after an answer to a turn that used a tool or changed the goal, one short model call may save, update or forget a note without asking you; it shows a one-line notice, refuses secrets, and a failed reflection never fails the turn.
- `/memory` command (`/memory list [PAGE]`, `/memory show ID`, `/memory delete ID`) to list, read and delete your saved memories in `--plain`, the terminal UI and the GUI, with no model call.
- `memory_update` now shows the stored record in a notice after it runs.
- Optional meaning-based (vector) memory search, switched on with `[tools] memory_vectors = true` (needs `memory`, a `user_id` and an `embedding` model): recall, `memory_search` and reflection merge keyword and vector hits, and fall back to keyword search with one notice on any problem.
- `/memory reindex` indexes the memories that have no vector yet and answers `reindexed N of M memories`; it also switches vector search back on after a failure (it retries the embedding model and the vector store, and answers `vector search is off: <reason>` if the problem remains).
- Remote qdrant vector backend: `[storage.vector] backend = "qdrant"` with a `url`, the optional extra `remote`, an optional API key in `~/.config/llm-harness/secrets.toml` (mode 0600), one warning line for a key sent over plain http, and `--storage-status` showing the remote store without contacting it; see the README section "Remote qdrant".
- Postgres relational backend: `[storage.relational] backend = "postgres"` with a `url`, the optional extra `postgres` and an optional password in `~/.config/llm-harness/secrets.toml` under `[postgres]` keep the usage ledger and schema versions on a Postgres server; `--storage-status` reports it read-only; agent memory works with it too (next entry); see the README section "Postgres".
- pgvector vector backend: `[storage.vector] backend = "pgvector"` with a `url` keeps the vectors in a PostgreSQL database with the pgvector extension, which an administrator creates with `CREATE EXTENSION vector;`; it uses the same optional extra `postgres` and the optional `[postgres] password` in `~/.config/llm-harness/secrets.toml`; `--storage-status` reports the extension and the collections read-only; it works with SQLite as the relational store, so agent memory keeps working; see the README section "pgvector".
- Agent memory now works with `[storage.relational] backend = "postgres"` (`[tools] memory = true` is no longer refused with it): the notes live on the server and are searched with PostgreSQL full-text search (the `english` configuration), which finds the same notes as SQLite on the corpus used to check it but may rank them differently, and does not fold accents (`cafe` does not match `café`); memories saved in SQLite are not copied to Postgres; see the README sections "Postgres" and "Agent memory".

### Changed

- `examples` in `agents.yaml` is now used: an agent other than the orchestrator with at least one example is offered to the orchestrator as a subagent, and one without examples is not.
- `delegate` no longer answers `delegate is not available yet` when subagents are configured; the model is told to use the `## Subagents` list.
- The session total in the usage line now includes the calls of subagents, and it never decreases within a session.
- The GUI prompt gets the focus back when a turn ends.
- All front ends now run through the orchestrator: replies appear whole when a step finishes, not word by word; thinking text still streams.
- BREAKING for existing configs: at least one model in `models.yaml` needs the `orchestrator` tag; without it startup stops with `no model is tagged 'orchestrator'; tag one in models.yaml or use --model`.
- The fallback chain uses the `orchestrator` tag instead of `chat` (chosen model, then `orchestrator`-tagged models by priority, then `default`-tagged models).
- The starting model is now chosen by the `orchestrator` tag instead of `default`; `default` only marks the last-resort fallback.
- The ledger path can now be set with `[storage.relational] path`, and the ledger file gets a `schema_versions` table on first start (existing rows are not touched).
- Embedding models are hidden from the model selectors and rejected by `--model`; an entry tagged `embedding` together with `chat`, `default` or `orchestrator` is a config error; the example config files the program offers to write now explain every key in comments, and `models.yaml` includes a commented-out embedding example.
- The unknown-backend error for `[storage.relational]` now lists `sqlite, postgres` (postgres was recognised but rejected before).
- The error for a `url` with the `qdrant-edge` or `none` vector backend now reads `url is only used with backend = "qdrant" or "pgvector"`, and the unknown-backend error for `[storage.vector]` now lists `qdrant-edge, qdrant, pgvector, none`.

### Removed

- The chat history is no longer sent to the model; each step sends only the instructions, the state and the latest observation.
- The `ChatSession` class (`src/llm_harness/session.py`).

### Fixed

- An embedding call that gets no answer now fails after 60 seconds instead of waiting forever; it shows as a connection error, and memory vector search switches off with one notice.
