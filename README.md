# llm-harness

A personal harness for trying LLMs from Ollama, OpenAI-compatible APIs
(OpenAI, OpenRouter) and Vertex AI. Today it chats with a plain line mode, a
terminal UI and a web GUI, and shows the model's thinking text when the model
provides it. Every message goes through an orchestrator that keeps a small typed
state instead of the chat history (see Orchestrator). It falls back to another
model when one fails and records what each call cost. Real tools and subagents
do not exist yet.

## Quick start

```sh
uv sync
uv run llm-harness --gui     # web GUI at http://localhost:8080, opens the browser
uv run llm-harness           # terminal UI, needs a terminal
uv run llm-harness --plain   # line mode, works over pipes; /exit or /quit ends it
```

To end the application, type `/quit` as the whole chat input (in the GUI, the
terminal UI and `--plain`). Ctrl-C also stops the GUI cleanly: exit code 130,
no traceback.

On the first run, if a config file is missing, the program names the missing
file and offers to write example files (only after you answer yes, and only
when a person is at the terminal). It then exits; review the files and start it
again.

## Configuration

Two files under `~/.config/llm-harness/`. These are the example files the
program offers to write (`EXAMPLE_CONFIG_TOML` and `EXAMPLE_MODELS_YAML` in
`src/llm_harness/config.py`).

`config.toml` - connection settings, one `[providers.<id>]` table per provider,
and an optional `user_id`:

```toml
# llm-harness connection settings: where each provider lives and how to sign in.
# Keep this file private: chmod 600. The list of models is in models.yaml.
# Lines that start with "# " are examples; remove the "# " to switch one on.

# Your name. Use the same user_id on every computer so your state follows you.
# Optional. It must stay above the first [table] header, because a key written
# after a header belongs to that table.
# user_id = "your-name"

# One [providers.<id>] table per provider. The <id> (here "local") is the name
# that "provider:" in models.yaml refers to. `type` is "ollama", "openai" or
# "vertex"; the other keys depend on the type.

# Ollama on this computer. It needs `base_url` and takes no key.
[providers.local]
type = "ollama"
base_url = "http://localhost:11434"

# An OpenAI-compatible service (OpenAI, OpenRouter, ...). It needs `base_url`
# and one key: either the name of an environment variable (api_key_env, the
# safer way) or the key itself (api_key). Never both. `reasoning_style` is
# optional: it says how the `thinking` setting of a model is sent.
# [providers.openai]
# type = "openai"
# base_url = "https://api.openai.com/v1"
# api_key_env = "OPENAI_API_KEY"   # name of an environment variable, or:
# api_key = "sk-..."               # an inline key (not both)
# reasoning_style = "openai"       # or "openrouter"; how `thinking` is sent

# Google Vertex AI. It needs `project` and `location` and takes no key, no
# base_url: it signs in with Application Default Credentials (run
# `gcloud auth application-default login` once).
# [providers.vertex]
# type = "vertex"
# project = "my-gcp-project"
# location = "us-central1"

# Where data lives; every key is optional and the values below are what you
# get when you write nothing. Paths must be absolute (or start with ~).
# Write these tables after user_id.
# Relational data (usage ledger): "sqlite" is the only backend available now.
# [storage.relational]
# backend = "sqlite"
# path = "/absolute/path/harness.db"   # default: ~/.local/share/llm-harness/harness.db

# Vector search: "qdrant-edge" (local), or "none" to turn vector search off.
# [storage.vector]
# backend = "qdrant-edge"              # or "none" to turn vector search off
# path = "/absolute/path"              # default: ~/.local/share/llm-harness/vectors

# Files: "local" is the only backend available now.
# [storage.files]
# backend = "local"
# root = "/absolute/path"              # default: ~/.local/share/llm-harness/files
```

- `user_id` is optional: a stable string that identifies you, the same on every
  computer. Put it at the top of the file, before any `[providers...]` header
  (a key written after a header belongs to that table, and the program stops
  with an error telling you to move it). It must not be blank, at most 128
  characters, printable (no control characters or newlines), and it is kept
  exactly as written. It is carried in the orchestrator state's `environment`
  and sent to the model; when it is not set, the state shows `null` and the
  model is told the identity is unknown.
- `type` is `ollama`, `openai` or `vertex`. OpenRouter is an `openai` provider
  with its own `base_url` and `reasoning_style = "openrouter"`.
