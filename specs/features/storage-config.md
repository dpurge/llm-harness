---
title: "Storage config: relational, vector and file stores with local backends"
kind: feature
status: done
version: 2
updated: 2026-10-28
branch: main
---

## Problem / Motivation

Umbrella plan: `specs/features/environment-and-identity.md` (done). This is its item `storage-config`.

Today persistence is one SQLite file with a `PRAGMA user_version` scheme owned by `src/llm_harness/store.py` (tables `llm_calls` and `model_backoff`). It is sync, opens one connection per call, migrates on every connect, and `default_db_path()` is patched by `tests/conftest.py`. There is no file store, no vector store and no `[storage]` config. `Tool.run(arguments)` in `src/llm_harness/tools.py` gets no context.

Future tools and agents must read and write relational data, vectors and files only through interfaces whose backend is chosen in config. This feature builds the interfaces, the local backends (SQLite, local files, qdrant-edge) and the wiring. Postgres and remote qdrant are `storage-backends-remote`.

## Acceptance Criteria

1. `config.toml` accepts `[storage.relational]` (`backend = "sqlite"`, optional `path`), `[storage.vector]` (`backend = "qdrant-edge"` or `"none"`, optional `path`) and `[storage.files]` (`backend = "local"`, optional `root`).
   - Defaults when absent: sqlite at the data dir's `harness.db`; vector `qdrant-edge` at `<data dir>/vectors`; files at `<data dir>/files`. The data dir is `~/.local/share/llm-harness`. The two new paths are an ASSUMPTION.
   - Absent `[storage]` leaves chat and the ledger behaving exactly as today.
   - `~` is expanded. Nothing is created at import or startup, only on first use.
