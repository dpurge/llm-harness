---
title: "Environment, identity and the data layer plan: users, agents, tools, storage"
kind: feature
status: done
version: 4
updated: 2026-10-28
branch: main
---

## Problem / Motivation

This is a plan-only umbrella. It contains no code. It fixes the contracts so later roadmap items know what to implement. It governs these items, in order: `environment-and-identity` (this one), `storage-config`, `embeddings`, `agent-registry`, `tool-registry`, `subagents`, `tools-first-set`, `storage-backends-remote`. `a2ui-nicegui` is unrelated.

The author's words (2026-10-28), verbatim:

> "I want the state to include user name, computer name, current date and current time. In the future there will be agents (all tagged, orchestrator one of them, but there will be researcher, writer, reviewer etc), tools (also tagged, but the orchestrator will be able to search the registry for tools; agent tags will be matched automatically, but orchestrator will be able to manipulate tool context). Relational data will be stored either locally (SQLite) or remotely (Postgres). Vector data will also be stored either locally (qdrant-edge) or remotely (qdrant). Files can be stored either locally or remotely (shared filesystem or maybe garage/cloud storage, undecided yet) Configuration must support it and future tools need to be able to read and write this data."

Earlier requirement: recognise the same user on another computer (for external memory).

Already built (evidence):

- `Environment{now, user_name, computer_name}` at `src/llm_harness/state.py:35`, filled in `src/llm_harness/orchestrator.py:65` and `:160` (`getpass`, `socket.gethostname`), protected from patching (`_PROTECTED` in `state.py:30`).
- `src/llm_harness/tools.py` is a name-lookup stub: `Tool` protocol with `name` and `async run(arguments) -> str`; `ToolRegistry` with register/get/names.
- Persistence: one SQLite file `~/.local/share/llm-harness/harness.db` (tables `llm_calls`, `model_backoff`) owned by `src/llm_harness/store.py`, with `PRAGMA user_version` migrations. No Postgres, qdrant or file-store code or dependency exists.
- Config: `~/.config/llm-harness/config.toml` (providers) and `models.yaml`; `HarnessConfig` in `src/llm_harness/config.py` has only `providers` and `models`.

Inspiration (not a dependency): the course notebook `/Volumes/Public/notebooks/context-engineering/04` (`C1_M2_Lab_2_Tool_Registry.ipynb`, `tool_utils.py`). It has a `Tool` with name, description, input/output model and JSON schema; a `ToolRegistry` with register/get; each tool indexed as "Name: .. Description: .." into a vector collection; top-K search for a task; reciprocal-rank fusion `rrf(dense, sparse, rrf_k=60)` for hybrid search. Its own heuristic: a registry pays off at about 10+ tools or 10k+ tokens of definitions. It uses Chroma and the OpenAI Responses API; this project uses neither.

## Acceptance Criteria

1. The spec records every decision below with the human's choice, and every open question as OPEN.
2. Each later roadmap item has a subsection in Approach with scope, depends on, and 3-6 checkable acceptance criteria that a future spec can copy.
3. `specs/roadmap.md` and `specs/tech-stack.md` (v14, updated in this same step) are consistent with it: every slug named here exists in the roadmap `## Later` list. `validate-specs` passes.
4. No file under `src/` or `tests/` is changed by this feature.
5. The human approved the spec.
6. The decisions on qdrant-edge and bge-m3 are recorded.

## Approach

Memory consulted: storage|sqlite|store, environment|identity|user_id, tool|registry, config — 2 relevant entries (the PRAGMA user_version migration gotcha; test fixtures must patch store.default_db_path through its module)

Everything below is **planned**; none of it is built.

### A. Decisions