- A key is given inline (`api_key`) or by the name of an environment variable
  (`api_key_env`), never both. Vertex uses Application Default Credentials and
  takes no key.

`models.yaml` - the model list (no secrets):

```yaml
# llm-harness model registry: the list of models you can use. No secrets here
# (keys go in config.toml). Lines that start with "# " are notes or examples.
#
# Every entry has these keys:
#   id        short unique name; this is what --model and the selectors use.
#   name      the label people see in menus.
#   provider  the id of a [providers.<id>] table in config.toml.
#   model     the name the provider knows the model by (for Ollama, what
#             `ollama list` shows).
#   tags      a list of labels:
#               orchestrator  the model the program starts with: the one with
#                             the highest priority. When a model fails, the
#                             other orchestrator models are tried next, by
#                             priority.
#               default       a last resort: after the orchestrator models,
#                             the models tagged default are tried, by
#                             priority.
#               chat          marks a model as a chat model; starting and
#                             falling back use orchestrator and default.
#               embedding     reserved, see the embedding example below.
#             Any other tag (like "cheap") is a free label you can use to
#             group models; it changes nothing.
#   priority  a number; higher wins. Two orchestrator models at the same top
#             priority are an error, and so are two embedding models, so give
#             each a different number.
#   thinking  optional. true or false asks the model to think (or not) before
#             it answers; leave it out to use the model's own default.
#   price     optional. Cost per million tokens, used for the usage
#             numbers: input_per_mtok and output_per_mtok. Both default
#             to 0, which is right for local models.
models:
  - id: gemma-local
    name: Gemma 4 12B (local)
    provider: local
    model: gemma4:12b
    tags: [default, chat, orchestrator, cheap]
    priority: 100
    # thinking: false   # optional; omitted = the model's own default
    price:
      input_per_mtok: 0
      output_per_mtok: 0

  # An embedding model turns text into numbers (vectors) for search. It is not
  # a chat model:
  #   - Tag it [embedding]. Never combine that tag with chat, default or
  #     orchestrator: the program refuses to start.
  #   - It never answers chat, and it is hidden from the chat selectors and
  #     from --model.
  #   - There is no fallback between embedding models: the one with the highest
  #     priority is used.
  #   - `dimensions` is optional, a positive whole number. It is sent to the
  #     provider only when you set it; leave it out to keep the model's own
  #     size. Check that your model supports the value you ask for.
  # To use it, run `ollama pull bge-m3`, then remove the "# " in front of the
  # lines between the two marker lines (the markers stay commented).
  # --- embedding example: begin ---
  #   - id: bge-m3-local
  #     name: BGE-M3 embeddings (local)
  #     provider: local
  #     model: bge-m3
  #     tags: [embedding]
  #     priority: 1
  #     # dimensions: 256   # optional; omitted = the model's own size
  #     price:
  #       input_per_mtok: 0
  #       output_per_mtok: 0
  # --- embedding example: end ---
```

- `provider` is the id of a provider in `config.toml`; `model` is the
  provider's own model id.
- The starting model is the one tagged `orchestrator` with the highest
  `priority` (a higher number wins). Two such models with the same top priority
  are an error. If no model carries the tag, startup stops with an error naming
  it.
- `price` gives the cost per million input and output tokens; an omitted price
  is 0.
- `tags`: `orchestrator` marks the models the program starts with (the highest
  priority) and tries next when one fails, by priority; `default` is the last
  resort after them (see Model fallback below); `chat` marks a chat model;
  `embedding` marks an embedding model (see Embedding models below); any other
  tag is a free label.
- `priority`: a higher number wins. Two `orchestrator` models, or two
  `embedding` models, at the same top priority are an error.

### Embedding models

An embedding model turns text into vectors. It is meant for tool search and RAG,
which are planned; nothing calls it yet, so configuring one changes no chat
behaviour today. It is an ordinary entry in `models.yaml` with the tag
`embedding`, on a provider from `config.toml`. This is the entry in the example
file, commented out between the markers `# --- embedding example: begin ---` and
`# --- embedding example: end ---`:

```yaml
models:
  - id: bge-m3-local
    name: BGE-M3 embeddings (local)
    provider: local
    model: bge-m3
    tags: [embedding]
    priority: 1
    # dimensions: 256   # optional; omitted = the model's own size
    price:
      input_per_mtok: 0
      output_per_mtok: 0
```

