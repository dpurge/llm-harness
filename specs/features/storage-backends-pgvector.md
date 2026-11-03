---
title: "Storage backends: pgvector vector store"
kind: feature
status: done
version: 1
updated: 2026-11-03
branch: main
---

## Problem / Motivation

The vector backends today are `qdrant-edge` (local), `qdrant` (remote, `QdrantRemoteVectorStore` in `src/llm_harness/storage/qdrant_remote.py`) and `none` (`DisabledVectorStore`). A user who already runs PostgreSQL cannot keep vectors there. The roadmap item `storage-backends-pgvector` is: vectors in Postgres with the pgvector extension behind the `VectorStore` interface; it depends on `storage-backends-postgres` (done) and on a bounded spike (done 2026-11-03).

Contract to keep. The `VectorStore` protocol (`src/llm_harness/storage/vectors.py`) has the async methods `ensure_collection(name, dim, distance="cosine")`, `upsert(collection, points)`, `search(collection, vector, k, filter)`, `delete(collection, ids)` and `close()`. Value types are `VectorPoint(id, vector, payload)` and `VectorHit(id, score, payload)`. The score contract: higher is nearer for every distance (`cosine`, `dot`, `euclid`). Errors are `VectorStoreError`. Shared helpers live in `src/llm_harness/storage/vector_common.py`: `ID_NAMESPACE`, `ORIGINAL_ID_KEY`, `NAME_PATTERN` (`[a-z][a-z0-9_]{0,63}`, so names up to 64 characters), `check_name`, `point_id`, `checked_filter` (refuses bool filter values so every backend shares one contract).

Configuration today: `[storage.vector]` is `VectorConfig` in `src/llm_harness/config.py` with `backend` (`qdrant-edge` default, `qdrant`, `none`), `path` (local only) and `url` (qdrant only). `open_storage` in `src/llm_harness/storage/__init__.py` builds the vector store. `--storage-status` (`src/llm_harness/storage/status.py`) prints the vector lines; for qdrant it prints `vector: qdrant  <url>  (remote server, not contacted by --storage-status)`.

The only consumer of the vector store is the memory index (`src/llm_harness/memory_index.py`): one collection per embedding model, named `memories_` plus 16 hex characters; points are `VectorPoint(str(memory.id), vector, {"user_id": ...})`; search uses `filter={"user_id": user_id}`; delete is by id. Memory is REFUSED when the relational backend is Postgres (`[tools] memory = true` with `[storage.relational] backend = "postgres"` fails at config load; planned follow-up `storage-backends-postgres-memory`). So pgvector MUST work with SQLite as the relational store. That is why its connection settings are its own.

Reusable plumbing from the finished Postgres feature: `PostgresRelationalStore` (`src/llm_harness/storage/postgres.py`: sync psycopg 3, one connection per call, async via `asyncio.to_thread`, `?` translated to `%s` by `translate_sql`, `BEGIN IMMEDIATE` mapped to `BEGIN` plus an advisory lock, `migrate_sync(namespace, migrations)` with per-namespace versions in `schema_versions`, `connect_sync()`, `read_schema_versions_sync()` read-only, `label` = `host:port/dbname`, `PostgresConnectError`); the url rules `_check_postgres_url` in `src/llm_harness/config.py` (scheme postgresql or postgres, one host, one database, no password in the url, only `sslmode` and `connect_timeout` query parameters, port 1-65535); the optional extra `postgres` (`psycopg[binary]>=3.3.7`, also in the `dev` group); `_postgres_driver_installed`; the secrets file `<config dir>/secrets.toml` with `[postgres] password`, loaded by `load_secrets` into `HarnessConfig.postgres_password`; and the rule that error text names only exception types (psycopg error text names host, port, user and database, and constraint violations quote the failing row).

Human decisions (2026-11-03):

1. Connection: `[storage.vector] backend = "pgvector"` with its OWN `url` (same url rules as the relational postgres url; the vectors may live in another database or server than the ledger) and the password from the existing `[postgres] password` in `secrets.toml` (no new secret table).
2. The backend only CHECKS that the `vector` extension exists and never creates it. On first use, when it is missing, the program stops with a message that names the SQL line an administrator must run (`CREATE EXTENSION vector;`).
3. NO ANN index: exact search (same exact results as the other backends). The README says how to add an HNSW index by hand and what it changes. An index option becomes a later roadmap item.
4. `--storage-status` contacts the server read-only (connect timeout at most 5 s) and prints `vector: pgvector  <host:port/dbname>`, whether the extension is installed (and its version), the collections with dimension and distance, or one `cannot connect (<ExceptionType>)` line. It creates and writes nothing.

Assumptions confirmed by the human on 2026-11-03 (by approving this spec):

- No new extra and no new dependency in `pyproject.toml`.
- Offline tests with a recording fake, and live tests against `pgvector/pgvector:pg17` in a database whose name contains `live` or `test`.
- A hit whose cosine distance is NaN (a stored zero vector) is returned last, and NaN or infinite values in vectors are refused.

## Acceptance Criteria

1. Config accepts `[storage.vector] backend = "pgvector"` with a valid `url`. It rejects, each with a message that names the problem and never echoes the url: a missing `url`; `path` set with pgvector; a url problem (the same cases as the relational url: scheme, host, database name, port, several hosts, password in the url, disallowed query parameter, bad `sslmode`, bad `connect_timeout`, fragment, whitespace). `url` with `qdrant-edge` or `none` is still rejected. The unknown-backend message lists `qdrant-edge, qdrant, pgvector, none`. Covered by `tests/test_config_pgvector.py` (new) and the existing `tests/test_config.py`.
2. When psycopg is not installed, selecting `pgvector` fails with a message that contains `uv sync --extra postgres`. `pyproject.toml` gets no new extra and no new dependency (checked by a test that compares the extras and dependencies with the current ones).
3. Secrets: the file is read when the relational backend is postgres OR the vector backend is qdrant or pgvector; one read serves all. `HarnessConfig.postgres_password` is set when the relational backend is postgres OR the vector backend is pgvector; each secret is set only for its own backend. With `qdrant-edge`, `none` and sqlite the file is never opened (a garbage 0644 file causes no error). Covered by `tests/test_secrets_file.py` and `tests/test_config_pgvector.py`.
4. No secret is printed anywhere: tests assert that the password does not appear in `repr`, `str`, log output, exception messages or `--storage-status` output.
5. `PgVectorStore` implements the five protocol methods. Construction touches nothing, connects to nothing and does not import psycopg (a test checks `sys.modules`).
6. The shared vector contract passes: names, ids, validation order and messages, bool filter refusal, dimension and distance mismatch, sorted scores, original ids returned, delete, empty batches, close. It runs on the recording fake offline for what can be checked there, and on a real pgvector server under `-m live` through a new `pgvector` parameter of `store_factory` in `tests/test_storage_vectors.py` (the edge-only tests stay edge-only).
7. Scores per the spike for all three distances: cosine `1 - distance`, euclid `-distance`, dot `-distance`. A NaN or infinite value in a vector (stored or query) is refused client-side with a clear message. A hit whose cosine distance is NaN (a stored zero vector) is returned last; the rule is fixed in step 3 and tested.
8. Missing-extension messages, produced by the program and never the server's text: "not available on this server" (the server needs the pgvector package installed) and "available but not created" (it names the one SQL line `CREATE EXTENSION vector;`, to run as a superuser in the database named in `[storage.vector] url`). The program never runs `CREATE EXTENSION` (a test asserts that no statement recorded by the fake contains it).
9. The catalog and points schema are created by a versioned migration in the namespace `vector`. A second instance, concurrent `ensure_collection` calls and concurrent upserts leave one consistent state (live test).
10. A 64-character collection name works (single table). No identifier is built from user input: the statement text contains only fixed identifiers, and a test asserts that no collection name appears inside any SQL text, only as a parameter.
11. Errors reach callers as `VectorStoreError` naming the collection and the exception TYPE only; never the server's text, a url, host, user, password or a row. Tested with the fake raising errors that carry such text.
12. `open_storage` builds `PgVectorStore` for pgvector, creates nothing and connects to nothing at build time. A non-test call site exists (the production path through `open_storage` and `src/llm_harness/cli.py`).
13. `--storage-status` for pgvector per decision 4 (fake and live): the line `vector: pgvector  <host:port/dbname>`, the extension state and version, the collections with dimension and distance, or one `cannot connect (<ExceptionType>)` line; the connection is read-only and nothing is created or written. The SQLite, qdrant-edge and qdrant outputs are byte-identical to today (existing tests pin them).
14. The memory index works end to end on the real server with SQLite as the relational store: through a fake embedder, a memory saved and then searched by meaning returns the note, filtered by `user_id` (live test).
15. The full default suite (`-m "not live"`) passes offline. The live tests are deselected by default and pass against `pgvector/pgvector:pg17`.

## Approach

Memory consulted: grep -n -i -E "pgvector|vector extension|hnsw" specs/memory.md - 0 hits, none relevant

Ordered steps. Each step is shown to the human before the next one starts. Repo rules: tests never use string-target `monkeypatch.setattr("llm_harness....")`; the wiring test file is run first in ordered runs.