| Question | Decision | Chosen by human 2026-10-28 |
| --- | --- | --- |
| Scope of this feature | Plan-only plus roadmap split | yes |
| Same user on other computers | Explicit `user_id` in `config.toml`. Memory-based matching and "ask the user once" are recorded as later candidates, not chosen | yes |
| Tool discovery | Tags auto-match, plus a `tool_search` tool for the orchestrator, which may grant/revoke tool names for one delegation within a config allowlist | yes |
| Backends | Remote backends (Postgres, qdrant) are optional uv extras with local defaults | yes |
| qdrant-edge | Local vector default and base dependency, gated on a spike in `storage-config`; Postgres and remote qdrant stay optional extras | yes |
| Embeddings | Configured like other models (`models.yaml`, tag `embedding`), first model bge-m3 on local Ollama; in-process ONNX deferred | yes |

### B. Cross-cutting contracts (all planned)

**B1. Identity**

- New top-level key `user_id` (string, stable, same on every computer) in `config.toml`. Config keys are snake_case.
- `Environment` gains `user_id: str | None`. `None` when unset: the context then says identity is unknown, and memory tools refuse with a clear message.
- `user_name` and `computer_name` stay display facts from `getpass`/`socket`; they are never used as keys.
- Memory and any per-user data are keyed by `user_id`.

**B2. Storage interfaces**

Python Protocols in a new module, async to match the orchestrator. One `Storage` object bundles them and is the only way a tool touches data.

- `RelationalStore`: `execute(sql, params)`, `query(sql, params) -> rows`, `migrate(namespace, migrations)`.
  - SQL is a portable subset; the adapter converts the parameter style.
  - Each tool/feature owns tables under its own `<namespace>_` prefix.
  - Schema versions live in a `schema_versions(namespace, version)` table. `storage-config` migrates the existing `PRAGMA user_version` scheme of `store.py` to it without losing `llm_calls` and `model_backoff` data. No automatic copy of data between SQLite and Postgres is planned.
- `VectorStore`: `ensure_collection(name, dim, distance)`, `upsert(collection, points)`, `search(collection, vector, k, filter) -> hits`, `delete(collection, ids)`.
  - Points carry a payload with at least `user_id` for per-user filtering.
  - The store does not embed; a separate `Embedder` interface does (embed texts to vectors; it reports the model id and the dimension). The `Embedder` wraps the embedding model selected from `models.yaml` (an entry tagged `embedding`; first model: bge-m3 on local Ollama, entry `bge-m3-local`, provider `local`, model `bge-m3`, tags [embedding], price 0/0). Planned config additions: an optional `dimensions` field on an entry (passed to the provider only when set) and a rule that rejects `embedding` together with `chat`, `default` or `orchestrator`. The design, checked facts and the deferred in-process candidate are in the tech-stack bullet "Embeddings (planned, not built)".
  - The embedding model id and its dimension are recorded per collection; changing the model requires a re-index, and a mismatch is refused with a re-index message. There is no automatic fallback between embedding models: vectors from two models are not comparable.
- `FileStore`: `put(path, data)`, `get(path)`, `exists(path)`, `list(prefix)`, `delete(path)`.
  - Paths are relative to a per-namespace root and validated (no absolute path, no `..`).
  - Only `local` is planned first: root `~/.local/share/llm-harness/files`, directories mode 0700, files 0600 like the existing store.
  - Shared filesystem, garage and cloud object storage: UNDECIDED candidates.
- `ToolContext` is handed to every tool run: `storage`, `user_id`, the calling agent's id and tags. Tools never open connections or read config themselves.

**B3. Config**

| Table | Backend | Keys |
| --- | --- | --- |
| `[storage.relational]` | `sqlite` (default) | `path` |
| | `postgres` | `dsn_env` (name of an environment variable; no inline DSN) |
| `[storage.vector]` | `none` (default until the qdrant-edge spike passes), then `qdrant-edge` (base dependency) | `path` |
| | `qdrant` | `url`, `api_key_env` |
| `[storage.files]` | `local` (default) | `root` |
| embedding model (in `models.yaml`, not `config.toml`; no `[embeddings]` table) | an entry with the tag `embedding` and none of `chat`, `default`, `orchestrator`; uses a provider from `config.toml` | same fields as any model entry, plus optional `dimensions` |