The same entry with a smaller vector size:

```yaml
models:
  - id: bge-m3-local
    name: BGE-M3 embeddings (local)
    provider: local
    model: bge-m3
    tags: [embedding]
    priority: 1
    dimensions: 256
    price:
      input_per_mtok: 0
      output_per_mtok: 0
```

To use the local one, pull the model with `ollama pull bge-m3` (standard Ollama
command, not run here), then remove the `# ` in front of the entry's lines.

Rules:

- The tag `embedding` cannot be combined with `chat`, `default` or
  `orchestrator`. The program refuses to start and names the entry and the tag,
  for example `invalid models.yaml: 0: Value error, model 'bad' is tagged
  'embedding' and cannot also be tagged 'chat'`.
- Embedding models are not listed in the GUI and terminal UI model selectors.
  `--model <id>` with an embedding model stops with `error: model '<id>' is
  tagged 'embedding' and cannot be used for chat`.
- There is no fallback between embedding models, because vectors from two models
  cannot be compared. The one with the highest `priority` is used; two at the
  same top priority are an error (`several embedding models share the top
  priority <n>: <ids>`).
- `dimensions` is optional and must be a positive integer (`0` is rejected). It
  is sent to the provider only when set, so leave it out to keep the model's own
  size; the model must support the value you ask for. The argument names are
  `dimensions` for Ollama, `dimensions` (with `encoding_format="float"`) for
  OpenAI-compatible providers and `output_dimensionality` for Vertex. Whether
  the real Vertex service accepts `output_dimensionality` is unverified.

### Storage

An optional `[storage]` table in `config.toml` chooses where data lives. Every
key is optional; this is what you get when it is absent (`<data dir>` is
`~/.local/share/llm-harness`):

```toml
# [storage.relational]
# backend = "sqlite"                    # the only backend available now
# path = "/absolute/path/harness.db"    # default: <data dir>/harness.db

# [storage.vector]
# backend = "qdrant-edge"               # or "none" to turn vector search off
# path = "/absolute/path"               # default: <data dir>/vectors

# [storage.files]
# backend = "local"                     # the only backend available now
# root = "/absolute/path"               # default: <data dir>/files
```

- Paths must be absolute or start with `~`. Like `user_id`, a `[storage...]`
  header closes the tables above it, so write it after `user_id`.
- `postgres` (relational) and `qdrant` (vector) are recognised but not
  available yet: the program stops with an error that names the backend and
  `storage-backends-remote`. An unknown backend is an error that lists the
  valid values, and an unknown key is an error too.
- Nothing is created at startup. Each directory and file appears on first use,
  with directory mode 0700 and file mode 0600.
- The usage ledger honours `[storage.relational] path`. On the first start with
  this version, an existing ledger file only gets a new `schema_versions` table;
  its rows are not touched and no backup copy is made.
- The vector store is held open by one llm-harness process at a time. A second
  process reports that the vector store is in use by another llm-harness
  process and continues without it.
- `qdrant-edge-py` (a native wheel of about 11 MB) is now a base dependency.

`llm-harness --storage-status` prints the state of the three stores and exits.
It needs no model and no terminal, and it creates nothing. Sample output
(`<data dir>` stands for the full path):

```text
relational: sqlite  <data dir>/harness.db
  file: not created yet (it is created on first use)
  schema: nothing to read yet
vector: qdrant-edge  <data dir>/vectors
  not created yet
files: local  <data dir>/files
  not created yet
```

Once the stores exist, the lines read like `schema: ledger version 1 (the code
expects 1)`, `schema: legacy file (user_version 1), it will be migrated on the
next start`, `collection tools: 4 dimensions, cosine, free` or `collection
tools: locked (in use by another llm-harness process)`.

## Model fallback and usage tracking

When a model fails, the harness moves to the next suitable one. The chain for a
turn is: the chosen model (your `--model` or selector pick, otherwise the
starting model), then the other models tagged `orchestrator` by `priority`
(highest first), then the models tagged `default` by `priority`. Duplicates are
removed; models with equal priority keep their order in `models.yaml`.

