---
title: "Model registry: fallback and usage tracking"
kind: feature
status: done
version: 1
updated: 2026-10-27
branch: none
---

## Problem / Motivation

Roadmap item `model-registry`: "Fallback to lower-priority models and token and price tracking".

Today one model answers every chat turn. If its host is down, every turn fails with an error, and nothing records what a call cost: the prices in `models.yaml` are read and validated but not used, and the usage line shows only token counts for one turn.

Why it matters: the author wants to pick models that are fast and cheap but work. That needs (1) a turn that survives a dead or rate-limited model by moving to the next suitable one, (2) no slow retry of a dead host on every turn, and (3) a record of what each model call cost.

Decisions by the author (2026-10-27), in plain words:

1. The tag `chat` marks the models that handle chat turns. The tag `default` marks the last-resort fallback "when nothing else matches".
2. The chain for a chat turn is: the chosen model (the user's pick, or the default model), then the other `chat`-tagged models by priority (highest first), then the `default`-tagged models by priority; duplicates removed; models in backoff skipped (a manual pick is exempt).
3. Error kinds `connection`, `rate_limit`, `not_found`, `unknown` and `auth` trigger fallback; `bad_request` does not. Fallback happens only before any output was shown; a failure after output started is shown as an error. A cancellation is never a failure.
4. A failing model goes into backoff: 1, 2, 3, 5, 10, 15, 30, 60 minutes (capped at 60) for consecutive failures, reset on success, persisted in SQLite so it survives restarts.
5. A manual pick (the UI selector or `--model`) that is in backoff is tried anyway: success resets the backoff, failure extends it and the chain continues. Automatic choices respect backoff.
6. A ledger in SQLite (stdlib `sqlite3`), file `~/.local/share/llm-harness/harness.db` (the author will reuse it for other data), schema versioned with `PRAGMA user_version`. Confirmed names: table `llm_calls` (`id`, `time`, `model_id`, `provider_id`, `input_tokens`, `output_tokens`, `reasoning_tokens`, `cost_usd`, `seconds`, `outcome` [`ok`|`error`|`cancelled`], `error_kind`, `fallback_from`) and table `model_backoff` (`model_id`, `failures`, `until`). No prompts, replies or keys are stored.
7. Cost = (input tokens x `input_per_mtok` + output tokens x `output_per_mtok`) / 1,000,000. Output tokens already include reasoning tokens. An omitted price is 0 (free or local). Unknown usage is stored as NULL and shown as `?`, never as 0. Every attempt is recorded, failed ones too.
8. All three front ends show a note when a fallback happens (for example `gemma-gpu unavailable (connection); using gemma-local`) and a usage line naming the model, tokens, cost and a session total (for example `[gemma-local tokens in=12 out=31 $0.00 2.1s | session $0.00]`). Plain mode keeps stdout clean: notes and usage go to stderr.

Standing rules that apply: no new config keys without the author confirming names (this design adds none); the database directory is 0700 and the file 0600; GUI layout stays full width with large text (only a text line is added); the assistant makes no git operations; thinking text is display-only.

Compatibility: `orchestrator-state` will pick models by tag through this registry and uses the fallback and the manual-pick-overrides-backoff rule as defined here. The chain function therefore takes the required tags as a parameter; chat turns pass `{"chat"}`.

## Acceptance Criteria

Chain and triggers:

1. For a chat turn the chain is the first model, then the other models tagged `chat` by priority (highest first), then the models tagged `default` by priority (highest first), with duplicates removed. Models with equal priority keep their `models.yaml` order. Checked by a unit test of the pure `fallback_chain`.
2. `fallback_chain` takes the required tags as a parameter: a model qualifies for the middle group only if it carries all required tags; chat turns pass `{"chat"}`. A test passes another tag set and gets that pool.
3. Models in backoff are skipped silently when automatic, except that a manual first model is always tried (criterion 12).
4. A terminal `ErrorEvent` with kind `connection`, `rate_limit`, `not_found`, `unknown` or `auth`, received before any thinking or text delta, makes the router try the next model of the chain. One test per kind.
5. A stream that ends without a terminal event counts as kind `unknown` and triggers fallback (if it ended before any output).
6. `bad_request` does not trigger fallback: the error is shown, the chain stops, the attempt is recorded, and the backoff state of that model is unchanged.
7. A failure after a thinking or text delta was already shown is passed through as an error, recorded, and not followed by a fallback attempt. If its kind is in the trigger set it counts toward backoff (assumption A1).
8. A cancellation is never a failure: the attempt is recorded with outcome `cancelled`, the backoff state is unchanged, no fallback is tried, and `CancelledError` propagates. A test cancels a turn and finds one `cancelled` row.
9. A programming exception raised by a provider is recorded as `error` with kind `unknown`, leaves backoff unchanged and is re-raised.
10. `ChatSession` is unchanged: a test through the router shows that history is rolled back after a failed turn and kept after a fallback that ends with `Done`.

Backoff:

11. The n-th consecutive failure of a model sets `until` to now plus the delay `1, 2, 3, 5, 10, 15, 30, 60` minutes for n = 1..8, and 60 minutes for n > 8. `until` is stored as UTC ISO-8601. Pure test of `backoff_delay` for n = 1..9.
12. A manual pick (selector or `--model`) that is in backoff is tried anyway; on success the row is deleted, on failure `failures` is incremented, `until` is extended and the chain continues.
13. A success deletes the model's `model_backoff` row (reset).
14. Backoff persists across restarts: with a database file written by one `Store` instance, a new instance in a new process-like setup still reports the model as backing off, and the next turn skips it.
15. A clock moved back is guarded: an entry whose `until` is more than 60 minutes after now is treated as expired. Pure test of `is_backing_off`.
16. If the first (automatic) model is skipped because of backoff, a `ModelSwitch` note with reason `backoff` is shown; other skipped candidates are skipped silently.
17. Two clients sharing the database do not double-count one failure: the failure counter increments only if there is no row, or `until <= attempt_started`, or the attempt was a manual pick. A test with two threads (and the same database file) proves it.
18. When every candidate failed or is in backoff there is no forced attempt, and the final error is `no model available: <id> <kind-or-backoff-Nm-or-config>; ...; last error: <redacted message>; pick a model to try it now`. The message goes through `redact()`.

Ledger and cost:

19. Every attempt writes one `llm_calls` row, including failed ones: `ok` rows carry tokens and cost, `error` rows carry `error_kind`, `cancelled` rows carry outcome only. `fallback_from` holds the id of the model that failed just before this attempt in the same turn (NULL for the first attempt).
20. `cost_usd` = (`input_tokens` x `input_per_mtok` + `output_tokens` x `output_per_mtok`) / 1,000,000, with `output_tokens` already including reasoning. An omitted price gives 0. If the usage is unknown (the provider reported no tokens) `input_tokens`, `output_tokens` and `cost_usd` are NULL and shown as `?`, never 0. Pure test of `compute_cost` with a price of 0 and with unknown usage.
21. The usage line names the model, tokens, cost, time and the session total, for example `[gemma-local tokens in=12 out=31 $0.00 2.1s | session $0.00]`. Cost format: `$0.00` for 0 or for values >= 0.01, otherwise up to 4 decimals. Unknown tokens show `tokens ?` and unknown cost `$?`. A session total that misses an unknown cost shows `session $0.12+?`.
22. The session total is the sum of the costs of the successful calls of this session; an `ok` call with unknown cost marks the total incomplete.
23. In `--plain`, the fallback note and the usage line go to stderr and stdout holds only the reply text; a test captures both streams.
24. The terminal UI and the GUI show the fallback note and the usage line as text lines in the chat log (`Static(markup=False)` and `ui.label` respectively); the GUI layout is unchanged (full width, large text).
25. The ledger stores no prompts, replies or keys: a test with a canary prompt, canary reply and canary key finds none of them in the database file bytes.

Database:

26. The database directory is created with mode 0700 and the file with mode 0600 (checked with `stat`). Constructing a `Store` does not touch the disk.
27. Schema versioning uses `PRAGMA user_version`; a fresh file gets the current version; the store module owns it; a migration is re-checked inside the write transaction.
28. If the database cannot be used (any `sqlite3.Error` or `OSError`, a file that is not a database, or a `user_version` newer than the store knows), the store is disabled for the process, exactly ONE `Notice` is shown (`usage database unavailable (<ExceptionType>): <path>; usage and backoff are not saved`), the chat keeps working, and the file is never deleted, renamed or migrated down. A test with a garbage-bytes file and one with a newer `user_version` checks the notice count and that the file bytes are unchanged.
29. All SQL uses parameterized queries; no value is concatenated into SQL text.

Structure:

30. No new config keys: `config.toml` and `models.yaml` accept exactly the keys they accept today. The data directory is a code keyword (`data_dir`) of `cli.main` for tests, not a config key.
31. No new dependency: `pyproject.toml` and `uv.lock` are unchanged; the store uses stdlib `sqlite3`.
32. Every new function and class has a non-test use in the application (no code that only tests call).
33. Tests never touch the real database: an autouse fixture sends the default database path to `tmp_path`.
34. Tests stay offline: scripted providers, a fake clock and timer, a real database in `tmp_path`, no sleeps.
35. A live test `tests/test_live_fallback.py` (marker `live`) uses a dead model (a closed local port, tag `default`, priority 200) and the local Ollama model (tags `chat`, `default`). It asserts the fallback note, the reply, one `ok` row and one `error` row with kind `connection` and `fallback_from` set, `model_backoff.failures == 1`, and that a second run shows the `backoff` note.

## Approach

Memory consulted: fallback|price|token|registry|backoff|sqlite|NiceGUI|Textual — 2 relevant entries: "google-genai uses aiohttp automatically when aiohttp is installed (nicegui depends on it), so network failures raise aiohttp exceptions, not httpx ones; adapters must catch both, and a test against a real closed port is needed because mocks hide it." (so the live test in step 6 uses a real closed port) and "Textual 8.2: a `str` Select option or Static content is parsed as console markup (`[/]` raises MarkupError); pass `rich.text.Text` or `Static(..., markup=False)`." (so notes and the usage line in the terminal UI use `markup=False`); 4 other matched lines (NiceGUI `ui.run` host, Ctrl-C, `ui.html` sanitize, Textual `Markdown` widget) are not relevant. No entry about fallback, backoff, SQLite, price or the registry exists.

STATUS: DRAFT, awaiting the author's approval. The parts marked PROPOSAL, the Assumptions and the Names to confirm need the author's answer first.

Seam (PROPOSAL): a new `ModelRouter` implements `ChatProvider`, so `ChatSession` stays unchanged: it already passes unknown events through (`session.py` lines 52-66) and rolls history back on an `ErrorEvent` (lines 61-65). The router fixes its first model when it is built. For each candidate it sends `req.model_copy(update={"model": m.model, "think": resolve_think(flag, m)})`, so every model gets its own provider model id and its own thinking setting. Unverified: that `model_copy(update=)` works on these frozen pydantic models (`ChatRequest` is frozen, `types.py` line 13).

Factories (PROPOSAL): the front ends receive `RouterFactory = Callable[[ModelConfig, bool], ChatProvider]` (model, is_manual) instead of `ProviderFactory` plus `think_flag`; `cli.main` builds it once. Today `ProviderFactory` is defined in `cli.py` (line 46), not in `providers/__init__.py`, and `resolve_think` is in `cli.py` (line 81); both move (`ProviderFactory` to `providers/__init__.py`, `resolve_think` to `config.py`) so the router does not import `cli`. The TUI and the GUI today import both from `cli` (`tui/app.py` line 23, `gui/app.py` line 9). Provider instances are cached per router by provider id. The first model's provider is built eagerly, which keeps today's `ConfigError` handling (`cli.py` lines 299-303). Fallback providers are built lazily; a build failure skips that candidate with reason `config`, is not recorded in the ledger and sets no backoff. `aclose()` closes all built providers and is idempotent.

Pure functions in `routing.py` (PROPOSAL):
- `fallback_chain(models, first, required_tags)`: `[first]` + models carrying all `required_tags` by priority descending + `default`-tagged models by priority descending, duplicates removed, stable order for ties.
- `is_backing_off(entry, now)`, with the clock-back guard (an `until` more than 60 minutes ahead is expired).
- `backoff_delay(failures)`: `LADDER[min(n, 8) - 1]` minutes with the ladder `(1, 2, 3, 5, 10, 15, 30, 60)`.
- `compute_cost(usage, price)`: `None` when tokens are unknown; price 0 gives 0.
- Chat turns pass the required tags `{"chat"}`; the orchestrator will pass its own later.

New events in `types.py` (PROPOSAL; frozen like the existing ones, adapters never emit them):
- `ModelSwitch(from_model_id, reason, to_model_id)` where `reason` is an `ErrorKind`, `"backoff"` or `"config"`.
- `UsageReport(model_id, usage | None, cost_usd | None, seconds, session_cost_usd, session_cost_incomplete)`, sent just before `Done`. The router swallows the adapter's `UsageEvent`, and the front ends stop timing (they no longer call `time.monotonic()` for the usage line).
- `Notice(text)` for the one-time "usage database unavailable" note.

Display helpers in `display.py` (PROPOSAL): `format_usage_report` and `format_switch`. They replace the use of `format_usage` (`display.py` line 28), whose output today is `[tokens in=.. out=.. 2.1s]`; whether `format_usage` is deleted or kept depends on whether anything else still uses it (criterion 32).

Store in `store.py` (PROPOSAL):
- Synchronous `Store(path)` that does not touch disk when constructed.
- One connection per call, opened inside `asyncio.to_thread` with `sqlite3.connect(path, timeout=5, isolation_level=None)`.
- Writes run in `BEGIN IMMEDIATE`; the backoff read and update are in the same transaction, which is safe across several GUI clients and across a terminal UI process and a GUI process. No WAL for now.
- A `_MIGRATIONS` tuple and `PRAGMA user_version`, re-checked inside the write transaction; the store module owns `user_version`.
- Any `sqlite3.Error` or `OSError`, or a newer `user_version`, disables the store for the process with ONE `Notice`. The file is never deleted, renamed or migrated down. The chat keeps working but without backoff: a dead remote host then costs its timeout on each turn (accepted).
- The directory is created 0700, the file is pre-created 0600, queries are parameterized only.
- The cancelled-attempt row is written with `asyncio.shield(asyncio.to_thread(...))`, then `CancelledError` is re-raised.

Backoff semantics (PROPOSAL):
- The n-th consecutive failure sets the delay `LADDER[min(n, 8) - 1]` minutes; `until` is stored as UTC ISO-8601; a success deletes the row.
- A failure is an `ErrorEvent` of kind `connection`, `rate_limit`, `not_found`, `unknown` or `auth` before any thinking or text delta; a stream ending without a terminal event counts as `unknown`.
- `bad_request` is shown, the chain stops, the attempt is recorded, backoff unchanged.
- A mid-stream failure is passed through, recorded, no fallback; it counts toward backoff if its kind is in the trigger set (A1).
- Cancellation: outcome `cancelled`, backoff unchanged, the error propagates. Programming exceptions: recorded as `error`/`unknown`, backoff unchanged, re-raised.
- A manual pick in backoff is tried anyway. Automatic candidates in backoff are skipped silently, but if the FIRST model itself is skipped a `ModelSwitch(reason="backoff")` note is shown.
- With two concurrent clients the counter increments only if there is no row, or `until <= attempt_started`, or the attempt was a manual pick.
- When everything failed or is in backoff there is no forced attempt; the final error is `no model available: gemma-gpu connection; gemma-local backoff 3m; gpt-mini config; last error: <redacted message>; pick a model to try it now` (the ids and kinds here are an example).

`cli.main` gets a keyword `data_dir` (code only, for tests, not a config key); the default database path is `~/.local/share/llm-harness/harness.db`.

Ordered plan (6 small steps, each runnable and separately verified; the risk each retires is in brackets):
1. Pure `routing.py` and its tests: chain order, duplicates, ties, ladder and cap, `is_backing_off` with the clock-back guard, `compute_cost` with `None` and price 0 [wrong chain or cost rules].
2. `store.py` with `tmp_path` database tests: creation, `user_version`, 0700/0600 via `stat`, fail/reset/keep, the two-thread guard, a garbage-bytes file and a newer `user_version` giving a disabled store, one notice, file bytes unchanged [data safety and permissions].
3. Events and `router.py` with a `ScriptedProvider`, a fake clock and timer and a real `tmp_path` store: each trigger kind, `bad_request`, a mid-stream error, cancellation writes a `cancelled` row, manual pick in backoff, all candidates in backoff, a fallback build failure, lazy builds, `aclose`, history rollback through the router [control flow].
4. Plain mode: move `ProviderFactory` and `resolve_think`, build the store and the router factory in `cli.main`, add the display formats; an autouse test fixture sends the default database path to `tmp_path` [wiring and test isolation].
5. TUI and GUI: `ModelSwitch` and `Notice` as notes, `UsageReport` at `Done`, update tests [front-end drift].
6. Live test `tests/test_live_fallback.py` as in criterion 35 [real failure path against a closed port and local Ollama].

Testing strategy: offline and deterministic, fake clock and timer, `tmp_path` database, scripted failing providers, no sleeps; real network only in step 6, against local Ollama.

Risks:
- What is stored: ids, counts, cost, times and error kinds only (no prompts, replies or keys).
- SQLite's journal-file permissions are unverified; the 0700 directory covers them.
- `llm_calls` grows without bound (retention not requested; about 100 bytes a row, an estimate).
- The disabled-store case loses backoff (accepted, see Store).
- Clock changes (guarded by the 60-minute rule).

Unverified:
- `sqlite3.connect(isolation_level=None, timeout=)` behaviour and the `BEGIN IMMEDIATE` locking across threads and processes.
- `PRAGMA user_version` read and write inside a transaction.
- pydantic `model_copy(update=)` on frozen models.
- `asyncio.shield` with `asyncio.to_thread` on cancellation (that the row is written before `CancelledError` is re-raised).
- That SQLite journal-file permissions are covered by the 0700 directory.
- That the three front ends can drop their own timing and `UsageEvent` handling without changing existing behaviour (they currently handle `UsageEvent`: `cli.py` line 200, `tui/app.py` line 235, `gui/app.py` line 308).

Assumptions:
- A1: a mid-stream failure (after output started) counts toward backoff if its kind is in the trigger set.
- `bad_request` leaves the backoff state unchanged (it is the request's fault, not the model's).
- A fallback provider build failure (reason `config`) is not recorded in the ledger and sets no backoff.
- Interpretation of the chain: the user's pick (or the default model) first, then the `chat` pool by priority, then the `default` models by priority; the author's wording was "chosen model + other `chat`-tagged models by priority + `default`-tagged models by priority".
- Cost display formats: `$0.00` for 0 or values >= 0.01, otherwise up to 4 decimals; unknown shown as `tokens ?` and `$?`; an incomplete session total as `session $0.12+?`.
- The session total is per application session (per chat session in the GUI is not decided; the default is one total per router).
- The usage line in the author's example (`[gemma-local tokens in=12 out=31 $0.00 2.1s | session $0.00]`) replaces the existing `[tokens ...]` line; `reasoning` tokens are shown only if reported (existing behaviour of `format_usage`).

Names to confirm with the human before implementation:
1. Modules `routing.py`, `store.py`, `router.py`.
2. Classes `ModelRouter` and `Store`.
3. Events `ModelSwitch`, `UsageReport`, `Notice`.
4. `ModelSwitch.reason` values `backoff` and `config` (the others are the existing `ErrorKind` values).
5. The cost format (see Assumptions).
6. The `data_dir` keyword of `cli.main`.

Constitution wording to update after approval (to be done through the approval-gated update path, not in this draft):
- `specs/tech-stack.md` line 39: "In `chat-streaming` the file is loaded and validated, the default is chosen by tag and priority, and the models appear in the UI selectors; prices are read but not yet used. Later, in `model-registry`: when the best matching model fails, the next lower-priority matching model is tried (fallback chain), and tokens and price are tracked per call." Replace with the implemented chain, the triggers, the backoff ladder and the ledger, and drop "prices are read but not yet used".
- `specs/tech-stack.md` line 64 (Layout): add `routing.py`, `store.py` and `router.py` to the core modules.
- `specs/tech-stack.md` Frameworks & libraries (lines 15-33) and line 77 ("read with stdlib `tomllib`"): add a mention of stdlib `sqlite3` for the usage ledger and backoff, database `~/.local/share/llm-harness/harness.db`, directory 0700, file 0600, with `PRAGMA user_version`; the Key conventions "Environment target" bullets (lines 88-91) should record the database path as a target for tests (tests use `tmp_path`).
- `specs/mission.md` line 11: "...the orchestrator, model fallback and context inspector are not built yet." and line 21: "What does not exist yet: fallback to the next model, tracking of tokens and price per call, the orchestrator, and the context inspector." Remove fallback and tracking from both. Line 23 ("If the best matching model fails...") then describes existing behaviour.

Next step: human confirmation of the assumptions and names, then approval (B1).

## Affected Areas

- New: `src/llm_harness/routing.py` (pure), `src/llm_harness/store.py` (SQLite), `src/llm_harness/router.py` (`ModelRouter`, `RouterFactory`, factory builder); tests `tests/test_routing.py`, `tests/test_store.py`, `tests/test_router.py`, `tests/test_live_fallback.py`.
- Changed: `src/llm_harness/cli.py` (build the store and router factory once, `data_dir` keyword, plain-mode notes and usage on stderr, `ProviderFactory` and `resolve_think` moved out), `src/llm_harness/config.py` (receives `resolve_think`), `src/llm_harness/providers/__init__.py` (receives `ProviderFactory`), `src/llm_harness/types.py` (`ModelSwitch`, `UsageReport`, `Notice`, in the `StreamEvent` union), `src/llm_harness/display.py` (`format_usage_report`, `format_switch`), `src/llm_harness/tui/app.py` and `src/llm_harness/gui/app.py` (take the router factory, show notes and the usage line), existing tests of the front ends and of `cli`/`display` (`tests/test_cli.py`, `tests/test_tui.py`, `tests/test_gui.py`, `tests/test_display.py`) and a conftest fixture for the database path.
- Unchanged: `src/llm_harness/session.py`, the three provider adapters, `models.yaml` and `config.toml` formats.
- Constitution at documentation time: `specs/mission.md` and `specs/tech-stack.md` (see Approach); README and CHANGELOG if present.

## Out of Scope

- Retention or pruning of `llm_calls`.
- SQLite WAL mode.
- A circuit-breaker setting or any backoff schedule other than the fixed ladder.
- Task tags beyond `chat` (`orchestrator-state` owns them).
- A UI or command to browse usage.
- Cost budgets or limits.
- Currencies other than USD.
- New config keys.
- Token counting when the provider reports none (the usage stays unknown and is shown as `?`).

## Implementation Notes

The approved Approach was implemented as proposed; the deviations and decisions the engineer recorded are listed under each stage for the final approval.

### Stage 1: pure routing, store, router, events (offline)

Files:
- New `src/llm_harness/routing.py` (pure: `fallback_chain`, `backoff_delay`, `is_backing_off`, `compute_cost`, `FALLBACK_KINDS`, `BACKOFF_LADDER_MINUTES`), `src/llm_harness/store.py` (`Store`, `CallRecord`, `SchemaTooNewError`, `default_db_path`) and `src/llm_harness/router.py` (`ModelRouter`, `RouterFactory`, `make_router_factory`).
- `src/llm_harness/types.py`: new events `ModelSwitch`, `UsageReport`, `Notice`, in the `StreamEvent` union.
- `resolve_think` moved to `src/llm_harness/config.py` and `ProviderFactory` to `src/llm_harness/providers/__init__.py`; both are still importable from `llm_harness.cli`. `assert_stream_contract` (tests) was extended.
- New tests `tests/test_routing.py`, `tests/test_store.py`, `tests/test_router.py`.

Behaviour as built:
- The chain is the first model, then `chat`-tagged models by priority, then `default`-tagged models by priority, duplicates removed (`routing.py`, `fallback_chain`).
- Triggers: `connection`, `rate_limit`, `not_found`, `unknown`, `auth` (`FALLBACK_KINDS`); `bad_request` is shown directly. Ladder 1, 2, 3, 5, 10, 15, 30, 60 minutes, capped at 60.
- Every attempt writes one `llm_calls` row; a success deletes the `model_backoff` row; the store uses stdlib `sqlite3` with `PRAGMA user_version`; the directory is 0700 and the file 0600.

Decisions and deviations (for the final approval):
- `_MIGRATIONS` is a tuple of statement tuples: `execute` runs one statement, and `executescript` would commit the open transaction.
- `until = attempt_started + seconds + delay` (the delay counts from the end of the attempt).
- The two-client guard compares parsed datetimes, not the stored text.
- `PRAGMA user_version = N` is built with an f-string from our own integer, because a PRAGMA takes no bound parameter; every other query is parameterized.
- `ModelSwitch` is emitted when the router arrives at a candidate that is not skipped. `fallback_from` is the last attempted model that failed; models skipped because of backoff or configuration do not count.
- `session_cost_incomplete` is set only when an `ok` call has unknown cost.
- A cancellation is also recorded when the consumer closes the stream early.
- The final `no model available` message lists each attempt, with the last error redacted.
- A `Notice` from a failed write is relayed at the start of the next turn.
- Beyond the approved Approach, the store also disables itself on `ValueError` and on a newer schema (`SchemaTooNewError`); the notice shows only the exception type.
- Unverified API facts from the Approach were run by the engineer: `sqlite3.connect(path, timeout=5, isolation_level=None)`, `BEGIN IMMEDIATE`, `PRAGMA user_version` (0 on a new file, readable and writable inside the transaction), `ChatRequest.model_copy(update=...)` on a frozen model, and `asyncio.shield(asyncio.to_thread(...))` finishing the write before `CancelledError` is re-raised. The upsert needs SQLite 3.24 or newer; the SQLite version of the project's interpreter was not printed.

### Stage 2: wiring and live check

- `src/llm_harness/display.py`: `format_usage_report` and `format_switch` added; `format_usage` removed (no other user).
- `src/llm_harness/cli.py`: `main` builds one `Store` and one router factory (`make_router_factory`) and has the code-only keyword `data_dir` (not a config key); the default ledger path is `~/.local/share/llm-harness/harness.db`, read through `store.default_db_path()`. `--plain` relays `ModelSwitch`, `Notice` and `UsageReport` on stderr; stdout stays reply text only; the usage line is held until `Done`.
- `src/llm_harness/tui/app.py` and `src/llm_harness/gui/app.py` take `(config, model, router_factory, is_manual)` and show the note line and the usage line. `--model` counts as a manual pick; a selector change makes the next router manual; New chat keeps the previous flag unless the selector changed. The front ends no longer time turns and no longer handle `UsageEvent` (the router swallows it).
- Usage line: `[<model> tokens in=<n> out=<n> reasoning=<n> $<cost> <s>s | session $<total>]`; `tokens ?` and `$?` when unknown; `+?` after the session total when incomplete; cost `$0.00` for 0 or >= 0.01, otherwise 4 decimals (under 0.00005 it shows `$0.0000`). A `UsageReport` is always shown, so a turn with unknown usage prints `[m tokens ? $? 0.0s | session $0.00+?]`.
- Switch notes: `<from> unavailable (<kind>); using <to>`, `<from> is in backoff; using <to>`, `<from> is not available (configuration); using <to>`.
- Visible behaviour change: with a single configured model, an `unknown`-kind error before any output now goes through the chain and ends as `no model available: <id> <kind>; ...; last error: <redacted message>; pick a model to try it now` instead of the raw provider message. Several TUI and GUI tests that asserted raw messages now use `bad_request`, which is shown directly.
- `tests/conftest.py` (new, autouse) sends `default_db_path` to a temporary path.
- New live test `tests/test_live_fallback.py` (criterion 35).

## Validation

Commands run at closeout (offline, scratch HOME, no model, no network):
- `uv run ruff check`: `All checks passed!`
- `uv run ruff format --check`: `61 files already formatted`
- `uv run pytest -q -rs`: `990 passed, 4 deselected, 1 warning in 29.47s`; 0 skipped (no skip lines under `-rs`). The warning is a `DeprecationWarning` from the google-genai package (`AiohttpClientSession`).
- The 4 deselected tests are the live tests (marker `live`, deselected by `addopts`): `tests/test_live_fallback.py::test_dead_model_falls_back_to_local_ollama_then_stays_in_backoff`, `tests/test_live_ollama.py::test_live_ollama_streams_text_and_usage`, `tests/test_live_openai_compat.py::test_live_openai_compat_streams_text_and_usage`, `tests/test_live_vertex.py::test_live_vertex_streams_text_and_usage`.
- `uv run llm-harness --help` lists exactly `--plain`, `--gui`, `--model ID`, `--think`, `--no-think`: no new flag.
- Reported by the engineer: stage 1 had 939 passed (825 earlier plus 114 new: 28 routing, 28 store, 49 router, others), stage 2 has 990 (51 more).

Live check (engineer, local Ollama, 2 tiny calls, `--no-think`): `pytest -m live -k fallback` 1 passed in 14.32 s. A dead model (closed local port, tags `default` and `chat`, priority 200) then `gemma-local`: the note, the reply, one `error`/`connection` row and one `ok` row with `fallback_from`, and `model_backoff.failures == 1`; a second run showed the `backoff` note and no new connection row.

Manual run (coordinator, real CLI, scratch HOME, `dead` and `gemma-local`): run 1 stdout `pong`, stderr `dead unavailable (connection); using gemma-local` then `[gemma-local tokens in=20 out=2 $0.00 0.4s | session $0.00]`; run 2 `dead is in backoff; using gemma-local` and the usage line; run 3 with `--model dead` (a manual pick in backoff) tried `dead` anyway (new connection error row, failure count 1 to 2) and fell back. Ledger rows correct; directory mode 0700, file mode 0600; the real `~/.local/share/llm-harness` was never created. The GUI and plain mode still start and Ctrl-C still exits 130.

Mutation checks (engineer; each reverted, diff empty):
- Stage 1: dropping `auth` from the trigger set failed 2 tests; ladder cap 30 instead of 60 failed 5; a manual pick respecting backoff failed 4; storing the error message in a row failed 11; skipping the cancelled-attempt row failed 3.
- Stage 1, NOT caught: replacing `asyncio.shield(write)` by `await write` failed no test. The thread write completes even if the await is cancelled again; the shield only matters if the executor queue is saturated. A test was added that cannot tell the difference. The shield stays, and no test protects it.
- Stage 2: `--model` not manual failed 3 tests; `ModelSwitch` dropped from the TUI failed 2; `UsageReport` dropped from the GUI failed 3; the conftest patch removed failed the guard test.

Not verified: the real browser or terminal appearance of the new lines; SQLite journal-file permissions (the 0700 directory covers them); behaviour across separate processes (only threads with separate `Store` instances were tested); the effect of the shield; the SQLite version of the project's interpreter. I did not run the live tests, the mutation checks or git.

## Documentation Review

Checked against the code: `specs/mission.md`, `specs/tech-stack.md`, `specs/roadmap.md`, `README.md`, `CHANGELOG.md`.

Drift found (all Medium: stale statements, nothing a command depends on):
- `specs/mission.md:11` says the model fallback is not built; `specs/mission.md:21` says fallback and token and price tracking do not exist yet; `specs/mission.md:23` describes the next-model fallback as a goal. Code: `src/llm_harness/router.py` (`ModelRouter.stream_chat`) and `src/llm_harness/store.py`.
- `specs/tech-stack.md:39` says prices are not yet used and fallback comes "later, in `model-registry`". Code: `src/llm_harness/routing.py` (`compute_cost`, `fallback_chain`).
- `specs/tech-stack.md:64` (Layout) lacks `routing.py`, `store.py`, `router.py`; the key conventions lack the database location and the test fixture (`tests/conftest.py`).
- `specs/roadmap.md:11` still lists this feature under `## Now`.
- `README.md:6` and `README.md:167` say model fallback does not exist; `README.md:79` says `price` is not used yet. Code: `src/llm_harness/router.py`, `src/llm_harness/routing.py` (`compute_cost`).
- `CHANGELOG.md:15` describes a "token and time usage line"; the line now also carries the model, cost and session total (`src/llm_harness/display.py`, `format_usage_report`).
- Section 3 of this spec still says `STATUS: DRAFT` and lists constitution wording to update; it is history of the approved Approach and was left unchanged (sections 1-5 are not edited, no version bump).

CI review: no CI, release or deploy files exist; nothing to update.
{pyproject.toml, needs change: no, reason: no dependency was added; the store uses the standard library `sqlite3`, and the `[project] dependencies` list contains no new entry}

## Documentation Updates

Changed (all edits made in this closeout):
- `specs/tech-stack.md` (version 8 to 9): the model registry paragraph now describes the chain, triggers, backoff, manual pick, ledger and usage line; the Layout line lists `routing.py`, `store.py`, `router.py`; Libraries and Key conventions record stdlib `sqlite3`, the database location and permissions, schema versioning owned by `store.py`, the autouse test fixture and `data_dir`.
- `specs/mission.md` (version 4 to 5): the sentences about fallback and tracking now say what exists; the orchestrator and the context inspector remain goals.
- `specs/roadmap.md` (version 4 to 5): the `## Now` line for this feature removed (`Nothing in flight.`).
- `README.md`: new section "Model fallback and usage tracking"; the intro, the `price` bullet and "Known limits" corrected.
- `CHANGELOG.md`: two new `### Added` lines; the usage-line line amended.
- `specs/memory.md`: 4 entries appended (a gotcha on PRAGMA and migrations, a gotcha on the shield around a thread write, a gotcha on the undetectable shield mutation, a build note on the test fixture). Their text is in `specs/memory.md` after the entry timestamped 2026-10-27T19:19:48Z.

Closeout checklist:
- [x] Validation counts: 990 passed, 4 deselected, 0 skipped; the 4 deselected are the live tests listed in Validation (deselected by design).
- [x] Ruff: check clean, format check clean (61 files).
- [x] README and CHANGELOG updated (see above).
- [x] The `## Now` line of this feature is removed from `specs/roadmap.md`.
- [x] CI and release review recorded in Documentation Review (no CI files exist).
- [x] Every new function, type and config key has a non-test use (grep over `src/`): `fallback_chain` and `is_backing_off` and `compute_cost` in `router.py`; `backoff_delay` in `store.py`; `format_usage_report` and `format_switch` in `cli.py`, `tui/app.py`, `gui/app.py`; `make_router_factory`, `default_db_path` and `Store(` in `cli.py`; `ModelRouter` through `make_router_factory`; `backoff_entries`, `record_attempt`, `pop_notice` in `router.py`; `SchemaTooNewError` in `store.py`; `ModelSwitch`, `UsageReport`, `Notice` are emitted by `router.py` and handled in the three front ends. No exception. No new config key exists (`data_dir` is a code keyword).
- [x] Spec validator passes (command and output are in the closeout report).