- Secrets only by `*_env` reference or `Secret`, printed as `[REDACTED:...]`; the existing permission warnings apply.
- A postgres or qdrant backend whose extra is not installed fails at startup with one message naming the extra to install (`uv sync --extra postgres`).
- Absent `[storage]` = today's behavior exactly.

**B4. Tags**

- Agents and tools both carry `tags: list[str]` (lowercase, no spaces).
- Agents are declared in a new `agents.yaml` next to `models.yaml`: `id`, `name`, `description`, `examples` (intents, per the subagents design in tech-stack), `tags`, `model_tags`, optional `tools` (explicit names) and `deny` (names).
- The orchestrator is an agent with tag `orchestrator`.
- Matching rule: an agent receives every registered tool whose tags intersect the agent's tags, plus its explicit `tools`, minus `deny`. No model call is involved.
- Only the tool set an agent receives enters its context: names and one-line descriptions listed, full schemas for the tools it may call (existing context-control principle).

**B5. Tool search and context manipulation**

- `ToolRegistry.search(query, k, tags=None)`: optional tag filter, then lexical ranking over name, description and tags (in memory, no dependency). When a `VectorStore` and `Embedder` are configured, it is fused by reciprocal-rank fusion with vector ranking (collection `tools`, one point per tool, payload tags). Lexical only otherwise.
- Exposed to the orchestrator as the tool `tool_search(query, k)`, returning name, description, tags and schema token estimate.
- Manipulation: the `delegate` action gains an optional `tools: {grant: [...], revoke: [...]}`. Names must exist in the registry, and `grant` must also be in the allowlist `[tools] grantable` in `config.toml`. Invalid names are rejected back to the model as an observation (same retry path as an invalid reply).
- Grants valid for the current delegation are shown in the context inspector.
- The tagged-union action set (`respond`, `ask_user`, `call_tool`, `delegate`) is otherwise unchanged. A `call_tool` for a tool outside the caller's set is rejected as an observation.

**B6. Tool data access**

- Tools read and write only via `ToolContext.storage`.
- Per-user data always carries `user_id`.
- A tool declares the namespaces it uses so `storage-config` can create them.

### C. Per-item plans

#### `environment-and-identity` (first implementation slice, small)

- Scope: `user_id` config key; `Environment.user_id`; shown in state, context and inspectors.
- Depends on: nothing.
- Acceptance criteria:
  1. `config.toml` accepts a top-level `user_id`; an empty or whitespace value is rejected with a clear message; absent means `None`.
  2. `Environment.user_id` is filled by the orchestrator each turn and is protected from patching.
  3. The context shows the id, or says identity is unknown when `None`.
  4. The context inspector shows it in the TUI and GUI State tab; the plain mode has no state view, the id reaches its model request.
  5. Tests use a fake clock and an unset id.

#### `storage-config`

- Scope: interfaces, `[storage]` parsing and defaults, SQLite and local-file adapters, per-namespace migrations, `Storage`/`ToolContext` wiring; the qdrant-edge spike, adapter and base-dependency addition.
- Depends on: `environment-and-identity`.
- Acceptance criteria:
  1. `RelationalStore`, `VectorStore`, `FileStore`, `Storage` and `ToolContext` exist as async Protocols (`Embedder` belongs to `embeddings`).
  2. `[storage]` parses with the defaults in B3; absent `[storage]` behaves exactly as today.
  3. Per-namespace `schema_versions` replaces `PRAGMA user_version`; existing `llm_calls` and `model_backoff` rows survive (tested on a populated old-format database).
  4. `FileStore` local rejects absolute paths and `..`, and creates directories 0700 and files 0600.
  5. A test fixture keeps tests off the real data directory.
  6. A spike script with a hard timeout (seconds) checks that qdrant-edge is the intended package and that create-collection, upsert, search and filter by `user_id` work locally; its result is recorded in this spec.
  7. `pyproject.toml` lists the package only after the spike passed; until then the vector default stays `none`.
  8. The contract test for `VectorStore` passes on the qdrant-edge adapter in a temp dir.