1. Bounded spike: DONE 2026-11-03. Setup: image `pgvector/pgvector:pg17` (PostgreSQL 17.11, pgvector 0.8.7), psycopg 3.3.7, a second container `postgres:17` for the missing-extension case; hard timeout 60 s, exit 0, containers removed, ports free. Script and output (session scratchpad, not in the repo): `/private/tmp/claude-501/-Users-jprusaczyk-Documents-Sources-DPurge-llm-harness/29f66909-a605-46a7-ac77-5833f8c6905c/scratchpad/pgvector_spike.py` and `.../pgvector_spike.out`. Findings:
   - Plain `postgres:17`: `pg_available_extensions` has no `vector`; `CREATE EXTENSION vector` fails with `FeatureNotSupported` (0A000), extension control file missing.
   - pgvector image before the extension is created: `pg_available_extensions` shows `('vector', '0.8.7', None)`; using the type fails with `UndefinedObject` (42704) `type "vector" does not exist`.
   - A non-superuser `CREATE EXTENSION IF NOT EXISTS vector` fails with `InsufficientPrivilege` (42501). After a superuser created it, the ordinary user can use the type. The installed version is read with `SELECT extversion FROM pg_extension WHERE extname='vector'`.
   - Vectors travel as text: `INSERT ... VALUES (%s, %s, %s::vector, %s::jsonb)` with `'[1.0,0.0,0.0]'` built from `repr(float(v))` and the payload from `json.dumps(...)`. The `pgvector` Python package is NOT needed, only psycopg.
   - Single-table design works: `vs_points (collection TEXT, id TEXT, embedding vector, payload JSONB, PRIMARY KEY (collection, id))` with an UNCONSTRAINED `vector` column held a 3-dimensional and a 4-dimensional collection side by side (`[('coll3', 3), ('coll4', 4)]`); `INSERT ... ON CONFLICT (collection, id) DO UPDATE` kept one row per id; a catalog table `(name TEXT PRIMARY KEY, dim INTEGER, distance TEXT)` was created fine.
   - Distances with a=[1,0,0], b=[0.9,0.1,0], c=[0,1,0], query [1,0,0]: cosine `<=>` gave 0.0, 0.006116, 1.0 (score `1 - d`: 1.0, 0.99388, 0.0, the same as qdrant); euclid `<->` gave 0.0, 0.14142, 1.41421 (score `-d`); dot `<#>` gave the NEGATIVE inner product -1.0, -0.9, -0.0 (score `-d`: 1.0, 0.9, 0.0).
   - Filter `payload @> %s::jsonb` with `{"user_id":"u1"}` matched a, b; an integer value matches integer rows only and the string `"5"` matched nothing (type-sensitive, like qdrant `MatchValue`); two keys combine as AND; a key nobody has returns no rows.
   - A query vector of the wrong dimension raises `DataException` (22000) "different vector dimensions 3 and 2" and does not touch other collections' rows; an empty collection search returns no rows; a zero vector with cosine gives `NaN` as the distance; a `NaN` inside an inserted or query vector is refused with `DataException` (22000) "NaN not allowed in vector".
   - `DELETE ... WHERE collection=%s AND id = ANY(%s)` with a Python list works (rowcount 0 for a missing id, 2 for two present ids).
   - An HNSW index (`USING hnsw (e vector_cosine_ops)`) works on `vector(1024)`, fails on `vector(2001)` with `ProgramLimitExceeded` (54000) "column cannot have more than 2000 dimensions for hnsw index"; a partial expression index `USING hnsw ((embedding::vector(3)) vector_cosine_ops) WHERE collection = 'coll3'` on the unconstrained column is accepted.
   - A 64-character table name was silently truncated to 63 characters, so one table per collection would break the 64-character collection names of the shared contract tests. That is why the single-table design is chosen.
   - A connection opened with `options="-c default_transaction_read_only=on"` reads the extension version and the catalog and refuses INSERT with `ReadOnlySqlTransaction` (25006).
   - 10 x connect+query+close took 0.082 s.
   - NOT tested: other pgvector or PostgreSQL versions, TLS, large data sizes and performance, halfvec/sparsevec, concurrent writers, float32 effects on score parity beyond the three test rows.
