---
title: "Storage backends: agent memory on Postgres"
kind: feature
status: done
version: 1
updated: 2026-11-03
branch: main
---

## Problem / Motivation

With `[storage.relational] backend = "postgres"`, `[tools] memory = true` is REFUSED at config load. `load_config` in `src/llm_harness/config.py` raises `invalid config.toml: [tools] memory = true does not work with [storage.relational] backend = "postgres" yet (planned feature storage-backends-postgres-memory); use backend "sqlite" or switch memory off`. Memory (`src/llm_harness/memory.py`) works only with SQLite as the relational store because it uses SQLite-only features:

- An FTS5 virtual table `memories_fts` (external content over `memories`, `tokenize='porter unicode61'`, kept by three triggers), queried with `MATCH` and ranked by `bm25`.
- `INSERT OR REPLACE` in `src/llm_harness/memory_index.py` (two sites, around lines 256 and 302).

The SQL is otherwise portable: `INSERT/UPDATE/DELETE ... RETURNING`, `LIMIT ? OFFSET ?` and TEXT ISO timestamps. The Postgres adapter translates `?` to `%s`.

Schema now (namespace `memory`, 2 steps in `MEMORY_MIGRATIONS`):

- Step 1: `memories (id INTEGER PRIMARY KEY, user_id, text, keywords, created_at, updated_at)`, index `memories_user_id`, the FTS table and 3 triggers.
- Step 2: `memory_vectors (collection, memory_id, PRIMARY KEY (collection, memory_id))`, `memory_vector_meta (collection PRIMARY KEY, model_id, dim)` and two triggers (`memories_vectors_stale` AFTER UPDATE OF text, keywords; `memories_vectors_gone` AFTER DELETE) that drop the marker rows.
- Keywords are stored newline-joined. The program builds the FTS5 query as `"w1" OR "w2"` from the `\w+` words of the message minus a stop-word list, at most 32 terms; no usable word means no search and an empty result.

Design decided (human-approved plan, 2026-11-03):

- Memory migrations become dialect-specific, chosen by `relational.dialect` exactly like the usage ledger (`_MIGRATIONS_BY_DIALECT` in `src/llm_harness/store.py` is the pattern). Both dialects must have the SAME number of steps (2), so schema versions stay aligned and `optional_schema_versions()` in `store.py` keeps working.
- Postgres step 1: `memories (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id TEXT NOT NULL, text TEXT NOT NULL, keywords TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, search tsvector GENERATED ALWAYS AS (to_tsvector('english', text || ' ' || keywords)) STORED)`, `CREATE INDEX memories_user_id ON memories (user_id)`, `CREATE INDEX memories_search ON memories USING gin (search)`.
- Postgres step 2: `memory_vectors` and `memory_vector_meta` with BIGINT `memory_id`; one PL/pgSQL function `memories_clear_markers()` (body `DELETE FROM memory_vectors WHERE memory_id = OLD.id; RETURN NULL;`, `LANGUAGE plpgsql`); two triggers `AFTER UPDATE OF text, keywords ... FOR EACH ROW EXECUTE FUNCTION` and `AFTER DELETE ... FOR EACH ROW EXECUTE FUNCTION`.
- Postgres search: `SELECT m.id, m.user_id, m.text, m.keywords, m.created_at, m.updated_at FROM memories AS m, websearch_to_tsquery('english', ?) AS q WHERE m.search @@ q AND m.user_id = ? ORDER BY ts_rank(m.search, q) DESC, m.id DESC LIMIT ?`. The first parameter is built from the same stop-word-filtered words, each word double-quoted and joined by ` or `. The SQLite statement is unchanged.
- `INSERT OR REPLACE INTO memory_vectors` becomes the standard `INSERT INTO memory_vectors (collection, memory_id) VALUES (?, ?) ON CONFLICT (collection, memory_id) DO NOTHING` at both sites (SQLite supports it; no dialect split).
- The refusal in `load_config` is removed. A memory-with-postgres config loads; the combinations relational sqlite/postgres x vector qdrant-edge/qdrant/pgvector/none are all valid.
- Existing SQLite memories are NOT copied into Postgres (out of scope, like the usage ledger).