#### `embeddings`

- Scope: the `Embedder` interface over embedding models declared in `models.yaml` with the tag `embedding` (first: bge-m3 on local Ollama), `embed` on the three provider adapters, the optional `dimensions` field, the tag validation and a selector. The in-process backend is deferred.
- Depends on: `storage-config`.
- Acceptance criteria:
  1. An `Embedder` interface exists, with a fake embedder used by all offline tests.
  2. `embed(texts)` exists on the Ollama, OpenAI-compatible and Vertex adapters, each with an offline test using a fake client and an opt-in live test that prints why it is skipped (live Ollama bge-m3 expected: 1024 dimensions).
  3. An optional `dimensions` field on a `models.yaml` entry (passed to the provider only when set), and a validation that rejects an entry tagged `embedding` together with `chat`, `default` or `orchestrator`, both with tests.
  4. `select_embedding_model(config, model_id)` picks by tag `embedding` and top priority; a tie at the top is an error.
  5. The collection records the model id and the dimension; a mismatch is refused with a re-index message. There is no fallback between embedding models; a failing one is reported as an error.
  6. Embedding calls are recorded in the usage ledger like chat calls (input tokens, cost from `price`). No schema change is needed: `llm_calls` (`src/llm_harness/store.py:25-37`) already has `model_id`, `provider_id`, nullable `input_tokens` and `output_tokens`, `cost_usd` and `outcome`; an embedding row leaves `output_tokens` NULL (checked 2026-10-28).

#### `agent-registry`

- Scope: `agents.yaml`, validation, inspector view.
- Depends on: `storage-config` is not required.
- Acceptance criteria:
  1. `agents.yaml` loads agents with the fields in B4.
  2. Duplicate ids and unknown tags are rejected with a message naming the agent.
  3. Exactly one agent with tag `orchestrator` is required.
  4. The inspector lists the agents and their tags.

#### `tool-registry`

- Scope: typed `Tool` (name, description, tags, input and output pydantic models, JSON schema, `run(arguments, ctx)`); registry with tag matching and `search`; `tool_search` tool; grant/revoke on `delegate`; allowlist; token-estimate measurement versus sending all tools (as in the notebook); inspector view.
- Depends on: `agent-registry`, `storage-config`; vector search uses `embeddings` when configured.
- Acceptance criteria:
  1. An agent receives tools by the matching rule in B4 (tags intersect, plus `tools`, minus `deny`), with no model call.
  2. `search(query, k, tags)` ranks lexically, and fuses with vector ranking when configured (tested with a fake `VectorStore` and `Embedder`).
  3. `tool_search` returns name, description, tags and schema token estimate.
  4. `delegate.tools` rejects unknown names and grants outside `[tools] grantable` as an observation; a `call_tool` outside the caller's set is rejected likewise.
  5. A measurement compares context tokens with the matched set versus all tools.
  6. The inspector shows the tool set and the grants of the current delegation.

#### `subagents`

- Scope: unchanged from the roadmap; now uses `agent-registry` and `tool-registry`.
- Depends on: `agent-registry`, `tool-registry`.
- Acceptance criteria: to be written in its own spec, using the registries above.

#### `tools-first-set`

- Scope: web search, RAG query, memory access, scrapers/API calls, using `Storage` only.
- Depends on: `tool-registry`, `storage-config`, `embeddings`.
- Acceptance criteria:
  1. Every tool registers with tags and typed input/output models.
  2. No tool opens a connection or reads config; all data goes through `ToolContext.storage`.
  3. Memory tools refuse with a clear message when `user_id` is `None`, and filter every query by `user_id`.
  4. Each tool declares its storage namespaces.

#### `storage-backends-remote`