2. Config and secrets wiring. `VectorConfig` accepts `backend = "pgvector"` with `url` (required; the relational postgres url rules, reuse `_check_postgres_url`; `path` rejected; `url` stays rejected for `qdrant-edge` and `none`). The missing-driver check reuses `_postgres_driver_installed` and its message names `uv sync --extra postgres` (no new extra, no new dependency). The secrets file is read when the relational backend is postgres OR the vector backend is qdrant OR pgvector; `HarnessConfig.postgres_password` is assigned when the relational backend is postgres OR the vector backend is pgvector. The unknown-backend message for `[storage.vector]` lists `qdrant-edge, qdrant, pgvector, none`. `EXAMPLE_CONFIG_TOML` gets the commented pgvector lines. INTERIM GUARD, exactly like the Postgres feature's: until steps 3 and 4 build the store, `open_storage` for pgvector fails loudly (a `ConfigError` saying it is not wired yet, marked `# interim`) instead of falling back to the local edge store.
3. Adapter `PgVectorStore` in `src/llm_harness/storage/pgvector.py` behind the `VectorStore` protocol, built on a `PostgresRelationalStore(url, password)` for connections (composition: `connect_sync`, `migrate_sync`, `query_sync`, `execute_sync`; no second connection layer).
   - Schema (namespace `vector`, per-namespace versioned through `migrate_sync`): `vector_collections (name TEXT PRIMARY KEY, dim INTEGER NOT NULL, distance TEXT NOT NULL)` and `vector_points (collection TEXT NOT NULL, id TEXT NOT NULL, embedding vector NOT NULL, payload JSONB NOT NULL, PRIMARY KEY (collection, id))`.
   - First use: check the extension (`SELECT extversion FROM pg_extension WHERE extname='vector'`). When missing, distinguish "not available on this server" (`pg_available_extensions` has no row) from "available but not created", with OUR OWN messages (never the server's text); then migrate.
   - Contract: same validation order and messages as the other backends (`check_name`, dimension, distance, k, reserved `_id` payload key; wrong vector length checked client-side from the catalog dimension cached by `ensure_collection`; unknown collection gives "call ensure_collection first"; whole-batch-or-nothing upsert in one transaction; empty upsert and delete are no-ops; close is idempotent and later use is refused; search sorted score descending; hits carry the ORIGINAL id and the payload without `_id`); `checked_filter` bool refusal; scores per the spike (cosine `1 - d`, euclid `-d`, dot `-d`).
   - NaN rule: a NaN or infinite value in a vector is refused client-side with a clear message. A hit whose cosine distance is NaN (a stored zero vector) is returned last; the exact rule is stated in Implementation Notes when chosen and tested.
   - `ensure_collection` inserts into the catalog with `ON CONFLICT DO NOTHING` and compares the stored dimension and distance with the request (race-safe, same mismatch messages as the edge backend).
   - The original id is stored as given in the `id` column (no uuid mapping is needed).
   - Errors reach callers as `VectorStoreError` naming the collection and the exception TYPE only. The password is passed as a `password=` argument only. Construction connects to nothing. `close()` releases nothing heavy (one connection per call).
4. Wiring. `open_storage` builds `PgVectorStore(url, postgres_password)` for backend pgvector (the interim guard goes); `src/llm_harness/cli.py` passes the password as it does for Postgres. `--storage-status` for pgvector per decision 4: read-only connection, a `read_schema_versions_sync`-style helper for the extension and the catalog, status line shapes fixed in this step; SQLite and qdrant output stay byte-identical.
5. Tests.
   - Offline: a recording fake connection injected through a factory (statement text and parameters, translation, error mapping, no secret or row text in any message, lazy import, extension-missing messages for both cases, NaN handling, catalog race, config and secrets rules, the interim guard).
   - The shared vector contract tests (`tests/test_storage_vectors.py`, fixture `store_factory`) get a third parameter `pgvector` marked `live`, so the SAME contract runs against a real server.
   - New live tests (`LLM_HARNESS_LIVE_PGVECTOR_URL` plus the existing password variable; skipped with a printed reason unless the url answers and the database name contains `live` or `test`): the extension states (this one drops and recreates the extension in the scratch database, which is why the safety rule exists), the catalog, ids with a long collection name, the 64-character collection name, filters, scores per distance, concurrent upserts, status output.
   - The module docstring of the live test file gives the exact `docker --context rancher-desktop run ... pgvector/pgvector:pg17` commands.
6. Documentation (B4/B5).
   - README: the Storage section, and a step-by-step "pgvector" section for a blank-slate user with the exact docker command for `pgvector/pgvector:pg17`, the one-line admin step `CREATE EXTENSION vector;` run with `docker exec ... psql`, the config block, the shared password, the `--storage-status` lines, a table of every message with its fix, memory vector search with pgvector (it works with SQLite as the relational store), what changes when you switch (vectors are NOT moved; the SQLite markers say "indexed"; the roadmap item `memory-reindex-all` and the documented workaround apply), how to add an HNSW index by hand and its limits (2000 dimensions for the plain `vector` type; approximate results), security notes (all users' vectors of a collection live in one table and are separated only by a `user_id` filter: protect the server), and how to undo.
   - CHANGELOG, constitution proposals (tech-stack and mission) for approval, memory entries, CI review.
   - Every README command is run once exactly as written by an independent tester before delivery.

Risks:

- float32 storage in pgvector: scores differ slightly from float64 ground truth.
- A zero vector gives a NaN cosine distance.
- Large collections get slow without an index (exact scan).
- The `vector` column is unconstrained, so dimension checking is the program's job (catalog plus client-side check).
- The extension must be created by an administrator.
- One connection per call over a WAN is slow.
- Unverified (not tested in the spike): other pgvector or PostgreSQL versions, TLS, large data sizes and performance, halfvec/sparsevec, concurrent writers, float32 effects on score parity beyond the three test rows.

Testing strategy: offline, fast and repeatable by default; live tests are opt-in with `-m live`.

### Roadmap changes proposed with this spec

Approved by the human and applied on 2026-11-03 (roadmap version 24):

- (a) At implementation start, move `storage-backends-pgvector` from `## Later` to `## Now` (gated move).
- (b) Add `pgvector-index` to `## Later`: an optional ANN (HNSW) index per collection for the pgvector backend, with a configuration key, a recall comparison against exact search and the 2000-dimension limit; after a bounded spike.
- (c) Leave `memory-reindex-all`, `storage-backends-postgres-memory` and the rest as they are.

## Affected Areas

- `src/llm_harness/storage/vectors.py` (protocol, value types and errors; expected unchanged)
- `src/llm_harness/storage/vector_common.py` (shared helpers; reused)
- `src/llm_harness/storage/pgvector.py` (new)
- `src/llm_harness/storage/postgres.py` (reused for connections and migrations; a small read-only helper may be added)
- `src/llm_harness/storage/__init__.py` (`open_storage` wiring, interim guard)
- `src/llm_harness/storage/status.py` (`--storage-status` lines for pgvector)
- `src/llm_harness/config.py` (`VectorConfig`, backend list and message, secrets wiring, `EXAMPLE_CONFIG_TOML`)
- `src/llm_harness/cli.py` (password passed to `open_storage`)
- `tests/test_storage_vectors.py` (`pgvector` parameter of `store_factory`), new offline and live test files, `tests/test_config.py`, `tests/test_secrets_file.py`
- `README.md` and `CHANGELOG.md` (documented at the documentation phase)
- `specs/roadmap.md`, `specs/tech-stack.md`, `specs/mission.md` (proposals only, after approval)
- `pyproject.toml` and `uv.lock`: expected unchanged

## Out of Scope

- An ANN index (later item `pgvector-index`).
- halfvec and sparsevec.
- Deleting collections.
- Moving vectors between backends (and the stale-marker limit, item `memory-reindex-all`).
- Connection pooling.
- TLS options beyond `sslmode` in the url.
- Memory on Postgres (item `storage-backends-postgres-memory`).
- pgvector versions other than 0.8.x.
- PostgreSQL versions other than 17.

## Implementation Notes

### Step 1: spike (done 2026-11-03)

The facts are in Approach step 1. Setup:

- Images `pgvector/pgvector:pg17` (646 MB) and `postgres:17` (667 MB) were pulled earlier on `docker --context rancher-desktop`.
- Two containers on 127.0.0.1:15433 (pgvector) and 15434 (plain), each with a throwaway `POSTGRES_PASSWORD`.
- `psycopg[binary]` was run through `uv run --no-project --with "psycopg[binary]"` under `timeout 60`.
- The FIRST run failed because of a mistake in the spike script (`CREATE ROLE ... PASSWORD` cannot take a bind parameter). It was fixed and re-run: exit 0.
- The containers were removed afterwards (0 leftovers, ports free).
- Script and output are in the session scratchpad (`pgvector_spike.py`, `pgvector_spike.out`), not in the repo.

### Pre-change baseline

The coordinator's last full run before any change for this feature, 2026-11-03: `timeout 420 uv run pytest -q -rs` -> 3591 passed, 51 deselected, 1 warning (the pre-existing google-genai `AiohttpClientSession` DeprecationWarning), 0 failed, 0 errors, 0 skipped, 143.50 s, exit 0.

- It was taken after the last code change of the previous feature.
- The coordinator verified with `find src tests pyproject.toml uv.lock -newer <that run's output file>` that no source, test or dependency file has changed since. Only `README.md` was edited afterwards, for documentation, and `tests/test_command_tool.py` still passes (36).
- Branch `main`, with the uncommitted changes of earlier features already in the tree. The human chose to stay on `main` and to commit manually.

### Step 2: config and secrets wiring (done 2026-11-03)

Source of each fact: the engineer's report, except where marked "coordinator" (the full suite, ruff, greps of `config.py` and `pyproject.toml`, and the guard call sites).

Files:

- Changed: `src/llm_harness/config.py`, `src/llm_harness/storage/__init__.py`, `src/llm_harness/cli.py` (the guard call only), `tests/test_config.py` (two tests updated).
- New: `tests/test_config_pgvector.py` (56 tests).
- Not changed: `pyproject.toml`, `uv.lock`, README, CHANGELOG, `status.py`. Coordinator grep: no `pgvector` in `pyproject.toml`.

`config.py`:

- `_AVAILABLE_BACKENDS["vector"]` is `("qdrant-edge", "qdrant", "pgvector", "none")`. The unknown-backend message lists them in that order.
- `VectorConfig._check_url_rules` handles pgvector in this order: url missing, path set, url format (`_check_postgres_url`, reused unchanged), driver installed (`_postgres_driver_installed()`).
- Missing url: `backend "pgvector" requires url, for example url = "postgresql://user@db.example:5432/llm_harness"`.
- Path set: `path is only used with a local backend, not "pgvector"`. It is built from `self.backend` and is byte-identical for qdrant.
- Driver missing: `backend "pgvector" needs the optional extra postgres, which is not installed; run: uv sync --extra postgres`.
- `url` with `qdrant-edge` or `none` is still rejected, with the CHANGED text `url is only used with backend = "qdrant" or "pgvector"`.
- `load_config` reads the secrets file when relational is postgres OR the vector backend is qdrant or pgvector. It assigns `postgres_password` when relational is postgres OR vector is pgvector, and `qdrant_api_key` only for qdrant.
- The memory refusal rule is untouched: pgvector with SQLite and memory on is accepted; relational postgres with memory is still refused.
- `EXAMPLE_CONFIG_TOML` got the exact pgvector lines agreed for the README re-sync.

Interim guard (replaced in steps 3 and 4):

- `refuse_unwired_vector(config)` in `storage/__init__.py` raises `ConfigError` with `[storage.vector] backend = "pgvector" is not wired yet (steps 3 and 4 of storage-backends-pgvector)`. It is marked `# interim: replaced in steps 3 and 4`.
- It is called first in `open_storage` and in the `--storage-status` branch of `cli.py` (printed as `error: ...`, exit 1). In the normal start, the existing ConfigError path of `open_storage` prints it.
- Why: without it `open_storage` would silently build the local qdrant-edge store, and status would print local-store lines.
- Engineer's scratch-HOME runs (`.venv/bin/llm-harness`, config built from the `tests/test_cli.py` constants plus a pgvector url): `--plain < /dev/null` and `--storage-status` both printed the guard and exited 1. Afterwards only `.config` existed (no `.local`, no vectors directory).

Tests:

- Two existing tests were updated because of the two message changes: `tests/test_config.py::test_unknown_backend_lists_valid_values` (new list) and `tests/test_config.py::test_url_with_a_local_backend_is_rejected` (renamed from `test_url_with_a_non_qdrant_backend_is_rejected`; new text).
- `tests/test_config_postgres.py` and `tests/test_secrets_file.py` needed no change. The new file imports shared names from them (`BAD_URLS`, `FAKE_VALUES`, `PASSWORD`, `URL`, `pg_toml`; `KEY`, `VALID`, `write_secrets`), so those names are now shared between test files.
- A new test pins that `pyproject.toml` has exactly the extras `{"remote", "postgres"}` and no dependency naming `pgvector`.

Open items for later steps:

1. Step 4 must make the plain-http key warning in `cli.py` conditional on the qdrant backend. Today `if config.storage.vector.url is not None:` calls `insecure_key_warning(config.storage.vector.url, config.qdrant_api_key)` for ANY backend with a url. This is harmless now (the guard fires first and a pgvector config has no qdrant key) but wrong in principle.
2. Documentation step: the README row that quotes the old text `url is only used with backend = "qdrant"` (about `README.md` line 558, the qdrant section's table) must become `url is only used with backend = "qdrant" or "pgvector"`. The README Configuration example block must be re-synced byte for byte with `EXAMPLE_CONFIG_TOML`; the README copy is out of sync since this step.
3. The README text about the qdrant-only `url` key in the Storage section ("`url` is for `qdrant` only") needs the pgvector wording.

Verification (coordinator):

- Engineer's ordered runs (wiring file first): 1051 passed, 20 deselected; without it 1025 passed.
- Full default suite `timeout 420 uv run pytest -q -rs`: 3647 passed, 51 deselected, 1 warning (the pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 146.42 s, exit 0. Baseline was 3591.
- `ruff check src tests` clean; `ruff format --check src tests`: 151 files already formatted.

Unverified: nothing ran against a real Postgres or pgvector server (config rules only). No mutation check was run (time rule).

### Step 3: PgVectorStore adapter (done 2026-11-03)

Source of each fact: the engineer's report, except where marked "coordinator" (the full suite, ruff, a full read of `src/llm_harness/storage/pgvector.py`, greps). Mutation checks and the stand-in's behaviour are the engineer's report only.

Files:

- New: `src/llm_harness/storage/pgvector.py`.
- New: `tests/fake_pgvector.py`, a stand-in server for `connect_factory`. It implements exactly the module's fixed statements and dispatches by exact text on the module's own constants. The extension state is selectable: CREATED, AVAILABLE or MISSING. It offers `fail_when(sql, params)` and `refuse_connections`. Transactions work by snapshot at BEGIN, restored on ROLLBACK or on a close mid-transaction. An unknown statement raises `UnknownStatement` and fails the test. It reuses `CREATE_VERSIONS`, `LOCK`, `SELECT_VERSION` and `UPSERT`, imported from `tests/test_storage_postgres.py`, which was not changed.
- New: `tests/test_pgvector.py` (50 tests).
- Changed: `tests/test_storage_vectors.py`, only the fourth `store_factory` param `pgvector_fake`. 40 shared contract tests run on it and all pass unweakened. Edge-only tests stay edge-only. No shared test had to be skipped.

Adapter:

- `PgVectorStore(url, password=None, *, connect_factory=None)` owns a `PostgresRelationalStore` and uses only `connect_sync`, `migrate_sync`, `query_sync`, `execute_sync` and `label`. Construction connects to nothing and imports nothing. `repr` is `PgVectorStore(<label>, password=set|none)`.
- Schema: `MIGRATIONS` is one step of two statements in the namespace `vector`: `vector_collections (name TEXT PRIMARY KEY, dim INTEGER NOT NULL, distance TEXT NOT NULL)` and `vector_points (collection TEXT NOT NULL, id TEXT NOT NULL, embedding vector NOT NULL, payload JSONB NOT NULL, PRIMARY KEY (collection, id))`.
- `_prepare` runs once per instance under a lock: extension check first, then migrate. It is not marked done after a missing extension, so a retry works once an administrator creates it. Only `ensure_collection` calls `_prepare`; every other method needs a collection ensured in this process, so an unprepared instance never reaches the server.
- The two extension messages are the program's own text and name `CREATE EXTENSION vector;`. The program never runs it. Coordinator grep: those words appear only at `pgvector.py` lines 101 and 106, nowhere else in `src`.
- `SchemaTooNewError` becomes `the vector schema in this database is newer than this version of llm-harness`.
- `ensure_collection`: one connection, `INSERT ... ON CONFLICT (name) DO NOTHING`, then `SELECT dim, distance`, then compare (the edge backend's mismatch messages, copied). It short-circuits on the cached (dim, distance) without connecting.
- `upsert`: validates every point first (length, finite values, the reserved `_id` key, `json.dumps(..., allow_nan=False)`), then BEGIN, one `INSERT ... ON CONFLICT (collection, id) DO UPDATE` per point, COMMIT, all on one connection. On any error it runs ROLLBACK (suppressed if it fails).
- `search`: six fixed statement texts built once at import from the operator strings (`<=>`, `<->`, `<#>`; with and without the `AND payload @> ?::jsonb` filter), `ORDER BY 3 ASC LIMIT ?`. The payload is read as `payload::text` and `json.loads`ed by the program. SQL order is kept (no Python re-sort). Scores: cosine `1 - d`, euclid `-d`, dot `-d`. A NaN cosine distance scores -1.0 (`_NAN_COSINE_SCORE`; for dot and euclid a NaN would score -inf, defensive only).
- NaN or infinity is refused with `collection 'x': a vector may not hold NaN or infinity` (vector) and `collection 'x': a payload value may not be NaN or infinity` (payload float).
- `delete`: `DELETE FROM vector_points WHERE collection = ? AND id = ANY(?)` with the list as one parameter.
- Errors: `VectorStoreError` passes through; `RelationalStoreError` becomes `VectorStoreError(str(exc))`; anything else becomes `vector store failed on collection 'x': <ExceptionType>`; cancellation passes through.
- Every statement text is a module constant and every user value is a parameter. A test asserts that a 64-character collection name, hostile ids and payload keys never appear inside any recorded SQL text.
- Validation order: upsert length, finite, `_id`, payload JSON; search k, known collection, length, bool filter, finite.

Cleanup candidate: `_check_matches` is now a THIRD copy (edge, remote, pgvector). Moving it into `vector_common.py` was outside the allowed files for this step.

Mutation checks (engineer's report only; the module was restored and the engineer confirmed it with `diff` against a saved copy; the coordinator confirmed the module is intact by the full suite and by reading it): changing the NaN score constant failed 2 tests; removing ROLLBACK failed `test_a_failing_statement_rolls_the_whole_batch_back`.

Verification (coordinator):

- Engineer's ordered runs (wiring file first): 531 passed, 20 deselected; without it 505 passed.
- Full default suite `timeout 420 uv run pytest -q -rs`: 3737 passed, 51 deselected, 1 warning (the pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 141.05 s, exit 0. It was 3647 after step 2.
- `ruff check src tests` clean; `ruff format --check src tests`: 154 files already formatted.
- `PgVectorStore` has no non-test caller until step 4 (intentional; the interim guard still stops `open_storage`).

Unverified (all need a real pgvector server; step 5 checks them):

- That this exact module text runs on pgvector 0.8.x.
- The LIMIT passed as a PARAMETER (`LIMIT ?`; the spike used a literal `LIMIT 3`), `ORDER BY 3` with the unaliased distance expression, the `%s::vector` cast with the text parameter inside this module's statements, and `ANY(%s)` with a list through this module. The spike verified the pieces in separate statements.
- The stand-in's distance maths, NaN-last ordering, jsonb containment and transaction behaviour (encoded from the spike only).
- jsonb behaviours the spike did not test: `payload::text` spacing, float round trip of payload numbers, bool versus int in `@>`, NUL characters in payload strings.
- Concurrency: two instances racing on `ensure_collection`, two threads preparing at once. The offline tests run them only sequentially or with `asyncio.gather`.
- Float32 effects.
- The exception types of a real connection failure.

### Step 4: wiring and status (done 2026-11-03)

Source of each fact: the engineer's report, except where marked "coordinator" (the full suite, ruff, greps, the real CLI run, the one-word test fix, the sight of `cli.py` line 536). Mutation check and the other behaviour descriptions are the engineer's report only.

Files:

- `src/llm_harness/storage/postgres.py`: new public `connect_read_only_sync()` returning the connection wrapper, with `options="-c default_transaction_read_only=on"`, `connect_timeout=min(5, the store's value)`, the password only as `password=`, and a connect failure raising `PostgresConnectError`. New module function `is_undefined_table(exc)` = `getattr(exc, "sqlstate", None) == "42P01"`. `read_schema_versions_sync` now uses both with the same kwargs and results; `tests/test_status_postgres.py` passes unchanged.
- `src/llm_harness/storage/pgvector.py`: frozen `PgVectorStatus(extension_version, extension_available, collections)`; `PgVectorStore.read_status_sync()` sends only SELECTs (the two extension queries and the new constant `_SELECT_COLLECTIONS`: `SELECT name, dim, distance FROM vector_collections ORDER BY name`); a `label` property, now used by `__repr__`.
- `src/llm_harness/storage/status.py`: pgvector branch.
- `src/llm_harness/storage/__init__.py`: `open_storage` builds `PgVectorStore(url, postgres_password)`, creating and connecting to nothing; a missing url raises `VectorStoreError`, as for qdrant; `refuse_unwired_vector` and its comment are removed.
- `src/llm_harness/cli.py`: the guard import and both uses are removed; the plain-http key warning block now runs only for `backend == "qdrant"` and a url (line about 536).
- `src/llm_harness/store.py`: `optional_schema_versions()` also holds `vector` = `len(pgvector.MIGRATIONS)`. When the relational Postgres database and the pgvector database are the same, the relational `--storage-status` prints `schema: vector version 1 (the code expects 1)`.
- `tests/fake_pgvector.py`: answers `_SELECT_COLLECTIONS` sorted by name; raises SQLSTATE 42P01 when the catalog table is missing.
- New: `tests/test_status_pgvector.py` (20 tests).
- Changed: `tests/test_config_pgvector.py` (the interim-guard tests replaced).
- Changed: `tests/test_store_postgres.py`, ONE WORD, made by the coordinator: line 226 now asserts `set(optional_schema_versions()) == {"memory", "vector"}`, a required consequence of the schema-versions change. The engineer stopped at that failing test because the file was outside its allowed files.

Status output for pgvector (exact shapes, two-space indent):

- `vector: pgvector  <host:port/dbname>` (the store's `label`: no user, no password, no query).
- Then ONE of:
  - `  extension: vector <version>`
  - `  extension: not created in this database (an administrator runs: CREATE EXTENSION vector;)`
  - `  extension: not installed on this server (install the pgvector package, then run: CREATE EXTENSION vector;)`
- Then, only when the extension is present, one of:
  - `  collections: not set up yet (it is set up on first use)` (catalog table missing)
  - `  no collections yet`
  - one line per collection: `  collection <name>: <dim> dimensions, <distance>`
- On a connection failure exactly ONE line `  cannot connect (<ExceptionType>)`; the command still exits 0 and prints the files lines.
- Any other database error: `  not readable (<ExceptionType>)`. The server's own message is never printed.
- Only SELECT statements are sent (no BEGIN, no advisory lock, no CREATE, no INSERT).
- `PgVectorStatus.collections` is None both when the catalog table is missing and when the extension is absent (the catalog is not queried then).
- A hand-built pgvector config without a url raises ConfigError in `storage_status`.
- Output for sqlite, qdrant-edge, none and qdrant is unchanged (`tests/test_storage_status.py` and `tests/test_qdrant_wiring.py` pass unchanged).

Other assumptions about the vector backend (engineer's grep): none broken. The final `else` branches of `open_storage` and `status.py` are no longer reachable for pgvector; `_VECTORS_DISABLED` mentions `qdrant-edge` but applies only to the `none` backend; `memory_tools.py` and `builtin_tools.py` are backend-neutral.

Interim guard: gone (coordinator grep: nothing for `refuse_unwired_vector`, `not wired` or `interim` in src or tests).

Real CLI run (coordinator): scratch HOME, `.venv/bin/llm-harness`, config built from the `tests/test_cli.py` constants plus `[storage.vector] backend = "pgvector"` and `url = "postgresql://scratchuser@127.0.0.1:1/scratch_test"`, nothing listening on port 1.

- `--plain < /dev/null`: exit 0, empty stdout and stderr, only the three config files created (nothing connects at startup).
- `--storage-status`: exit 0, printing `relational: sqlite  <home>/.local/share/llm-harness/harness.db`, its two SQLite lines, `vector: pgvector  127.0.0.1:1/scratch_test`, `  cannot connect (OperationalError)`, and the files lines. Stderr empty; nothing created beyond the config files.

Mutation check (engineer's report only; restored and confirmed by the engineer; the coordinator saw the condition at `cli.py` line 536): with the old key-warning condition `url is not None` restored, `test_a_pgvector_start_never_asks_for_the_key_warning` failed.

Verification (coordinator):

- The engineer's ordered runs (wiring file first) showed exactly one failure, the stale assertion above. After the one-word fix, the full default suite `timeout 420 uv run pytest -q -rs`: 3763 passed, 51 deselected, 1 warning (the pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 141.81 s, exit 0. It was 3737 after step 3.
- `ruff check src tests` clean; `ruff format --check src tests`: 155 files already formatted.

Unverified (all need a real server; step 5 checks them):

- That the read-only session really refuses writes through this path.
- That the extension and catalog SELECTs run on pgvector 0.8.x through this exact path.
- A real SQLSTATE 42P01 on `vector_collections` (tests use the stand-in's `FakeDatabaseError` and, for the relational path, psycopg's own `UndefinedTable`).
- The real text and type of a closed-port or wrong-password failure, beyond the one observed `OperationalError`.
- Everything already listed as unverified under Step 3.

### Step 5: first real-server checks (done 2026-11-03)

Source of each fact: the engineer's report, plus these coordinator checks: an independent live run of everything on fresh containers, the full suite, ruff, and a read of the fix (`pgvector.py` lines 225-228). Everything else below (the stand-in table, the findings, the per-test behaviour, the two engineer runs) is the engineer's report only. No acceptance criterion is marked as met here; that happens in `## Validation`.

Servers: this step ran against real servers: `pgvector/pgvector:pg17` (PostgreSQL 17.11, pgvector 0.8.7) on 127.0.0.1:15433, and plain `postgres:17` on 127.0.0.1:15434 (only for the "not installed" tests). Both were started with `docker --context rancher-desktop run -d --rm`, a throwaway password and the database `llm_harness_live`, and removed afterwards. The engineer was allowed to run Docker in this step under strict bounds (explicit context, port checks, trap cleanup, timeouts) and did; the coordinator re-ran everything independently.

Files:

- `tests/live_postgres.py` (extended): `PGVECTOR_URL_VARIABLE` = `LLM_HARNESS_LIVE_PGVECTOR_URL`, `PGVECTOR_PLAIN_URL_VARIABLE` = `LLM_HARNESS_LIVE_PGVECTOR_PLAIN_URL`, `pgvector_target()`, `pgvector_plain_target()`, and `reset_vector_schema(url, password)`. The reset runs `CREATE EXTENSION IF NOT EXISTS vector` through a raw administrator connection as TEST SETUP ONLY (a fresh pgvector image has no extension created; the program never does it), then drops `vector_points` and `vector_collections` and deletes the `vector` row of `schema_versions`. `live_target` and `scratch_target` got an optional variable-name argument. The safety rule (the database name must contain `live` or `test`) applies.
- `tests/test_storage_vectors.py`: `store_factory` is now a generator fixture with a live param `pgvector` that resets before and after each test; the 40 shared contract tests run on the real server unchanged.
- NEW `tests/test_live_pgvector.py`: 46 tests, `pytestmark = pytest.mark.live`, `async def` under `asyncio.timeout(30)`, threaded tests with `ThreadPoolExecutor` and a `Barrier`. The module docstring gives the docker, pytest and `rm -f` commands.
- `tests/fake_pgvector.py`: docstring only (three differences found against the real server); no fake behaviour changed.
- `src/llm_harness/storage/pgvector.py`: ONE bug fix (below). `status.py` was not touched.

The bug found by the real server:

- Two writers upserting the same ids listed in OPPOSITE order deadlocked. `test_two_instances_upserting_the_same_ids_in_opposite_order_never_deadlock` failed with `VectorStoreError: vector store failed on collection 'tools': DeadlockDetected`. The engineer saw `DeadlockDetected` in 15 of 15 probe rounds before the fix; the server aborted one writer and its batch rolled back whole.
- Fix: in `PgVectorStore.upsert`, after the empty-batch check, `rows.sort(key=lambda row: row[1])` (id order, so rows are always locked in the same order). The sort is stable, so for an id repeated inside one batch the last point still wins (`test_a_batch_with_a_repeated_id_keeps_the_last_payload_in_every_order`). The coordinator read the fix (`pgvector.py` lines 225-228).
- The offline stand-in could not have found this: it has no row locks.

Results (coordinator, independent run on fresh containers, 2026-11-03):

- Command: `LLM_HARNESS_LIVE_PGVECTOR_URL=postgresql://postgres@127.0.0.1:15433/llm_harness_live LLM_HARNESS_LIVE_PGVECTOR_PLAIN_URL=postgresql://postgres@127.0.0.1:15434/llm_harness_live LLM_HARNESS_LIVE_POSTGRES_PASSWORD=<throwaway> timeout 180 uv run pytest -m live tests/test_live_pgvector.py tests/test_storage_vectors.py -v -rs -p no:cacheprovider`
- Outcome: 86 passed, 126 deselected in 11.09 s, 0 failed, 0 skipped, exit 0 (46 in `test_live_pgvector.py` and the 40 shared contract tests on the real server).
- The engineer's two runs on fresh containers gave 86 passed in 11.69 s and 11.43 s (engineer's report only).
- Afterwards the live database held only the shared table `schema_versions` and the extension `vector 0.8.7` (restored by the extension tests); containers removed (0 left), ports 15433 and 15434 free.

Stand-in assumptions checked against the real server (engineer's report only):

| Assumption | Result on the real server |
| --- | --- |
| Distance values and score signs | Held: cosine 1.0 / 0.99388 / 0.0, euclid -0.0 / -0.14142 / -1.41421, dot 1.0 / 0.9 / 0.0; tolerance 1e-6 because pgvector stores float32 |
| NaN-last and the zero-vector rule | Held: a stored zero vector with cosine comes back last with score -1.0; with k=3 the NaN hit is cut |
| Zero QUERY vector | Every distance is NaN, every score -1.0, no crash; the order is the insertion/scan order and repeatable (asserted only as repeatable) |
| jsonb containment filter | Type-sensitive: `{"n":1}` matches the stored int only, not the stored `true` or the string "1"; the program still refuses bool filters |
| Bound `LIMIT ?` and `ORDER BY 3` on the unaliased distance expression | Correct for all three distances (k = 1, 2, 3, 50) |
| `?::vector` text cast | Works: 1e-07 stored, 1e-50 stored; 1e39 refused by the server with `NumericValueOutOfRange`, surfaced as the exception type only (float32 range) |
| `ANY(?)` with a Python list | Works for one id, several, a missing id and 12 hostile ids |
| Transactions | A batch of 3 with a NUL in the third leaves no row of the batch; an existing row that the batch had updated first keeps its old payload |
| Read-only status connection | Refuses writes (SQLSTATE 25006); a real `UndefinedTable` has SQLSTATE 42P01 (`is_undefined_table` true; a syntax error is not recognised as a missing table) |
| Status states | Verified: version read from the server and matching `0.8.x`; tables absent `collections: not set up yet`; empty catalog `no collections yet`; two collections sorted by name; extension available but not created; not installed on the plain server; status creates nothing |
| Status connection failures | A wrong password and a closed port each print exactly one `  cannot connect (OperationalError)` line, the closed port within a few seconds, no password text |
| Store connection failures | A wrong password or a closed port gives `vector store failed on collection 'tools': OperationalError` with no host, user or password |
| Extension states | After `DROP EXTENSION vector CASCADE` the message is exactly the program's `_NOT_CREATED` text; on the plain server exactly `_NOT_INSTALLED`; `pg_extension` and the table set are unchanged after the failed attempt (the program never runs `CREATE EXTENSION`); a role with `rolsuper = false` can use the store once the extension exists |
| Concurrent preparation | Four threads or two gathered instances preparing at once end with one schema, `vector` version 1, no error |
| Concurrent `ensure_collection` | Eight calls leave one catalog row; with different dimensions: one winner and the five others get the exact mismatch message (never a raw database error) |
| Concurrent writers and readers | Two instances upserting the same 50 ids concurrently leave exactly 50 rows; three writers with three readers leave 150 rows and raise nothing |

Findings that become documented limits (the README must say each one in step 6):

1. `payload::text` is jsonb-formatted: key order is NOT kept (jsonb orders keys by length, then bytes: `{"ok": true, "apple": 1, "zebra": "z"}`); dict equality hides it.
2. Payload values str, int (even 2**70), bool and ordinary floats (0.1, 1e-07, pi, -2.5, 1.0, 0.0, 5e-324) round-trip equal and with the same type, BUT very large floats do NOT: jsonb stores `numeric` and keeps decimal digits. 1e22 comes back as the int 10**22 (equal in value, not in type); 1.5e300 and 1.7976931348623157e308 come back as ints of the shortest decimal digits plus zeros, which differ from the float. The memory index stores only `user_id` strings, so it is not affected. Documented only, not fixed.
3. A payload string with a NUL character is refused by jsonb (`UntranslatableCharacter`) and ends as `vector store failed on collection 'x': UntranslatableCharacter` with no row text.
4. Vector values outside float32 are refused by the server (1e39: `NumericValueOutOfRange`), surfaced as the exception type only.
5. A zero query vector gives NaN for every hit and every score -1.0.

The memory index end to end (criterion 14) passed on the real server with SQLite as the relational store (engineer's report only):

- An `IndexedMemoryStore` over `SqliteRelationalStore(tmp/memory.db)` with the REAL `PgVectorStore` (live) and the fake embedder (reusing `Rig`, `NOW` and the embedder factory of `tests/test_memory_index_store.py`, replacing only `rig.store`). Two notes for `u1` and one for `u2`.
- The query `"cook for her"` shares no keyword with the note (the test first asserts that plain SQLite keyword search finds nothing, so a hit proves the vector path) and returns the right note for `u1` and never `u2`'s note.
- Then `DELETE FROM memory_vectors`, drop the pgvector tables, and `reindex` for `u1` makes vector search work again (the documented backend-switch workaround, now proven on a real server).

Verification:

- Engineer's offline ordered runs (wiring file first), engineer's report only: 469 passed, 128 deselected; without it 443 passed, 128 deselected.
- Coordinator: full default suite `timeout 420 uv run pytest -q -rs` gave 3763 passed, 137 deselected (the earlier 51 plus the 86 live items), 1 warning (the pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 142.18 s, exit 0. The passed count is unchanged because the live items are deselected by default.
- Coordinator: `ruff check src tests` clean; `ruff format --check src tests`: 156 files already formatted.

Still unverified:

- Other pgvector or PostgreSQL versions (only 17.11 / 0.8.7), TLS, large data sizes and performance, halfvec/sparsevec.
- The wrong-password and closed-port exception types only as `OperationalError` on this local setup.
- Other lock-order cases (a delete against an upsert, several collections in one process) were not tested; the deadlock fix is proven for opposite-order writers on one collection.
- No mutation check was run on the other live tests (time rule).
- `_check_matches` remains a third copy (cleanup candidate).

Open items carried to step 6 (documentation): the README must document the five findings above as limits, the key order of payloads, and that `pgvector` needs a database administrator to run `CREATE EXTENSION vector;`; plus the open items from Step 2 (example block re-sync, the `url is only used...` row, the "`url` is for `qdrant` only" wording).

Done 2026-11-03.

## Validation

B3 record, 2026-11-03. Sources: the coordinator's runs and an independent tester that ran every README command exactly as written against real containers. I (technical-writer) was given the tester's results in the coordinator's brief; from its raw folder (`readme-pgv/run1/` in the scratchpad) I only listed the files and read the last lines of `pytest.txt` (`86 passed, 126 deselected in 9.37s`); I did not read the other raw files. I checked in `README.md` that the section "pgvector (step by step)" has steps 1-11 and that `CREATE INDEX` occurs 0 times.

### Full default suite (coordinator, after the last code change and after the README edits)

- `timeout 420 uv run pytest -q -rs` -> 3763 passed, 137 deselected, 1 warning (the pre-existing google-genai `AiohttpClientSession` DeprecationWarning), 0 failed, 0 errors, 0 skipped, 141.08 s, exit 0. README round 2 touched only `README.md`; `tests/test_command_tool.py` passed after it (36).
- Baseline before any change for this feature: 3591 passed, 51 deselected. Progression: 3647 (step 2), 3737 (step 3), 3763 (step 4), 3763 (step 5; the 86 live items are deselected).
- The 137 deselected are the 51 earlier live items plus 86 pgvector live items (46 in `tests/test_live_pgvector.py` and 40 shared contract tests on the real server).
- `ruff check src tests` clean; `ruff format --check src tests`: 156 files already formatted.

### Live runs on real containers

`pgvector/pgvector:pg17` (PostgreSQL 17.11, pgvector 0.8.7), plus plain `postgres:17` for the not-installed tests.

- Engineer, two runs: 86 passed (11.69 s and 11.43 s).
- Coordinator, independent run `... uv run pytest -m live tests/test_live_pgvector.py tests/test_storage_vectors.py -v -rs`: 86 passed, 126 deselected, 11.09 s, 0 failed, 0 skipped.
- Tester, README Tests-section commands as written: 86 passed in 9.37 s. Containers removed and ports free each time.

### Real bug found by the live tests

Two writers upserting the same ids in opposite order deadlocked (`DeadlockDetected`, 15 of 15 probe rounds). Fixed by sorting each upsert batch by id (`src/llm_harness/storage/pgvector.py`); proven by `test_two_instances_upserting_the_same_ids_in_opposite_order_never_deadlock`.

### Independent README test (tester, 2026-11-03)

- Part 1, server start: `pg_isready` has three states: `no response` (exit 2), `rejecting connections` (exit 1, about a second), `accepting connections` (exit 0). `CREATE EXTENSION vector;` printed `CREATE EXTENSION`; a second run exits 1 with `ERROR:  extension "vector" already exists`. The extension (0.8.7) and the data folder survive `docker stop && docker rm` and a second `docker run`.
- Part 2: `uv sync --extra postgres` then plain `uv run` finds psycopg. In a `--no-dev` copy `uv run --no-dev llm-harness --storage-status` exits 1 with the README's missing-extra message; a plain `uv run` installs the dev group again (16 packages).
- Part 3, `--storage-status`, every case MATCHES the README: extension not created; extension created without tables (`extension: vector 0.8.7`, `collections: not set up yet (it is set up on first use)`); two collections sorted by name (`collection memories_test: 3 dimensions, cosine`, `collection zeta: 4 dimensions, dot`); wrong password, no password and stopped container each give exactly one `  cannot connect (OperationalError)` line with exit 0; plain `postgres:17` gives `extension: not installed on this server (...)`; the password is never in any output; nothing is created under the scratch data dir.
- Part 4, error table: 20 config cases (exit 1, prefix `error: invalid config.toml: storage.vector: `, texts equal to the README) and the runtime messages through the real store (both extension messages, schema too new with the version row set to 99 and restored, dimension and distance mismatch, unknown collection on upsert and search, the NaN and infinity refusal, `vector store failed on collection 'c': OperationalError` for a wrong password and a closed port): all MATCH.
- Part 5: the complete minimal memory config of the README loads; `--storage-status` exits 0 and `--plain < /dev/null` exits 0 with empty output, both with the server running and with the container stopped (nothing connects at startup); no model called.
- Part 7: the Tests-section commands: 86 passed.
- Part 8: the README example block equals `EXAMPLE_CONFIG_TOML` up to `# Optional tools.`; no stale statement; every new name has a non-test use.
- Round 2 corrections applied to the README: the third `pg_isready` state, the real `psql` texts, the index finding.

### The index question (settled deviation from decision 3)

Tester, EXPLAIN on a real server with 3000 rows of 3 dimensions: the program's search shape `ORDER BY embedding <=> ?::vector LIMIT k` plans as `Seq Scan` + `Sort`. With a partial expression index `USING hnsw ((embedding::vector(3)) vector_cosine_ops) WHERE collection = 'idxtest'` created and `ANALYZE` run, the plan is unchanged. With `enable_seqscan = off` it uses the primary key (`vector_points_pkey`), not the HNSW index. Only a query with the cast on both sides (`ORDER BY embedding::vector(3) <=> '[1,0,0]'::vector(3)`) uses `idx_test`.

Conclusion: a hand-made HNSW index is NOT used by the program's current queries. Decision 3 of the approved spec promised a by-hand HNSW recipe; the real server showed it cannot work with the current queries. The README therefore gives no `CREATE INDEX` instruction (0 occurrences), says search is exact, says a hand-made index would not be used (checked with EXPLAIN on 3000 rows) and points to the roadmap item `pgvector-index`, which has to change the queries. The human is told in the delivery summary.

### Call-site check (tester, grep over src, non-test uses)

- `PgVectorStore`: `storage/__init__.py` (`open_storage`), `storage/status.py`.
- `PgVectorStatus` and `read_status_sync`: `storage/status.py`.
- `connect_read_only_sync`: `storage/pgvector.py` and `storage/postgres.py`.
- `is_undefined_table`: `storage/pgvector.py` and `storage/postgres.py`.
- `MIGRATIONS` of pgvector: `store.py` (`optional_schema_versions`).
- `_postgres_driver_installed`: `config.py` (relational and pgvector branches).

### Acceptance criteria 1-15

| Criterion | Evidence | Result |
| --- | --- | --- |
| 1 | `tests/test_config_pgvector.py`, `tests/test_config.py`, the tester's 20 config cases | met |
| 2 | `tests/test_config_pgvector.py` (driver missing, extras pin), the tester's `--no-dev` run | met |
| 3 | `tests/test_config_pgvector.py`, `tests/test_secrets_file.py` | met |
| 4 | the no-secret tests in `tests/test_pgvector.py`, `tests/test_status_pgvector.py` and the live tests; the tester's runs (password: 0 occurrences) | met |
| 5 | `tests/test_pgvector.py` (construction, `sys.modules`) | met |
| 6 | 40 shared contract tests on the offline stand-in and the same 40 on the real server | met |
| 7 | `tests/test_pgvector.py` and the live score tests | met |
| 8 | the extension-message tests offline and live; the tester's cases B and H | met |
| 9 | the live schema, preparation and concurrency tests | met |
| 10 | the no-SQL-from-user-data test and the live 64-character name test | met |
| 11 | the error tests offline and live | met |
| 12 | the `open_storage` tests in `tests/test_config_pgvector.py` | met |
| 13 | `tests/test_status_pgvector.py`, the live status tests, the tester's cases B to H | met |
| 14 | the live memory end-to-end test in `tests/test_live_pgvector.py` | met |
| 15 | the suite numbers above | met |

### Unverified

- Other pgvector or PostgreSQL versions (only 17.11 / 0.8.7), TLS, large data and performance.
- Other lock orders (a delete against an upsert, several collections in one process).
- `memory vector search is off: ...` end to end through the CLI (it needs a model chat; the message text is grounded in `memory_index.py` and `pgvector.py`) and the README step 8 `sqlite3` workaround were not re-run by the tester (the same procedure was verified for qdrant and Postgres; the live memory test proved the reindex path with project code).
- Wrong-password and closed-port exception types only as `OperationalError` on a local setup.
- Mutation checks: only the few the engineer ran and noted.
- `_check_matches` is a third copy (cleanup candidate); large payload floats come back as ints (documented limit); one `[postgres] password` serves both Postgres uses (documented limit).

## Documentation Review

Written 2026-11-03 (B4). The six constitution proposals were approved by the human on 2026-11-03 and applied in B5 (see Documentation Updates); the CHANGELOG, memory and roadmap items were applied in B5 as well.

### a. README

- Status: DONE and verified. The README was written by another agent and verified by an independent tester that ran every command as written on 2026-11-03 against real containers (details in `## Validation`, "Independent README test"). Round 2 corrections applied: the third `pg_isready` state, the real `psql` texts, the index finding.
- DEVIATION from decision 3 of this spec, SETTLED. Decision 3 said the README says how to add an HNSW index by hand. The `vector` column has no fixed dimension, so a hand-made index must be an expression index on `embedding::vector(N)`, and the program's queries (`embedding <=> ?::vector`) do not use it. The tester's EXPLAIN on a real server (3000 rows) confirmed it: Seq Scan + Sort, unchanged with the index and `ANALYZE`; with `enable_seqscan = off` the primary key `vector_points_pkey` is used, not the HNSW index; only a query with the cast on both sides uses the index. So the README gives no `CREATE INDEX` instruction (0 occurrences), says search is exact, says a hand-made index would not be used, and points to the roadmap item `pgvector-index`, which has to change the queries. The human is told in the delivery summary.

### b. CHANGELOG

- This project has exactly one changelog, `CHANGELOG.md` (it has a `## [Unreleased]` heading with `### Added`). `specs/tech-stack.md` has no `### Artifacts` subsection (grep for `Artifacts` found nothing), so the implicit default applies: one artifact, root `.`, changelog `CHANGELOG.md`.
- Proposed entries (user-facing, one line each, to be written in B5):
  1. `{CHANGELOG.md, Added, "pgvector vector backend: `[storage.vector] backend = "pgvector"` with a `url` keeps the vectors in a PostgreSQL database with the pgvector extension, which an administrator creates with `CREATE EXTENSION vector;`; it uses the same optional extra `postgres` and the optional `[postgres] password` in `~/.config/llm-harness/secrets.toml`; `--storage-status` reports the extension and the collections read-only; it works with SQLite as the relational store, so agent memory keeps working; see the README section \"pgvector\"."}`
  2. `{CHANGELOG.md, Changed, "The error for a `url` with the `qdrant-edge` or `none` vector backend now reads `url is only used with backend = \"qdrant\" or \"pgvector\"`, and the unknown-backend error for `[storage.vector]` now lists `qdrant-edge, qdrant, pgvector, none`."}`
- Considered and judged internal (not user-facing, so no entry): `PgVectorStore` itself (covered by entry 1), the connection-layer refactor (`connect_read_only_sync` and `is_undefined_table` in `postgres.py`; `read_schema_versions_sync` returns the same results), `optional_schema_versions()` learning `vector` (visible only as one extra `schema: vector version 1` line in the relational `--storage-status` when both databases are the same; judged too small, say so if you disagree), the stand-in test server `tests/fake_pgvector.py`, the test helpers in `tests/live_postgres.py`, and the third copy of `_check_matches`.
- The deadlock fix (sorting a batch by id in `upsert`) is part of the new backend, not a fix of released behaviour, so it gets no separate entry.

### c. CI, build and release

- `{pyproject.toml, needs change: no, reason: no new extra and no new dependency; a test pins that it has exactly the extras remote and postgres and no dependency naming pgvector}`
- `{uv.lock, needs change: no, reason: no dependency changed}`
- Wheel contents check: done by the coordinator, 2026-11-03. `uv build --wheel --out-dir <scratch folder>` produced a wheel that contains `llm_harness/storage/pgvector.py` (and the changed `storage/postgres.py` and `storage/status.py`), whose METADATA still has exactly `Provides-Extra: postgres` and `Provides-Extra: remote`, `Requires-Dist: psycopg[binary]>=3.3.7 ; extra == 'postgres'` and the qdrant-client line for `remote`, and no line mentioning pgvector (no new extra and no new dependency, as the spec requires). No `dist/` was left in the project.
- No CI workflow, Dockerfile, Makefile, task-runner or release script exists. Re-run of `find . -maxdepth 2` (pruning `.venv`, `.git`, `.pytest_cache`, `.ruff_cache`) for `Makefile`, `Dockerfile*`, `*.yml`, `*.yaml`, `Jenkinsfile`, `tox.ini`, `noxfile.py`, `.gitlab-ci*`, `justfile`, `Taskfile*` and `.github`: no match. `ls .github`: no such directory. The top level holds `.agent` (only `tmp`), `.gitignore`, `.python-version`, `CHANGELOG.md`, `README.md`, `pyproject.toml`, `uv.lock`, `specs`, `src`, `tests`. So no tag-creating workflow exists and the release-wiring check has nothing to inspect.

### d. Constitution drift (six proposals, approved by the human 2026-11-03 and applied)

Grep of `specs/tech-stack.md` and `specs/mission.md` for `pgvector`, `qdrant`, `[storage.vector`, `Still planned`, `VectorStore`, `secrets`, `psycopg`, `planned`: no occurrence of `pgvector` except the "Still planned" one below. Stale statements:

1. `{file: specs/tech-stack.md, line: 96, current: "Still planned: pgvector (`storage-backends-pgvector`, depends on the Postgres store), `storage-backends-postgres-memory` and the roadmap item `memory-reindex-all`;", proposed: "Still planned: `storage-backends-postgres-memory`, the roadmap item `memory-reindex-all` and the roadmap item `pgvector-index` (an optional approximate index);", status: approved by the human 2026-11-03; applied (tech-stack.md version 29, mission.md version 19)}`
2. `{file: specs/tech-stack.md, line: 96, current: "`VectorStore` (`qdrant-edge`, remote `qdrant` through `QdrantRemoteVectorStore` in `src/llm_harness/storage/qdrant_remote.py` with helpers shared by both stores in `storage/vector_common.py`, or `none`)", proposed: "`VectorStore` (`qdrant-edge`, remote `qdrant` through `QdrantRemoteVectorStore` in `src/llm_harness/storage/qdrant_remote.py`, `pgvector` through `PgVectorStore` in `src/llm_harness/storage/pgvector.py` (one table `vector_points` plus the catalog `vector_collections` in the namespace `vector`; vectors travel as text, no `pgvector` Python package; exact search, scores cosine `1 - d`, euclid `-d`, dot `-d`; the extension is only checked, never created; batches are sorted by id before writing to avoid deadlocks; verified against pgvector 0.8.7 on PostgreSQL 17.11 with psycopg 3.3.7), with helpers shared by the three in `storage/vector_common.py`, or `none`)", status: approved by the human 2026-11-03; applied (tech-stack.md version 29, mission.md version 19)}`
3. `{file: specs/tech-stack.md, line: 86, current: "`[storage.vector]` (`backend`, `path`, and `url` for `qdrant` only)", proposed: "`[storage.vector]` (`backend`, `path`, and `url` for `qdrant` and `pgvector` only)", status: approved by the human 2026-11-03; applied (tech-stack.md version 29, mission.md version 19)}`
4. `{file: specs/tech-stack.md, line: 86, current: "`url` is rejected for `qdrant-edge` and `none`).", proposed: "`url` is rejected for `qdrant-edge` and `none` with the text `url is only used with backend = \"qdrant\" or \"pgvector\"`); `pgvector` is accepted and needs its own `url` (the same rules as the relational Postgres url; `path` is rejected with it).", status: approved by the human 2026-11-03; applied (tech-stack.md version 29, mission.md version 19)}`
5. `{file: specs/tech-stack.md, line: 86, current: "is read once, only when relational is `postgres` or vector is `qdrant`; each secret is loaded only for its own backend", proposed: "is read once, only when relational is `postgres` or vector is `qdrant` or `pgvector`; each secret is loaded only for its own backend (`HarnessConfig.postgres_password` is set for relational `postgres` or vector `pgvector`; one `[postgres] password` serves both)", status: approved by the human 2026-11-03; applied (tech-stack.md version 29, mission.md version 19)}`
6. `{file: specs/mission.md, line: 21, current: "a file store and a qdrant-edge vector store or a remote qdrant server (optional extra `remote`)", proposed: "a file store and a qdrant-edge vector store, a remote qdrant server (optional extra `remote`) or a pgvector database (optional extra `postgres`)", status: approved by the human 2026-11-03; applied (tech-stack.md version 29, mission.md version 19)}`

Checked and found still true, no proposal: `specs/tech-stack.md` line 61 (extras `remote` and `postgres`; no new dependency), line 95 (Postgres ledger; the memory refusal is unchanged), line 97 (`open_storage` creates nothing; still true for pgvector).

Noted, not caused by this feature: `specs/mission.md` line 21 ends with "What does not exist yet: ... and vector search over memory", which `memory-vector-search` already made stale; and `specs/tech-stack.md` line 13 `planned` is about the frontend. I propose nothing for them here.

### e. Memory candidates for B5 (appended in B5; timestamps replaced)

Shape: `YYYY-MM-DDTHH:MM:SSZ [tag] text`; the timestamp is replaced when appended in B5. The previous Postgres feature's entries are `specs/memory.md` lines 71-74 (ledger DDL differences, psycopg error text, Postgres live-test variable, dev group). Grep of `specs/memory.md` for `postgres|pgvector|jsonb|deadlock|live_|LIVE_` returned only lines 71-74, and for `float32|NaN|HNSW|superuser|NUL|key order` nothing. None of the four candidates duplicates them; candidate 4 is the pgvector twin of line 73 (different variable, test files and dropped tables).

1. `2026-11-03T17:29:20Z [gotcha] Concurrent writers that write several rows in one transaction must lock them in a fixed order: PgVectorStore.upsert in src/llm_harness/storage/pgvector.py sorts the batch by id because two writers listing the same ids in opposite order failed with DeadlockDetected on a real server, which an offline stand-in without row locks could not show.`
2. `2026-11-03T17:29:21Z [gotcha] PostgreSQL jsonb (payload JSONB read back as payload::text) does not keep key order, keeps decimal digits of numbers so a float such as 1e22 comes back as an int, and refuses a NUL character in a string with UntranslatableCharacter.`
3. `2026-11-03T17:29:22Z [domain] Verified on pgvector 0.8.7 and PostgreSQL 17.11: the vector type stores float32 (beyond about 3.4e38 gives NumericValueOutOfRange), cosine of a zero vector is NaN and sorts last, HNSW refuses over 2000 dimensions for the plain vector type, and only a superuser can run CREATE EXTENSION vector (the pgvector/pgvector:pg17 image has it available, not created).`
4. `2026-11-03T17:29:23Z [env] The pgvector live tests (tests/test_live_pgvector.py, store_factory[pgvector] in tests/test_storage_vectors.py, tests/live_postgres.py) need LLM_HARNESS_LIVE_PGVECTOR_URL (and LLM_HARNESS_LIVE_PGVECTOR_PLAIN_URL for the not-installed case) and a database named with live or test, because they drop vector_points, vector_collections and the extension.`

Each candidate line is at most 400 characters (measured). No candidate holds a path outside the repo, a host, a port or a secret.

### f. Not reviewed

- The README: written by another agent and verified by the independent tester (see a); not reviewed line by line here.
- `CHANGELOG.md`, `specs/memory.md`, `specs/roadmap.md`: not edited or checked in full by instruction (bookkeeping is B5); the memory file was only grepped.
- Source and tests: not read again in this phase; facts come from the Implementation Notes of steps 1 to 5 and from the brief.
- The built wheel: checked by the coordinator (see c).
- Whether an HNSW or expression index is used by the program's queries: settled by the tester's EXPLAIN (part a): it is not used.
- Other pgvector or PostgreSQL versions, TLS and performance: unverified (see Implementation Notes, Still unverified).

## Documentation Updates

B5, 2026-11-03.

### What was changed where

- `README.md`: written by another agent and verified by the independent tester; not edited by the technical-writer. Changes: a new section "pgvector (step by step)" with steps 1-11; the Storage section and the "Which vector backend?" table row; the qdrant table row that now quotes `url is only used with backend = "qdrant" or "pgvector"`; the qdrant and Postgres step-5 sentences saying the secrets file is read for pgvector too; the Vector search section; the flags row; Security notes; six Troubleshooting entries; the Tests commands; Known limits; the example config block re-synced with `EXAMPLE_CONFIG_TOML`. Round 2 applied the tester's corrections (see `## Validation`).
- Constitution (all six proposals approved by the human 2026-11-03, applied exactly as recorded in Documentation Review, "Constitution drift"): `specs/tech-stack.md` version 28 -> 29 (five edits), `specs/mission.md` version 18 -> 19 (one edit); both `status: approved`, `updated: 2026-11-03`.
- `CHANGELOG.md`, under `## [Unreleased]`: one line at the end of `### Added` and one line at the end of `### Changed`, as proposed in Documentation Review, "CHANGELOG". Added (under `### Added`):

```
- pgvector vector backend: `[storage.vector] backend = "pgvector"` with a `url` keeps the vectors in a PostgreSQL database with the pgvector extension, which an administrator creates with `CREATE EXTENSION vector;`; it uses the same optional extra `postgres` and the optional `[postgres] password` in `~/.config/llm-harness/secrets.toml`; `--storage-status` reports the extension and the collections read-only; it works with SQLite as the relational store, so agent memory keeps working; see the README section "pgvector".
```

  Added (under `### Changed`):

```
- The error for a `url` with the `qdrant-edge` or `none` vector backend now reads `url is only used with backend = "qdrant" or "pgvector"`, and the unknown-backend error for `[storage.vector]` now lists `qdrant-edge, qdrant, pgvector, none`.
```

- `specs/roadmap.md`: the `## Now` line for this feature was removed; the section body is now "Nothing in progress." `version` is unchanged (Now-line edits do not bump it). No other roadmap line was touched.
- `specs/memory.md`: four entries appended, each at most 400 characters (372, 262, 388, 377), no existing line changed. Entries appended, verbatim:

```
2026-11-03T17:31:44Z [gotcha] Concurrent writers that write several rows in one transaction must lock them in a fixed order: PgVectorStore.upsert in src/llm_harness/storage/pgvector.py sorts the batch by id because two writers listing the same ids in opposite order failed with DeadlockDetected on a real server, which an offline stand-in without row locks could not show.
2026-11-03T17:31:45Z [gotcha] PostgreSQL jsonb (payload JSONB read back as payload::text) does not keep key order, keeps decimal digits of numbers so a float such as 1e22 comes back as an int, and refuses a NUL character in a string with UntranslatableCharacter.
2026-11-03T17:31:46Z [domain] Verified on pgvector 0.8.7 and PostgreSQL 17.11: the vector type stores float32 (beyond about 3.4e38 gives NumericValueOutOfRange), cosine of a zero vector is NaN and sorts last, HNSW refuses over 2000 dimensions for the plain vector type, and only a superuser can run CREATE EXTENSION vector (the pgvector/pgvector:pg17 image has it available, not created).
2026-11-03T17:31:47Z [env] The pgvector live tests (tests/test_live_pgvector.py, store_factory[pgvector] in tests/test_storage_vectors.py, tests/live_postgres.py) need LLM_HARNESS_LIVE_PGVECTOR_URL (and LLM_HARNESS_LIVE_PGVECTOR_PLAIN_URL for the not-installed case) and a database named with live or test, because they drop vector_points, vector_collections and the extension.
```

- Memory count after the appends: 74 entries (`grep -cE '^[0-9]{4}-' specs/memory.md`), below the 200 threshold, so no maintenance pass ran.

### B5 closeout checklist

| Item | State | Evidence |
| --- | --- | --- |
| Validation of steps recorded | true | `## Implementation Notes`: full suite 3763 passed, 137 deselected, 0 failed, 0 skipped after step 5; live tests 86 passed on real containers in the coordinator's independent run. |
| README written and verified by the independent tester (and the HNSW-index deviation settled) | true | `## Validation`, "Independent README test": every command run as written, every error row MATCH, example block equal to `EXAMPLE_CONFIG_TOML`; "The index question": EXPLAIN shows a hand-made HNSW index is not used, so the README has no `CREATE INDEX` and points to `pgvector-index`. |
| Full-suite re-run after the documentation changes | true | Coordinator: `timeout 420 uv run pytest -q -rs` -> 3763 passed, 137 deselected, 1 warning, 0 failed, 0 errors, 0 skipped, 141.08 s, exit 0; ruff check clean, ruff format check 156 files already formatted. |
| `## Now` line removed | true | `specs/roadmap.md` `## Now` reads "Nothing in progress." |
| CI and release review recorded | true | Documentation Review, c (pyproject.toml, uv.lock, wheel contents, no CI or release files). |
| Every new function, type and config key has a non-test use | true | `## Validation`, "Call-site check (tester, grep over src, non-test uses)". |
| `validate-specs` passes | true | exit 0 after the last edit (run by the technical-writer; "specs validation successful"). |

Every item is true, so `status` is `done`.