```yaml
models:
  - id: gemma-gpu
    name: Gemma on the GPU box
    provider: gpu-box
    model: gemma4:27b
    tags: [orchestrator, default]
    priority: 200
  - id: gemma-local
    name: Gemma 4 12B (local)
    provider: local
    model: gemma4:12b
    tags: [orchestrator, default]
    priority: 100
```

(`gpu-box` and `local` are `[providers.<id>]` tables in `config.toml`.)

- Fallback happens only for a failure before any output: error kinds
  `connection`, `rate_limit`, `not_found`, `unknown` and `auth`. A
  `bad_request` is shown directly, a failure after the reply started is shown
  as an error, and cancelling a reply is never a failure.
- A failing model is skipped for 1, 2, 3, 5, 10, 15, 30, then 60 minutes after
  consecutive failures (60 is the cap); a success resets it. The state is kept
  across restarts.
- A manual pick (`--model` or a change of the model selector) is tried even when
  the model is in backoff: a success resets it, a failure extends it and the
  chain continues.
- If every model failed or is in backoff, the turn ends with
  `no model available: ...; pick a model to try it now`. A single configured
  model whose error kind is `unknown` also ends this way, with the redacted
  error message inside, instead of the raw provider message.

Each switch prints a note, for example `gemma-gpu unavailable (connection);
using gemma-local`, `gemma-gpu is in backoff; using gemma-local` or
`gemma-gpu is not available (configuration); using gemma-local`. After each
reply a usage line follows:

```text
[gemma-local tokens in=12 out=31 $0.00 2.1s | session $0.00]
```

It shows the model, tokens (`reasoning=<n>` when reported), cost, time and the
session total. When a turn takes several steps, the usage line shows the last
step's call; the session total counts every call. Unknown values show as `?` (`tokens ?`, `$?`) and a session total
that misses an unknown cost ends with `+?`. Cost is (input tokens x
`input_per_mtok` + output tokens x `output_per_mtok`) / 1,000,000; an omitted
price counts as 0. In `--plain` the notes and the usage line go to stderr and
stdout holds only the reply.

Every model call, failed ones too, is recorded in a SQLite file,
`~/.local/share/llm-harness/harness.db` (directory mode 0700, file mode 0600;
the path can be changed with `[storage.relational] path`, see Storage).
It stores the model id, provider id, token counts, cost, time, outcome and error
kind, plus the backoff state. It never stores prompts, replies, error messages
or keys. If the file cannot be used, the chat keeps working, one notice says
that usage and backoff are not saved, and the file is left untouched.

## Orchestrator

Every message goes through an orchestrator. The model never sees the chat
history. Each step it gets one request with four parts: the instructions
(`src/llm_harness/prompts/orchestrator.md`), the generated JSON schemas of the
reply and the state, the current state as JSON, and the latest observation (your
latest message, or the result of the previous action). The chat transcript on
screen is for display only.

The state has these fields: `goal`; `goal_history` (the last 20 goal changes,
written by the harness); `findings` (each with a `text` and a non-empty
`source`); `plan` (steps with status `todo`, `doing`, `done` or `dropped`);
`environment` (date and time, user name, computer name and `user_id`,
refreshed by the harness at the start of every turn). `turn` and `step` are counters. The model
can change only `goal`, `findings` and `plan`.

The model answers with one JSON object with exactly two keys: `state_patch` and
`action`. The action is one of `respond` (the answer, ends the turn), `ask_user`
(a question, ends the turn; your next message is the answer), `call_tool` and
`delegate`. No tools or subagents exist yet, so `call_tool` reports an unknown
tool and `delegate` answers that it is not available yet.

- Each step prints a status line: `[step 1: answering]` on stderr in `--plain`;
  the terminal UI and the GUI show `step 1: answering` as a note.
- Replies appear whole when the step has finished, because the model's reply is
  JSON. Thinking text still streams while the model thinks.
- A rejected reply is retried up to 2 times (3 attempts) with the error text as
  the new observation; a turn ends after 8 steps. One code fence around the
  whole reply is accepted; anything else around the JSON is rejected.
- The reply schema is sent to the model as a constraint only by the Ollama
  adapter (`format`); the OpenAI-compatible and Vertex adapters ignore it, so
  there the contract rests on the instructions and the retries.
- The state is capped at 8000 characters; a patch that would exceed it is
  rejected.

## Context inspector (GUI and terminal UI)