- Scope: first a bounded spike per backend (hard timeout, seconds): `psycopg` and `qdrant-client`. Then a Postgres adapter and a remote qdrant adapter as optional extras.
- Depends on: `storage-config`.
- Acceptance criteria:
  1. The spike result per backend is recorded before any adapter is written.
  2. Each backend is an optional uv extra; the base install is unchanged.
  3. Contract tests shared by every backend run against each adapter.
  4. Tests against real services run only when available; otherwise skipped with a printed reason, never silently green.
  5. A missing extra fails at startup naming the extra to install.

### D. Risks and open questions

- OPEN: portability of one SQL subset across SQLite and Postgres.
- OPEN: re-index cost when the embedding model changes.
- OPEN: qdrant-edge package identity and API (resolved by the `storage-config` spike).
- OPEN: in-process bge-m3 backend (deferred): download size 2.2 GB, RAM and speed on CPU, ONNX output format.
- OPEN: `huggingface-hub` offline behaviour.
- OPEN: remote file backend choice (shared filesystem, garage, cloud object storage).
- OPEN: privacy across users sharing one remote store; `user_id` filtering is a convention, not access control.
- OPEN: how much of the tool set is described to the orchestrator before it searches.
- OPEN: same-user recognition beyond a config `user_id` (candidates: memory-based matching, ask once).

### E. Security notes

- DSN and API keys by env reference only.
- File paths validated against traversal.
- Model-supplied tool names and grants validated against the registry and the allowlist.
- Per-user filter on every query.
- Model download rules (apply to the deferred in-process backend, when it is built): huggingface.co only, a pinned revision, no pickle files (never load `pytorch_model.bin`), no remote code, and user consent (with the size) before any download.
- Remote stores are shared targets: tests must never use them unless the human names them (operations rule).

## Affected Areas

Specs only for this feature: `specs/roadmap.md`, `specs/tech-stack.md` (constitution, updated in the same step), and this file.

Future files, planned and owned by later items:

- `src/llm_harness/storage/` (interfaces and adapters)
- `src/llm_harness/config.py` (+ `user_id`, `[storage]`, `[tools]`)
- `src/llm_harness/state.py` and `src/llm_harness/orchestrator.py` (`Environment.user_id`)
- `src/llm_harness/tools.py` (typed tools, search)
- a new agents module and an `agents.yaml` example
- `src/llm_harness/store.py` (migrated to storage)
- `src/llm_harness/context.py` (tool sets)
- GUI and TUI inspectors
- the provider adapters in `src/llm_harness/providers/` (`embed`), `src/llm_harness/config.py` (`dimensions`, tag validation, `select_embedding_model`) and a planned `Embedder` module in `src/llm_harness/`; the models directory `~/.local/share/llm-harness/models/` only for the deferred in-process backend
- `pyproject.toml` (base dependency `qdrant-edge-py` after the spike; optional extras for Postgres and remote qdrant)

## Out of Scope

- No code, no dependency added.
- No Postgres, qdrant or garage setup.
- No choice of remote file backend.
- No memory-based identity matching.
- No change to the model reply contract beyond the optional `tools` field on `delegate` (which belongs to `tool-registry`).
- No A2UI.
- In-process embedding backend (deferred).

## Implementation Notes

Done in three steps on `main`; each step was checked with the full test run before the next.

**Step 1 - config** (`src/llm_harness/config.py`)

- `USER_ID_MAX_LENGTH = 128` (line 142) and `_check_user_id` (line 145): `None` stays `None`; otherwise a string, not blank, at most 128 characters, `isprintable()`. The value is stored exactly as written, with no stripping, because "ann " and "ann" would look like one user while their data differ.
- `HarnessConfig.user_id: str | None = None` (line 180) with a before-validator (line 182).
- `load_config` accepts a top-level `user_id` (line 246) and checks it directly (line 264), so a bad value is reported as `invalid config.toml: user_id ...`.
- `EXAMPLE_CONFIG_TOML` has commented example lines (lines 378-379).
- Tests: `tests/test_config.py`.