Human decisions (2026-11-03):

1. Ranking need not be identical to SQLite's bm25. The human answered "Yes: same notes found, ranking may differ" (same notes found, top hit equal on a fixed corpus; the README states the difference).
2. Approval: the human approved this plan with the words "Start now; this plan is the spec approval (fastest)". The roadmap move of this item from Later to Now (roadmap version 25) is part of the approval the human gave with those words. The human was told before answering that the plan includes this move and the implementation of steps 1-5 without waiting. The constitution edits and the delivery still need the human's approval.
3. Branch: stay on `main`; the human commits manually and the agent does not commit.

Verified facts (bounded spike, 2026-11-03: `postgres:17`, PostgreSQL 17.11, psycopg 3.3.7, 127.0.0.1:15435, hard timeout 90 s, exit 0, container removed, port free; script `fts_spike.py` and output `fts_spike.out` in the session scratchpad, not in the repo). Cited only as the output shows:

- The generated tsvector column with a GIN index works, and `INSERT ... RETURNING` of the six memory columns works without the search column.
- On a fixed corpus of 10 notes and 13 queries, the production SQLite search (`MemoryStore` over FTS5/bm25/porter) and the Postgres statement return the SAME result set for 13 of 13 queries.
- Top-1 and full order are equal in 13 of 13 for plain `ts_rank` and for `ts_rank_cd`, and in 12 of 13 for the length-normalised variants (`ts_rank(.., 1)`, `ts_rank_cd(.., 1)`, `ts_rank_cd(.., 2)`). Those flipped the query "peanuts" (note 9 "Peanut butter sandwich for lunch" against note 1 "Partner is allergic to peanuts": SQLite ranks 9 first, the normalised variants rank 1 first). Plain `ts_rank` is therefore chosen.
- Stemming agreement on 11 of 12 word pairs. Match in both: run/running, cook/cooking, cook/cooks, peanut/peanuts, flight/flights, happy/happiness, meet/meeting, travel/travelling. Match in neither: allergy/allergic, index/indices, colour/color. ONE REAL DIFFERENCE: `cafe` matches a note containing `café` on SQLite (`unicode61` folds accents) but not on Postgres (`english` does not fold accents). This is a documented limit; the `unaccent` extension would need an administrator, like pgvector's `CREATE EXTENSION`, and is out of scope.
- A stop-word-only query gives an empty tsquery that matches nothing and raises no error. Hostile characters (quotes, `&`, `|`, `!`, `:`, parentheses) stayed a plain query. `x_y` became the phrase `'x' <-> 'y'`.
- The migration runner (`PostgresRelationalStore.migrate_sync`) ran statements with `$$` function bodies unchanged, and a second migrate was a no-op.
- Marker semantics are identical to SQLite: UPDATE of `updated_at` only keeps the marker; UPDATE of `text` or of `keywords` and DELETE of the memory clear it.
- `INSERT ... ON CONFLICT (collection, memory_id) DO NOTHING` inserted once (rowcount 1) and did nothing the second time (rowcount 0).
- NOT tested in the spike: the exact production statements through the new code, concurrent writers, large corpora and performance, other server versions, non-English text (the `english` configuration stems only English; other languages are matched as plain tokens, as with SQLite's porter tokenizer), TLS.

## Acceptance Criteria

1. With relational postgres, `[tools] memory = true` loads: the refusal is gone and its message text no longer exists in `src`. Every combination of relational sqlite/postgres and vector qdrant-edge/qdrant/pgvector/none with memory on loads. Tests: `tests/test_config.py`, `tests/test_config_postgres.py` (the tests that pinned the refusal are updated).
2. `MemoryStore` picks its migrations by `relational.dialect`. An unknown dialect raises a clear `ValueError` naming the known ones. Both dialects have the same number of steps. The SQLite migration text and the SQLite search statement are byte-identical to today (a test pins the old literals).
3. On Postgres the memory schema is created by the versioned migration in the namespace `memory` (version 2). `--storage-status` shows `schema: memory version 2 (the code expects 2)` through the existing optional-namespace logic.
4. Save, update, forget, get, list and search behave the same on both backends: the shared memory tests run on SQLite offline and on Postgres under `-m live`.
5. Search finds the same notes as SQLite on the spike's fixed corpus (13 queries: same result set, same top hit). A stop-word-only or empty query returns no result without error. Hostile characters in a query never raise.
6. `INSERT OR REPLACE` no longer appears in `src`. The marker upsert is `ON CONFLICT (collection, memory_id) DO NOTHING` on both backends.
7. On Postgres the marker triggers behave as on SQLite: updating only `updated_at` keeps the marker; updating text or keywords, or deleting the memory, clears it (live).
8. The vector index works end to end with relational Postgres plus pgvector and with relational Postgres plus qdrant-edge, through a fake embedder (live): save, search by meaning, user_id isolation, reindex.
9. A user's memories never appear for another user on Postgres (live).
10. The accent difference (`cafe` against `café`) is pinned by a test that documents Postgres' behaviour.
11. No new dependency and no new extra: `pyproject.toml` is unchanged, and a test pins it.
12. Errors from the Postgres memory path name the exception type only where the program prints them (no host, user, password, server text or row).
13. Nothing connects at startup with memory on and relational postgres (`--plain < /dev/null`, dead port).
14. The README documents every change, and an independent tester runs every changed command as written.
15. The full default suite passes offline, and the live tests pass against `postgres:17` and `pgvector/pgvector:pg17`.

## Approach

Memory consulted: grep -n -i -E "fts5|tsvector|websearch|memory_vectors" specs/memory.md - 1 hit (line 69, a `[gotcha]`: the memory index keeps its markers in the relational store, so changing `[storage.vector]` leaves them stale and `/memory reindex` reports 0 of 0; relevant to the vector-index live tests and to the README switching notes, not to the schema design)

Ordered small steps. The human is away, so each step's result is shown in the final summary.

1. Spike: DONE 2026-11-03 (facts in Problem / Motivation; record in Implementation Notes, Step 1).
2. Core: dialect-specific memory migrations (SQLite text unchanged, Postgres as above) selected by `relational.dialect` in `MemoryStore`; the Postgres search statement and query builder; `INSERT OR REPLACE` replaced by `ON CONFLICT DO NOTHING` at both sites; the memory refusal removed from `load_config`; the config tests that pinned the refusal updated. Offline tests: statement text and parameters on a recording fake, dialect selection, equal step counts, SQLite statements byte-identical, no secret or user text in messages.
3. Live tests (Docker allowed for the engineer under strict bounds): the memory store contract and search expectations on a real Postgres (the existing SQLite memory tests that assert search behaviour run against Postgres through a rig), the vector-marker triggers, the index flows with relational Postgres plus pgvector and with relational Postgres plus qdrant-edge, the accent limit documented as a test, and concurrency of saves.
4. Documentation: README (Agent memory section; Postgres section: the refusal is gone, search differences, the accent limit, switching: memories are NOT copied; storage table), CHANGELOG, constitution proposals, memory entries, CI review. An independent tester runs every changed README command.
5. Closeout.

Risks:

- Ranking differs from bm25 (documented).
- Accent-sensitive search on Postgres.
- The English stemmer differs from Porter (allergy/allergic and others match in neither engine).
- A generated tsvector column is recomputed on every UPDATE.
- PL/pgSQL triggers require plpgsql (installed by default).

Testing strategy: offline, fast and repeatable by default; live tests are opt-in with `-m live`.

## Affected Areas

- `src/llm_harness/memory.py` (dialect-specific migrations, Postgres search statement and query builder)
- `src/llm_harness/memory_index.py` (`INSERT OR REPLACE` at two sites)
- `src/llm_harness/config.py` (memory refusal removed)
- `src/llm_harness/store.py` (pattern only; `optional_schema_versions()` expected to keep working)
- Tests: `tests/test_config.py`, `tests/test_config_postgres.py`, the memory tests (`tests/test_memory_index.py`, `tests/test_memory_index_store.py`, `tests/test_memory_vectors_wiring.py`), the live helpers `tests/live_postgres.py`, and new test files for the dialect and live memory work
- `README.md` and `CHANGELOG.md` (documented at the documentation phase); constitution files only through approved proposals

## Out of Scope

- Copying existing SQLite memories into Postgres.
- Accent folding (`unaccent`).
- Languages other than the English stemmer.
- Ranking identical to bm25.
- Connection pooling.
- Postgres versions other than 17.
- Memory-specific index tuning.

## Implementation Notes

### Step 1: spike (done 2026-11-03)

The facts are in Problem / Motivation. Record of the run:

- Setup: image `postgres:17` was pulled with a cap; one container on 127.0.0.1:15435; `uv run python` from the project root, so the production `MemoryStore` ran on the SQLite side.
- The script compared the production SQLite results with the candidate Postgres SQL.
- First run exit 0. The container was removed afterwards and the port is free.
- Script and output are in the session scratchpad, not in the repo: `fts_spike.py` and `fts_spike.out`.

### Pre-change baseline

The coordinator's run before any change for this feature, 2026-11-03: `timeout 420 uv run pytest -q -rs` -> 3763 passed, 137 deselected, 1 warning (the pre-existing google-genai `AiohttpClientSession` DeprecationWarning), 0 failed, 0 errors, 0 skipped, 141.08 s, exit 0. It was taken after the last code change of the previous feature, and the coordinator verified with `find src tests pyproject.toml uv.lock -newer <that run's output>` that no file has changed since. Branch `main`, with the earlier features' uncommitted changes in the tree.

### Step 2: core implementation (done 2026-11-03)

Source of each fact: "engineer" means the engineer's report only; "coordinator" means the coordinator checked it (full suite, ruff, greps, reading `_describe` and `_SEARCH_POSTGRES`).

Files changed (engineer's report):

- `src/llm_harness/memory.py`: `MEMORY_MIGRATIONS` untouched. New `MEMORY_MIGRATIONS_POSTGRES` with the SAME 2 steps. Step 1: `memories` with `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` and a generated `search tsvector` column over `to_tsvector('english', text || ' ' || keywords)`, indexes `memories_user_id` and the GIN index `memories_search`. Step 2: `memory_vectors`, `memory_vector_meta`, the PL/pgSQL function `memories_clear_markers()` and the triggers `memories_vectors_stale` (AFTER UPDATE OF text, keywords) and `memories_vectors_gone` (AFTER DELETE). `_MIGRATIONS_BY_DIALECT`; `MemoryStore.__init__` picks the tuple by `relational.dialect`; an unknown or missing dialect raises `ValueError("no memory schema for dialect X; known: sqlite, postgres")`. Module constants `_SEARCH_SQLITE` (the old statement, same text) and `_SEARCH_POSTGRES` (`SELECT m.id, ... FROM memories AS m, websearch_to_tsquery('english', ?) AS q WHERE m.search @@ q AND m.user_id = ? ORDER BY ts_rank(m.search, q) DESC, m.id DESC LIMIT ?`; the coordinator read it). `_query_words` with `_match_expression` rebuilt on it (SQLite output unchanged) and `_websearch_expression` (every word double-quoted, joined by ` or `). `search` dispatches on the dialect.
- `src/llm_harness/memory_index.py`: both `INSERT OR REPLACE` sites became `INSERT INTO memory_vectors (collection, memory_id) VALUES (?, ?) ON CONFLICT (collection, memory_id) DO NOTHING`. Every other statement was read and found portable: `NOT EXISTS`, `COUNT(*)`, `ORDER BY id LIMIT ?`, the `memory_vector_meta` statements. Plus the leak fix below, which was beyond the brief.
- `src/llm_harness/config.py`: the memory refusal removed from `load_config`; there was no helper to remove.
- `tests/test_config_postgres.py`: the refusal tests replaced by `test_memory_with_postgres_loads_with_every_vector_backend` (parametrized over qdrant-edge, none, qdrant and pgvector) and `test_the_old_refusal_is_gone_from_the_source`.
- `tests/test_config_pgvector.py`: `test_relational_postgres_with_memory_is_still_refused` became `test_relational_postgres_with_memory_loads`.
- `tests/test_memory_recall.py`: the stand-in `RaisingRelational` got `dialect = "sqlite"`; two tests failed with the new ValueError until then.
- NEW `tests/test_memory_postgres.py`: 47 offline tests; subclasses `FakeRelational` from `tests/test_store_postgres.py`.
- `src/llm_harness/store.py` was not changed: `optional_schema_versions()` still uses `len(MEMORY_MIGRATIONS)`, both tuples have 2 steps, and a test pins it.

Security finding, fixed (record): the required test "no secret in any error" failed before the fix. `_guarded` in `memory_index.py` printed `redact(f"{type}: {error}")` in the vector-index failure notice. `redact` only knows key and URL shapes, so a psycopg-style message carrying the host, the user and a quoted row came out in the notice (only the password was masked). New `_describe(error)` shows a PostgreSQL driver error by TYPE NAME only (as `store.py` does); SQLite errors and non-database errors keep their redacted text; one test pins that. The coordinator read `_describe`. Step 3 hunts for the same class of leak in the other memory paths.

Verification:

- Coordinator: full default suite `timeout 420 uv run pytest -q -rs` -> 3815 passed, 137 deselected, 1 warning (pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 139.12 s, exit 0 (baseline 3763). `ruff check src tests` clean; `ruff format --check src tests` 157 files already formatted. `grep -rn "INSERT OR REPLACE\|storage-backends-postgres-memory" src` shows nothing.
- Engineer's report only: ordered runs with the wiring file first: 1500 passed, 60 deselected; without it 1474 passed. Scratch-HOME CLI run: relational postgres + `[tools] memory = true` + `user_id` + a dead-port url: `--plain < /dev/null` exit 0, stderr empty, no `.local` directory (nothing connects at startup); `--storage-status` exit 0 with `relational: postgres  127.0.0.1:1/db` and `  cannot connect (OperationalError)`.
- The SQLite proof: `OLD_SQLITE_MIGRATIONS` and `OLD_SQLITE_SEARCH` in `tests/test_memory_postgres.py` are the pre-edit text copied from the working file before editing (`memory.py` is tracked at HEAD but the working tree already carried the uncommitted vector-index step). The existing SQLite memory tests pass unchanged except the stand-in noted above.

UNVERIFIED (needs a real server; step 3): the DDL on PostgreSQL (generated column, GIN index, function, triggers); the OR-search and the `ts_rank` order with these exact statements; `ON CONFLICT DO NOTHING` on both engines; `RETURNING` with identity ids; concurrency; the other memory error paths. There is no proof against the old code (git writes forbidden). The accent limit (`cafe` vs `café`) is known and documented, not fixed.

Next: step 3 (live tests on real Postgres and a security sweep of the memory error paths).

### Step 3: live tests (done 2026-11-03)

Source: the coordinator's report to the technical-writer (verified by the coordinator), not re-run by the technical-writer.

- Live suite `tests/test_live_memory_postgres.py` plus `tests/test_memory_store.py`, run with `-m live` against `postgres:17` (127.0.0.1:15436) and `pgvector/pgvector:pg17` (127.0.0.1:15437): 128 passed, 0 skipped, 0 failed, 14.32 s, exit 0. The containers were removed and the ports are free. The run recipe is in the module docstring of `tests/test_live_memory_postgres.py`.
- Covered: the shared memory contract, the 13-query search corpus, the stemming pairs and the accent limit, the GIN index, the function and the triggers exist, the marker triggers, the `ON CONFLICT` upsert, 20 concurrent first-use saves, concurrent save/update/search/forget, the error shows the type name only (test near line 1044), the vector index with pgvector and with qdrant-edge.
- Still UNVERIFIED: other server versions (only PostgreSQL 17 was used), TLS, large corpora and performance.

## Validation

- Live (2026-11-03, `-m live`, `postgres:17` and `pgvector/pgvector:pg17`): 128 passed, 0 skipped, 0 failed, 14.32 s, exit 0 (files `tests/test_live_memory_postgres.py` and `tests/test_memory_store.py`).
- Offline default suite: 3815 passed, 137 deselected, 0 failed at the end of step 2; final run after the documentation edits (coordinator, 2026-11-03): `timeout 420 uv run pytest -q -rs` -> 3815 passed, 265 deselected (live), 0 failed, 0 skipped, 143.80 s, exit 0; `ruff check src tests` clean; `ruff format --check` 158 files formatted; `validate-specs` exit 0.
- Coverage gaps: other server versions, TLS, large corpora and performance (not tested).

## Documentation Review

- `{file: README.md, needs change: yes, reason: it said memory is refused with relational postgres (storage table, Postgres section and step 8, error table row, `[storage.relational] path` row, troubleshooting, Known limits) and did not describe the search differences}`
- `{file: CHANGELOG.md, needs change: yes, reason: the Postgres relational backend line said memory is refused (planned follow-up); a new Added line is needed for memory on Postgres}`
- `{file: specs/mission.md, needs change: yes, reason: line 21 says agent memory needs the SQLite relational store and that `[tools] memory = true` is refused with Postgres, and describes keyword search as SQLite FTS5 only; constitution edit proposed to the human, not applied}`
- `{file: specs/tech-stack.md, needs change: yes, reason: line 95 says `[tools] memory = true` is refused (planned follow-up) and line 96 lists `storage-backends-postgres-memory` under "Still planned"; constitution edit proposed to the human, not applied}`
- `{file: specs/roadmap.md, needs change: yes, reason: the `## Now` line for this feature is removed (pre-approved by the human); version 26}`
- CI review: not applicable (no `.github` directory and no Makefile, Dockerfile, `*.yml`, `*.yaml`, Jenkinsfile, tox.ini, noxfile.py, `.gitlab-ci*`, justfile or Taskfile within two levels of the project root, found with `find . -maxdepth 2` pruning `.venv`, `.git`, `.pytest_cache`, `.ruff_cache`; `pyproject.toml` is unchanged by this feature, acceptance criterion 11; so no tag-creating workflow exists).

## Documentation Updates

- README.md: storage table rows (sqlite, postgres), Postgres section intro, step 1 requirement list, step 7 error-table row for the removed refusal deleted, step 8 (memories are not copied, schema created by migration `memory` version 2, search differences, accent limit, English stemmer only), pgvector intro, Agent memory section (new paragraph on Postgres), the `[storage.relational] path` row, troubleshooting bullet for the removed refusal deleted, Known limits.
- CHANGELOG.md `## [Unreleased]`: the Postgres relational backend line no longer says memory is refused; one new Added line for memory on Postgres.
- specs/roadmap.md: `## Now` line removed, version 26, updated 2026-11-03.
- Constitution, applied after human approval (2026-11-03): `specs/mission.md` v20 (memory works with either relational store; Postgres full-text search differences), `specs/tech-stack.md` v30 (Persistence, Tools lines; `storage-backends-postgres-memory` removed from Still planned). `specs/memory.md`: 2 entries appended (redact leak gotcha, Postgres search domain note).