The web GUI and the terminal UI have three tabs: `Chat`, `State` and `Trace`.
`--plain` has no inspector. The tabs refresh after every step, at the end of
every turn, on New chat and (terminal UI) on a model change. When a turn ends,
the prompt gets the focus back.

- `State` shows the orchestrator state in collapsible sections: goal, findings,
  plan and environment (date and time, user name, computer name and `user_id`,
  or `user_id: (not set)`), plus the turn and step counters. Below them is a preview
  of the next request, built by the same code that builds the real one, with
  the character counts of the system message, the state JSON and the
  observation (the text typed in the prompt). In the terminal UI the system
  message and the state JSON start collapsed.
- `Trace` shows the calls of the LAST turn as nested expansions: turn, then
  step, then `llm_call` or `tool_call`. Each title reads
  `kind: name | status | N ms`. Open one to see its full, untruncated details:
  the messages sent, thinking text, reply, rejection reason, tool arguments and
  result, usage, `cost_usd`, `seconds`, `model` and `reported_model`. Secrets
  are masked (`[REDACTED:api_key]`). A new turn replaces the trace; it is kept
  in memory only. Before the first turn the terminal UI shows `No trace yet.`
- `Export state` (GUI) downloads `llm-harness-state.yaml`: the state and the
  pending question (if the model asked you something). The trace is not
  exported.
- `Import state` (GUI) opens a file picker for a `.yaml` file and loads it as the
  orchestrator state. The file must be UTF-8 text of at most 1,000,000 bytes.
  YAML aliases and Python tags are rejected, and the content is validated
  against the state model (unknown keys, wrong types and a date without a time
  zone are errors). A rejected file shows one line, `import failed: ...`, and
  changes nothing. Import is refused while a turn is running (the buttons are
  disabled then). The on-screen chat transcript is not restored, and the
  environment (date and time, user name, computer name, `user_id`) is overwritten at the
  start of the next turn.
- In the GUI the messages `state loaded`, `state exported` and
  `import failed: ...` appear below the two buttons.

Terminal UI only:

- While the `State` or `Trace` tab is active, the focus is on the tabs and
  sections (Enter opens or closes a section); go back to `Chat` to type.
- `/export [path]` writes the same YAML as `Export state`. Without a path the
  file is `llm-harness-state.yaml` in the current directory; `~` is expanded.
  It refuses to overwrite an existing file, a missing parent directory and a
  directory. The file is created with mode 0600 and a symlink is not followed.
  It also works while a turn is running.
- `/import <path>` loads a state file with the same rules as `Import state`
  (regular file only, 1,000,000 bytes at most, checked before reading). Any
  failure shows one line, `import failed: ...`, and changes nothing. It is
  refused while a turn is running.
- Both commands are handled locally and never sent to the model. Only the first
  word is the command (`/exportx` is an ordinary message). On success the
  prompt is cleared and a note line says `state exported to <path>` or
  `state loaded from <path>`; on failure the typed command stays in the prompt
  so the path can be fixed.

## Flags

| Flag | Effect |
| --- | --- |
| `--model ID` | use the model with this `id` from `models.yaml` as the orchestrator model; it needs no `orchestrator` tag |
| `--think` | ask the model to think |
| `--no-think` | turn thinking off |
| `--plain` | line-based chat (mutually exclusive with `--gui`) |
| `--gui` | web GUI (mutually exclusive with `--plain`) |
| `--storage-status` | show the state of the three stores and exit; changes nothing (exclusive with `--plain` and `--gui`; see Storage) |

`--think` and `--no-think` are mutually exclusive. Precedence: flag, then the
model's `thinking` key, then the model's own default.

## Chat commands

`/quit` ends the application in every front end; the terminal UI and `--plain`
also accept `/exit` (the terminal UI also quits with Ctrl+Q). The command must
be the whole message, any letter case; it is not sent to the model (`/quit now`
is an ordinary message). A running reply is cancelled first.

