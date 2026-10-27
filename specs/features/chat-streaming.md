---
title: Chat streaming across providers
kind: feature
status: done
version: 5
updated: 2026-10-27
branch: none
---

## Problem / Motivation

Nothing exists yet. This is the first runnable slice of llm-harness: chat with streaming over the three provider families through neutral types.

- Providers: Ollama (`ollama` SDK), OpenAI and OpenRouter (`openai` SDK, OpenRouter through `base_url`), Vertex AI (`google-genai`).
- Models are declared in `~/.config/llm-harness/models.yaml`; connection settings in `~/.config/llm-harness/config.toml`.
- Three front ends: plain line mode (`--plain`), a Textual terminal UI and a NiceGUI web GUI.
- The full chat history is sent to the model in this slice. This is temporary: the constitution's non-goal ("Sending the full chat history to the model") has a matching exception for `chat-streaming`, removed later by `orchestrator-state`. The constitution is already updated for this.

Deliberately not here: orchestrator and goal/context state, tools, A2UI, fallback to lower-priority models, token and price tracking (roadmap `model-registry`).

Decisions by the user:

1. One spec, five ordered steps (see Approach), each runnable and separately verified.
2. Full history is sent now. No system prompt in this slice (assumption, safe default). The fake provider is test-only; there is no `--provider fake` (assumption).
3. `config.toml` entries are `[providers.<id>]` with `type` (`ollama`|`openai`|`vertex`), `base_url`, `api_key` or `api_key_env` (not both) and, for Vertex, `project` and `location`.
4. `models.yaml` is a list under `models:`. Each entry has `id`, `name` (display), `provider` (a provider id from `config.toml`), `model` (the provider's own model id), `tags` (list), `priority` (number, HIGHER wins) and `price` {`input_per_mtok`, `output_per_mtok`} (optional, default 0; read and validated, not used yet). These key names are confirmed.
5. Default model = the model tagged `default` with the highest priority. The user's default is `gemma4:12b` on local Ollama. `--model <id>` overrides.
6. Command line (confirmed): `llm-harness` starts the terminal UI; `llm-harness --gui` starts the web GUI; `llm-harness --plain` is a permanent line-based chat (works over pipes and in tests; the only runnable UI in steps 1-3); `--model <id>`.
7. Thinking in config (confirmed names): per model in `models.yaml` an optional key `thinking: true|false` (omitted = the model's own default). The CLI flags `--think` and `--no-think` (mutually exclusive) override it for one run. Precedence: CLI flag > model `thinking` > model default (the request's `think` is then None). This replaces the earlier proposal "`--think` (ask the model to emit reasoning; off by default)". Reason, found live: gemma4:12b thinks by default, so without a way to turn thinking off the user could not disable it. `--config-dir PATH` is NOT included (YAGNI).
8. First run: if `models.yaml` (or `config.toml`) is missing, stop with a message naming the path and offering to write an example file. Write only on an explicit yes. In non-interactive mode never write: print the example and exit non-zero. No hidden built-in default model.
9. Neutral stream events from adapters: thinking delta, text delta, usage, done, error. UIs show thinking dimmed/collapsed; thinking is never added to history. Providers without reasoning emit none.
10. uv is the only project manager (constitution). Editable install: `uv sync` + `uv run llm-harness ...`, or `uv tool install --editable .`; `git pull` + restart applies changes. Python >= 3.12 (assumption carried from the constitution).
11. New optional provider key in `config.toml`, valid only on `type = "openai"` providers (confirmed name and values): `reasoning_style = "openai" | "openrouter"`, default `"openai"`. It selects how the OpenAI-compatible adapter sends the thinking toggle. Style `openai`: `thinking: false` -> request `reasoning_effort="none"`; `thinking: true` -> `reasoning_effort="medium"`; None -> nothing sent. Style `openrouter`: `thinking: false` -> `reasoning: {"enabled": false}`; `thinking: true` -> `reasoning: {"enabled": true}` (sent through the SDK's extra request body); None -> nothing sent. A `reasoning_style` on an ollama or vertex provider is a config error.
12. OpenRouter cannot be tested from the user's computer: the OpenAI-compatible adapter is implemented OFFLINE for OpenRouter from its documentation (https://openrouter.ai/docs/guides/best-practices/reasoning-tokens, fetched 2026-10-27, NOT verified live) and the user will test it later. The real OpenAI service is not tested at all (no credentials; whether it accepts `reasoning_effort="none"` or `"medium"` for a given model is unverified). Verified live on 2026-10-27 (openai SDK 3.40.0 against local Ollama's OpenAI-compatible endpoint `http://localhost:11434/v1`, model gemma4:12b): streaming works; `stream_options={"include_usage": True}` delivers usage (prompt_tokens 23, completion_tokens 81 in the probe) and `completion_tokens_details` is None (so reasoning_tokens stays None there; completion_tokens includes thinking); thinking text arrives as an extra delta field named `reasoning` (a string); `reasoning_effort="none"` turns thinking off (no reasoning chunks, 2 completion tokens); `extra_body={"think": False}` does NOT turn it off; `max_completion_tokens` is accepted.
13. Test plan change: the OpenAI-compatible adapter is tested LIVE against local Ollama through its `/v1` endpoint (a provider `type = "openai"`, `base_url = "http://localhost:11434/v1"`, `api_key = "ollama"`; Ollama ignores the key, so the example needs no real credentials). OpenRouter and OpenAI real services get offline tests only, built from synthetic chunks following the documented shapes; the live OpenRouter check is done by the user later.
14. The web GUI lives in the package `src/llm_harness/gui/` (module `gui/app.py`), not `web/app.py` (user decision 2026-10-27). The command line is unchanged: `llm-harness --gui`.

## Acceptance Criteria

Stream contract
1. Every stream yields zero or more thinking/text deltas, at most one usage event, then exactly one terminal `Done` or `ErrorEvent`. One parametrized contract test covers all adapters.
2. Cancelling (task cancel or `aclose()`) closes the SDK stream and lets `CancelledError` propagate.
3. SDK exceptions become `ErrorEvent` (kind one of `auth|connection|not_found|rate_limit|bad_request|unknown`), message passed through `redact()` and truncated to about 300 characters. Programming errors still raise.

History
4. Thinking is never added to history.
5. On error or cancel the user message is removed from history (the UI puts the text back).
6. On turn N the provider receives the whole history (all prior user and assistant messages plus the new user message).

Model selection
7. With no `--model`, the model tagged `default` with the highest `priority` is chosen.
8. `--model <id>` overrides the default.
9. Errors: unknown `--model` id (message lists valid ids); no model tagged `default`; two `default` models with the same top priority (assumption).

Config
10. `config.toml` validation (`extra=forbid`): `api_key` and `api_key_env` not both; `openai` requires one of them; `vertex` requires `project` and `location`; `ollama` rejects a key in this slice.
11. `models.yaml` validation: unique ids; `provider` must exist in `config.toml`; `priority` numeric; `tags` list of strings; `price` values non-negative; read with `yaml.safe_load` only.
12. Keys are held in a wrapper whose `repr` and `str` are `[REDACTED:api_key]`; unwrapped only in `build_provider`.
13. A warning on stderr naming the path and suggesting `chmod 600` when `config.toml` mode has `mode & 0o077` set.
14. `api_key`, `base_url`, `project`, `location` are always passed explicitly to SDKs (no fallback to `OPENAI_API_KEY`, `GOOGLE_CLOUD_PROJECT` and similar).

First run
15. Missing `models.yaml` or `config.toml`: message names the path and offers an example file; interactive: written only on an explicit yes; non-interactive: nothing written, example printed, exit code non-zero.

Front ends
16. `llm-harness --plain` works over a pipe (input from stdin, streamed output to stdout).
17. TUI and GUI show streamed text, dimmed/collapsible thinking, and a model selector fed from `models.yaml`.
18. The GUI binds `127.0.0.1` explicitly, holds one `ChatSession` per client (never global), and renders model output as plain text in labels, never as HTML or markdown.

Providers (live)
19. Ollama streams live (required): text, thinking with `--think`, usage line.
20. The OpenAI-compatible adapter streams live against local Ollama's `/v1` endpoint (step 2); Vertex streams live in step 3 with the user's credentials; the OpenRouter live check is done by the user later (not runnable on this machine).

Safety and tooling
21. A canary test shows no API key and no prompt text in logs or error output. Prompts and responses are never logged; stdlib logging level is WARNING by default.
22. Editable install check: after `uv tool install --editable .`, editing a string in `cli.py` is visible on the next run with no reinstall.
23. `uv run ruff check` and `uv run pytest` pass offline with zero skips and no network. Live tests are deselected (`addopts = -m "not live"`), not skipped; the validation record counts them separately.
24. Non-test call-site rule: every new function, type and config key has a non-test use.

Added at approval
25. Until step 4, `llm-harness` with no flags prints a short message pointing to `--plain` and exits non-zero.
26. The example file written on first run contains one local Ollama provider and one `gemma4:12b` model tagged `default`.

Added 2026-10-27 (thinking in config, OpenAI-compatible adapter)
27. Precedence is CLI flag > model `thinking` > default None; `--think` and `--no-think` are mutually exclusive.
28. `models.yaml` `thinking` is an optional boolean (a non-bool is rejected) and the config examples mention it.
29. `reasoning_style` is accepted only on `openai` providers, with values `openai` or `openrouter` (default `openai`); anything else is rejected.
30. The Ollama adapter sends `think=False`, `think=True` or nothing according to the request, and `think=False` is verified live to produce no thinking.
31. The OpenAI-compatible adapter maps the toggle per `reasoning_style` as in decision 11 and extracts thinking from `reasoning_details`, `reasoning` or `reasoning_content` as in the Approach (offline tests from synthetic chunks; live against Ollama `/v1` for the `openai` style, including `thinking: false` producing no thinking).
32. Usage comes from the final chunk (`stream_options` `include_usage`): input = `prompt_tokens`, output = `completion_tokens`, reasoning = `completion_tokens_details.reasoning_tokens` when present, else None.
33. The thinking text of an unreachable or untested provider (OpenRouter) is documented as unverified in the final delivery.

Added 2026-10-27 after the validation review (security and robustness fixes)
34. Network failures of all three adapters (connection refused, DNS failure, server disconnect, timeout, mid-stream payload errors, including Vertex under the aiohttp backend) become exactly one terminal `ErrorEvent(kind="connection")`; the tests use the real SDK clients against a closed local port and real aiohttp exception types, not only mocked httpx errors.
35. Each adapter hides the environment variables its SDK reads at client construction (OpenAI: `OPENAI_*`; Vertex: eleven `GOOGLE_*`/`GEMINI_*` variables; Ollama: `OLLAMA_API_KEY` and `OLLAMA_HOST`) under a module lock and restores them afterwards (criterion 14 extended to Ollama).
36. An unexpected exception in the TUI or the GUI is shown as one line `error (unexpected): <ExceptionType>: <redacted message>` with no traceback and no locals, the text is restored to the input, the input is re-enabled and the app keeps running.
37. The GUI serves only requests whose Host is `127.0.0.1:<port>` or `localhost:<port>` and whose Origin, if present, is `http://127.0.0.1:<port>` or `http://localhost:<port>`; anything else gets HTTP 403 (a websocket is closed with code 1008 before it is accepted), including NiceGUI's socket.io path.
38. Config robustness: `base_url` must be an http(s) URL with a host and without credentials; `priority` and `price` must be finite; an unreadable or non-UTF-8 config file raises ConfigError (never a traceback); the example files are created with mode 0600 (`config.toml`) and 0644 (`models.yaml`) at creation and the config directory with 0700.
39. Permission warnings name the path and the fix: `config.toml` readable by group/other, `config.toml` writable by group/other (`chmod 600`), and a config directory writable by group/other (`chmod 700`); a readable-only directory (0755) gives no warning.
40. Text shown by the CLI, TUI and GUI is sanitized: ESC, C0 controls except newline and tab, DEL, C1, carriage return, the bidi controls U+202A-U+202E and U+2066-U+2069, and U+2028/U+2029 are removed; ZWJ, ZWNJ, LRM, RLM, ALM and variation selectors are kept, so Persian, Arabic, Hebrew, Japanese and emoji text passes unchanged. Model names from models.yaml are shown literally in the TUI selector (never parsed as markup).
41. Redaction, applied before truncation, also masks `bearer` tokens in any case (8+ token characters with a digit or 20+ characters), `Authorization`/`x-api-key`/`api-key` header values, Google API keys (`AIza` + 35 characters), credentials in URLs and JWT-shaped strings, in addition to `sk-` keys and the configured key; ordinary prose and URLs without credentials are untouched.
42. Ctrl-C at the `you>` prompt of `--plain` exits with code 130 at once, without pressing Enter, and prints no traceback (verified in a pseudo-terminal).
43. The stream-contract test helper enforces the documented order: deltas before the usage event, at most one usage event, the terminal event last.

## Approach

Memory consulted: chat|stream|provider|adapter|ollama|openai|vertex — none relevant (specs/memory.md does not exist yet)

Layout `src/llm_harness/`: `types.py`, `session.py`, `config.py` (loads `config.toml` and `models.yaml`), `redact.py`, `cli.py` (stdlib `argparse`), `providers/{base,__init__,ollama,openai_compat,vertex}.py`, `tui/app.py`, `gui/app.py`. Tests in `tests/`; scripted fake provider in `tests/fakes.py`. The module is `openai_compat.py` to avoid clashing with the `openai` package.

Types (pydantic, frozen):
- `Message(role: system|user|assistant, content)`
- `ChatRequest(model, messages, think: bool|None)`
- `Usage(input_tokens, output_tokens, reasoning_tokens)`, with `output_tokens` including reasoning
- Events `ThinkingDelta`, `TextDelta`, `UsageEvent`, `Done(finish_reason)`, `ErrorEvent(kind, message)`; `StreamEvent` is their union. No `ToolCall` yet.

Provider protocol `ChatProvider`: `stream_chat(req) -> AsyncIterator[StreamEvent]` and `aclose()`.
- Contract and cancellation as in criteria 1-3; adapters close the SDK stream in `finally`.
- Rejected alternative: raising a neutral `ProviderError`.
- Adapters take an injected SDK client. `build_provider(cfg)` builds the client and imports the adapter lazily, so only adapter modules import an SDK and startup does not load all three.

`ChatSession(provider, model, think)` holds history. `send(text)` adds the user message, re-yields events and joins text deltas; on `Done` it adds the assistant message; thinking is never added; on error or cancel it removes the user message. The whole history is sent each turn.

Usage mapping (assumptions, verified in steps 1b/2/3):
- Ollama: input = `prompt_eval_count`, output = `eval_count`, from the done chunk.
- OpenAI/OpenRouter: `stream_options={"include_usage": True}`; usage on the last chunk with empty `choices`; input = `prompt_tokens`, output = `completion_tokens`, reasoning = `completion_tokens_details.reasoning_tokens` when present, else None. Verified on Ollama `/v1` (usage delivered, `completion_tokens_details` None, `completion_tokens` includes thinking); not verified on OpenRouter or OpenAI.
- Vertex: input = `prompt_token_count`, output = `candidates_token_count` + `thoughts_token_count`, reasoning = `thoughts_token_count`; last chunk with `usage_metadata`.

Thinking mapping: Ollama `message.thinking` (verified live); OpenAI-compatible: `delta.reasoning` (a string, verified live on Ollama `/v1`; the installed openai SDK declares no such ChoiceDelta field, so it is read from the delta's extra fields), `delta.reasoning_details` (documented by OpenRouter, unverified: entries with `type` `reasoning.text` use `text`, `reasoning.summary` use `summary`, `reasoning.encrypted` is ignored), `delta.reasoning_content` (unverified); per chunk, if `reasoning_details` is present use it, otherwise `reasoning`, otherwise `reasoning_content`, never more than one for a chunk (avoids double output); Vertex parts with `thought` true (unverified).

Thinking toggle: `ChatRequest.think` is resolved in `cli.py` as CLI flag (`--think`/`--no-think`, mutually exclusive) > model `thinking` > None, and passed to `ChatSession`. The Ollama adapter sends `think=req.think`. The OpenAI-compatible adapter maps it per the provider's `reasoning_style` (decision 11).

Config rules are listed in criteria 10-14. A tie of two top-priority `default` models is an error. `--model` with an unknown id lists valid ids.

Libraries (versions per constitution): ollama 0.6.3, openai 3.40.0, google-genai 2.33.0, textual 8.2.8, nicegui 3.18.0, pydantic 2.14.1, pyyaml, pytest, pytest-asyncio, ruff.

Ordered steps. Each step is runnable and separately verified.

Step 1a (offline)
- Files: `pyproject.toml` (uv, src layout, `[project.scripts] llm-harness`), `.python-version`, `.gitignore` (must include `.agent/`), `types.py`, `redact.py`, `config.py`, `session.py`, `providers/base.py`, `providers/__init__.py`, fake provider in `tests/fakes.py`, `cli.py` with `--plain` and the first-run message.
- Runnable: `--plain` against the fake provider in tests; first-run message.
- Verify: `uv sync`; `uv run ruff check`; `uv run pytest` (offline); `uv run llm-harness --help`; `uv run llm-harness --plain` with a missing `models.yaml` prints the message and exits non-zero.

Step 1b (live Ollama)
- Files: `providers/ollama.py`.
- Verify: `uv run llm-harness --plain` against local `gemma4:12b` streams thinking (with `--think`), text and a usage line; `uv run pytest -m live -k ollama`; editable check: `uv tool install --editable .`, edit a string in `cli.py`, rerun with no reinstall, change visible.
- Retires: editable install, stream contract, history handling, permission warning, Ollama `think`.

Step 1c (thinking in config, Ollama SDK)
- Files: `config.py` (`thinking` in `models.yaml`: optional bool), `cli.py` (`--think`/`--no-think`, precedence), wiring into `ChatSession`'s think value, the config examples.
- Verify: offline tests for validation and precedence; live on Ollama with the `ollama` SDK that `think=False` really disables thinking and `think=True` enables it (`think=False` is NOT yet verified on the SDK path).
- Retires: Ollama SDK `think=False`.

Step 2
- Files: `providers/openai_compat.py` (OpenAI-compatible adapter for OpenAI and OpenRouter, with `reasoning_style`), `config.py` (`reasoning_style`), the openai SDK dependency (already added to `pyproject.toml` by the coordinator: openai 3.40.0).
- Verify: live against local Ollama's `/v1` endpoint (provider `type = "openai"`, `base_url = "http://localhost:11434/v1"`, `api_key = "ollama"`), including `thinking: false` producing no thinking; OpenRouter is implemented and tested OFFLINE only, from synthetic chunks following its documentation, and the user tests it live later.
- Request cap parameter: `max_completion_tokens` (accepted by Ollama `/v1`; unverified for OpenRouter and OpenAI).
- Retires: live streaming and the `include_usage` chunk on Ollama `/v1`, the `reasoning` delta field on Ollama `/v1` and `reasoning_effort="none"` there.

Step 3
- Files: `providers/vertex.py`.
- Verify: live with a flash-class model.
- Retires: `genai.Client(vertexai=True, project=, location=)` signature (unverified), ADC with explicit project and location, thought parts with `include_thoughts`, token semantics, and that no `GOOGLE_CLOUD_PROJECT` fallback happens (test with it unset).

Step 4
- Files: `tui/app.py` (Textual TUI, `llm-harness` with no flags): model selector from `models.yaml`, dimmed/collapsible thinking, cancel on Escape.
- Verify: offline Pilot test with the fake provider (Textual Pilot API unverified until checked); manual run on Ollama.

Step 5
- Files: `gui/app.py` (NiceGUI page, `llm-harness --gui`): one `ChatSession` per client, model selector, escaped output, host `127.0.0.1`.
- Verify: offline test with the NiceGUI `user` fixture (unverified until checked); manual run on Ollama.

Testing strategy:
- Default pytest is offline and takes seconds (`addopts = -m "not live"`).
- Session tests drive `ChatSession` with a scripted provider: thinking excluded from history, rollback on error and on `task.cancel()`, full history sent on turn N.
- Adapter tests inject a fake SDK client returning an async iterator built from the real SDK types (`ollama.ChatResponse`, openai `ChatCompletionChunk`, genai `GenerateContentResponse`); one parametrized contract test over all adapters.
- Config tests cover every validation rule, key masking (`repr`/`str`), the permission warning, the first-run message, default-model selection and the tie error.
- Live tests: marker `live`; read the real home config; skip with a reason if the provider or model is missing; each call inside `asyncio.timeout` (90 s for the first Ollama call because the measured cold start was 21.1 s, 30 s for others); output capped at about 32 tokens; one-line prompts; one live test per adapter (the OpenAI-compatible one runs against Ollama `/v1`; OpenRouter and OpenAI have offline tests only).

Security: the web GUI binds `127.0.0.1` explicitly; model output is never rendered as raw HTML; prompts and responses are never logged.

Review fixes (2026-10-27): the Vertex adapter handles aiohttp exceptions (the aiohttp backend is used once aiohttp is installed) and `aiohttp` is a declared dependency (criterion 34). The Ollama adapter hides `OLLAMA_API_KEY` and `OLLAMA_HOST` while the client is built, and the shared helper holds a module lock during the hide-and-restore window (criterion 35). The TUI and the GUI catch any unexpected exception and show the single line `error (unexpected): <Type>: <redacted message>` (criterion 36). A Host/Origin middleware in `gui/security.py` is registered by `run_gui` and answers 403 to any other Host or Origin (criterion 37). Config hardening adds `base_url` and finite-number checks, read-error handling and creation modes, and `permission_warnings` replaces `check_permissions` (criteria 38, 39). The sanitizer is stricter about bidi, line-separator and carriage-return characters while keeping joiners (criterion 40), and redaction gains more patterns (criterion 41). `cli.py` reads stdin in a daemon thread so Ctrl-C at the prompt exits at once (criterion 42), and the stream-contract helper is stricter (criterion 43).

Unverified:
- OpenRouter `reasoning_details` shapes.
- OpenRouter `delta.reasoning`.
- Whether OpenAI and OpenRouter accept the `reasoning_effort` values used.
- `max_completion_tokens` on OpenRouter and OpenAI.
- Ollama SDK `think=False`.
- Vertex client signature and thought parts.
- Token semantics per provider.
- Textual Pilot and NiceGUI `user` fixture APIs.
- Whether Ollama `eval_count` includes thinking tokens.
- A real mid-stream aiohttp failure from the real Vertex SDK (only real exception types through fakes).
- Browser behaviour: Private Network Access, a real browser's socket.io/websocket use against the Host/Origin check, and how the GUI looks.
- Whether starlette (used by gui/security.py through NiceGUI) should be a declared dependency.
- --no-think on models that cannot disable thinking (Vertex Pro-class models reject thinking_budget=0; older OpenAI reasoning models may reject reasoning_effort="none").

Assumptions:
- Python 3.12 floor.
- No system prompt in this slice.
- Fake provider is test-only.
- Tie error on two top-priority `default` models.
- `thinking: true` maps to `reasoning_effort="medium"`.
- Per-chunk precedence of the reasoning fields: `reasoning_details`, then `reasoning`, then `reasoning_content`.
- The model selector applies to the next new session.

## Affected Areas

- New: `src/llm_harness/` files listed in Approach; `tests/` including `tests/fakes.py`.
- New project files: `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`.
- New docs: `README.md` (short, with run instructions), `CHANGELOG.md` (with `## [Unreleased]`).
- Config keys: `models.yaml` key `thinking` (per model) and `config.toml` key `reasoning_style` (per `openai` provider), handled in `config.py` and `cli.py`.
- The `openai` dependency in `pyproject.toml` and `uv.lock` (openai 3.40.0).
- Constitution (`specs/mission.md`, `specs/tech-stack.md`, `specs/roadmap.md`) is already updated; no further constitution change is expected.
- Added after the validation review: `src/llm_harness/gui/security.py` (new), `src/llm_harness/display.py`, `src/llm_harness/providers/environment.py`, `src/llm_harness/redact.py`, `aiohttp` (declared dependency), and the new tests `tests/test_gui_security.py`, `tests/test_environment.py`, `tests/test_fakes.py`.

## Out of Scope

- Orchestrator and goal/context state, subagents, tools, tool calls, memory, A2UI.
- Fallback chain and token/price tracking (roadmap `model-registry`); `price` is read and validated only.
- System prompts; persisting chat history; authentication.
- Model switching mid-conversation: the selector only chooses the model for the next new session (assumption).
- `--config-dir`, `--provider fake`.
- An authentication token for the GUI (the Host/Origin check is the only protection beyond binding to 127.0.0.1).

## Implementation Notes

Step 1a (offline core) — done 2026-10-27.

Setup (no git by the user's instruction: no `git init`, no commits; `branch: none` in this spec's frontmatter and in the roadmap `## Now` line):
- Scaffold by `uv init --package --vcs none` (uv_build backend, src layout). `requires-python >=3.12`, `.python-version` 3.13 (3.13.7 installed). `authors` removed from pyproject.toml at the user's request.
- Dependencies added with `uv add`: pydantic 2.14.1, pyyaml 6.0.3; dev: pytest 9.1.1, pytest-asyncio 1.4.1, ruff 0.17.3 — locked versions equal those in the constitution.
- Pytest config in pyproject: `addopts = '-m "not live"'`, marker `live`, `asyncio_mode = "auto"`, `testpaths = ["tests"]`. `.gitignore` written (includes `.agent/`). Console script `llm-harness = "llm_harness.cli:main"`.
- Baseline before any code: `uv run pytest` collected 0 items; `uv run ruff check` passed.

Chunk 1 (core):
- `src/llm_harness/types.py`: frozen pydantic models, private `_Frozen` base with extra=forbid; Message, ChatRequest, Usage, ThinkingDelta, TextDelta, UsageEvent, Done, ErrorEvent, ErrorKind, StreamEvent.
- `redact.py`: `Secret` with masked repr/str/format and `reveal()`; `redact()` masks known secrets and `sk-`/`Bearer` shapes BEFORE truncating to 300 characters.
- `providers/base.py`: `ChatProvider` protocol.
- `session.py`: `ChatSession` sends the whole history each turn, never stores thinking, rolls back on error, on cancel/GeneratorExit and on a missing terminal event; busy guard raises RuntimeError on the first `anext`.
- `tests/fakes.py` (`ScriptedProvider`, `assert_stream_contract`), tests for types, redact and session. 52 tests.

Chunk 2 (config):
- `config.py`: `ProviderConfig`, `Price`, `ModelConfig`, `HarnessConfig`, `ConfigError`, `MissingConfigError`, `load_config`, `resolve_api_key`, `check_permissions`, `select_model`, `write_examples` (exclusive create, config.toml 0o600, models.yaml 0o644), `default_config_dir`, example constants.
- Validation errors are built from location and message only (`include_input=False`) so an inline key cannot leak; YAML read with `yaml.safe_load` only. 59 new tests (111 total).

Chunk 3 (CLI):
- `cli.py`: `main` returning an int; `--plain`, `--gui`, `--model ID`, `--think`; no flags -> exit 2 pointing to `--plain`; `--gui` -> exit 2; first run: non-interactive prints the example files and exits 1 writing nothing, interactive asks `[y/N]`.
- `providers/__init__.py`: `build_provider` resolves the key first, has an empty `_ADAPTER_MODULES` registry, and raises `no adapter for provider type '<type>' yet`.
- `tests/test_cli.py`, `tests/test_providers.py`. 31 new tests (142 total).

Deviations and assumptions beyond the spec (all kept, listed for the approval of the final delivery):
- A `vertex` provider rejects `base_url`, `api_key`, `api_key_env` (ADC only).
- `config.toml` rejects unknown top-level keys and `models.yaml` must contain only `models`.
- Interactive first-run returns exit 0 after writing the example files.
- Thinking and the `[tokens in= out= reasoning= <s>s]` usage line go to stderr (stdout is clean text); thinking is dimmed only when stderr is a TTY.
- Model text, thinking and error messages are sanitized: C0 controls except newline/tab/CR, plus DEL and C1 (0x7f-0x9f) are removed (so the rest of an escape sequence stays visible as text).
- `build_provider` also raises `adapter <module> is registered but not wired yet` after lazily importing a registered module (placeholder until step 1b replaces it with adapter construction).

Verification (coordinator re-ran everything):
- `uv run ruff check` clean, `uv run ruff format --check` clean (20 files), `uv run pytest -q -rs` 142 passed, 0 skipped, 0 deselected, 0 failed.
- Real CLI runs with `HOME` pointed at a scratch dir: `llm-harness --help` exit 0; no flags exit 2; `--gui` exit 2; `--plain < /dev/null` first run exit 1 and nothing created; `echo y | llm-harness --plain` (piped, not a TTY) exit 1 and nothing written; the real `~/.config/llm-harness` was never created.
- Coordinator probe in a scratch dir: examples written with modes 0o600/0o644 reload and select `gemma-local` (`gemma4:12b`, priority 100); a config with three different canary keys produced an error naming only the fields, no canary; a missing env var error names the variable only.
- Coordinator mutation check: truncating before redacting made `test_redact_before_truncating_leaks_no_fragment` fail, file restored byte-identical.
- The engineer reported nine mutation checks in total, three per chunk (truncating before redacting, which the coordinator repeated above; rollback on error, thinking stored, select_model ignoring priority, error formatter echoing input, `yaml.unsafe_load`, first-run writing when non-interactive, thinking to stdout, sanitizing removed — each failed the intended tests and was reverted; the other eight are engineer only, not repeated by the coordinator).

Acceptance criteria coverage:
- Covered by offline tests: 3 (partly: adapters later), 4-6, 7-15, 21, 25, 26; criterion 1 is covered for the fake provider only (the parametrized adapter contract test arrives with the adapters).
- Not yet covered: 2 (cancel closing a real SDK stream), 16 (`--plain` over a pipe against a real provider), 17-20, 22, 23 final, 24 (final grep at the end).

Call sites:
- Used by `cli.py`: `load_config`, `check_permissions`, `select_model`, `write_examples`, `default_config_dir`, `build_provider`, `ChatSession`, `Usage`, `UsageEvent`. `resolve_api_key` is used by `build_provider`; `Secret` by `config.py`.
- Intentionally unused until step 1b: `redact()` and `Secret.reveal()` (the adapters).
- Not tried: a real Ctrl-C in a terminal; nothing was run against a real provider.

Step 1b (live Ollama) — done 2026-10-27.

Files and dependencies:
- New `src/llm_harness/providers/ollama.py` (`OllamaProvider`, `create(config, api_key)`).
- Edited `src/llm_harness/providers/__init__.py`: registry entry `ollama` -> `llm_harness.providers.ollama`; `build_provider` now returns `module.create(config, key)`; the `registered but not wired yet` placeholder was removed.
- New tests `tests/test_ollama_adapter.py`, `tests/test_provider_contract.py` (one parametrized contract test, only the ollama entry so far; steps 2 and 3 add entries), `tests/test_live_ollama.py` (marker `live`); `tests/test_providers.py` updated where it encoded the old behaviour.
- Dependencies added with `uv add` by the coordinator: ollama 0.6.3 and httpx 0.28.1 (httpx is in the constitution's library list and the adapter imports it directly, so it is declared).
- A small follow-up fix in `src/llm_harness/cli.py` and `tests/test_cli.py` (see Findings for the user).

API facts found live (ollama 0.6.3, httpx 0.28.1):
- An unreachable server raises `httpx.ConnectError("All connection attempts failed")`, not the builtin `ConnectionError`; the adapter catches `httpx.TransportError` and `ConnectionError` -> kind `connection`.
- An unknown model raises `ollama.ResponseError` with `status_code` 404 and message `model 'no-such-model-xyz' not found (status code: 404)`.
- `ollama.AsyncClient.close` is a public coroutine function; the host is readable as `client._client.base_url` (a private attribute, used by one test only).
- `client.chat` accepts `options`; the live test injects `{"num_predict": 32}` through a wrapper, the production adapter has no test-only parameter.

Behaviour:
- Messages are mapped to `{"role","content"}`; `chat(model, messages, stream=True, think=req.think)`.
- `message.thinking` -> ThinkingDelta, `message.content` -> TextDelta (empty strings produce none).
- On the final chunk: a UsageEvent (input = prompt_eval_count, output = eval_count, reasoning None) only if at least one count is not None, then Done(finish_reason = done_reason).
- `ResponseError` by status (400 bad_request, 401/403 auth, 404 not_found, 429 rate_limit, others unknown), `RequestError` -> bad_request, transport errors -> connection; message = `redact(str(exc))` truncated at 300 characters.
- Only SDK and transport exceptions are caught; programming errors such as TypeError propagate (tested).
- Added beyond the spec: if the stream ends without a `done` chunk the adapter emits ErrorEvent(unknown) so the contract's exactly-one-terminal-event always holds.
- The stream is closed with `contextlib.aclosing` (cancellation closes the SDK stream, CancelledError propagates).
- `create()` ignores `api_key` (Ollama takes no key in this slice).

Verification (coordinator re-ran):
- `uv run ruff check` clean; `uv run ruff format --check` clean (24 files).
- `uv run pytest -q -rs` after the adapter: 176 passed, 1 deselected (the live test; deselected by addopts, not skipped), 0 skipped, 0 failed (earlier 142 plus 34 new).
- Live test `pytest -m live -k ollama`: 1 passed in 16.98 s (engineer run, scratch HOME with the example config; the real home config does not exist, so against the real home the test would skip with a reason).
- Coordinator live run: `printf 'Reply with exactly one word: pong\n' | llm-harness --plain` against local gemma4:12b exit 0, stdout `pong`, thinking and `[tokens in=23 out=71 4.4s]` on stderr.
- Engineer manual two-turn run through a pipe: exit 0; turn 1 input tokens 18, turn 2 input tokens 34 (consistent with the whole history being sent).
- `--model nope` exit 1, `error: unknown model 'nope'; valid ids: gemma-local`.
- Provider base_url `http://127.0.0.1:9`: exit 1 `error (connection): All connection attempts failed`, no traceback.
- Engineer mutation checks (engineer only, each reverted, diff empty, suite back to 176 passed): think always forwarded as True -> a test failed; Done emitted after an error -> a test failed; `aclosing` replaced by `nullcontext` -> 4 tests failed including the cancellation test.
- Editable install check (criterion 22, run by the coordinator in ISOLATED directories so nothing was installed on the user's machine): `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` pointed at scratch dirs, `uv tool install --editable .`; the installed `llm-harness` printed the original message; after editing the message string in `cli.py` the next run showed the edit with NO reinstall; `cli.py` was restored byte-identical (cmp), the tool was uninstalled and the scratch dirs removed.

Acceptance criteria coverage:
- Now covered: 1 (contract test for the ollama adapter; the other adapters later), 2 and 3 (for the Ollama adapter, offline), 16 (`--plain` over a pipe verified against real Ollama; offline test with the fake), 19 (Ollama streams live: text, thinking, usage line), 22.
- Still open: 17, 18, 20, 23 (final), 24 (final grep).

Call sites:
- `OllamaProvider` and `create` (ollama) via `build_provider`; `redact()` in `providers/ollama.py` `_error` (its first non-test use); `_ADAPTER_MODULES` in `build_provider`.
- Intentionally unused until step 2: `Secret.reveal()`.

Findings for the user:
1. `--think` only asks for thinking: without it, gemma4:12b still produced thinking in the live runs because `think=None` means the model's default; there is currently no way to turn thinking off (decision pending: a `--no-think` flag or accept the model default). RESOLVED 2026-10-27: decision 7 and step 1c add a per-model `thinking` key and `--think`/`--no-think`.
2. Cosmetic bug found in the coordinator's live run and fixed in `cli.py`: the usage and error lines were glued to the end of the thinking text because the thinking line was never terminated; `_run_turn` now closes an open thinking line (after the dim reset code) before the first text delta, the usage line and an error line; 6 tests added (182 passed, 1 deselected, 0 skipped); engineer mutation check (closing disabled) failed 5 tests; coordinator re-run live: the usage line is on its own line. Not run: a real interactive terminal (the dim-wrapper path is covered by a unit test only), a real Ctrl-C.

Step 1c (thinking in config, Ollama SDK) — done 2026-10-27.

Files:
- `src/llm_harness/config.py`: `ModelConfig.thinking: StrictBool | None = None`; a commented `# thinking: false   # optional; omitted = the model's own default` line in the example models.yaml, which still loads and still selects `gemma-local`.
- `src/llm_harness/cli.py`: mutually exclusive `--think` / `--no-think` into one destination, default None; new `resolve_think(flag, model)` = CLI flag > model `thinking` > None, used in `main` and passed to `ChatSession`.
- `tests/test_config.py`, `tests/test_cli.py` (the old `--think`-only test replaced).
- The Ollama adapter was not changed (it already sends `think=req.think`; None sends nothing).

Behaviour:
- `thinking` must be a real boolean: strings such as "false", integers 0/1 and lists are rejected with a ConfigError that names `models.yaml` and `thinking` and does not echo the value.
- Passing both flags is an argparse error (exit 2).

Verification (coordinator re-ran):
- `uv run ruff check` clean; `uv run ruff format --check` clean (24 files).
- `uv run pytest -q -rs`: 208 passed, 1 deselected (the live test), 0 skipped, 0 failed (earlier 182 plus 26 new).
- Live runs against local gemma4:12b with a scratch HOME and the prompt `Reply with exactly one word: pong` (engineer, six runs; all stdout `pong`, exit 0): no flags and no model `thinking` -> thinking text on stderr (7 lines); `--no-think` -> stderr only the usage line `[tokens in=20 out=2 0.4s]` (1 line), which verifies on the `ollama` SDK path that `think=False` disables thinking; `--think` -> thinking (8 lines); model `thinking: false` with no flag -> no thinking (1 line); model `thinking: false` with `--think` -> thinking (9 lines, flag wins); model `thinking: true` with `--no-think` -> no thinking (1 line, flag wins).
- Coordinator re-ran `--no-think` (1 stderr line, usage only) and `--think` (6 stderr lines, thinking) live, and confirmed `--think --no-think` together prints `llm-harness: error: argument --no-think: not allowed with argument --think`.
- Engineer mutation checks (engineer only, each reverted, diff empty, suite back to 208 passed): model setting winning over the flag -> 4 tests failed; `thinking` accepting any value -> 5 failed; the mutual-exclusion group removed -> 1 failed (only the exit-2 test passes both flags).
- The real `~/.config/llm-harness` was not created.

Acceptance criteria coverage:
- Now covered: 27, 28, 30 (Ollama SDK `think=False` verified live). The `Unverified:` entry `Ollama SDK think=False` is now verified.
- Still open: 17, 18, 20, 29, 31, 32, 33, 23 (final), 24 (final grep). 29 and 31-33 belong to step 2 (`reasoning_style` and the OpenAI-compatible adapter).

Call sites:
- `resolve_think` in `main` (cli.py); `--no-think` feeds `args.think`; `ModelConfig.thinking` is read in `resolve_think`.

Step 2 (OpenAI-compatible adapter) — done 2026-10-27; OpenRouter and OpenAI not exercised.

Files:
- New `src/llm_harness/providers/openai_compat.py` (`OpenAICompatProvider`, `create(config, api_key)`).
- `src/llm_harness/providers/__init__.py`: registry entry `openai`.
- `src/llm_harness/config.py`: `ProviderConfig.reasoning_style: "openai"|"openrouter"|None` (None = default `openai`), rejected on ollama and vertex providers; a commented example line.
- New tests `tests/test_openai_compat_adapter.py` and `tests/test_live_openai_compat.py` (marker `live`); additions to `tests/test_provider_contract.py` (openai entry) and `tests/test_config.py` (20 reasoning_style tests).

Facts found (openai 3.40.0, by `inspect` and runs):
- `AsyncStream` has `close()`/`aclose()` and async context-manager methods; the adapter uses `contextlib.aclosing`, same as `ollama.py`. `AsyncOpenAI.close()` exists.
- The SDK uses the `httpx2` package (2.16.0, transitive through openai), not `httpx`, so the new tests import `httpx2` directly (not a declared dependency).
- Exception constructors: `APIStatusError` subclasses `(message, *, response, body)`; `APIConnectionError(*, message='Connection error.', request)`; `APITimeoutError(request)` subclasses `APIConnectionError`.
- Extra delta fields such as `reasoning` land in `delta.model_extra`.
- The client constructor reads `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, `OPENAI_ADMIN_KEY`, `OPENAI_WEBHOOK_SECRET` and `OPENAI_CUSTOM_HEADERS` from the environment; the last can inject an `Authorization` header overriding our key.
- `organization=""` sends a bogus empty header and `default_headers` with `Omit()` mistypes, so neither is a clean fix.

Behaviour:
- Request: `chat.completions.create(model, messages, stream=True, stream_options={"include_usage": True}, + thinking kwargs)`; no max-token parameter in production code.
- Thinking kwargs, style `openai`: False -> `reasoning_effort="none"`, True -> `"medium"`, None -> nothing. Style `openrouter`: `extra_body={"reasoning": {"enabled": <bool>}}`, None -> nothing.
- Per chunk, thinking comes from ONE source: `reasoning_details` (text of `reasoning.text`, summary of `reasoning.summary`, encrypted/malformed ignored), else string `reasoning`, else string `reasoning_content`; text from `delta.content`.
- Usage (last chunk, empty choices) and finish reason are held back until the stream ends: one UsageEvent (input = prompt_tokens, output = completion_tokens, reasoning = completion_tokens_details.reasoning_tokens or None), then Done(finish_reason), or ErrorEvent(unknown, "the stream ended without a finish reason") when none.
- Errors: Authentication/PermissionDenied -> auth, NotFound -> not_found, RateLimit -> rate_limit, BadRequest -> bad_request, APIConnectionError (incl. timeouts) -> connection, other OpenAIError -> unknown; message = `redact(str(exc), secrets=[the key])` truncated to 300; only SDK exceptions are caught.
- `create()` passes api_key and base_url explicitly, `max_retries=0`, and hides every `OPENAI_*` environment variable only while the SDK constructor runs (synchronous context manager, restored afterwards).

Verification (coordinator re-ran):
- `uv run ruff check` clean; `uv run ruff format --check` clean (27 files).
- `uv run pytest -q -rs`: 271 passed, 2 deselected (both live tests), 0 skipped, 0 failed (earlier 208 plus 63 new).
- Engineer live test `pytest -m live -k openai` with a scratch HOME config (provider type openai, base_url `http://localhost:11434/v1`, api_key `ollama`): 1 passed in 13.86 s; with an empty HOME it skips with the reason `no config.toml / models.yaml in <dir>`.
- Coordinator live CLI runs against local gemma4:12b through Ollama's `/v1` with `--model gemma-oai`: `--no-think` -> stderr 1 line (usage only, `[tokens in=20 out=2 0.4s]`); `--think` -> thinking text (10 lines).
- Engineer runs: no flag -> thinking (7 lines); `--no-think` -> none; `--think` -> thinking (4 lines); model `thinking: false` with no flag -> none. Informational: `reasoning_style = "openrouter"` with `thinking: false` against Ollama `/v1` -> exit 0 and thinking STILL appears (Ollama accepts and ignores the `reasoning` object; only `reasoning_effort` works there). base_url `http://127.0.0.1:9` -> exit 1 `error (connection): Connection error.`, no traceback, the dummy key text did not appear in the output.
- Coordinator canary check through the real CLI: with `OPENAI_BASE_URL=http://127.0.0.1:9/v1` and `OPENAI_API_KEY=sk-CANARY-123456789` set in the environment the call still reached Ollama and returned `pong` (the ambient variables did not redirect it).
- Engineer mutation checks (engineer only, each reverted, diff empty, suite back to 271 passed): emitting both reasoning_details and reasoning -> 1 test failed; Done emitted at the finish_reason chunk before usage -> 8 failed; style openai with think False sending nothing (or "low") -> 1 failed; `secrets=[]` to redact -> 1 failed; env scrub removed -> the ambient-environment test failed.
- No request went to any host other than localhost; the real `~/.config/llm-harness` was not created.

Acceptance criteria coverage:
- Now covered: 29, 31 (offline from synthetic chunks; live against Ollama `/v1` for the `openai` style incl. `thinking: false` -> no thinking), 32, 14 (ambient environment does not reach the client), 20 (OpenAI-compatible part: live against Ollama `/v1`), 1-3 and 21 (for this adapter).
- Still open: 17, 18, 20 (Vertex part; OpenRouter live check by the user later), 33, 23 (final), 24 (final grep).

Call sites:
- `Secret.reveal()` in `openai_compat.create` (its first non-test use).
- `reasoning_style` read in `create` (`config.reasoning_style or "openai"`).
- `OpenAICompatProvider` returned by `create`; `create` called by `build_provider` via the registry entry `openai`.
- `redact(..., secrets=)` on the adapter's error path.

Decisions and deviations (for approval at delivery):
- `max_retries=0` (fail fast; retries belong to the later fallback feature).
- The environment scrub of `OPENAI_*` variables during client construction (not an SDK feature; the SDK has no clean argument to block all of them).
- A new test-only environment variable `LLM_HARNESS_LIVE_MODEL`: the live OpenAI-compatible test uses it if set; otherwise only a model on an openai-type provider whose host is localhost or 127.0.0.1; it skips with a reason otherwise. This is how the user can run the live test against OpenRouter later.
- Within one chunk thinking is emitted before text.
- Not covered: proxy and TLS variables read by httpx2 (`HTTP_PROXY`, `SSL_CERT_FILE`) and `OPENAI_LOG` (read at import).
- Unverified: OpenRouter `reasoning_details` shapes and the `reasoning` request object (offline tests only); whether OpenAI and OpenRouter accept the `reasoning_effort` values and `max_completion_tokens`; `reasoning_tokens` from a real provider (synthetic usage only; Ollama returns `completion_tokens_details` as None).

Step 3 (Vertex adapter) — done 2026-10-27; verified live against Vertex AI.

Files and dependencies:
- New `src/llm_harness/providers/vertex.py` (`VertexProvider`, `create(config, api_key)`).
- New `src/llm_harness/providers/environment.py` (`hidden_environment(prefixes, names)`, a shared helper).
- New tests `tests/test_vertex_adapter.py` and `tests/test_live_vertex.py` (marker `live`).
- Edited `providers/__init__.py` (registry entry `vertex`), `providers/openai_compat.py` (its private env-scrub helper replaced by `hidden_environment(prefixes=("OPENAI_",))`; its tests unchanged and green) and `tests/test_provider_contract.py` (vertex entry with happy path, mid-stream error and credentials error).
- Dependency added with `uv add google-genai` (the only dependency command): google-genai 2.33.0 (by importlib.metadata; the package's `__version__` attribute is stale) and google-auth 2.62.0 plus transitive packages; pyproject has `google-genai>=2.33.0`.

Facts found (google-genai 2.33.0):
- `Client.__init__(*, enterprise, vertexai, api_key, credentials, project, location, debug_config, http_options)`. With explicit project and location the constructor does not load ADC; credentials load lazily at the first request, so auth errors surface inside `stream_chat`.
- `await client.aio.models.generate_content_stream(model, contents, config)` returns an async generator, closed with `contextlib.aclosing`. The SDK does not retry by default. The HTTP backend is httpx (aiohttp is not installed).
- Thought parts have `thought=True`, text parts `thought=None`. Intermediate chunks have `usage_metadata` None; the final chunk carries `finish_reason` (`FinishReason.STOP`, name "STOP"), `usage_metadata` and one empty-text part.
- Close is `client.aio.aclose()` (async) and `client.close()` (sync), both idempotent.
- `errors.APIError` has `code` (`ClientError` 4xx, `ServerError` 5xx, constructed as `cls(code, response_json)`). `google.auth.exceptions.GoogleAuthError` is the base of `DefaultCredentialsError`, `RefreshError` and `TransportError`.
- `google.auth._cloud_sdk.get_config_path()` honours `CLOUDSDK_CONFIG` for the ADC file lookup (the live runs used a scratch HOME plus `CLOUDSDK_CONFIG` pointing at the user's existing gcloud config dir; the credentials file was never read or printed).
- The SDK reads these variables at construction and `create()` hides all eleven while the client is built (then restores them): `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_GENAI_USE_ENTERPRISE`, `GOOGLE_VERTEX_BASE_URL`, `GOOGLE_GEMINI_BASE_URL`, `GOOGLE_GENAI_CLIENT_MODE`, `GOOGLE_GENAI_REPLAYS_DIRECTORY`, `GOOGLE_GENAI_REPLAY_ID`.
- Not hidden on purpose: `GOOGLE_GENAI_HTTP_CLIENT`, `SSL_CERT_FILE`, `SSL_CERT_DIR` (transport settings) and the ADC inputs `GOOGLE_APPLICATION_CREDENTIALS`, `CLOUDSDK_CONFIG`.
- An offline probe showed an ambient `GOOGLE_VERTEX_BASE_URL=https://canary.invalid/` reached the client without hiding and did not with `create()`. Project, location and api key are already protected by SDK precedence when passed explicitly.
- Token semantics (live): `candidates_token_count` EXCLUDES thought tokens. Thinking on: prompt 13, candidates 12, thoughts 230, total 255. Thinking off: prompt 13, candidates 11, thoughts None, total 24.
- Mapping: input = prompt_token_count; output = candidates_token_count + thoughts_token_count (None counts as 0; None if both None); reasoning = thoughts_token_count.

Behaviour:
- Messages: system messages -> `system_instruction`; user -> role `user`; assistant -> role `model`.
- Thinking mapping found by raw-SDK probes (3 calls, math prompt, max_output_tokens 512): `ThinkingConfig(include_thoughts=True)` alone -> 400 `Thinking_config.include_thoughts is only enabled when thinking is enabled`; `include_thoughts=True, thinking_budget=-1` -> 2 thought parts then 1 text part; `include_thoughts=False, thinking_budget=0` -> no thought parts, thoughts_token_count None.
- Production mapping: `think=True` -> `ThinkingConfig(include_thoughts=True, thinking_budget=-1)`; `think=False` -> `ThinkingConfig(include_thoughts=False, thinking_budget=0)`; `think=None` -> no `thinking_config`.
- No max-output-token parameter in production code (the live test caps via a test-local wrapper at 32).
- Parts: thought + text -> ThinkingDelta, other non-empty text -> TextDelta. Usage and finish reason are held back to the end: one UsageEvent, then Done(finish_reason name) or ErrorEvent(unknown, "the stream ended without a finish reason").
- Errors: APIError by `code` (400 bad_request, 401/403 auth, 404 not_found, 429 rate_limit, others unknown); `auth_exceptions.TransportError` -> connection (checked before the general GoogleAuthError -> auth); httpx.TransportError and ConnectionError -> connection; message = `redact(str(exc))` truncated to 300; only SDK, auth and transport exceptions are caught.
- `create()` passes project and location explicitly and ignores `api_key`.

Verification (coordinator re-ran):
- `uv run ruff check` clean; `uv run ruff format --check` clean (31 files).
- `uv run pytest -q -rs`: 326 passed, 3 deselected (the ollama, openai and vertex live tests), 0 skipped, 0 failed (earlier 271 plus 55 new).
- Live target (approved by the user, explicit in a scratch config): project `np-te-core-ai`, location `us-central1`, model `gemini-2.5-flash-lite`, using the user's existing ADC; no gcloud command was run.
- Engineer live runs (10 Vertex calls in total: 3 probes including the 400, plus L1-L7): `pytest -m live -k vertex` 1 passed in 3.09 s; no flag -> `pong`, `[tokens in=7 out=1 2.2s]`; `--think` -> thinking text, `[tokens in=7 out=25 reasoning=24 2.8s]`; `--no-think` -> `[tokens in=7 out=1 2.0s]`; model `thinking: false` with no flag -> no thinking.
- Ambient canaries `GOOGLE_CLOUD_PROJECT=canary-project GOOGLE_API_KEY=sk-CANARY-123456789 GOOGLE_CLOUD_LOCATION=canary-loc` -> still succeeded against np-te-core-ai (no "canary" text in the output).
- Bogus project `llm-harness-no-such-project` -> `error (auth): 403 PERMISSION_DENIED ... Permission denied on resource project ...`, exit 1, no traceback (kind auth, not not_found).
- `GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/path.json` -> `error (auth): File /nonexistent/path.json was not found.`, exit 1, no traceback, no network call (exception class `google.auth.exceptions.DefaultCredentialsError`).
- Coordinator live runs (2 more Vertex calls, same target, same prompt `Reply with exactly one word: pong`): `--no-think` -> stdout `Pong`, stderr only `[tokens in=7 out=1 2.3s]`; `--think` -> stdout `pong`, 5 stderr lines ending `[tokens in=7 out=34 reasoning=33 2.5s]`. Total Vertex calls in step 3: 12.
- Engineer mutation checks (engineer only, each reverted, diff empty, suite back to 326 passed): dropping the `thought` check -> 4 tests failed; Done before usage -> many failed including the vertex contract entry; `think=False` mapped to no thinking_config -> 1 failed; env hiding removed -> 2 failed.
- The real `~/.config/llm-harness` was not created.

Acceptance criteria coverage:
- Now covered: 20 (Vertex part: live; the OpenRouter live check remains for the user), 14 (project and location from config; ambient values do not reach the client), 1-3 and 21 (for this adapter). 10 is unchanged.
- Still open: 17, 18, 33, 20 (OpenRouter live check by the user), 23 (final), 24 (final grep).

Call sites:
- `VertexProvider` and `create` via `build_provider` and the registry entry `vertex`.
- `hidden_environment` in `vertex.create` and `openai_compat.create`; `_HIDDEN_VARIABLES` in `vertex.create`.
- `redact()` in `vertex._error`.

Decisions and deviations (for the final delivery approval):
- Extracted the shared `hidden_environment` helper and switched openai_compat to it.
- `thinking_budget=-1` (dynamic) for `think=True` because include_thoughts alone is rejected on flash-lite; `thinking_budget=0` for `think=False`.
- `GOOGLE_GENAI_HTTP_CLIENT` is intentionally not hidden (add it to `_HIDDEN_VARIABLES` if it should be forced to the default).
- A nonexistent project returns 403 and is therefore reported as kind `auth`, not `not_found`.
- `LLM_HARNESS_LIVE_VERTEX_MODEL` is a new test-only variable (the live Vertex test uses it if set, else the first model on a `vertex` provider; skips with a reason otherwise).

Unverified:
- Behaviour on real 429, 5xx and mid-stream network failures (constructed exceptions only; a connection failure was never exercised live).
- `candidates_token_count` semantics for models other than flash-lite.
- A `thinking_budget=-1` run that produces no thought parts (only one math prompt was probed).
- The `GOOGLE_GENAI_USE_*`, `GOOGLE_GEMINI_BASE_URL` and replay variables are hidden on the strength of the source grep and were tested only as canaries absent at construction.

Step 4 (Textual TUI) — done 2026-10-27; keyboard behaviour beyond Enter and Ctrl+Q checked only through Textual's Pilot, visuals not checked.

Files and dependencies:
- New `src/llm_harness/tui/__init__.py`, `src/llm_harness/tui/app.py` (`ChatApp`, `run_tui`), `src/llm_harness/display.py` (`sanitize_text`, `format_usage`, moved out of cli.py, same behaviour), `tests/test_tui.py`, `tests/test_display.py`.
- Edited `src/llm_harness/cli.py`: new helper `_load_config_and_model` shared by the plain and TUI paths; `main` has a new `run_tui` parameter whose default `_run_tui_lazily` imports `llm_harness.tui.app` lazily; with no `--plain`/`--gui`, `main` returns 2 with a `--plain` hint if stdin or stdout is not a TTY, before loading anything, otherwise loads config like `--plain` and calls `run_tui(config, model, provider_factory, args.think)`; `--gui` unchanged, still `arrives in step 5`.
- Edited `tests/test_cli.py`: `run_cli` gained `stdout_tty` and `tui_runner`.
- Dependency added with `uv add textual` (the only dependency command): textual 8.2.8 (importlib.metadata) and rich 15.0.0; transitive linkify-it-py, markdown-it-py, mdit-py-plugins, mdurl, platformdirs; pyproject has `textual>=8.2.8`.
- Importing `llm_harness.cli` and `--help` do not import textual (tested in a subprocess; coordinator re-checked: `textual imported: False`).

Textual APIs verified (8.2.8, by inspect and a scratch probe):
- `Static(content, *, markup=True, ...)`: `markup=False` keeps `[bold red]x[/]` literal.
- `Collapsible(*children, title, collapsed=True)`.
- `Select(options, allow_blank=False, value=...)` and `Select.Changed`: setting `value` fires Changed, also once at mount; `select.prevent(Select.Changed)` suppresses it; `Select.NULL`.
- `Input.Submitted.value`.
- `Binding(key, action, description, show=True)`: default App bindings are `ctrl+q` quit with show=False and `ctrl+c` help_quit; `ctrl+q` is re-declared with show=True so the footer shows it.
- `App.run_worker(...)` returns a Worker with `.cancel()` and `.is_running`.
- `App.run_test(size=(80,24))` gives a `Pilot` (`press`, `pause`, `click`).
- `App.on_unmount` fires on quit (provider closed there); `App.return_code` is 0 after quit.
- Differences from expectation: `App._log` is a private Textual name, so the widget property is `_chat_log`; `Binding` lists need `ClassVar` for ruff RUF012. Private API used in one test: `Select._options`. `Pilot.click` was not used.

Behaviour:
- Layout: header, model Select (names from models.yaml, value = id), scrolling log, Input, footer. Enter ignores blank input.
- A worker consumes `session.send(text)`. Thinking goes into a `Collapsible` titled "Thinking", collapsed by default, created on the first thinking delta; text goes into the reply widget; the usage line comes from `format_usage`.
- `ErrorEvent` -> `error (kind): message` and the text is put back in the input. Escape cancels the worker -> `cancelled` note, text restored, session rolled back. The input is disabled during a turn.
- Model selector: with an empty history a change swaps the session immediately; with history it shows `applies to the next new chat (Ctrl+N)`.
- Ctrl+N builds a session for the selected model, closes the old provider and clears the log.
- A `ConfigError` from the provider factory is shown as an error note and keeps the current session (a failed selector change is reverted in the widget, a failed Ctrl+N keeps the log).
- `think` for a session = `resolve_think(think_flag, model)`.
- All model text, thinking, error messages and the user echo go through `sanitize_text` and `Static(..., markup=False)`; nothing is rendered as Markdown.

Verification (coordinator re-ran):
- `uv run ruff check` clean; `uv run ruff format --check` clean (36 files).
- `uv run pytest -q -rs`: 375 passed, 3 deselected, 0 skipped, 0 failed in about 7 s (earlier 326; one test that encoded the old no-flags message was replaced; 49 net new tests; `tests/test_tui.py` 24 tests in 5.5 s).
- Piped no-flags run: `llm-harness </dev/null` -> `The terminal UI needs a terminal for input and output; use --plain over pipes.`, exit 2, nothing created.
- Pseudo-terminal check (engineer, then re-run by the coordinator with the same driver script; 100x30, TERM=xterm-256color, scratch HOME, real local Ollama gemma4:12b, `timeout 120`): the first engineer run was a false positive (the typed prompt contains `pong`) so the success condition was changed to wait for the usage line. Second engineer run: usage line `[tokens in=23 out=39 11.7s]`, a standalone reply line `pong` rendered, no `Traceback`, Ctrl+Q (`\x11`) exited the process by itself within 10 s with exit code 0. Coordinator run: usage line seen, standalone `pong` reply lines, exited cleanly with exit code 0, no traceback, no leftover process, Ollama still answering (version 0.40.11), real `~/.config/llm-harness` not created.
- Engineer mutation checks (engineer only, each reverted, diff empty, suite back to 375 passed): `markup=True` on Static -> 3 tests failed (the usage line `[tokens in=...]` is parsed as markup and crashes the worker, which also shows why `markup=False` matters there); no input restore on ErrorEvent -> 1 failed; selector applying immediately with a non-empty history -> 2 failed; sanitizing removed in the TUI -> 1 failed.

Acceptance criteria coverage:
- Now covered: 17 (TUI part: streamed text, collapsible thinking, model selector from models.yaml; offline Pilot tests plus the pseudo-terminal run), 2 (Escape cancel closes the stream, in the TUI via Pilot), 4-6 and 27 for the TUI path, 25 (now: no flags starts the TUI, or exits 2 without a terminal).
- Still open: 17 (GUI part), 18, 33, 20 (OpenRouter live check by the user), 23 (final), 24 (final grep).

Call sites:
- `sanitize_text` (cli.py `_run_turn`, tui/app.py); `format_usage` (cli.py, tui/app.py).
- `run_tui` via `cli._run_tui_lazily`, the default of `main`'s `run_tui` parameter; `ChatApp` in `tui/app.run_tui`; `_load_config_and_model` in `cli.main`.

Decisions and deviations (for the final delivery approval):
- The initial provider is built in `ChatApp.on_mount`, not in `main`, so a provider error (for example a missing API-key environment variable) does not stop the TUI from starting (unlike `--plain`): it shows the error with no session, a submit then says `no working model: pick another and press Ctrl+N`, and choosing a working model recovers (tested).
- There is no stored pending model; Ctrl+N reads the Select value.
- The thinking block is collapsed by default (the user expands it).

Not verified:
- How the TUI looks (colours, dimming, layout; only the escape-stripped byte stream was searched).
- Real keyboard behaviour beyond Enter and Ctrl+Q (Escape cancel and Ctrl+N were tested only through Pilot).
- The default runner `run_tui` has no unit test (covered only by the pseudo-terminal run).
- The Textual uses `prevent`, the `on_unmount` hook and `Select._options` were exercised only through a probe and the tests.

Step 5 (NiceGUI web GUI) — done 2026-10-27; no real browser was used, page appearance and real keyboard behaviour not checked.

Files and dependencies:
- New `src/llm_harness/gui/__init__.py`, `src/llm_harness/gui/app.py` (`ChatPage`, `register_page`, `run_gui`), `tests/test_gui.py`; edited `src/llm_harness/cli.py`, `tests/test_cli.py`, `pyproject.toml`.
- The module is `gui/app.py`, not `web/app.py` (spec decision 14; spec version 3 and tech-stack version 5 record it).
- Dependency added with `uv add nicegui` (the only dependency command): nicegui 3.18.0 (importlib.metadata), `nicegui>=3.18.0` in pyproject; transitive fastapi 0.162.0, starlette 1.10.0, uvicorn 0.54.0, python-socketio and others.
- The NiceGUI `user` test fixture needed no extra dev dependencies; no browser or Playwright was installed.
- Exact pytest change in `[tool.pytest.ini_options]`: `addopts = '-m "not live"'` became `addopts = '-m "not live" -p nicegui.testing.user_plugin'` (the plugin is not auto-registered) and `main_file = ""` was added (without it the plugin raises FileNotFoundError looking for main.py).
- The project root has no new directory (no `.nicegui`); `.gitignore` unchanged.

NiceGUI APIs verified (3.18.0, by inspect, source reading and tests):
- `ui.run(root=None, *, host, port, reload, show, title, storage_secret, ...)`: the default host is `0.0.0.0`, so the explicit host matters; `storage_secret` not needed; no `__main__` guard required; under pytest the real `ui.run` overrides the port through an environment variable so the host test monkeypatches `ui.run`; if the server cannot start it calls `sys.exit`; `on_air` is off by default.
- `ui.label` sends `text` as a Vue text child (no innerHTML); `ui.html` and `ui.markdown` are `ContentElement` subclasses and a test asserts no ContentElement exists on the page (not seen in a real browser).
- `@ui.page('/')` works with an async page function; `ui.context.client` works; `await client.connected()` is not needed.
- `ui.select` takes a `{id: name}` dict; `on_change` also fires on programmatic changes (the revert re-enters the handler, which returns early).
- `ui.input(...).on('keydown.enter', handler)`, `ui.button(on_click=...)`, `set_enabled`, `.mark()`, `scroll_area().scroll_to(percent=1.0)`, `ui.expansion('Thinking', value=False)` and `ui.notify` work in the simulation.
- Differences from expectation: in NiceGUI 3 `client.on_disconnect` also fires for a socket that later reconnects (it would close a live session's provider), so cleanup uses `client.on_delete` (fires after the reconnect timeout, default 3 s); the simulation replaces `ui.notify` on the `ui` module, so code must call it through the module; `user.find(...).type()` skips disabled inputs but `trigger('keydown.enter')` does not check `enabled`, so the page guards against a running turn itself.

Behaviour:
- One page `/`; a `ChatPage` instance per page load with its own provider and ChatSession (no module-level session state).
- Layout: title, model select (names from models.yaml, value = id), scroll area, input (Enter sends), Send, Stop (enabled only during a turn), New chat.
- Turns run as an asyncio task; Stop cancels it. Mirrors the TUI: thinking in a collapsed `ui.expansion` titled "Thinking"; text in the reply label; usage line via `format_usage`.
- `ErrorEvent` -> `error (kind): message` and the text restored to the input; cancel -> `cancelled` note, text restored, session rolled back; input, Send and New chat disabled during a turn.
- Selector with an empty history swaps immediately; with history it notifies `applies to the next new chat` and keeps the session until New chat.
- `ConfigError` from the provider factory is shown as an error note and the current session is kept (a failed selector change is reverted in the widget).
- `think` per session = `resolve_think(think_flag, model)`.
- All model-derived text is passed through `sanitize_text` and shown in `ui.label`; `ui.html`, `ui.markdown` and `sanitize=False` are never used.
- Cleanup on `client.on_delete` cancels a running turn and closes the provider.
- `run_gui(config, model, provider_factory, think_flag, *, host="127.0.0.1", port=8080, show=True)` calls `ui.run(host=host, port=port, reload=False, show=show, title="llm-harness")`.
- cli.py: `GuiRunner` alias, `_run_gui_lazily` (imports `llm_harness.gui.app` inside the function), a `run_gui` parameter on `main`; `--gui` loads config via `_load_config_and_model` (same first-run and permission handling as the other modes), needs no TTY and returns the runner's exit code; the old 'arrives in step 5' message is gone.
- Importing `llm_harness.cli`, `--help`, `--plain` and importing `llm_harness.tui.app` do not import nicegui (subprocess `-I` checks; coordinator re-checked: `nicegui imported: False`).

Verification (coordinator re-ran):
- `uv run ruff check` clean; `uv run ruff format --check` clean (39 files).
- `uv run pytest -q -rs`: 423 passed, 3 deselected, 0 skipped, 0 failed in about 13 s (earlier 375 plus 48 net new: 34 in tests/test_gui.py and 14 in tests/test_cli.py).
- Tests updated: `test_gui_flag_exits_2_without_touching_config` removed (it encoded the old message); `test_importing_the_cli_and_help_do_not_import_textual` refactored onto a shared subprocess helper with the same assertion; `run_cli` gained a `gui_runner` parameter.
- Real server check, engineer (127.0.0.1:18765, real Ollama provider through `build_provider`, `show=False`, scratch HOME): HTTP 200 after about 6 s, `<title>llm-harness</title>`, the only `gemma` text in the page is the select's option label from models.yaml; `lsof` showed `TCP 127.0.0.1:18765 (LISTEN)` only; `[::1]` refused (curl exit 7); stopped by its own pid, port free.
- Coordinator re-ran the same driver on 127.0.0.1:18771: HTTP 200 after about 3 s with the same title; `lsof` showed `TCP 127.0.0.1:18771 (LISTEN)` only; a request to the machine's en0 address got no answer (status 000, a timeout, not an observed refusal); stopped by its own pid (server pid 68014), port free, no driver process left.
- Live end-to-end turn (engineer; NiceGUI user simulation with the real Ollama factory and a scratch config, run as a temporary pytest file under the scratchpad): 1 passed in 10.7 s: echo `you: Reply with exactly one word: pong`, reply `pong`, usage `[tokens in=23 out=28 10.4s]`, 86 characters of thinking in the thinking label, no errors.
- Engineer mutation checks (engineer only, each reverted, diff empty, suite back to 423 passed): reply rendered with `ui.html(sanitize=False)` -> 8 GUI tests failed (most as AttributeError on `.text` of the html element; the ContentElement assertion is what would catch a working html element); a first attempt at a module-level session was not a real global (only one unrelated test failed), a truly shared session made `test_two_clients_have_separate_sessions_and_histories` fail (`assert 1 == 2` on providers built); host `"0.0.0.0"` -> 2 tests failed (`test_run_gui_binds_loopback_without_reload`, `test_run_gui_passes_the_given_host_port_and_show`); `sanitize_text` removed in all 5 places -> 2 tests failed.
- The real `~/.config/llm-harness` was not created.

Acceptance criteria coverage:
- Now covered: 17 (GUI part: streamed text, collapsible thinking, model selector, offline simulation plus one live end-to-end turn), 18 (loopback bind verified by `lsof`; one ChatSession per client by the two-clients test; model text rendered as text), 2, 4-6 and 27 for the GUI path.
- Still open: 33, 20 (OpenRouter live check by the user), 23 (final), 24 (final grep).

Call sites:
- `run_gui` in `cli._run_gui_lazily` (the default of `main`'s `run_gui` parameter); `register_page` in `run_gui`; `ChatPage` in `register_page`; `GuiRunner` as the parameter type in `cli.py`.

Decisions and deviations (for the final delivery approval):
- Default port 8080 (NiceGUI's own default) with no CLI port flag (a busy port makes `ui.run` exit non-zero).
- `run_gui` defaults to `show=True`, which opens the user's browser in production (no verification run used it).
- `ui.run` is called with `reload=False` and `title="llm-harness"`; the NiceGUI welcome message and uvicorn log level are left at their defaults.
- The initial provider is built inside the page, so a missing key shows an error and the user recovers by picking another model (as in the TUI).
- `tests/test_gui.py` defines its own `RecordingProvider` and `Factory` (duplicating helpers in `tests/test_tui.py`).
- New chat is disabled during a turn.

Not verified:
- The `on_delete` cleanup is tested by calling `client.delete()` directly (the simulation cannot cause a socket disconnect plus the 3 s reconnect timeout).
- Real browser rendering, real keyboard Enter and the scroll-to-end call were checked only through simulated events (layout and CSS classes unseen).
- Server shutdown with live clients is untested (no `app.on_shutdown` hook).
- The en0 non-loopback check timed out instead of being refused.

Fix pass 1 (providers) — done 2026-10-27.

Trigger:
- The independent review (finding 1, High), confirmed by the coordinator's own offline reproduction: with aiohttp installed (nicegui depends on it, so google-genai 2.33.0 switched its HTTP backend from httpx to aiohttp after step 3) a network failure raised `aiohttp.client_exceptions.ClientConnectorError` out of the Vertex adapter. The step-3 live check had run on httpx and the old test injected `httpx.ConnectError`.
- A tester finding: an ambient `OLLAMA_API_KEY` reached the Ollama client (a probe showed an Authorization header present); the ollama package reads `OLLAMA_API_KEY` and `OLLAMA_HOST` at client construction.

Files:
- `providers/vertex.py`: `aiohttp.ClientConnectionError` (including `ClientConnectorError`, `ClientOSError`, `ServerDisconnectedError`, `ServerTimeoutError`), `ClientPayloadError` and the builtin `TimeoutError` -> `connection`; other `aiohttp.ClientError` -> `unknown`; `CancelledError` and programming errors still propagate.
- `providers/ollama.py`: hides `OLLAMA_API_KEY` and `OLLAMA_HOST` while the client is built.
- `providers/environment.py`: a module-level `threading.Lock` held for the whole hide-and-restore window.
- Tests: `test_vertex_adapter.py`, `test_ollama_adapter.py`, `test_openai_compat_adapter.py`, `test_provider_contract.py`, new `test_environment.py`.
- Dependency: `uv add aiohttp` (the only dependency command) -> aiohttp 3.14.5, `aiohttp>=3.14.5` in pyproject.

Behaviour:
- New real-SDK closed-port tests for all three adapters (a port bound to 0 on 127.0.0.1 and closed): Vertex 0.04 s, OpenAI-compatible 0.04 s, Ollama 0.01 s.
- The Vertex test passes `types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))` because with default options the real SDK sleeps 1-10 s (`asyncio.sleep(1 + randint(0, 9))`) before re-raising (engineer 9.06 s and 10.07 s; coordinator's reproduction 8.96 s).

Decisions and deviations:
- Production code is unchanged for that delay, so a real user with an unreachable Vertex endpoint waits 1-10 s before the error (open decision for the user).

Verification (coordinator re-ran):
- ruff clean (40 files); 443 passed, 3 deselected, 0 skipped (earlier 423 plus 20).
- The coordinator's reproduction now ends in `ErrorEvent connection`; the Ollama probe now shows no Authorization header and the environment restored.
- Live Vertex re-check on the aiohttp backend (engineer, 2 calls, np-te-core-ai / us-central1 / gemini-2.5-flash-lite, `_use_aiohttp()` True): `--no-think` -> `pong`, `[tokens in=7 out=1 2.9s]`; `--think` -> `pong`, thinking text, `[tokens in=7 out=32 reasoning=31 2.3s]`.
- Engineer mutation checks (each reverted, diff empty): aiohttp handling removed -> 11 failures (ServerTimeoutError and TimeoutError still passed because ServerTimeoutError subclasses TimeoutError); `OLLAMA_API_KEY` unhidden -> 1 failed; lock removed -> `test_environment.py` failed 3 of 3 runs.

Fix pass 2 (config, sanitizer, redaction) — done 2026-10-27.

Files:
- `config.py`, `display.py`, `redact.py`, `cli.py` (only the permission-warning call site), `tests/fakes.py`.
- Tests: `test_config.py`, `test_display.py`, `test_redact.py`, `test_cli.py`, new `test_fakes.py`.

Behaviour:
- `base_url` validation: http/https, a host, no credentials; the message never echoes the value.
- `priority` and `price` must be finite (NaN made `select_model` raise IndexError before).
- `write_examples` uses `os.open(..., O_EXCL, mode)` so the mode is set at creation, and `os.makedirs(mode=0o700)` for the directory.
- Read errors (OSError, UnicodeDecodeError) become `ConfigError("cannot read <path>: <ExceptionName>")`.
- `permission_warnings(config_dir)` replaces `check_permissions`: read bits 0o044 -> readable warning; write bits 0o022 -> writable warning; directory write bits -> writable-directory warning; each names the path and `chmod 600` or `chmod 700`.
- `sanitize_text` also removes `\r`, U+202A-U+202E, U+2066-U+2069 and U+2028/U+2029 and keeps ZWJ, ZWNJ, LRM, RLM, ALM and variation selectors.
- Redaction patterns added. Decision: after `bearer` in any case, a token of 8+ characters from `[A-Za-z0-9._~+/=-]` that contains a digit or is 20+ long is masked; `Authorization`/`x-api-key`/`api-key` followed by `:` or `=` mask the value whatever its shape, so prose such as `Authorization: required` is masked too; only the `user:pass` part of a URL is masked, only when an `@` follows.
- `assert_stream_contract` now enforces deltas before usage.

Decisions and deviations:
- Tests intentionally changed: the sanitizer row that kept `\r` and `test_control_characters_are_removed_but_layout_kept` (renamed `..._but_newline_and_tab_kept`, expected output `...\tz\n`); the cli `config_dir` fixture (now 0o700 explicitly); `test_check_permissions` replaced by the `permission_warnings` tests.

Verification (coordinator re-ran):
- ruff clean (41 files); 596 passed, 3 deselected, 0 skipped (earlier 443 plus 153).
- Coordinator probe: Persian text with ZWNJ and an emoji ZWJ family sequence pass unchanged; an injected string loses the bidi override, `\r` and U+2028 (ESC removed; the rest of an escape sequence stays as visible text); `bearer abcDEF123456789`, a Google key, URL credentials and a JWT are masked while `The bearer of bad news arrived` and `http://host:8080/x?m=a@b.c` are untouched; with `os.umask(0)` the directory is 0o700, `config.toml` 0o600, `models.yaml` 0o644; a 0o640 file and a 0o775 directory each give their own warning; `base_url` values `http://[bad`, `ftp://x`, `http://user:pass@host`, `localhost:11434` are rejected without echoing the value.
- Engineer mutation checks (each reverted, diff empty): permission mask `0o044` changed to `0o004` -> the 0o640 test failed (the mutation survived the old suite, which tested only 0o644); bidi controls not removed -> 8+ cases failed; case-sensitive Bearer -> 2 failed; `ftp://` or userinfo accepted -> failures; write_examples back to chmod-after-create -> `test_write_examples_does_not_rely_on_chmod` failed; usage-ordering assert removed -> 2 failed.

Fix pass 3 (front ends) — done 2026-10-27.

Files:
- `tui/app.py`, `gui/app.py`, new `gui/security.py` (`LoopbackOnlyMiddleware(app, port)`, registered in `run_gui` with `app.add_middleware` before `ui.run`).
- `cli.py` (the stdin reader `_read_line`: a daemon thread delivers the line through `loop.call_soon_threadsafe`).
- Tests: `test_tui.py`, `test_gui.py`, new `test_gui_security.py`, `test_cli.py`.

Behaviour:
- Both UIs catch any `Exception` around provider construction and the turn and show `error (unexpected): <Type>: <redact(sanitize_text(msg))>` (confirmed by the engineer's mutation: without the catch Textual's crash panel printed `RuntimeError: kaboom sk-CANARY-123456789` unmasked). The helper `_unexpected_error_line` is duplicated in both app.py files; five `except Exception` carry `# noqa: BLE001` with a reason.
- TUI model names: Textual 8.2.8 parses a `str` Select prompt as markup (`GPT [beta]` showed `GPT `, `[/]` raised MarkupError), so options are passed as `rich.text.Text`.
- GUI select labels cannot be HTML: Quasar's QSelect uses innerHTML only when an option has `html: true` or `options-html`/`display-value-html` props are set (static/quasar.umd.js), NiceGUI builds options as `{value, label}` only (elements/select.py), and a test asserts `<b>x</b>` and `<img src=x onerror=alert(1)>` reach the select options as plain labels.
- Middleware: NiceGUI's app is a Starlette subclass, `add_middleware` works before startup, and socket.io is mounted at `/_nicegui_ws/` on the same app so the middleware covers it (shown live). Rules as in criterion 37; a missing Host is rejected; a missing Origin is allowed; HTTP gets a plain-text 403 `forbidden` without echo. Duplicated Origin headers must all be allowed.

Decisions and deviations:
- Starlette is a transitive dependency of NiceGUI and not declared in pyproject (decision for the delivery).
- Tests changed: `test_selector_lists_exactly_the_models_yaml_names` compares `label.plain`; two `run_gui` tests stub `app.add_middleware` (otherwise the real middleware stayed on the shared test app and the next `user.open` got 403).

Verification (coordinator re-ran):
- ruff clean (43 files); 663 passed, 3 deselected, 0 skipped (earlier 596 plus 67), identical over three runs (about 14.2 s).
- Coordinator real-server curl matrix (port 18772, `show=False`, scratch HOME; stopped by its own pid 1730, port free, no process left): good Host `127.0.0.1` 200 and `localhost` 200; `Host: evil.example` 403; `Host: evil.example:18772` 403; `Host: 127.0.0.1.evil.example:18772` 403; `Origin: http://evil.example` on `/` 403; `Origin: null` 403; socket.io polling `/_nicegui_ws/socket.io/?EIO=4&transport=polling`: no Origin and good Host 200, good Origin 200, bad Origin 403, bad Host 403.
- Engineer's matrix (port 18417, pid 98494) also showed websocket upgrades via aiohttp: bad Origin refused (403), bad Host refused (403), no Origin connected, good Origin connected.
- Ctrl-C: in a pseudo-terminal with `.venv/bin/llm-harness --plain`, Ctrl-C at the `you>` prompt gave exit code 130 after 0.10 s (engineer) and 0.08 s (coordinator), no Enter, no Traceback; with the old `asyncio.to_thread` reader the check still hung after 5.0 s and the process was terminated by its own pid, so the check can fail.
- Engineer mutation checks (each reverted, diff empty): no Host check -> 22 failed; no Origin check -> 14 failed; no generic except in the TUI turn -> 1 failed; `to_thread` reader back -> the blocked-reader cancel test failed (5.0 s against a 2 s bound).
- The real `~/.config/llm-harness` was never created in any pass.

GUI layout change (user feedback after a manual try) — done 2026-10-27.

User feedback, verbatim: 'I want full width widgets, not cramped narrow chat window with minuscule letters'; later 'EVERY time you go for this cramped fixed width awful widgets'.

Change in `src/llm_harness/gui/app.py` (one `ui.add_css` block per page, no JavaScript):
- Page container `llm-page w-full h-full no-wrap` (marker `page`); `max-w-3xl mx-auto` removed.
- `.nicegui-content` is `100dvh` with 12px padding and `html, body` have `overflow: hidden`, so only the message area scrolls.
- Title `text-h4` (34px). Model selector `w-full`, 20px.
- Message area `ui.scroll_area` with classes `w-full grow border` (the fixed `h-96` removed). Message text base 18px.
- Input 20px with `grow` and a minimum width of 12rem; buttons 18px; input row `w-full items-center gap-2` (wraps on a phone).
- Thinking text, usage line, cancelled note and the Thinking header 16px (`_DIM` is now `text-grey llm-secondary`).
- `.q-scrollarea__content` has `width: 100%; overflow-wrap: anywhere` (the old layout let long lines run past the edge).
- Unchanged: behaviour, markers, `ui.label` with `sanitize_text`, host 127.0.0.1 and the middleware.

Tests: two added. The page container is `w-full` with no `max-w-*`/`mx-auto`; the message area has `grow` and `w-full` and no `h-*`/`max-h-*` class; the input row comes after the message area and is `w-full`; the selector is `w-full`.

Mutation check (engineer): putting `max-w-3xl mx-auto` back made the layout test fail (`assert not ['max-w-3xl']`).

Verification (coordinator): `ruff check` and `ruff format --check` clean (43 files); `pytest -q -rs`: 665 passed, 3 deselected, 0 skipped (earlier 663 plus 2); real `uv run llm-harness --gui` (scratch HOME, `BROWSER=/usr/bin/true`) served HTTP 200 on localhost:8080 after about 2 s; the served page contains `max-w-3xl` 0 times, `mx-auto` 0, `h-96` 0, `w-full` 4 and `grow` 2, and the 18px rule; the server was stopped and port 8080 was free afterwards.

Process note: the engineer took headless-Chrome screenshots before the user said screenshots waste tokens (the layout was judged visually in three rounds at 1440, 1024 and 390 px widths; a first run showed a 'Connection lost' toast that was a virtual-time artefact); after the correction no more were taken. A bullet in `specs/tech-stack.md` about GUI layout was added once (tech-stack version 7). The user's own manual run of `llm-harness --gui` (their words: 'It does work') happened before this change and showed the layout they rejected.

Steps 1a-5, fix passes 1-3 and the GUI layout change are done; documentation update is recorded below.

## Validation

Independent review:
- The reviewer (read-only, not given the author's conclusions) reported 13 findings: 1 High (Vertex aiohttp errors; CONFIRMED by the coordinator's reproduction), 2 Medium (unexpected exceptions dump locals in the TUI, PLAUSIBLE, confirmed later by mutation; no Host/Origin check in the GUI, absence CONFIRMED, exploit PLAUSIBLE) and 10 Low (Ctrl-C hang at the prompt; NaN priority; chmod after create; permission check scope; config read errors; sanitizer gaps; markup in TUI model names; redaction coverage; unlocked `os.environ` mutation; `--no-think` on models that cannot disable thinking).
- Disposition: findings 1-12 fixed in fix passes 1-3 (see Implementation Notes); finding 13 is documented (README note, spec Unverified list), not changed.

Independent test validation (tester, before the fixes):
- ruff clean (39 files); 423 passed, 3 deselected, 0 skipped, identical over three runs.
- Offline-isolation check by reading (no real host or real home in default tests; not enforced by a network block).
- Live local `-m "live and not vertex" -k "ollama or openai"`: 2 passed, 424 deselected, 10.87 s.
- Real CLI runs with scratch HOME and local Ollama: `--help` exit 0; two-turn pipe conversation (`My name is Ada...` then `What is my name?`) answered `Ada` with input tokens 21 then 63 (whole history sent), same through `--model gemma-oai` (21 then 67); `--model nope` exit 1 with ids listed; empty HOME first run exit 1 with nothing written; no flags without a terminal exit 2; `base_url` `http://127.0.0.1:9` -> `error (connection)` exit 1, no traceback; `--think`/`--no-think` on both provider routes behave as specified.
- Mutation testing on a scratch copy: 21 of 22 mutants killed; the survivor (permission mask) was fixed in pass 2.
- Criteria map by the tester (33 rows): all covered except the ones listed next.
- Tester gaps and their disposition: permission mask only tested at 0o644 (fixed, pass 2); ambient `OLLAMA_API_KEY` reaches the client (fixed, pass 1); criterion 21 thin (`src/` has no logging calls at all, so there is nothing to configure or leak by logging; the canary tests plus per-adapter redaction tests cover error text; no code sets a WARNING level because nothing logs) — kept as stated; live tests assert text and usage only, thinking behaviour is verified by CLI runs (manual), not by pytest; untested production paths: the default `run_tui` runner (covered by the pseudo-terminal run only), `run_gui(show=True)` (opens a browser), GUI shutdown with live clients; `assert_stream_contract` ordering (fixed, pass 2); `write_examples` chmod race (fixed, pass 2).

Final state (coordinator, after the fix passes):
- `uv run ruff check` clean; `uv run ruff format --check` clean (43 files).
- `uv run pytest -q -rs`: 663 passed, 3 deselected (`tests/test_live_ollama.py`, `tests/test_live_openai_compat.py`, `tests/test_live_vertex.py`), 0 skipped, 0 failed, one DeprecationWarning from google-genai, identical over three runs (14.35 s, 14.16 s, 14.27 s); 663 of 666 collected.
- Baseline before any code was 0 tests collected (no pre-existing failures, so no regression is possible).
- Call-site evidence: an AST scan of `src/llm_harness` gave 160 rows; the only items with no textual use outside their definition are `main` (console script entry point), the three pydantic validators (`ProviderConfig._wrap_api_key`, `ProviderConfig._check_rules`, `HarnessConfig._check_references`) and the Textual hooks and actions of `ChatApp` (`compose`, `on_mount`, `on_unmount`, `on_select_changed`, `action_new_chat`, `on_input_submitted`, `action_cancel_turn`), all called by their framework.
- `price` is read and validated but not used (by design), and the `system` role has no producer in this slice (planned stub, consumed and tested by the Vertex adapter).
- The real `~/.config/llm-harness` was never created.
- Criteria status: all of 1-32 and 34-43 covered offline; criteria 17 and 18 (UIs) covered by offline simulation plus the pseudo-terminal run and the real-server checks, with appearance and real keyboard behaviour unverified; criterion 20: live for Ollama `/v1` and Vertex, the OpenRouter live check is for the user; criterion 22 verified by the coordinator in isolated tool directories (step 1b), not re-run by the tester; criterion 33 is satisfied by the Unverified list and is restated in the delivery summary; criteria 23 and 24 are covered by the final state above.
- Known limits: OpenRouter and OpenAI are implemented from documentation and untested; the Vertex SDK sleeps 1-10 s before reporting an unreachable endpoint; nothing was tried in a real browser or by a human at a keyboard.

After the GUI layout change (coordinator, 2026-10-27; re-run by the technical writer for the closeout, see Documentation Updates):
- 665 passed, 3 deselected, 0 skipped, 0 failed (the technical writer's re-run: `uv run pytest -q -rs`, 18.98 s; the same 665 as the coordinator's).
- `uv run ruff check`: All checks passed. `uv run ruff format --check`: 46 files already formatted (the coordinator reported 43; the count differs and the reason is not known, the result is clean in both).

## Documentation Review

Reviewed 2026-10-27 against the current code. Nothing in the constitution (`specs/mission.md`, `specs/tech-stack.md`, `specs/roadmap.md`) was edited yet; every constitution item below awaits the human's approval. README.md and CHANGELOG.md do not exist (`ls -A` shows only `.gitignore`, `.python-version`, `pyproject.toml`, `uv.lock`, `specs`, `src`, `tests` and tool caches), so there is no README or CHANGELOG drift to report, only the proposals below. No constitution file names another project or a local path outside this repo (grep for `/Users`, `DPurge`, `agent-tools`, `jprusaczyk`: no hits; the only `~/` paths are this project's own `~/.config/llm-harness/...`).

### Constitution drift

Code evidence paths are under `src/llm_harness/` unless stated. 19 findings: mission 2, tech-stack 16, roadmap 1 (the `Environment target` check and roadmap item R2 are confirmations, not findings).

mission.md (2)
- M1. `specs/mission.md:11` "No code exists yet." Evidence: 18 Python files, 1973 lines under `src/llm_harness/` (`wc -l`); `cli.py:263` `main`; the Validation section records 663 passed tests. Proposed: "The harness streams chat over Ollama, OpenAI-compatible APIs and Vertex AI through a plain line mode, a terminal UI and a NiceGUI web UI; the orchestrator, model fallback and context inspector are not built yet."
- M2 (for the human to decide; not strictly contradicted). `specs/mission.md:21-25` describe fallback, price tracking, the orchestrator and the context inspector in the present tense. Evidence: `config.py:277-295` `select_model` returns exactly one model with no fallback; `config.py:123-126` `price` is only validated; no orchestrator or inspector code exists (`ls -R src`). Proposed: start the paragraphs with "Planned:" or keep them as the target state and rely on `specs/roadmap.md` `## Later`.

tech-stack.md (16)
- T1. `specs/tech-stack.md:13` presents the TypeScript/esbuild toolchain as in use. Evidence: no `.ts`, `.js`, `.css` or `package.json` outside `.venv` (`find`); roadmap `a2ui-nicegui` (`specs/roadmap.md:25`) is Later. Proposed: "Planned, not yet used (roadmap `a2ui-nicegui`): TypeScript for the web UI widgets, compiled with esbuild. ..." (keep the version facts; Node v26.8.2 and uv 0.12.13 were rechecked with `--version` and match).
- T2. `specs/tech-stack.md:53` "The wheel contains the prebuilt frontend bundles." and `:55` "Frontend build: esbuild and TypeScript via npm, run on the developer or release machine only." Evidence: the wheel built on 2026-10-27 (coordinator, per the brief; not rebuilt by me) holds only Python modules; no frontend files exist (T1). Proposed: "The wheel will contain prebuilt frontend bundles once the frontend exists (planned)." and "Frontend build (planned): esbuild and TypeScript via npm, run on the developer or release machine only."
- T3. `specs/tech-stack.md:15-26` library list. Versions all match: `uv.lock` and `uv pip list --offline` give pydantic 2.14.1, pyyaml 6.0.3, httpx 0.28.1, ollama 0.6.3, openai 3.40.0, google-genai 2.33.0, aiohttp 3.14.5, textual 8.2.8, nicegui 3.18.0, pytest 9.1.1, pytest-asyncio 1.4.1, ruff 0.17.3. Omissions: `aiohttp` is a declared direct dependency (`pyproject.toml:7`) imported by `providers/vertex.py:6`; `starlette` 1.10.0 is imported by `gui/security.py:3` but not declared (transitive through nicegui); `httpx2` 2.16.0 is imported only by tests (`tests/test_openai_compat_adapter.py:7`, `tests/test_provider_contract.py:7`) and not declared (transitive through openai). Proposed additions: "aiohttp 3.14.5: HTTP backend of the Vertex SDK; its exceptions are handled in the Vertex adapter (declared dependency)." and "Imported but not declared (transitive): starlette (`gui/security.py`) and httpx2 (tests only); whether to declare starlette is open." Also change `:24` to "httpx 0.28.1: HTTP client used by the Ollama and Vertex adapters" (`providers/ollama.py:6`, `providers/vertex.py:7`).
- T4. `specs/tech-stack.md:28` "SDK call signatures are UNVERIFIED until checked ... at implementation time." Stale: the three adapters exist (`providers/ollama.py`, `openai_compat.py`, `vertex.py`) and the spec's Implementation Notes record their signatures as checked. Proposed: "The adapters' SDK calls were checked against the installed packages; the OpenRouter and OpenAI paths are implemented from documentation and unverified (see Key conventions)." (Evidence is the spec's own notes, not re-run by me.)
- T5. `specs/tech-stack.md:57` "Live-LLM tests are opt-in behind a marker (for example `-m live`)". Evidence: `pyproject.toml:33` `addopts = '-m "not live"'`, `:35` marker `live`; `tests/test_live_ollama.py:21`, `test_live_openai_compat.py:30`, `test_live_vertex.py:29` set `pytestmark = pytest.mark.live`. Proposed: "Live-LLM tests carry the marker `live` and are deselected by default (`addopts = '-m \"not live\"'`); run them with `uv run pytest -m live`. The default pytest config also loads the NiceGUI test plugin (`-p nicegui.testing.user_plugin`, `main_file = \"\"`), which a plain `uv sync` provides."
- T6. `specs/tech-stack.md:59` "Intended layout". Actual (`ls -R src tests`): `src/llm_harness/{__init__,cli,config,display,redact,session,types}.py`, `providers/{__init__,base,environment,ollama,openai_compat,vertex}.py`, `tui/{__init__,app}.py`, `gui/{__init__,app,security}.py`; tests `tests/{fakes,test_*}.py` including `test_live_*.py`. Proposed: "Layout: `src/llm_harness/` (src layout): core in `types.py`, `session.py`, `config.py`, `redact.py`, `display.py`, `cli.py`; `providers/` (`base`, `environment`, one adapter module per SDK); `tui/` and `gui/` (`app.py`, plus `security.py`) as separate front-end packages; tests under `tests/` with the scripted fake provider in `tests/fakes.py`."
- T7. `specs/tech-stack.md:63` "Exact config key names ... will be confirmed with the author before any implementation." Stale: the keys are implemented and recorded as confirmed at `:34` and `:71` (`config.py:65-73`, `config.py:129-137`). Proposed: "Config key names are confirmed (see Decision above); tool and subagent identifiers will be confirmed with the author before their features."
- T8. `specs/tech-stack.md:65` Command line. `--no-think` is already listed (hypothesis refuted: `cli.py:70-77`). Stale: "the only runnable UI before the terminal UI exists" (the TUI exists: `cli.py:296-297`, `tui/app.py:230`). Missing: `--plain` and `--gui` are mutually exclusive (`cli.py:55`); `--gui` opens the user's browser on port 8080 with no port flag (`gui/app.py:264-266` defaults `host="127.0.0.1"`, `port=8080`, `show=True`; `cli.py:295` passes none); without a terminal, `llm-harness` prints a hint and exits 2 (`cli.py:279-286`); `/exit` or EOF ends a plain chat, Ctrl-C exits 130 (`cli.py:44,156-159,310-312`); the TUI keys are Escape cancel, Ctrl+N new chat, Ctrl+Q quit (`tui/app.py:47-49`). Proposed: "`llm-harness --plain` is a permanent line-based chat mode that works over pipes and in tests. `llm-harness` (terminal UI) needs a terminal and otherwise exits 2 with a hint to use `--plain`. `llm-harness --gui` serves on 127.0.0.1:8080 and opens the browser (no port flag). `--plain` and `--gui` are mutually exclusive."
- T9. `specs/tech-stack.md:72` "Warn if the config file is group- or world-readable." Incomplete: `config.py:245-274` `permission_warnings` also warns when `config.toml` is writable by group/other (mask `0o022`) and when the config directory is writable by group/other; creation modes are 0600 (`config.toml`), 0644 (`models.yaml`), directory 0700 (`config.py:333-340`). Proposed: "Warn on stderr, naming the path and the fix, when `config.toml` is readable or writable by group/other (`chmod 600`) or the config directory is writable by group/other (`chmod 700`). Example files are created with mode 0600 (`config.toml`), 0644 (`models.yaml`), directory 0700."
- T10. `specs/tech-stack.md:73` "mask them as `[REDACTED:<type>]`". Code has one mask, `[REDACTED:api_key]` (`redact.py:6`), and `redact()` masks more than keys: the configured keys, `Authorization`/`x-api-key`/`api-key` values, URL credentials, `bearer` tokens, `sk-` keys, `AIza` Google keys and JWT-shaped strings, before truncating to 300 characters (`redact.py:7-24,48-62`). Proposed: "Never print or log keys; `Secret` prints as `[REDACTED:api_key]`, and every provider error message goes through `redact()` (masks before it truncates to 300 characters)."
- T11. Omission, sanitizer rule. All model text shown by the CLI, TUI and GUI passes `sanitize_text` (`display.py:13-25`; `cli.py:188,191,213`; `tui/app.py:16`; `gui/app.py:11,204,209`): ESC, C0 except newline/tab, DEL, C1, bidi controls and U+2028/2029 are removed; joiners and variation selectors are kept. The TUI uses `markup=False` (`tui/app.py:152-153`); the GUI shows text in `ui.label` and never uses `ui.html` or `ui.markdown` (`gui/app.py:39-40`). Proposed: "Model output is untrusted: pass it through `llm_harness.display.sanitize_text` and render it as plain text (no markup, no raw HTML) in every front end."
- T12. Omission, GUI exposure rule. `gui/app.py:264,271-274` binds `127.0.0.1` explicitly (NiceGUI's own default is `0.0.0.0`) and registers `LoopbackOnlyMiddleware`; `gui/security.py:19-20` allows only Host `127.0.0.1:<port>` / `localhost:<port>` and Origin `http://` + those, else 403 (websocket close 1008, `gui/security.py:40-46`). One `ChatSession` per client (`gui/app.py:1`). Proposed: "The GUI binds 127.0.0.1 only and rejects any other Host or Origin with 403; there is no login, so never bind another address and keep one session per browser client."
- T13. Omission, environment hiding for all three adapters. `specs/tech-stack.md:77` covers only GCP. Code: `providers/environment.py:14-35` `hidden_environment` (module lock); OpenAI `OPENAI_*` (`openai_compat.py:147`); Ollama `OLLAMA_API_KEY`, `OLLAMA_HOST` (`ollama.py:34,91`); Vertex 11 `GOOGLE_*`/`GEMINI_*` names (`vertex.py:54-64,176`). Proposed: "Every adapter hides the SDK's ambient environment variables while it builds its client (`providers/environment.py`); `base_url`, `api_key`, `project` and `location` always come from the config."
- T14. Omission, `error (unexpected)` line. TUI and GUI show an unexpected exception as one line `error (unexpected): <Type>: <redacted message>` without traceback (`tui/app.py:30-33,100,221`; `gui/app.py:32,112,231`); the plain CLI prints `error (<kind>): <message>` (`cli.py:213`). Proposed: "Front ends show unexpected exceptions as the single line `error (unexpected): <Type>: <redacted message>`; never a traceback or locals."
- T15. Omission, test-only environment variables. `LLM_HARNESS_LIVE_MODEL` (`tests/test_live_openai_compat.py:32,54`) and `LLM_HARNESS_LIVE_VERTEX_MODEL` (`tests/test_live_vertex.py:31,61`) select the model for the live tests. Proposed: "Test-only variables: `LLM_HARNESS_LIVE_MODEL` (live OpenAI-compatible test) and `LLM_HARNESS_LIVE_VERTEX_MODEL` (live Vertex test); the application never reads them."
- T16. `specs/tech-stack.md:71` and `:34` omit rules a contributor will hit: `base_url` must be an http(s) URL with a host and no credentials (`config.py:44-52,87-92`); an `ollama` provider requires `base_url` and rejects keys (`config.py:99-105`); an `openai` provider requires `base_url` and one key source (`config.py:106-110`); a `vertex` provider requires `project` and `location` and rejects `base_url` and keys (`config.py:111-118`); unknown keys are rejected everywhere (`extra="forbid"`, `config.py:62`; top level `config.py:208-212`, `models.yaml` only `models`, `config.py:221`); `priority` and `price` must be finite (`config.py:55-58,126,139`); two top-priority `default` models are an error (`config.py:290-294`). Proposed: append one sentence to the `:71` bullet listing these rules.
- Checked and still accurate (no finding): `Python >= 3.12` (`pyproject.toml:5`), uv 0.12.13 and `.python-version`, `Environment target` bullets at `:76-79` (Ollama default `http://localhost:11434` in `config.py:303`; Vertex project and location from config, ADC only, `vertex.py:171-178`; no production target), `:67` (only adapters import a provider SDK: `grep` of `ollama|openai|google` imports finds them only under `providers/`), `:70`, `:34` model keys (`config.py:129-137`), `:75`, `:58` (no git directory, no CI).

roadmap.md (2)
- R1. `specs/roadmap.md:11` `Chat streaming across providers — branch none — specs/features/chat-streaming.md` must leave `## Now` at closeout (B5), in the same edit as the CHANGELOG entry (constitution-format: `## Now` line lifecycle). Not applied. Proposed: delete the line and leave `## Now` with a placeholder such as "Nothing in progress." (`## Next` uses "Nothing queued.").
- R2 (confirmation, not a finding). No other roadmap line is contradicted: `## Later` items have no code (`ls -R src`), `a2ui-nicegui` frontend is not started. The `## Next` and `## Out of scope` text still holds.

### README and CHANGELOG

README.md and CHANGELOG.md are missing. The spec's Affected Areas (line 212) promise both; `pyproject.toml` declares no `readme`. Both are written in the next phase.

Proposed README.md outline (every command below exists: `--plain`, `--gui`, `--model`, `--think`, `--no-think` in `cli.py:53-78` and `uv run llm-harness --help` output; console script `pyproject.toml:18-19`):
- What it is: streaming chat harness over Ollama, OpenAI-compatible APIs (OpenRouter through `base_url`) and Vertex AI; three front ends; single user, desktop; full history is sent each turn (temporary).
- Install and dev loop: `uv sync`, then `uv run llm-harness`; optional `uv tool install --editable .` (from the constitution, verified by the coordinator in step 1b).
- Configuration, both under `~/.config/llm-harness/` (`config.py:160-161`). `config.toml`: `[providers.<id>]` with `type` (`ollama`|`openai`|`vertex`), `base_url`, `api_key` or `api_key_env` (not both), `project`, `location`, `reasoning_style` (`openai`|`openrouter`, openai providers only), as in `EXAMPLE_CONFIG_TOML` (`config.py:298-316`): for example `[providers.local]` `type = "ollama"` `base_url = "http://localhost:11434"`, and `[providers.openrouter]` `type = "openai"` `base_url = "https://openrouter.ai/api/v1"` `api_key_env = "OPENROUTER_API_KEY"` `reasoning_style = "openrouter"` (the OpenRouter URL is from the spec's decision 12 link, not from code: confirm before publishing). `models.yaml`: `models:` list with `id`, `name`, `provider`, `model`, `tags`, `priority`, optional `price` (`input_per_mtok`, `output_per_mtok`) and optional `thinking: true|false`, as in `EXAMPLE_MODELS_YAML` (`config.py:318-331`); the default is the `default`-tagged model with the highest priority. On first run the app offers to write example files.
- Commands and flags: `llm-harness` (terminal UI, needs a terminal), `--plain` (pipes; `/exit` ends), `--gui` (127.0.0.1:8080, opens the browser), `--model ID`, `--think`, `--no-think` (flag > model `thinking` > model default).
- Providers: verified live: Ollama, Ollama through its OpenAI-compatible `/v1` endpoint, Vertex AI. Implemented from documentation and untested: OpenRouter, OpenAI (spec decision 12, Validation known limits).
- Security notes: key inline or by `api_key_env`; permission warnings and creation modes (T9); the GUI has no login, binds 127.0.0.1 and checks Host and Origin, and `--gui` opens the browser on port 8080 (T12); model output is sanitized (T11); keys are masked in output.
- Troubleshooting, with source: `HTTP_PROXY` with local Ollama needs `NO_PROXY=localhost,127.0.0.1` (from the brief; not verified in code); Vertex uses Application Default Credentials (`vertex.py:171`; the `gcloud auth application-default login` command is from the brief, not verified by me); an unreachable Vertex endpoint takes 1-10 s to report (spec Validation, known limits); `--no-think` may be rejected by models that cannot disable thinking (spec Unverified, line 197).
- Tests: `uv run pytest` (offline); `uv run pytest -m live` for live tests; `LLM_HARNESS_LIVE_MODEL`, `LLM_HARNESS_LIVE_VERTEX_MODEL` (T15); `uv run ruff check`.
- Known limits: OpenRouter and OpenAI untested; nothing tried in a real browser or at a real keyboard; the full chat history is sent; no orchestrator, tools or fallback yet.

CHANGELOG records (one implicit artifact: root `.`, `CHANGELOG.md`; the constitution has no Artifacts table). Format `{changelog path, category, proposed entry text}`. All are `Added`; no `Changed` or `Fixed` entries because nothing was released before.
- {CHANGELOG.md, Added, "Streaming chat over Ollama, OpenAI-compatible APIs (including OpenRouter) and Vertex AI through shared stream events."}
- {CHANGELOG.md, Added, "`llm-harness --plain`: line-based chat that works over pipes."}
- {CHANGELOG.md, Added, "Terminal UI (`llm-harness`) with a model selector, collapsible thinking and Escape to cancel."}
- {CHANGELOG.md, Added, "Web GUI (`llm-harness --gui`) served on 127.0.0.1:8080; it opens the browser."}
- {CHANGELOG.md, Added, "Configuration files `config.toml` (providers) and `models.yaml` (models, tags, priority, price); example files are offered on first run."}
- {CHANGELOG.md, Added, "Per-model `thinking` setting, `reasoning_style` for OpenAI-compatible providers, and `--think` / `--no-think` flags."}
- {CHANGELOG.md, Added, "Display of the model's thinking text (never kept in the chat history) and a token and time usage line."}
- {CHANGELOG.md, Added, "Safety features: API keys masked in output, config file permission warnings, sanitized model output, GUI Host and Origin check, and ambient SDK environment variables ignored."}

### CI, build and release

- CI review: no CI, release or deploy files exist (no .github, Dockerfile, Makefile); nothing to update. Checked with `ls -A -d .github Dockerfile Makefile` (all absent). Release wiring check not applicable: no workflow creates a tag.
- {pyproject.toml, needs change: no, reason}. Evidence: backend `uv_build>=0.12.13,<0.13.0` (`pyproject.toml:21-23`); console script `llm-harness = "llm_harness.cli:main"` (`:18-19`) and `uv run llm-harness --help` ran from it; `requires-python >=3.12` (`:5`); 9 dependencies (`:6-16`), matching the 9 `Requires-Dist` in the wheel the coordinator built on 2026-10-27 (wheel contents, metadata and clean-venv install are the coordinator's checks, not re-run by me); `uv.lock` exists and `uv lock --check --offline` printed `Resolved 84 packages` and made no change; pytest needs `nicegui.testing.user_plugin` (`:33`), nicegui is a runtime dependency (`:10`) and the plugin file exists in `.venv/lib/python3.13/site-packages/nicegui/testing/user_plugin.py`, so a clean `uv sync` provides it. No `readme`, `license` or `authors` field (`pyproject.toml:1-5`): no change needed while nothing is published; after README.md exists, adding `readme = "README.md"` is optional. I do not know whether a license is wanted.

### Not checked

- Not rebuilt by me: `uv build`, the wheel contents and the clean-venv install (coordinator's 2026-10-27 results are quoted, not reproduced).
- Not run: pytest, ruff, any network or model command, a browser, a real terminal. Test counts (663 passed) come from the spec's Validation section.
- Troubleshooting items taken from the brief or the spec, not from code: `NO_PROXY`, `gcloud auth application-default login`, the Vertex 1-10 s delay, `--no-think` rejection; the OpenRouter base URL.
- Criterion 18 of this spec says "sanitized markdown", but the GUI code shows plain `ui.label` text and uses no markdown (`gui/app.py:39-40`); this is a spec wording point, not edited.
- `specs/memory.md` does not exist; no other documentation files exist outside `specs/`.

## Documentation Updates

Done 2026-10-27.

What was changed where:
- `README.md` created (quick start, configuration, flags, providers, security notes, troubleshooting, tests, known limits).
- `CHANGELOG.md` created with 8 entries under `[Unreleased]` / `Added` (the web GUI entry says it serves on 127.0.0.1:8080, opens the browser and uses the full window width).
- `specs/roadmap.md`: the `## Now` line for this feature removed, `Nothing in flight.` left (roadmap version 3).
- `pyproject.toml`: the line `readme = "README.md"` added under `[project]`. `uv lock --check --offline` passed (`Resolved 84 packages`, no change) and `uv build` succeeded; the wheel METADATA now carries the README (`Description-Content-Type: text/markdown`).
- This spec: criterion 18 reworded to match the code (plain text in labels, never HTML or markdown); spec version 5.
- `specs/memory.md` created with 4 entries (verbatim):

```
2026-10-27T18:46:20Z [gotcha] google-genai uses aiohttp automatically when aiohttp is installed (nicegui depends on it), so network failures raise aiohttp exceptions, not httpx ones; adapters must catch both, and a test against a real closed port is needed because mocks hide it.
2026-10-27T18:46:21Z [gotcha] NiceGUI 3.18: `ui.run` defaults host to all interfaces (pass the loopback host explicitly); `client.on_disconnect` also fires for sockets that reconnect (use `client.on_delete` for cleanup); call `ui.notify` through the `ui` module for the test `user` fixture; pytest needs `-p nicegui.testing.user_plugin` in addopts and `main_file = ""`.
2026-10-27T18:46:22Z [gotcha] Textual 8.2: a `str` Select option or Static content is parsed as console markup (`[/]` raises MarkupError); pass `rich.text.Text` or `Static(..., markup=False)`.
2026-10-27T18:46:23Z [build] `uv init` runs `git init` unless `--vcs none` is given.
```

- Constitution: the Documentation Review findings M1, M2 and T1-T16 were applied on 2026-10-27 (mission version 4, tech-stack version 7 including the GUI layout bullet); see Documentation Review above, not duplicated here.

Closeout checklist:
- [x] Validation recorded with pass/skip/fail counts and every skip explained: 665 passed, 0 skipped, 0 failed; 3 live tests deselected by design (`tests/test_live_ollama.py`, `tests/test_live_openai_compat.py`, `tests/test_live_vertex.py`).
- [x] README and CHANGELOG updated: `README.md` created; `CHANGELOG.md` has 8 entries.
- [x] This feature's `## Now` line removed from `specs/roadmap.md` (`## Now` holds `Nothing in flight.`).
- [x] CI and release review recorded in Documentation Review: the `CI review:` line (no CI, release or deploy files) and the `{pyproject.toml, needs change: no, ...}` record; adding `readme` was the one optional change named there, and it was made.
- [x] Every new function, type and config key has a non-test use: the 160-row AST scan in Validation; the only exceptions are framework and console-script entry points, `price` (read-only by design) and the `system` role (no producer), all listed in Validation.
- [x] `validate-specs` passes (`node /Users/jprusaczyk/.claude/skills/spec-validation/validate-specs.mjs` from the project root; result in the delivery report).
