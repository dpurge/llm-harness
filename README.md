# llm-harness

A personal harness for trying LLMs from Ollama, OpenAI-compatible APIs
(OpenAI, OpenRouter) and Vertex AI. Today it streams chat with a plain line
mode, a terminal UI and a web GUI, and shows the model's thinking text when the
model provides it. It falls back to another model when one fails and records
what each call cost. The orchestrator and tools do not exist yet.

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

`config.toml` - connection settings, one `[providers.<id>]` table per provider:

```toml
[providers.local]
type = "ollama"
base_url = "http://localhost:11434"

# [providers.openai]
# type = "openai"
# base_url = "https://api.openai.com/v1"
# api_key_env = "OPENAI_API_KEY"   # name of an environment variable, or:
# api_key = "sk-..."               # an inline key (not both)
# reasoning_style = "openai"       # or "openrouter"; how `thinking` is sent

# [providers.vertex]
# type = "vertex"
# project = "my-gcp-project"
# location = "us-central1"
```

- `type` is `ollama`, `openai` or `vertex`. OpenRouter is an `openai` provider
  with its own `base_url` and `reasoning_style = "openrouter"`.
- A key is given inline (`api_key`) or by the name of an environment variable
  (`api_key_env`), never both. Vertex uses Application Default Credentials and
  takes no key.

`models.yaml` - the model list (no secrets):

```yaml
models:
  - id: gemma-local
    name: Gemma 4 12B (local)
    provider: local
    model: gemma4:12b
    tags: [default, chat, cheap]
    priority: 100
    # thinking: false   # optional; omitted = the model's own default
    price:
      input_per_mtok: 0
      output_per_mtok: 0
```

- `provider` is the id of a provider in `config.toml`; `model` is the
  provider's own model id.
- The default model is the one tagged `default` with the highest `priority`
  (a higher number wins). Two such models with the same top priority are an
  error.
- `price` gives the cost per million input and output tokens; an omitted price
  is 0. `tags` of `chat` and `default` drive the fallback chain (below).

## Model fallback and usage tracking

When a model fails, the harness moves to the next suitable one. The chain for a
chat turn is: the chosen model (your `--model` or selector pick, otherwise the
default), then the other models tagged `chat` by `priority` (highest first),
then the models tagged `default` by `priority`. Duplicates are removed; models
with equal priority keep their order in `models.yaml`.

```yaml
models:
  - id: gemma-gpu
    name: Gemma on the GPU box
    provider: gpu-box
    model: gemma4:27b
    tags: [chat, default]
    priority: 200
  - id: gemma-local
    name: Gemma 4 12B (local)
    provider: local
    model: gemma4:12b
    tags: [chat, default]
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
session total. Unknown values show as `?` (`tokens ?`, `$?`) and a session total
that misses an unknown cost ends with `+?`. Cost is (input tokens x
`input_per_mtok` + output tokens x `output_per_mtok`) / 1,000,000; an omitted
price counts as 0. In `--plain` the notes and the usage line go to stderr and
stdout holds only the reply.

Every model call, failed ones too, is recorded in a SQLite file,
`~/.local/share/llm-harness/harness.db` (directory mode 0700, file mode 0600).
It stores the model id, provider id, token counts, cost, time, outcome and error
kind, plus the backoff state. It never stores prompts, replies, error messages
or keys. If the file cannot be used, the chat keeps working, one notice says
that usage and backoff are not saved, and the file is left untouched.

## Flags

| Flag | Effect |
| --- | --- |
| `--model ID` | use the model with this `id` from `models.yaml` |
| `--think` | ask the model to think |
| `--no-think` | turn thinking off |
| `--plain` | line-based chat (mutually exclusive with `--gui`) |
| `--gui` | web GUI (mutually exclusive with `--plain`) |

`--think` and `--no-think` are mutually exclusive. Precedence: flag, then the
model's `thinking` key, then the model's own default.

## Chat commands

`/quit` ends the application in every front end; the terminal UI and `--plain`
also accept `/exit` (the terminal UI also quits with Ctrl+Q). The command must
be the whole message, any letter case; it is not sent to the model (`/quit now`
is an ordinary message). A running reply is cancelled first.

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
| OpenRouter | implemented from documentation, untested |
| OpenAI | implemented from documentation, untested |

OpenRouter and OpenAI have only offline tests built from documented response
shapes; it is not known whether they accept the `thinking` settings as sent.

## Security notes

- API keys are masked in output (`[REDACTED:api_key]`), and error messages are
  redacted before they are shown.
- The program warns on stderr when `config.toml` is readable or writable by
  group or other (`chmod 600`) or the config directory is writable by group or
  other (`chmod 700`).
- The GUI has no login. It binds `127.0.0.1` only and answers HTTP 403 to any
  request whose Host or Origin is not `127.0.0.1:<port>` or `localhost:<port>`.
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

## Tests

```sh
uv run pytest              # offline, no network
uv run ruff check
uv run pytest -m live      # live tests, call real providers
```

Live tests read your real config. `LLM_HARNESS_LIVE_MODEL` (OpenAI-compatible
test) and `LLM_HARNESS_LIVE_VERTEX_MODEL` (Vertex test) are test-only variables
that choose the model; the application never reads them.

## Known limits

- The full chat history is sent on every turn (temporary).
- No orchestrator or tools yet.
- Usage and backoff are only saved if the SQLite file can be used; the new note
  and usage lines were not looked at in a real browser or terminal.
- OpenRouter and OpenAI are untested.
- Nothing was verified in a real browser except by the author's own manual run.
- Markdown was not looked at in a real browser or terminal (layout, CSS and
  code colours are unchecked); table column alignment is dropped in the GUI.