The terminal UI also has `/export [path]` and `/import <path>`; see
[Context inspector](#context-inspector-gui-and-terminal-ui). They are not sent to
the model, and only the first word is the command.

## Markdown

Assistant replies in the GUI and the terminal UI are rendered as markdown:
CommonMark plus tables, strikethrough, task lists, footnotes, links and
highlighted code (the list of extensions is the `_EXTENSIONS` tuple in
`src/llm_harness/markdown_render.py`). Raw HTML in a reply is not interpreted:
it is shown as text. Images are never rendered or loaded (an image shows as its
alt text). Links open only for `http`, `https`, `mailto` and `#fragment`. The
terminal UI does not support task lists and footnotes and shows their syntax as
text. Thinking text, the usage line, error lines and your own messages stay
plain text, and `--plain` stays plain text.

## Providers

| Provider | Status |
| --- | --- |
| Ollama | verified live |
| Ollama through its OpenAI-compatible `/v1` endpoint (`type = "openai"`) | verified live |
| Vertex AI | verified live |
| OpenRouter | works |
| OpenAI | implemented from documentation, untested |

OpenAI has only offline tests built from documented response shapes; it is not
known whether it accepts the `thinking` settings as sent.

## Security notes

- API keys are masked in output (`[REDACTED:api_key]`), and error messages are
  redacted before they are shown.
- The program warns on stderr when `config.toml` is readable or writable by
  group or other (`chmod 600`) or the config directory is writable by group or
  other (`chmod 700`).
- The GUI has no login. It binds `127.0.0.1` only and answers HTTP 403 to any
  request whose Host or Origin is not `127.0.0.1:<port>` or `localhost:<port>`.
- `/export` in the terminal UI creates the file exclusively with mode 0600 and
  never overwrites a file or follows a symlink; `/import` reads only a regular
  file of at most 1,000,000 bytes and rejects YAML aliases and Python tags.
- Model output is sanitized (escape and control characters removed). Replies are
  rendered as markdown, never as raw HTML: in the GUI the rendered HTML also goes
  through an allowlist sanitizer (no scripts, no images, restricted links).
- Ambient SDK environment variables such as `OPENAI_*`, `OLLAMA_*` and
  `GOOGLE_*` are ignored; connection settings come only from the config.

## Troubleshooting

- A proxy variable such as `HTTP_PROXY` may route local Ollama traffic through
  the proxy. Try `NO_PROXY=localhost,127.0.0.1` (not verified here).
- Vertex needs Application Default Credentials, created with
  `gcloud auth application-default login` (standard gcloud command, not run
  here).
- An unreachable Vertex endpoint takes 1-10 s to report an error.
- `--no-think` may be rejected by models that cannot disable thinking.
- The GUI port is fixed at 8080; there is no port flag.
- `error: no model is tagged 'orchestrator'; tag one in models.yaml or use --model`
  appears before the app starts. Add `orchestrator` to the `tags` of at least one
  model, or pass `--model <id>`. Config files made before the orchestrator need
  this.
- `error: several orchestrator models share the top priority <n>: <ids>` appears
  before the app starts. Give one of them a higher `priority`.
- `error (unknown): the orchestrator reply was rejected 3 times; last error:
  <text>` means the model did not follow the JSON contract in 3 attempts; try a
  stronger model or `--model`. State from earlier valid steps is kept.
- `error (unknown): the orchestrator reached the step cap of 8 steps without a
  respond or ask_user action` means the model kept calling tools or delegating;
  ask again more simply. (The remedies for the last two are advice, not tested.)

## Tests

```sh
uv run pytest              # offline, no network
uv run ruff check
uv run pytest -m live      # live tests, call real providers
uv run pytest -m live tests/test_live_ollama.py -k embed -rs   # embedding live test only
```

Live tests read your real config. `LLM_HARNESS_LIVE_MODEL` (OpenAI-compatible
test) and `LLM_HARNESS_LIVE_VERTEX_MODEL` (Vertex test) are test-only variables
that choose the model; the application never reads them.

The embedding live test embeds two short texts with the `embedding` model of
your config. It skips, and `-rs` prints the reason, when no `embedding` entry
exists, the entry is not on an `ollama` provider, or Ollama or the model is not
reachable.

## Known limits

- The model sees no earlier messages, only the state; what the state does not
  hold is forgotten.
- No tools and no subagents yet: the model is told not to use `call_tool` or
  `delegate`, and `delegate` is answered with `delegate is not available yet`.
- Usage and backoff are only saved if the SQLite file can be used; the new note
  and usage lines were not looked at in a real browser or terminal.
- OpenAI is untested.
- Nothing was verified in a real browser except by the author's own manual run.
- Markdown was not looked at in a real browser or terminal (layout, CSS and
  code colours are unchecked); table column alignment is dropped in the GUI.