**Step 2 - state** (`src/llm_harness/state.py`, `src/llm_harness/orchestrator.py`, `src/llm_harness/prompts/orchestrator.md`)

- `Environment.user_id: str | None = None` at `state.py:42`. The default keeps state files exported before this change importable. `environment` stays protected from patching.
- `Orchestrator.__init__` takes a keyword-only `user_id` (`orchestrator.py:115`), stores it (`:129`) and passes it in `_environment()` (`:166`), so it is refreshed every turn.
- The model sees it because `build_request` puts `state.model_dump_json()` in the user message.
- `prompts/orchestrator.md` has a new `## Environment` section (line 24): `null` means identity unknown, never invent an id.
- Tests: `tests/test_state.py`, `tests/test_state_io.py`, `tests/test_context.py`, `tests/test_orchestrator.py` (fake clock; a restored state with another id is overwritten by the next turn).

**Step 3 - wiring and display**

- `Orchestrator(router, model, user_id=...)` at `src/llm_harness/cli.py:341`, `src/llm_harness/gui/app.py:215` and `src/llm_harness/tui/app.py:166` (the only three production call sites).
- `src/llm_harness/inspect_state.py:52` (`environment_text`) adds `user_id: <value>` or `user_id: (not set)`; the GUI and the TUI State tab share it.
- `load_config` raises `invalid config.toml: user_id found inside [providers.<id>]; put user_id at the top of the file, before any [providers...] header` (`config.py:252-256`), because TOML puts a key written after a table header into that table.
- Tests: new `tests/test_inspect_state.py`; `tests/test_config.py`; `tests/test_cli_orchestrator.py` (plain mode: the recorded request's user message contains `"user_id":"ann"`); `tests/test_gui_inspector.py` and `tests/test_tui_inspector.py` (the exact environment text now ends with the `user_id` line; one end-to-end check each); `tests/test_gui.py` (the `HostileStatusOrchestrator` test double accepts `user_id`).

**Deviations from the approved spec**

1. Acceptance criterion 4 said the inspector shows the id "in plain, TUI and GUI views". Plain mode has only the commands `/exit` and `/quit` and no state view (`_EXIT_COMMANDS` at `src/llm_harness/cli.py:49`), so there is nothing to show it in. The id does reach the model in plain mode (tested and run). Criterion 4 was reworded and `version` raised to 4.
2. Beyond the spec: the 128-character and printable rule, the `isprintable` check, and the error hint for a key placed after a table header.
3. Known limitation: `Environment.user_id` itself has no length or character rule (the rule lives in `HarnessConfig`). An imported state file can carry any string there; the next turn overwrites it and the inspectors sanitise it; the next-request preview before the first turn could show it.

## Validation

Commands (run from the project root, 2026-10-28):

- `uv run pytest -q -p no:cacheprovider`
- `uv run ruff check .`
- `uv run ruff format --check .`

Results:

| Point | Tests |
| --- | --- |
| Baseline before any change | 1351 passed, 4 deselected; ruff check and format check clean (86 files) |
| After step 1 | 1367 passed |
| After step 2 | 1375 passed |
| After step 3 (final) | 1385 passed, 4 deselected, 0 skipped, 0 failed, 1 warning |

- 34 new tests in total (1385 - 1351); no regression against the baseline.
- `uv run ruff check .`: "All checks passed!". `uv run ruff format --check .`: "87 files already formatted".
- The 1 warning is an aiohttp class-inheritance `DeprecationWarning` from `tests/test_vertex_adapter.py`; it existed before this change.
- The 4 deselected tests are the live tests (`tests/test_live_ollama.py`, `tests/test_live_openai_compat.py`, `tests/test_live_vertex.py`), deselected by default; they need a real provider. 0 skipped.
- Call-site evidence: a grep for `user_id` in `src/llm_harness/cli.py`, `gui/app.py` and `tui/app.py` finds `Orchestrator(router, model, user_id=...)` at `cli.py:341`, `gui/app.py:215` and `tui/app.py:166`, the only three production call sites.
- Real run: with a scratch HOME (`config.toml` with `user_id = "ann-2026"`, local Ollama model `gemma4:12b`), `printf 'What is my user_id, my user name and the name of this computer? Answer in one short line.\n/exit\n' | HOME=<scratch> .venv/bin/llm-harness --plain` exited 0 and answered "Your user_id is ann-2026, your name is jprusaczyk, and the computer name is HHFXHXF2VP." (27.4 s, local, $0.00). The model's own reasoning text quoted `"user_id": "ann-2026"` from the state. (Reported by the engineer; not re-run by the technical writer.)

Coverage gaps:

- The new tests were not run against the old code to prove they fail without the change.
- No live manual run of the GUI or the TUI; their display is covered by the pilot and inspector tests only.

## Documentation Review

Checked 2026-10-28 against the code.

- `README.md`: the `config.toml` section had no `user_id`; the three places that list the environment block's contents (State fields, State tab, import note) omitted the user id. Fixed under Documentation Updates once approved.
- `CHANGELOG.md`: proposed entry `{CHANGELOG.md, Added, "`user_id` key in `config.toml`, carried in the orchestrator state's environment block, shown to the model and in the State tab of the GUI and TUI inspectors."}`.
- Constitution, `specs/tech-stack.md`: drift found. `{file: specs/tech-stack.md, line: 81, current: "Planned, not built: a `user_id` key in `config.toml` (a stable string, the same on every computer, given in `config.toml`) and the `[storage]` table. ...", proposed: "Built: a top-level `user_id` key in `config.toml` (optional, a stable string, the same on every computer, at most 128 printable characters, kept as written). Planned, not built: the `[storage]` table. ..." (the rest of the line unchanged)}`. Needs human approval (constitution); not applied.
- Constitution, `specs/mission.md`: yes, it mentions the environment block. Line 21 says "a simple environment block with date and time, user name and computer name" and "What does not exist yet: ... recognising the same user on another computer"; line 25 says "recognising the same user on another computer under a different name" is still a goal. The block text is incomplete (no user id) and the "does not exist yet" claim is now partly stale (the id is stored and shown, but nothing uses it for memory yet). Not edited (constitution); a human decision.
CI review: not applicable (no CI, build or release configuration exists in this repository)

## Documentation Updates

Checked 2026-10-28.

- `README.md`: the `config.toml` section now documents `user_id`, and the three places that list the environment block's contents (State fields, State tab, import note) now include it.
- `CHANGELOG.md`: `[Unreleased]` -> Added, line 29 (the `user_id` key in `config.toml`).
- Constitution, changed in this step with human approval (2026-10-28): `specs/tech-stack.md` (now version 15, the `user_id` bullet says built and `[storage]` stays planned) and `specs/mission.md` (now version 7, the environment block, the "does not exist yet" sentence and the inspector statements). The text is not repeated here. `specs/roadmap.md`: the `## Now` line for this feature is removed (the section reads "Nothing in flight."); no version bump.
- `specs/memory.md`: two `[gotcha]` entries appended, both timestamped 2026-10-28T16:42:31Z.
- Closeout checks: validation recorded with counts (see Validation); docs and changelog updated; Now line removed; CI review line present (not applicable); call sites: `grep -n user_id src/llm_harness/cli.py src/llm_harness/gui/app.py src/llm_harness/tui/app.py src/llm_harness/inspect_state.py src/llm_harness/orchestrator.py` finds `user_id=config.user_id` at `cli.py:341`, `user_id=self._config.user_id` at `gui/app.py:215` and `tui/app.py:166`, and `inspect_state.py:52` displays it; `_check_user_id` is used by `HarnessConfig` (`config.py:182`) and `load_config` (`config.py:264`).