2. `postgres` and `qdrant` are recognised but rejected with a message naming the backend and `storage-backends-remote`. An unknown backend is rejected with the valid values listed; an unknown key is rejected as an extra input (the message does not list valid keys). Errors never echo secrets (there are none yet).
3. `RelationalStore` has `execute`, `query` and `migrate(namespace, migrations)`. Schema versions are kept per namespace in a `schema_versions(namespace, version)` table. The usage ledger and model backoff run on it as namespace `ledger`.
4. An existing `harness.db` with `PRAGMA user_version = 1` is migrated automatically, in ONE transaction, with NO backup copy (human's choice).
   - Every row is kept (test on a populated database built with the old SQL).
   - A failed migration leaves the file as it was.
   - A database from a newer version disables the store with the one existing notice (existing tests stay green).
5. Data directory mode 0700 and database/file modes 0600 are preserved. No test touches the real data directory: the `tests/conftest.py` fixture is extended to the new default paths, and production code calls path functions through their module.
6. `FileStore` (local) has `put`, `get`, `exists`, `list(prefix)`, `delete`.
   - Paths are relative to a per-namespace root and rejected when absolute, empty, containing `..`, or resolving through a symlink outside the root.
   - `put` is atomic (temp file, then `os.replace`).
   - Modes 0700 for directories and 0600 for files.
   - `list` returns sorted relative paths.
7. `VectorStore` has `ensure_collection(name, dim, distance)`, `upsert`, `search(vector, k, filter)`, `delete`, `close`.
   - Any string id is mapped to a UUID5 under one fixed namespace constant (the engine accepts only unsigned integers or UUIDs), so the same string always gives the same point.
   - A payload filter by `user_id` works. Filter values are strings or integers; a bool value is refused with a message, because in qdrant-edge-py 0.9.0 `MatchValue(True)` matches the integer 1 and never a stored bool (the Protocol's type still says `str | int | bool`, a known inconsistency).
   - `ensure_collection` on an existing collection with a different dimension is refused with a message.
   - One directory per collection.
8. A second process opening a collection that is already open gets `VectorStoreLockedError` with the single line "vector store is in use by another llm-harness process". The app continues without vector features (human's choice: hold open until exit, clear error). Tested with a subprocess under a 20 s timeout.
9. `qdrant-edge-py` is a base dependency in `pyproject.toml` with `uv.lock` updated. The contract test for `VectorStore` passes on the qdrant-edge adapter in a temporary directory. The spike result is recorded in this spec (see Approach A).
10. `Storage` bundles the three stores, is built once in `cli.py`, `gui/app.py` and `tui/app.py`, and is closed on exit. `ToolContext` (storage, user_id, calling agent id and tags) is built by the orchestrator and passed as `Tool.run(arguments, ctx)`. Existing tool tests and fakes are updated.
11. `llm-harness --storage-status` is read-only, needs no model and no terminal, and exits 0. It prints, per store, the backend and path. For the relational store it also prints whether the file exists, and the schema version per namespace against the version the code expects (or that the file is a legacy file to be migrated on the next start). For the vector store it prints, per existing collection, the dimensions, the distance and whether it is free or locked by another process. For the file store it prints the number of files. A store that does not exist yet is reported as not created yet and nothing is created.
12. README (config section), CHANGELOG, `specs/tech-stack.md` (built versus planned) and tests are updated. The full suite and ruff pass; every skip is explained.

## Approach

Memory consulted: sqlite|user_version|migrat, 0o600|O_NOFOLLOW|O_EXCL|permission, data_dir|default_db_path|conftest|fixture, storage|vector|qdrant — 3 relevant entries (PRAGMA user_version and executescript gotcha; os.open O_EXCL|O_NOFOLLOW 0o600 pattern; test fixtures must patch store.default_db_path through its module), none relevant for storage|vector|qdrant

Everything below is **planned** unless it says "spike" or "checked".

### A. Spike result (done)

Package `qdrant-edge-py` 0.9.0, checked 2026-10-28 in an isolated scratch environment under `timeout 60`; the run took 0.17 s. The script lives in the scratchpad; it is NOT kept in the repo (`tests/manual/qdrant_edge_spike.py` is not planned unless asked).

Worked:

- Create: `EdgeShard.create(path, EdgeConfig(vectors=EdgeVectorParams(size, Distance.Cosine)))`.
- Upsert: `shard.update(UpdateOperation.upsert_points([Point(id, vector, payload)]))`; `count` reports the points.
- Search: `SearchRequest(query=Query.Nearest(vector), limit, filter=Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=...))]), with_payload=True)` returns only the matching user's points.
- `flush` and `close`; reopen with `EdgeShard.load(path)` returns the same data.
- Delete: `UpdateOperation.delete_points([id])`.

Findings:

- Point ids must be unsigned ints or UUIDs. A string like `tool-name` raises `ValueError: failed to parse ... as UUID`; a UUID5 works.
- A second process opening an open shard fails at once with `failed to open WAL ... Can't init WAL: Kind(WouldBlock)`. It does not hang.
- One `EdgeShard` is one directory, so one collection per directory.
- Payload filtering works without a payload index at spike scale. `create_field_index` exists for scale and was not tested.

This confirms the package is the qdrant-edge the human meant.

### B. Decisions

| Question | Decision | By |
| --- | --- | --- |
| Number of specs | One spec with six ordered steps | recommended default |
| Vector default | `qdrant-edge`, now the spike passed | human, 2026-10-28 (earlier) |
| Concurrency | Hold the collection open until exit; clear error for a second process | human |
| Migration | Automatic, no backup copy (risk in D) | human |
| Data layout paths | `<data dir>/vectors` and `<data dir>/files` | assumption |
| `--storage-status` | yes | human 2026-10-28 |
| Euclid scores | The adapter negates them, so a higher score is nearer for every distance (the engine reports euclid as a distance) | deviation, step 4 |
| Collection metadata | `meta.json` next to `shard/` keeps dimension and distance, because `EdgeShard.info()` reports neither | deviation, step 4 |
| Migration of an existing ledger | Purely additive: adds `schema_versions` and the row `('ledger', 1)`; `llm_calls` and `model_backoff` are untouched; `PRAGMA user_version` is left alone; no backup. A fresh file gets `user_version` 0, so an older checkout would disable its ledger with the one notice | human accepted |
| Directory helper | `_make_dirs` in `files.py` is public as `make_private_dirs`, because the other storage modules use it | deviation, step 2 |

### C. Ordered plan

Each step ends green, is small and reviewable; the engineer does one at a time.

1. **Config.** `StorageConfig` models in `src/llm_harness/config.py`, defaults, validation, example config comment, `tests/test_config.py`. Visible check: a scratch config with `[storage.vector] backend = "postgres"` fails with the named message.
2. **`FileStore` local.** New package `src/llm_harness/storage/` (`files.py`); tests including traversal and symlink cases.
3. **`RelationalStore` SQLite and per-namespace migrations** (`relational.py`).
   - Move the ledger and backoff of `src/llm_harness/store.py` onto it, keeping its public behaviour and `default_db_path()`.
   - Migrate `user_version` 1; tests on a populated old-format file.
   - Check: run against a COPY of the real `~/.local/share/llm-harness/harness.db` in a scratch dir (read-only source); compare row counts (28 ledger rows today).
   - Verify here whether `asyncio.to_thread` suits the sync SQLite code (OPEN, see D).
4. **`VectorStore`.** Protocol, `VectorStoreLockedError` and the qdrant-edge adapter (`vectors.py`, `qdrant_edge.py`); UUID5 mapping, dimension check, hold-open; `pyproject.toml` and `uv.lock`; contract tests; subprocess lock test.
5. **`Storage` and `ToolContext`.** Build and close in cli, gui and tui. The orchestrator builds the context and calls `tool.run(arguments, ctx)` (call site `orchestrator.py` about line 314). Update the `tools.py` Protocol and the tests.
6. **`--storage-status`** (criterion 11), README, CHANGELOG, `specs/tech-stack.md`.

### D. Risks

- Automatic migration without a backup relies on SQLite transactional DDL (a rollback leaves the file unchanged). If a bug destroys rows, there is no copy.
- The `Tool.run` signature change touches every tool double in the tests.
- `qdrant-edge-py` is 0.9.0, 6 days old on 2026-10-28, and ships a native wheel of about 11 MB in the base install.
- One process per vector collection.
- OPEN: the sync SQLite code versus the async Protocols. The adapter would wrap blocking calls with `asyncio.to_thread`; to be verified in step 3.
- OPEN: payload index not tested at scale.

### E. Security

- Modes 0700 for directories and 0600 for files.
- Path validation in `FileStore`; a model-supplied path never reaches `FileStore` without validation.
- No secrets in this feature; no network.

## Affected Areas

Planned:

- `src/llm_harness/storage/` (new)
- `src/llm_harness/config.py`, `src/llm_harness/store.py`, `src/llm_harness/tools.py`, `src/llm_harness/orchestrator.py`
- `src/llm_harness/cli.py`, `src/llm_harness/gui/app.py`, `src/llm_harness/tui/app.py`
- `pyproject.toml`, `uv.lock`
- `tests/conftest.py` and new tests
- `README.md`, `CHANGELOG.md`, `specs/tech-stack.md`

## Out of Scope

- Postgres and remote qdrant (`storage-backends-remote`).
- The `Embedder` and any embedding (`embeddings`).
- Typed tools, tag matching and `tool_search` (`tool-registry`).
- Agents (`agent-registry`).
- Moving data between backends.
- Any remote file backend.
- Access control between users: filtering by `user_id` is a convention.

## Implementation Notes

All six steps are done on branch `main`; nothing is committed (human's instruction). Line numbers were checked by grep on 2026-10-28.

1. **Config** (`src/llm_harness/config.py`). `default_data_dir()` line 177; `RelationalConfig` 228, `VectorConfig` 243, `FilesConfig` 258, `StorageConfig` 273, each with `resolved_path(data_dir)` or `resolved_root(data_dir)`; `HarnessConfig.storage` 284; `_load_storage` 342, called at 386. `[storage]` is accepted at the top level of `config.toml`; paths must be absolute or start with `~`; `postgres` and `qdrant` are recognised and rejected with a message naming `storage-backends-remote` (line 203). Commented examples are in `EXAMPLE_CONFIG_TOML` (lines 519-529). `default_db_path()` in `src/llm_harness/store.py:77` now uses `default_data_dir()`. Tests: `tests/test_config.py`.
2. **FileStore** (`src/llm_harness/storage/files.py`). `FileStoreError` 28, `FileStore` Protocol 32, `make_private_dirs` 88 (renamed from `_make_dirs`, now public), `LocalFileStore` 107. `put` is atomic through `<root>/.tmp`; modes 0700 and 0600; symlink containment and path validation; namespaces match `^[a-z][a-z0-9_]{0,63}$`. Tests: `tests/test_storage_files.py` (53 new tests in the step).
3. **RelationalStore** (`src/llm_harness/storage/relational.py`). `SchemaTooNewError` 35, `ReadOnlySchema` 40, `read_schema_versions` 49 (read-only through a `?mode=ro` URI, line 55; added in step 6), async `RelationalStore` Protocol 68, `SqliteRelationalStore` 94 with `execute_sync`, `query_sync`, `connect_sync` and `migrate_sync`. Schema versions are kept per namespace in `schema_versions(namespace, version)`. `src/llm_harness/store.py`: the ledger runs on `SqliteRelationalStore` as namespace `ledger` (lines 54, 94), migrates once per process with `adopt_user_version=True` (line 168), and `expected_schema_versions()` is at line 57. Migrating an existing `user_version` 1 file only adds `schema_versions` and the row `('ledger', 1)`; `llm_calls`, `model_backoff` and `PRAGMA user_version` are untouched; there is no backup (human's choice). A fresh file gets `user_version` 0; an older checkout would disable its ledger with one notice (human accepted, left as is). Real-data check on a COPY of the real ledger: 28 rows before; the hash of the first 28 rows is identical after the migration; 29 rows after one chat call. The real `harness.db` was never opened read-write and is still in the old format (28 rows) until the user next runs the app. Tests: `tests/test_storage_relational.py` (31 in the step) and three existing tests in `tests/test_store.py` updated to the new table set.
4. **VectorStore** (`src/llm_harness/storage/vectors.py`, `src/llm_harness/storage/qdrant_edge.py`). `VectorStoreError` 17, `VectorStoreLockedError` 21 (the line "vector store is in use by another llm-harness process"), `VectorPoint` 29, `VectorHit` 36, `VectorStore` Protocol 42. `QdrantEdgeVectorStore` is at `qdrant_edge.py:101`; layout `<root>/<name>/shard/` and `<root>/<name>/meta.json`; ids are mapped with `uuid5` from the constant `_ID_NAMESPACE` (line 38) and the original id is kept in the payload key `_id` (line 39); the collection stays open until `close()`; one lock serialises the engine calls; the lock error is a bare `Exception` recognised by the text `WouldBlock` or `Can't init WAL` (line 43). `qdrant-edge-py>=0.9.0` is in `pyproject.toml:20` (base dependencies); `uv.lock` gained only that package (17 lines, nothing removed). Tests: `tests/test_storage_vectors.py` (46 in the step), including a real second-process lock test.
   - Deviation (a): bool filter values are refused (`_checked_filter`, `qdrant_edge.py:261-271`), because `MatchValue(True)` matches the integer 1 in 0.9.0; the Protocol's filter type (`vectors.py:58`) still says `str | int | bool`, a known inconsistency.
   - Deviation (b): euclid scores are negated (`qdrant_edge.py:319`), so a higher score is nearer for every distance.
   - Deviation (c): `meta.json` is used instead of `EdgeShard.info()`, which reports neither dimension nor distance.
5. **Storage and ToolContext**. `src/llm_harness/storage/__init__.py`: `DisabledVectorStore` 28, `Storage` 56 (async idempotent `close()`), `open_storage(config, data_dir, *, relational_path=None)` 83; it creates nothing and never opens the vector shard, so a locked vector store never blocks startup; backend `none` gives `DisabledVectorStore`, whose methods raise a message with the fix. `src/llm_harness/tools.py`: `ToolContext(storage, user_id, agent_id, tags)` line 14 and `Tool.run(arguments, ctx)` line 30. `src/llm_harness/orchestrator.py`: the `storage=` keyword and `ToolContext` built at the call site (line 317) with agent id "orchestrator" and tags `{"orchestrator"}`. `src/llm_harness/cli.py`: one `Storage` is built after the config loads (`open_storage(` line 405); `[storage.relational] path` now takes effect for the ledger through `_relational_path` (line 344); plain mode closes it in a `finally` (line 211, also on Ctrl-C); the TUI closes it in `on_unmount` (`tui/app.py:149-153`); the GUI registers `app.on_shutdown(storage.close)` (`gui/app.py:433`). `import qdrant_edge` costs about 2 ms. Tests: `tests/test_storage.py` (12) plus additions in `test_orchestrator.py`, `test_cli_orchestrator.py`, `test_tui.py`, `test_gui.py` and `test_cli.py`.
6. **`--storage-status`**. `src/llm_harness/cli.py:87` (in the mutually exclusive mode group; needs no model and no terminal) and the call at line 387; `src/llm_harness/storage/status.py` `storage_status` (line 26). It reads the ledger through a `file:...?mode=ro` URI and never through `SqliteRelationalStore`; it briefly opens and closes each EXISTING vector collection to report free or locked, and creates nothing. Tests: `tests/test_storage_status.py` (19) and 8 in `tests/test_cli.py`.

Known limitations (open, not verified):

- `EdgeShard` thread-safety is unverified (one lock serialises the calls).
- Recovery of a collection whose first create crashed before `meta.json` was written.
- A directory swapped for a symlink between the check and the use in `FileStore` is documented but untested.
- The payload index was not tested at scale; NaN vectors and huge batches were not tested.
- Behaviour on Linux and Windows was not run.
- A ledger with a hot journal opened read-only by `--storage-status` was not tested.
- A real NiceGUI shutdown and a real terminal TUI quit were not run; tests cover the registered handlers.
- Cross-process migration concurrency is tested with threads only.
- The `--storage-status` lock probe is not strictly side-effect free: it opens and closes each existing collection.

## Validation

Commands run on 2026-10-28 from the project root:

- `uv run pytest -q -p no:cacheprovider` -> `1592 passed, 4 deselected, 1 warning in 75.66s`; 0 skipped, 0 failed. The one warning is the existing aiohttp `ClientSession` DeprecationWarning from `google/genai/_api_client.py` (`AiohttpClientSession`), raised by `tests/test_vertex_adapter.py::test_real_sdk_connection_refused_is_one_connection_error`.
- `uv run ruff check .` -> `All checks passed!`
- `uv run ruff format --check .` -> `99 files already formatted`
- The 4 deselected tests are the `live` tests: `addopts = '-m "not live" ...'` in `pyproject.toml:39` and the marker `live` at line 41; they call real model providers and are run with `-m live`.

Pass counts per step (baseline after `environment-and-identity`: 1385 passed, 4 deselected):

| Step | Passed | New tests |
| --- | --- | --- |
| 1 config | 1412 | +27 |
| 2 FileStore | 1465 | +53 |
| 3 RelationalStore | 1497 | +32 |
| 4 VectorStore | 1543 | +46 |
| 5 Storage and ToolContext | 1565 | +22 |
| 6 `--storage-status` | 1592 | +27 |

Total: 207 new tests (1592 - 1385).

Real runs:

- Real ledger copy (step 3): 28 rows before; the hash of the first 28 rows identical after the migration; 29 rows after one chat call. The real `harness.db` was not opened read-write.
- `--storage-status` (step 6): an empty HOME gives "not created yet" for everything and creates nothing (re-run on 2026-10-28, exit code 0, no data directory appeared); a custom ledger gives `schema: ledger version 1 (the code expects 1)`; an old-format ledger copy gives `legacy file (user_version 1), it will be migrated on the next start` with an identical sha256; a collection held by a second process gives `collection tools: locked (in use by another llm-harness process)`, then `4 dimensions, cosine, free`. On the human's real home it printed the legacy line for `harness.db` and left its sha256 and mtime unchanged.
- Real chat (step 5): a scratch HOME with `[storage.relational] path = <scratch>/custom/ledger.db` and local gemma4:12b; the ledger row went to the custom file and no default `harness.db`, `vectors/` or `files/` directory was created.
- Config errors (re-run on 2026-10-28): `backend = "qdrant"` -> `'qdrant' is not available yet (planned feature storage-backends-remote); use one of: qdrant-edge, none`; unknown backend -> `unknown backend 'x'; use one of: qdrant-edge, none`; unknown key -> `Extra inputs are not permitted`; a relative `root` -> `must be absolute (or start with ~)`.

Call-site evidence (grep, 2026-10-28), every new public name is used outside its definition and its tests:

- `grep -n "open_storage(" src/llm_harness/cli.py` -> `405:    storage = open_storage(`
- `grep -n "ToolContext(" src/llm_harness/orchestrator.py` -> `317:        ctx = ToolContext(`
- `grep -n "storage_status(" src/llm_harness/cli.py` -> `387:            storage_status(`
- `grep -n "SqliteRelationalStore(" src/llm_harness/store.py` -> `94:        self._relational = SqliteRelationalStore(path)` (and `storage/__init__.py:96`)
- `grep -n "QdrantEdgeVectorStore(" src/llm_harness/storage/__init__.py` -> `94:        vectors = QdrantEdgeVectorStore(config.vector.resolved_path(data_dir))` (created by `open_storage`)
- `grep -n "LocalFileStore(" src/llm_harness/storage/__init__.py` -> `99:        files=LocalFileStore(config.files.resolved_root(data_dir)),` (created by `open_storage`)

Coverage gaps: see the known limitations in Implementation Notes (EdgeShard thread-safety, crash before `meta.json`, symlink swap between check and use, payload index at scale, NaN vectors and huge batches, Linux and Windows, hot-journal ledger read by `--storage-status`, real NiceGUI shutdown and real terminal TUI quit, cross-process migration concurrency, the not strictly side-effect-free lock probe). No coverage tool was run.

## Documentation Review

Checked on 2026-10-28 against the code as it is now.

README (`README.md`): out of date before the feature (the config section described only providers and `user_id`, no `--storage-status` flag, line 156-157 gave a fixed ledger path). Updated in the documentation phase, see Documentation Updates once approved. Items checked: `### Storage` after the `models.yaml` bullets, the ledger sentence at the `harness.db` paragraph, the `--storage-status` row in the Flags table, and the sample output, which was copied from a real run with an empty HOME. One claim was corrected while checking: an unknown key does not list valid keys (the message is `Extra inputs are not permitted`), unlike an unknown backend.

CHANGELOG (`CHANGELOG.md`): `## [Unreleased]` gets one line under `### Added` and one under `### Changed`; the headings exist already.

Constitution drift, needs human approval (constitution); not applied:

- `{file: specs/tech-stack.md, line: 89, current: "Persistence: one SQLite file, `~/.local/share/llm-harness/harness.db` (tables `llm_calls` and `model_backoff`), opened with stdlib `sqlite3` ... `store.py` owns the schema version (`PRAGMA user_version`) and its migrations", proposed: "Persistence: one SQLite file, by default `~/.local/share/llm-harness/harness.db` (path settable with `[storage.relational] path`), with tables `llm_calls`, `model_backoff` and `schema_versions`. It is reached through `SqliteRelationalStore` (`src/llm_harness/storage/relational.py`); `store.py` runs the ledger on it as namespace `ledger`, and versions are kept per namespace in `schema_versions`; `PRAGMA user_version` is only read to adopt an old file (`user_version` 1) and is no longer written. A file that cannot be used or has a newer version still disables the store for the process with one notice and is never deleted or migrated down.", status: "needs human approval (constitution); not applied"}`
- `{file: specs/tech-stack.md, line: 90, current: "Storage interfaces (planned, not built): SQLite stays the default ... The backend of each store is chosen in a `[storage]` table in `config.toml`.", proposed: "Storage interfaces (built, local backends only): `RelationalStore` (SQLite), `VectorStore` (`qdrant-edge`, or `none`) and `FileStore` (`local`), in `src/llm_harness/storage/`, bundled by `Storage` and built by `open_storage`. Still planned: Postgres and remote qdrant (`storage-backends-remote`); shared filesystem, garage and cloud object storage stay UNDECIDED candidates. Tools and agents read and write data only through these interfaces.", status: "needs human approval (constitution); not applied"}`
- `{file: specs/tech-stack.md, line: 56, current: "Storage packages: `qdrant-edge-py` 0.9.0 is a planned BASE dependency (not installed), gated on the `storage-config` spike ... `psycopg` 3.3.6 ... and `qdrant-client` 1.20.0 ... remain optional uv extras (candidates, not decided, not installed).", proposed: "Storage packages: `qdrant-edge-py` (`>=0.9.0`, 0.9.0 verified by the `storage-config` spike) is a built BASE dependency (about 11 MB native wheel; the spike confirmed the package and its API). `psycopg` and `qdrant-client` remain optional uv extras (candidates, not decided, not installed).", status: "needs human approval (constitution); not applied"}`
- `{file: specs/tech-stack.md, line: 81, current: "Planned, not built: the `[storage]` table.", proposed: "Built: the optional `[storage]` table with `[storage.relational]` (`backend`, `path`), `[storage.vector]` (`backend`, `path`) and `[storage.files]` (`backend`, `root`); paths are absolute or start with `~`; `postgres` and `qdrant` are recognised and rejected with a message naming `storage-backends-remote`.", status: "needs human approval (constitution); not applied"}`
- `{file: specs/tech-stack.md, line: 90 (new bullet after it), current: "(none)", proposed: "Storage conventions: schema versions are kept per namespace in `schema_versions(namespace, version)`; the ledger's expected version is `store.expected_schema_versions()`; ledger tables keep their unprefixed names (`llm_calls`, `model_backoff`). `ToolContext(storage, user_id, agent_id, tags)` is passed to `Tool.run(arguments, ctx)`. `open_storage` creates nothing and never opens the vector shard; the `Storage` is closed on exit in plain, TUI and GUI modes. A vector collection is held open by one process and a second one gets `VectorStoreLockedError`. The ledger path rule lives in `_relational_path` in `cli.py`.", status: "needs human approval (constitution); not applied"}`
- `{file: specs/mission.md, line: 21, current: "What exists today: ... and a reply contract of a state patch plus one action; the chat history is not sent to the model.", proposed: "add before the last clause: \"a local storage layer (an SQLite relational store, a file store and a qdrant-edge vector store, chosen in `[storage]` in `config.toml`; only the usage ledger uses it so far)\"", status: "needs human approval (constitution); not applied"}`
- `{file: specs/roadmap.md, line: 11, current: "- Storage config: `[storage]` config, FileStore, ... — branch `main` — specs/features/storage-config.md", proposed: "remove this `## Now` line at close (B5) and change nothing else; I do not know which wording the project wants for an empty `## Now` section", status: "needs human approval (constitution); not applied"}`

CI review: not applicable (no CI, build or release configuration exists in this repository)

## Documentation Updates

Updated on 2026-10-28.

- `README.md`: new `### Storage` section (line 98, inside `## Configuration`), the `--storage-status` row in the Flags table (line 318), and the ledger paragraph under `## Model fallback and usage tracking` (line 212), which no longer gives a fixed ledger path only.
- `CHANGELOG.md`, `## [Unreleased]`: one line under `### Added` (line 30, the `[storage]` table and `--storage-status`) and one under `### Changed` (line 39, the ledger path and the `schema_versions` table).
- Constitution (approved by the human on 2026-10-28, the proposals recorded in Documentation Review): `specs/tech-stack.md` now at version 16 (five proposals applied: persistence, storage interfaces, storage packages, the `[storage]` config clause, and a new storage conventions bullet) and `specs/mission.md` now at version 8 (the "What exists today" clause). `specs/roadmap.md`: the `## Now` line for this feature is removed (body "Nothing in flight."); no version bump for that.
- `specs/memory.md`: nine entries appended with timestamp 2026-10-28T17:48:16Z (five `[gotcha]`, one `[decision]`, three `[convention]`; the line before them is from 16:42:31Z).

Closeout checklist:

- Validation recorded with counts: yes (1592 passed, 4 deselected; ruff clean).
- README and CHANGELOG updated: yes.
- `## Now` line removed: yes.
- CI review line present: yes ("not applicable").
- New functions have non-test call sites: yes. `grep -n "open_storage(\|ToolContext(\|storage_status(" src/llm_harness/cli.py src/llm_harness/orchestrator.py` -> `src/llm_harness/orchestrator.py:317:        ctx = ToolContext(`, `src/llm_harness/cli.py:387:            storage_status(`, `src/llm_harness/cli.py:405:    storage = open_storage(`.
- Validator passes: yes (`node ~/.claude/skills/spec-validation/validate-specs.mjs`, run after each file).
