---
title: "Storage backends: Postgres relational store (usage ledger and status)"
kind: feature
status: done
version: 1
updated: 2026-11-03
branch: main
---

## Problem / Motivation

SQLite is the only relational backend. `[storage.relational] backend = "postgres"` is recognised in `src/llm_harness/config.py` and rejected: `_PLANNED_BACKENDS` maps it to the slug `storage-backends-postgres`, so the message names this feature. A user who wants the usage ledger on a shared Postgres server cannot configure it. The relational layer is `SqliteRelationalStore` in `src/llm_harness/storage/relational.py`, behind the `RelationalStore` protocol: async `execute(sql, params) -> int`, `query(sql, params) -> list[tuple]` and `migrate(namespace, migrations, *, adopt_user_version=False) -> int`, with one version per namespace in the table `schema_versions`. The SQLite class also has public sync methods (`execute_sync`, `query_sync`, `connect_sync`, `migrate_sync`), opens one connection per call, and `SchemaTooNewError` exists.

Consumers today:

- The usage ledger `Store` in `src/llm_harness/store.py` is synchronous and is called from async code through `asyncio.to_thread` (`router.py`). It calls `migrate_sync(..., adopt_user_version=True)` and `connect_sync()`, then uses `sqlite3.Connection` calls directly: `conn.execute(sql, params)`, `fetchall()`, `fetchone()`, the strings `BEGIN IMMEDIATE`, `ROLLBACK` and `COMMIT`, and `?` placeholders. It catches `(sqlite3.Error, OSError, SchemaTooNewError, ValueError)`, and its disable notice prints the exception type and the file path. The ledger DDL (namespace `ledger`, tables `llm_calls` and `model_backoff`) uses `INTEGER PRIMARY KEY` and `REAL`. `cli.py` builds `Store(db_path)` separately from `open_storage`.
- `--storage-status` (`src/llm_harness/storage/status.py`) reads the ledger file through a read-only SQLite URI (`read_schema_versions`).
- Memory (`src/llm_harness/memory.py`, `src/llm_harness/memory_index.py`) is SQLite-only: keyword search is an FTS5 virtual table kept by triggers and queried with MATCH, and it uses `INSERT OR REPLACE`. Memory is NOT in this slice.

Human decisions (2026-11-03):

1. Scope: thin slice first. Postgres behind `RelationalStore` for the usage ledger, schema versions and `--storage-status`. With backend postgres and `[tools] memory = true` the program refuses with a clear message that names the follow-up `storage-backends-postgres-memory` (tsvector search, triggers, ranking parity). That follow-up becomes a new roadmap item.
2. Driver: psycopg 3, as an optional uv extra (like `qdrant-client`).
3. Connection settings: `url` WITHOUT a password in config.toml `[storage.relational]`; the password in the optional secrets file `<config dir>/secrets.toml` under `[postgres]`; the same pattern as qdrant.
4. The usage ledger runs on Postgres through a thin sync adapter in `src/llm_harness/storage/`. `Store` stays sync and is still called through `to_thread`.
5. Carried over from the qdrant feature, stated as assumptions that the human confirmed by approving this spec on 2026-11-03: default tests are offline with fakes; real Postgres runs only under `-m live` against a local docker container.

Assumptions confirmed by the human on 2026-11-03 (by approving this spec):

1. The password is optional (no file or no key means no password).
2. `--storage-status` contacts Postgres read-only with a 5 s timeout, unlike the qdrant status line, which does not contact the server.
3. psycopg is the optional extra `postgres` plus the `dev` dependency group.
4. Existing SQLite ledger data is not copied to Postgres.
5. Default tests are offline; live tests run against `postgres:17`.

Verified facts (bounded spike, 2026-11-03: PostgreSQL 17.11 in the container `postgres:17`, psycopg 3.3.7 with psycopg-binary 3.3.7, libpq 18.0.6, hard timeout 60 s, exit 0, container removed, port free; script `pg_spike.py` and output `pg_spike.out` in the session scratchpad, not in the repo). Not tested in the spike: a TLS connection that succeeds, `connect_timeout` against an unreachable (blackholed) host, concurrent writers through the adapter, any server other than 17.11.

- The current SQLite DDL runs on Postgres, but `id INTEGER PRIMARY KEY` does not autoincrement (an insert without id raises `NotNullViolation`) and `REAL` is 32-bit (0.000123456789 came back as 0.00012345679). So the ledger needs dialect-specific DDL: `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` and `DOUBLE PRECISION`; the round trip is then exact. Columns named `time` and `until` are accepted.
- `INSERT ... ON CONFLICT(col) DO UPDATE SET x = excluded.x` and `CREATE TABLE IF NOT EXISTS` work unchanged.
- `?` placeholders fail ("the query has 0 placeholders but 1 parameters were passed"), so the adapter translates `?` to `%s`. A `%` in SQL with `params=()` fails ("only '%s', '%b', '%t' are allowed as placeholders") but is literal with `params=None`, so the adapter escapes `%` as `%%` when parameters are passed and passes `None` when there are none.
- `BEGIN IMMEDIATE` is a syntax error. Plain `BEGIN`, `COMMIT` and `ROLLBACK` work in an autocommit connection (`psycopg.connect(..., autocommit=True)`); a rollback leaves no row, and DDL inside a transaction rolls back (the table is absent), so `migrate` can stay atomic.
- `pg_advisory_xact_lock(k)` inside `BEGIN` serialises: a second connection's `pg_try_advisory_xact_lock(k)` returned False while the lock was held and True after `COMMIT`. This replaces the write serialisation that `BEGIN IMMEDIATE` gave.
- A connection opened with `options="-c default_transaction_read_only=on"` refuses INSERT (`ReadOnlySqlTransaction`) and allows SELECT; a missing table raises `UndefinedTable`.
- Connection errors are `psycopg.OperationalError` whose text includes host, port, user name and database name (never the password). Constraint violations (`NotNullViolation`, `CheckViolation`) quote the FAILING ROW in their text. So messages must never be shown, only the exception type.
- `psycopg.connect("postgresql://postgres@127.0.0.1:PORT/postgres?sslmode=disable", password=..., connect_timeout=5)` works, so the password can stay out of the URL. `sslmode=require` against a server without TLS fails with a clear `OperationalError`. The connection's repr shows host, port and database but no password.
- 10 x (connect + one query + close) took 0.082 s, so one connection per call (like the SQLite store) is acceptable. The async API (`AsyncConnection`) exists and works but is not planned.

## Acceptance Criteria

1. Config accepts `[storage.relational] backend = "postgres"` with a valid `url` (scheme `postgresql://` or `postgres://`, a host and a database name, optional `sslmode` and `connect_timeout` query parameters). `postgres` is no longer in `_PLANNED_BACKENDS`. `tests/test_config.py` covers, each with a message that names the problem: missing `url`; `path` set with postgres; `url` set with sqlite; a url without a host or without a database name; a password in the url userinfo; a `password` query parameter; a wrong scheme. No message echoes the URL.
2. When `psycopg` is not installed, selecting `backend = "postgres"` fails at config load with a message that contains `uv sync --extra postgres`. A bare install can still use sqlite.
3. `pyproject.toml` declares `psycopg[binary]>=3.3.7` only in the optional extra `postgres` and in the `dev` dependency group (so offline tests can import it); base `dependencies` are unchanged. `uv.lock` changes only through `uv lock`.
4. Secrets file `<config dir>/secrets.toml` learns the known table `postgres` with the key `password`. The key is optional; no file or no key means no password (valid for trust or peer authentication). An unknown table or key is rejected by name. The 0600 permission rule and the no-echo rule are the same as for qdrant. The file is read only when a backend that needs it is selected (relational postgres or vector qdrant), and one read serves both. The loaded password is held as `HarnessConfig.postgres_password: Secret | None`. With relational `sqlite` and vector `qdrant-edge` or `none`, the file is never opened (a garbage 0644 file causes no error). Tests: `tests/test_secrets_file.py`, `tests/test_config.py`.
5. No secret is ever printed: tests assert that the password does not appear in `repr`, `str`, log output, exception messages or `--storage-status` output.
6. `PostgresRelationalStore` in `src/llm_harness/storage/postgres.py` implements the async `RelationalStore` methods and the sync methods the ledger uses (`execute_sync`, `query_sync`, `connect_sync`, `migrate_sync`). Construction touches nothing, connects to nothing, and does not import psycopg (a bare install can import the module). Test file: `tests/test_storage_postgres.py` (new).
7. Unit tests on fakes (a fake psycopg connection injected through a factory) cover: `?` to `%s` translation; `%` escaped as `%%` when parameters are passed and `None` passed when there are none; `BEGIN IMMEDIATE` mapped to `BEGIN` plus `SELECT pg_advisory_xact_lock(<one fixed 63-bit constant>)`; rollback on error; `close()`; the password passed as the `password=` argument and never inside the URL; default `connect_timeout` 10 s; `migrate_sync` taking the same advisory lock, creating `schema_versions` if missing, applying `migrations[version:]`, upserting the version with ON CONFLICT, ignoring `adopt_user_version`, and raising `SchemaTooNewError`. No message the program prints contains the URL, the password, the server's message text or a quoted row (type names and SQLSTATE codes only).
8. The shared relational contract tests (`tests/test_storage_relational.py`) pass on SQLite offline and on Postgres under `-m live` (new `postgres` parameter marked `live`).
9. Ledger on Postgres: `llm_calls` and `model_backoff` use IDENTITY and `DOUBLE PRECISION` with the same table and column names; floats round-trip exactly; two concurrent `record_attempt` calls (two threads) never lose a backoff increment (live test). The SQLite ledger is byte-identical in behaviour and its existing tests (the 27 test files that touch the store) pass unchanged.
10. The `Store` disable notice names the backend and the exception type only, never the URL, the password, the server text or a row. `Store` catches both the sqlite3 errors and psycopg errors through one documented tuple of database error classes.
11. `--storage-status` with relational postgres opens a read-only connection (`default_transaction_read_only=on`, 5 s connect timeout) and prints `relational: postgres  <host:port/dbname>` (no user name, no password, no query string) and the same schema lines as SQLite (`schema: ledger version N (the code expects N)`). It prints `schema: nothing to read yet` when `schema_versions` does not exist (`UndefinedTable`), and ONE line `cannot connect (<ExceptionType>)` when the server does not answer, without failing. It creates and writes nothing. The SQLite output stays byte-identical (the existing `tests/test_storage_status.py` pins it). Covered with a fake and live.
12. `[tools] memory = true` with relational backend postgres fails at config load with a message that names `storage-backends-postgres-memory`.
13. `open_storage` builds `PostgresRelationalStore` for backend postgres and, at build time, creates nothing and connects to nothing. A non-test call site exists: `cli.py` passes `storage.relational` to `Store`.
14. The full default suite (`-m "not live"`) passes offline. The live tests (`tests/test_live_postgres.py`, new) are deselected by default and pass against a `postgres:17` container.

## Approach

Memory consulted: grep -n -i "postgres|psycopg|asyncpg|fts5" specs/memory.md - 0 hits, none relevant

Ordered small steps. Each step is shown to the human before the next one starts.

1. Bounded spike: DONE 2026-11-03. The facts are listed in Problem / Motivation; the result is recorded in Implementation Notes (Step 1).
2. Config and secrets. `[storage.relational]` accepts `backend = "postgres"` with `url` (required for postgres; scheme `postgresql://` or `postgres://`; a host and a database name are required; a password in the userinfo and a `password` query parameter are rejected because credentials do not belong in config.toml; `sslmode` and `connect_timeout` query parameters are allowed; `path` is rejected with postgres; `url` is rejected with sqlite). The planned-backend rejection for postgres goes away. psycopg becomes the optional uv extra `postgres` (`psycopg[binary]>=3.3.7`) and is also added to the `dev` dependency group so offline tests can import it (same reasoning as `qdrant-client`). A missing package gives a config error naming `uv sync --extra postgres`. The secrets file (`src/llm_harness/secrets_file.py`) learns the known table `postgres` with the key `password`, and one read serves both qdrant and postgres. `HarnessConfig` gets `postgres_password: Secret | None`. With relational backend postgres and `[tools] memory = true`, config loading fails with a message that names `storage-backends-postgres-memory`. `uv.lock` is regenerated by `uv lock` only.
3. Adapter `PostgresRelationalStore` in `src/llm_harness/storage/postgres.py`. It implements the async `RelationalStore` methods through the sync ones in `asyncio.to_thread` (as SQLite does), plus `execute_sync`, `query_sync`, `connect_sync`, `migrate_sync`. One connection per call, created lazily; construction touches nothing and imports psycopg lazily. The password is passed as a `password=` argument, never inside the URL; `connect_timeout` defaults to 10 s. `connect_sync()` returns a small wrapper with `execute(sql, params)` (translates `?` to `%s`, escapes `%`, passes `None` when there are no params, maps `BEGIN IMMEDIATE` to `BEGIN` plus `SELECT pg_advisory_xact_lock(<one fixed 63-bit constant>)`), cursor-like `fetchone`, `fetchall` and `rowcount`, and `close()`. `migrate_sync` takes the same advisory lock, creates `schema_versions` if missing, applies `migrations[version:]`, upserts the version with ON CONFLICT, raises `SchemaTooNewError`, and ignores `adopt_user_version` (there are no legacy Postgres databases). Errors reach callers as the driver's own exception types; a documented tuple of database error classes lets `Store` catch both sqlite3 and psycopg errors. No message the program prints may contain the URL, the password, the server's message text or a quoted row (type names and SQLSTATE codes only).
4. Ledger on Postgres. `Store` accepts the configured relational store. Its migrations become dialect-specific: the SQLite DDL is unchanged; the Postgres DDL uses IDENTITY and `DOUBLE PRECISION`, with the same table and column names. `cli.py` passes `storage.relational`. The disable notice names the backend and the exception type, never the URL. `record_attempt` is serialised by the advisory lock, so two processes cannot lose a backoff increment. SQLite behaviour stays byte-identical (the existing 27 test files that touch the store keep passing unchanged).
5. `--storage-status` for relational postgres: a read-only connection (`default_transaction_read_only=on`) with a 5 s connect timeout; output as in criterion 11; it creates nothing and writes nothing; the SQLite output stays byte-identical. The human confirmed this assumption by approving this spec on 2026-11-03: status contacts Postgres (unlike the qdrant status line, which does not contact the server) because the schema lines are its purpose.
6. Tests. Offline: a fake psycopg connection injected through a factory; unit tests of the placeholder translator, `%` escaping, `BEGIN IMMEDIATE` mapping, advisory lock statements, rollback on error, closing, error mapping, no secret or row text in any message, lazy import, and the config and secrets rules. The shared relational contract tests (`tests/test_storage_relational.py`) get a `postgres` parameter marked `live`. New live tests (`-m live`, env var `LLM_HARNESS_LIVE_POSTGRES_URL` plus a password variable or the url's own settings; skipping with a printed reason when unset or unreachable) cover the adapter, migrations including a concurrent migrate, the ledger (insert, backoff, two threads calling `record_attempt`, exact float round trip), `SchemaTooNewError`, and status output. The module docstring of the live test gives the exact `docker --context rancher-desktop run ...` commands. Repo rule: tests never use string-target `monkeypatch.setattr("llm_harness....")`; they patch the module object.
7. Documentation (B4/B5): README (Storage section; a step-by-step "Postgres" section for a blank-slate user with the exact docker command, the config block, the secrets file, the `--storage-status` line, a table of every message with its fix, what happens to existing ledger data (it stays in the SQLite file and is NOT copied; the Postgres ledger starts empty), why memory is refused and what to do, security notes, undo), CHANGELOG, constitution proposals (tech-stack and mission), memory entries, roadmap bookkeeping, and CI/build review. Every README command is run once exactly as written by the tester before delivery.

Risks:

- Advisory-lock key collision with another application on the same database: one fixed constant, documented, derived from a name hash.
- `psycopg-binary` is a platform wheel; a platform without a wheel needs a source build or a different install.
- One connection per call becomes slow over a WAN; the limit is documented.
- The server's error text is never shown, so diagnosing needs the server log; this is documented.
- Not verified by the spike: a successful TLS connection, `connect_timeout` against a blackholed host, concurrent writers through the adapter, and any server other than 17.11.

Testing strategy: offline, fast and repeatable by default; live tests are opt-in with `-m live`.

### Roadmap changes proposed with this spec

The human approved these together with this spec, and they were applied on 2026-11-03 (roadmap version 23).

- (a) Applied: `storage-backends-postgres` moved from `## Later` to `## Now`.
- (b) Not needed: the reword of its `## Later` line, because the item moved to `## Now`.
- (c) Applied: `storage-backends-postgres-memory` added to `## Later`: Postgres full-text search and triggers for the memory tables (replacing the FTS5 table), lifting the refusal.
- (d) Unchanged: `storage-backends-pgvector` stays as it was (it depends on this feature).

## Affected Areas

- `src/llm_harness/storage/postgres.py` (new)
- `src/llm_harness/storage/relational.py` (protocol and `SchemaTooNewError`; expected unchanged)
- `src/llm_harness/storage/__init__.py` (`open_storage` wiring)
- `src/llm_harness/storage/status.py` (relational postgres branch)
- `src/llm_harness/store.py` (accepts the relational store; dialect-specific migrations; error tuple; disable notice)
- `src/llm_harness/cli.py` (passes `storage.relational` to `Store`)
- `src/llm_harness/config.py` (`_AVAILABLE_BACKENDS`, relational config model, memory refusal, `HarnessConfig.postgres_password`; `_PLANNED_BACKENDS` was removed in step 2)
- `src/llm_harness/secrets_file.py` (table `postgres`, key `password`, one read for both backends)
- `src/llm_harness/redact.py` (`Secret`, reused)
- `pyproject.toml` (optional extra `postgres`, `dev` group) and `uv.lock`
- `tests/test_config.py`, `tests/test_secrets_file.py`, `tests/test_storage_relational.py`, `tests/test_storage_status.py`, plus new `tests/test_storage_postgres.py` and `tests/test_live_postgres.py`
- `README.md` and `CHANGELOG.md` (documented at the documentation phase); constitution and roadmap files only through the approved proposals

## Out of Scope

- Memory on Postgres (follow-up item `storage-backends-postgres-memory`).
- Copying existing SQLite ledger data into Postgres.
- Connection pooling.
- TLS certificate options beyond what `sslmode` in the URL gives.
- pgvector (its own slice, `storage-backends-pgvector`).
- The async psycopg API.
- Postgres versions other than 17.

## Implementation Notes

### Step 1: spike (done 2026-11-03)

The facts are in Problem / Motivation. Record of the run:

- Image `postgres:17` was pulled with a 120 s cap on `docker --context rancher-desktop` and completed within the cap.
- One container on 127.0.0.1:15432 was started with `POSTGRES_PASSWORD` set to a throwaway value.
- `psycopg[binary]` ran through `uv run --no-project --with "psycopg[binary]"` under `timeout 60`; exit 0 on the first run.
- The container was removed afterwards (0 leftovers, port free).
- Script and output are in the session scratchpad, not in the repo: `pg_spike.py` and `pg_spike.out`.

### Pre-change baseline

The coordinator's last full run before any change for this feature, 2026-11-03: `timeout 420 uv run pytest -q -rs` -> 3365 passed, 9 deselected, 1 warning (the pre-existing google-genai `AiohttpClientSession` DeprecationWarning), 0 failed, 0 errors, 0 skipped, 145.69 s, exit 0. It was taken after the last code change of the previous feature, and the coordinator verified with `find src tests pyproject.toml uv.lock README.md -newer <that run's output file>` that no file has changed since. Branch `main`, with the uncommitted changes of earlier features already in the tree. The human chose to stay on `main` and to commit manually.

### Step 2: config and secrets (done 2026-11-03)

Source of facts: the engineer's report, plus these coordinator checks: the full suite, ruff, a `grep` of the call sites, a `grep` of `pyproject.toml` and `uv.lock`, and a read of the URL validation code. Facts not named as coordinator-checked are the engineer's report only. No acceptance criterion is marked as met by this step.

- Files: `src/llm_harness/config.py`, `src/llm_harness/secrets_file.py`, `src/llm_harness/storage/__init__.py`, `src/llm_harness/cli.py` (the `open_storage` call and the status branch), `pyproject.toml`, `uv.lock`, `tests/test_config.py`, `tests/test_secrets_file.py`, and new `tests/test_config_postgres.py` (76 test cases).
- `config.py`: `_AVAILABLE_BACKENDS["relational"]` is ("sqlite", "postgres"). `_PLANNED_BACKENDS` and the `backend_planned` branch were removed as dead code, because every kind's planned map was empty once postgres became available; the two planned-backend tests were removed; the unknown-backend message now lists `sqlite, postgres`. New: `_postgres_driver_installed()` (find_spec "psycopg"), `_check_postgres_url`, `_check_postgres_query`, `RelationalConfig.url` with a model check in the order url missing, path set, url format, driver installed, and `HarnessConfig.postgres_password`.
- URL rules (the coordinator read the validation code): scheme `postgresql` or `postgres`; exactly one host (a comma in the netloc is rejected); a host is required; exactly one database name (no extra path segments); port 1-65535; a password in the userinfo (a `:` there, so `user:@host` too) is rejected, while a bare `user@host` is allowed; no fragment; no whitespace or control characters. The query may hold only `sslmode` (disable, allow, prefer, require, verify-ca, verify-full) and `connect_timeout` (1-120); any other parameter is rejected, and its NAME is shown only when it is a Python identifier (a stray word could be a mistyped password); duplicates are rejected. No message echoes the url, host, user name or any value. The missing-driver message contains `uv sync --extra postgres`.
- Memory refusal: with relational postgres and `[tools] memory = true`, `load_config` raises ConfigError (`invalid config.toml: [tools] memory = true does not work with [storage.relational] backend = "postgres" yet (planned feature storage-backends-postgres-memory); use backend "sqlite" or switch memory off`). It lives in `load_config`, not in a model validator, because the existing rule "memory needs user_id" is in `builtin_tools.py` and only turns memory off with a hint, and a `HarnessConfig` validator would have produced a `models.yaml` prefix.
- `secrets_file.py`: `_KNOWN_KEYS` has `qdrant: (api_key,)` and `postgres: (password,)`; frozen dataclass `Secrets(qdrant_api_key, postgres_password)`; `load_secrets(config_dir)` replaces `load_qdrant_api_key` (one read, one permission check, one parse for both tables). One shared helper checks both secrets with the same messages (non-empty string, no control characters, no leading or trailing whitespace). `load_config` reads the file only when relational is postgres or vector is qdrant, and assigns each secret only when ITS backend is selected. Note for the README step: a password with a leading or trailing space cannot be used.
- `pyproject.toml`: extra `postgres = ["psycopg[binary]>=3.3.7"]` and the same requirement in the `dev` group (pyproject lines 30 and 46, coordinator grep). `uv.lock` changed only through `uv lock`: packages psycopg 3.3.7 and psycopg-binary 3.3.7 added, `provides-extras` is now ["remote", "postgres"], no existing package changed, no resolution-markers change (the engineer diffed the files before and after; the coordinator confirmed the two package entries and `provides-extras` by grep).
- INTERIM GUARD (to be replaced in step 3): the config accepts postgres but nothing builds a Postgres store yet, so `refuse_unwired_relational(config)` in `storage/__init__.py` raises ConfigError `[storage.relational] backend = "postgres" is not wired yet (step 3 of storage-backends-postgres)`. `open_storage` calls it first; `cli.py` prints it as `error: ...` with exit 1 and also calls it in the `--storage-status` branch. It is marked with the comment `# interim: replaced in step 3`. Scratch-HOME runs by the engineer: `--storage-status` and `--plain` both printed that error with exit 1 and created no `.local` or SQLite file (only the config files and a uv cache directory existed under the scratch home).
- Beyond the brief: the `[storage.relational]` lines in `EXAMPLE_CONFIG_TOML` (`config.py`) were updated for postgres. OPEN ITEM for step 7: the README's copy of the example config block (README Configuration section) is now OUT OF SYNC and must be made byte-identical again in the documentation step.
- Verification: the engineer's ordered runs (wiring file first): 841 passed; without the wiring file 815 passed. The coordinator's full default suite after the step: `timeout 420 uv run pytest -q -rs` -> 3466 passed, 9 deselected, 1 warning (the pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 161.22 s, exit 0 (baseline 3365). `uv run ruff check src tests` clean; `ruff format --check src tests` 144 files already formatted. Call sites (coordinator grep): `load_secrets` and `_postgres_driver_installed` are used in `config.py`; `HarnessConfig.postgres_password` has no non-test reader yet (step 3 adds it); the interim guard is used by `open_storage` and `cli.py`.
- Unverified: nothing here ran against a real Postgres (offline rules only); a mutation check was not run (time rule).

### Step 3: PostgresRelationalStore adapter (done 2026-11-03)

Source of facts: the engineer's report, plus these coordinator checks: the live run, the full suite, ruff, greps, and a full read of `src/llm_harness/storage/postgres.py`. Facts not named as coordinator-checked are the engineer's report only. No acceptance criterion is marked as met by this step.

- Files: NEW `src/llm_harness/storage/postgres.py`; `src/llm_harness/storage/relational.py` (only `SqliteRelationalStore.dialect = "sqlite"`, `RelationalStoreError`, `database_error_classes()`); `src/llm_harness/storage/__init__.py`; `src/llm_harness/cli.py`; `tests/test_config_postgres.py` (guard tests); NEW `tests/test_storage_postgres.py` (74 offline tests); NEW `tests/test_live_postgres.py` (10 live tests).
- Adapter: `PostgresRelationalStore(url, password=None, *, connect_factory=None)`, `dialect = "postgres"`, `label` (`host:port/dbname`, port 5432 when the url has none, no user, no password, no query string), `repr` `PostgresRelationalStore(<label>, password=set|none)`. Async `execute`/`query`/`migrate` run through `asyncio.to_thread`; public sync `execute_sync`, `query_sync`, `connect_sync`, `migrate_sync`. One connection per call, opened lazily: construction parses the url and connects to nothing; psycopg is imported at the first connect; a missing package raises `RelationalStoreError` with `uv sync --extra postgres`. The connection is created with keyword arguments only (`host`, `dbname`, `port`, `user` percent-decoded, `password` as `password=` only, `sslmode`, `connect_timeout` default 10, `autocommit=True`), never a conninfo string.
- `translate_sql` (public, module level): no params -> unchanged and `None`; with params -> `?` becomes `%s` outside single-quoted literals and double-quoted identifiers, every `%` becomes `%%`, and `ValueError` is raised before anything is sent when the `?` count differs from `len(params)`. Comments and dollar-quoted strings are not understood (callers' SQL is controlled program text).
- Transactions: `BEGIN IMMEDIATE` (case-insensitive, optional `;`) runs `BEGIN` then `SELECT pg_advisory_xact_lock(%s)` with `ADVISORY_LOCK_KEY = 6678241091312392647` (the literal derived as `int.from_bytes(hashlib.sha256(b"llm-harness:relational").digest()[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF`; a test recomputes it). Plain BEGIN/COMMIT/ROLLBACK pass through.
- `migrate_sync`: `BEGIN IMMEDIATE` (lock), `CREATE TABLE IF NOT EXISTS schema_versions`, read the version, `SchemaTooNewError` when the recorded version is higher, apply `migrations[version:]`, upsert with ON CONFLICT, COMMIT. On any exception it runs ROLLBACK (a rollback failure is suppressed so it cannot hide the original error; closing the connection makes the server abort) and always closes. `adopt_user_version` is accepted and ignored.
- `database_error_classes()` returns `(sqlite3.Error,)` plus `psycopg.Error` when psycopg is importable.
- Wiring: `open_storage(..., postgres_password=None)` builds `PostgresRelationalStore` for backend postgres (`storage/__init__.py`; it creates nothing and connects to nothing; a missing url raises a clear error). `cli.py` passes `postgres_password=config.postgres_password`, so `HarnessConfig.postgres_password` now has a production reader.
- NARROWED INTERIM GUARD: `refuse_unwired_relational` stays and runs in `cli.py` before `open_storage`/`Store` in the normal start and in the `--storage-status` branch, with the text `[storage.relational] backend = "postgres" is not wired for the usage ledger and --storage-status yet (steps 4 and 5 of storage-backends-postgres)`. The ledger `Store(db_path)` and the status code still only know SQLite, so without the guard the ledger would silently use a SQLite file. Because the guard runs first, the Postgres branch of `open_storage` is not yet reachable through the CLI; tests cover it, and it becomes reachable when steps 4 and 5 remove the guard. Scratch-HOME CLI run by the engineer: `--storage-status` and `--plain` print the guard and exit 1; nothing but the three config files exists afterwards.
- Intentionally unused until later steps (no non-test caller yet): `database_error_classes` (the ledger catches it in step 4) and the `dialect` attributes (step 4 selects migrations by them).
- LIVE RUN (coordinator, 2026-11-03): container `llm-harness-live-postgres` (image `postgres:17`, pulled again inside `docker run` on the `rancher-desktop` context, finished within the tool limit) with POSTGRES_PASSWORD and POSTGRES_DB=llm_harness_live on 127.0.0.1:15432, then `LLM_HARNESS_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:15432/llm_harness_live LLM_HARNESS_LIVE_POSTGRES_PASSWORD=<throwaway> timeout 90 uv run pytest -m live tests/test_live_postgres.py -q -rs` -> 10 passed in 1.55 s, exit 0. The container was removed afterwards (0 leftovers, port free). The 10 tests: `?` round trip with exact float; `%` with and without params; rowcount of update and delete; migrate creates, records and is a no-op the second time; a recorded higher version raises `SchemaTooNewError`; a failing second statement rolls back the first and the version (DDL rollback); four threads migrating one namespace create one table; `connect_sync` transactions roll back and commit; BEGIN IMMEDIATE serialises two writers so no update is lost (two threads read, wait 0.2 s, write counter+1: final counter 2); a wrong password raises a psycopg error without the password in its text (it skips if the server accepts the wrong password). This is the first check of the offline fake's assumptions against a real server, and they held.
- Verification (coordinator): the engineer's ordered runs (wiring file first): 993 passed; without it 967 passed. Full default suite `timeout 420 uv run pytest -q -rs` -> 3542 passed, 19 deselected (the 9 earlier live tests plus the 10 new ones), 1 warning (the pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 143.21 s, exit 0 (3466 after step 2, 3365 baseline). `ruff check src tests` clean; `ruff format --check src tests` 147 files already formatted. The live file is collected under `-m live` (10 tests) and deselected by default.
- Unverified: a successful TLS connection; `connect_timeout` against a blackholed host; servers other than 17; the real psycopg error text with a wrong user or database (only the wrong password is covered live). No mutation check was run (time rule).

### Step 4: usage ledger on Postgres (done 2026-11-03)

Source of facts: the engineer's report, plus these coordinator checks: the live run, the full suite, ruff, greps and a read of the Store changes. Facts not named as coordinator-checked are the engineer's report only. No acceptance criterion is marked as met by this step.

- Files: `src/llm_harness/store.py`, `src/llm_harness/cli.py`, `src/llm_harness/storage/__init__.py` (guard text and docstring only), `tests/test_config_postgres.py` (guard tests), NEW `tests/test_store_postgres.py` (51 offline tests), `tests/test_live_postgres.py` (7 live tests added; 17 live tests in total). `tests/test_store.py` and `tests/test_router.py` were NOT edited and pass unchanged.
- `Store(path: Path | None = None, *, relational=None)` takes exactly one of the two (`ValueError` otherwise). `Store(path)` builds its own `SqliteRelationalStore` as before; `relational=` takes any object with `dialect`, `migrate_sync` and `connect_sync`.
- Migrations by dialect: `_MIGRATIONS` (SQLite DDL, unchanged) and `_POSTGRES_MIGRATIONS` (`BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`, `DOUBLE PRECISION` for `cost_usd` and `seconds`, same table and column names), both in `_MIGRATIONS_BY_DIALECT`. An unknown dialect raises `ValueError` at construction naming the known ones. Both dialects have the same number of steps (a test pins it), so `expected_schema_versions()` is unchanged.
- Errors: `_database_errors()` (cached with `functools.cache`) returns `database_error_classes()` plus `OSError`, `SchemaTooNewError`, `ValueError`, `RelationalStoreError`; both `except` clauses use it. Notice text: SQLite unchanged (`usage database unavailable (<Type>): <path>; usage and backoff are not saved`); Postgres: `usage database unavailable (<Type>): postgres; usage and backoff are not saved` (the dialect name, not the label, because the label holds the host).
- The SQL strings and semantics of the ledger are unchanged. `BEGIN IMMEDIATE` on Postgres is the advisory lock, so the read-then-write in `_update_backoff` is serialised across processes.
- `cli.py`: `Store(relational=storage.relational)` for postgres, `Store(db_path)` otherwise. The normal start no longer calls `refuse_unwired_relational`; the `--storage-status` branch still does, with the text `[storage.relational] backend = "postgres" is not wired for --storage-status yet (step 5 of storage-backends-postgres)`.
- Nothing connects at startup (the chat loop reaches the ledger only through a model call). Verified by a test that patches `_default_connect` to raise and asserts it is never called, and by a scratch-HOME run `llm-harness --plain < /dev/null` with a postgres url on a dead port: exit 0, empty stderr, no `.local` directory and no SQLite file. `--storage-status` printed the step 5 guard and exit 1.
- Test pinning the SQLite behaviour: `tests/test_store_postgres.py::test_sqlite_ddl_and_notice_are_unchanged_by_the_postgres_work` (old DDL copied as a literal, `_NAMESPACE == "ledger"`, exact SQLite notice on a real `Store(path)`).
- Interim guard now covers only `--storage-status` (replaced in step 5). `database_error_classes` and the `dialect` attributes now have non-test callers in `store.py`.
- LIVE RUN (coordinator, 2026-11-03): container `llm-harness-live-postgres` (`postgres:17`, POSTGRES_DB=llm_harness_live, 127.0.0.1:15432, pulled again inside `docker run`, within the tool limit), `LLM_HARNESS_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:15432/llm_harness_live LLM_HARNESS_LIVE_POSTGRES_PASSWORD=<throwaway> timeout 120 uv run pytest -m live tests/test_live_postgres.py -v -rs` -> 17 passed in 1.69 s, exit 0: the 10 adapter tests from step 3 and 7 new ledger tests (identity id column and double precision floats via `information_schema.columns`; two inserts get increasing ids; a float round-trips exactly; a failure is stored in the backoff and a reset removes it; two threads recording a manual failure for the same model never lose an increment (5 rounds of 2 threads with a barrier; manual picks only, because the non-manual path deliberately skips a second concurrent failure while the row is still active); the `ledger` version in `schema_versions` equals `expected_schema_versions()`; a wrong password disables the ledger with a type-only notice). The ledger live tests drop `llm_calls`, `model_backoff` and the `ledger` version row before and after, so they SKIP unless the database name in the url contains `live` or `test` (safety check). The container was removed afterwards, 0 leftovers, port free.
- Verification: the engineer's ordered runs (wiring file first): 671 passed; without it 645. Coordinator: full default suite `timeout 420 uv run pytest -q -rs` -> 3574 passed, 26 deselected (the earlier 9 live tests plus 17 Postgres live tests), 1 warning (the pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 146.40 s, exit 0 (3542 after step 3); `ruff check src tests` clean, `ruff format --check src tests` 148 files already formatted.
- Unverified: the live tests passing does not prove they would FAIL without the advisory lock (no mutation check was run: time rule); a successful TLS connection and servers other than 17 were not tested; a real ledger that already holds SQLite history is not migrated to Postgres (out of scope, documented later).

### Step 5: --storage-status for relational postgres (done 2026-11-03)

Source of facts: the engineer's report, plus these coordinator checks: real CLI runs and live tests against a real container, the full suite, ruff and greps. Facts not named as coordinator-checked are the engineer's report only. No acceptance criterion is marked as met by this step.

- Files: `src/llm_harness/storage/postgres.py` (new `PostgresConnectError(RelationalStoreError)` whose text is only `cannot connect (<ExceptionType>)`; constants `STATUS_CONNECT_TIMEOUT_SECONDS = 5`, `_READ_ONLY_OPTIONS`, `_UNDEFINED_TABLE_SQLSTATE = "42P01"`; new `read_schema_versions_sync() -> dict[str, int] | None`; the keyword building moved into `_connect_kwargs()` with `connect_sync` producing the same arguments in the same order), `src/llm_harness/storage/status.py` (`storage_status` builds `PostgresRelationalStore(config.relational.url, config.postgres_password)` for postgres, prints `relational: postgres  <label>` and `_postgres_lines`, which reuses `_schema_lines`/`_unrecorded_line`/`_version_line` with `ReadOnlySchema(versions=..., user_version=0)`; no `file:` lines; the SQLite branch untouched), `src/llm_harness/storage/__init__.py` (`refuse_unwired_relational` deleted), `src/llm_harness/cli.py` (the guard import and call removed), `tests/test_config_postgres.py` (guard tests removed; three CLI tests added), NEW `tests/test_status_postgres.py` (16 offline tests), `tests/test_live_postgres.py` (5 live tests added; 22 live tests in total). `tests/test_storage_status.py` was not edited and passes unchanged.
- Behaviour: `read_schema_versions_sync` opens a connection through the same factory with `options="-c default_transaction_read_only=on"` and `connect_timeout=min(5, the store's value)`, runs only `SELECT namespace, version FROM schema_versions`, takes no advisory lock and sends no BEGIN, always closes, and returns None when the table does not exist (SQLSTATE 42P01, detected with `getattr(exc, "sqlstate", None)`). Status output: table missing -> `  schema: nothing to read yet`; connection failure -> exactly one `  cannot connect (<ExceptionType>)` line, and the command still exits 0 and still prints the vector and files lines; any other database error -> `  schema: not readable (<ExceptionType>)`; never a driver message, user name, password or url. A hand-built postgres config without a url raises `ConfigError` in `storage_status`, as `open_storage` does. A missing driver at connect time would also show as `cannot connect (<Type>)` (config validation already refuses that case).
- The interim guard is gone: `grep -rn "refuse_unwired_relational\|not wired\|interim" src tests` shows nothing (coordinator).
- REAL CLI RUNS (coordinator, 2026-11-03): container `llm-harness-live-postgres` (`postgres:17`, POSTGRES_DB=llm_harness_live, 127.0.0.1:15432), scratch HOME with a config built from the `tests/test_cli.py` constants plus `[storage.relational] backend = "postgres"` and `url = "postgresql://postgres@127.0.0.1:15432/llm_harness_live"`, `.venv/bin/llm-harness --storage-status`: (A) fresh database, right password in secrets.toml -> exit 0, `relational: postgres  127.0.0.1:15432/llm_harness_live` then `  schema: nothing to read yet`, then the vector and files lines; (B) after `Store(relational=PostgresRelationalStore(...)).record_attempt(...)` created the ledger -> `  schema: ledger version 1 (the code expects 1)`; (C) wrong password in secrets.toml -> exit 0 and one `  cannot connect (OperationalError)` line; (D) no secrets.toml while the server wants a password -> the same one line; (E) url pointing at the closed port 127.0.0.1:1 -> the same one line (timing is asserted by a live test). The password text appeared 0 times in stdout and stderr in every run; the scratch home held only the config files (no `.local`, no SQLite file).
- LIVE TESTS (coordinator, same container): `LLM_HARNESS_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:15432/llm_harness_live LLM_HARNESS_LIVE_POSTGRES_PASSWORD=<throwaway> timeout 150 uv run pytest -m live tests/test_live_postgres.py -q -rs` -> 22 passed in 1.64 s, exit 0. The five new tests: status without `schema_versions` reads nothing and creates nothing (the table set is identical before and after); status after the ledger is migrated shows its version; the status connection really refuses writes (SQLSTATE 25006); a wrong password prints one `cannot connect` line; a closed port prints one `cannot connect` line within seconds. The container was removed afterwards, 0 leftovers, port free.
- Verification (coordinator): the engineer's ordered runs (wiring file first): 597 passed; without it 571. Full default suite `timeout 420 uv run pytest -q -rs` -> 3591 passed, 31 deselected (the earlier 26 live tests plus 5), 1 warning (the pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 146.03 s, exit 0 (3574 after step 4); `ruff check src tests` clean, `ruff format --check src tests` 149 files already formatted.
- Unverified: the real text of a server `UndefinedTable` error is not needed (only the SQLSTATE is used and the live test confirms the path); no mutation check was run (time rule); a successful TLS connection and servers other than 17 are untested.

### Step 6: relational contract tests on Postgres (done 2026-11-03)

Source of facts: the engineer's report, plus these coordinator checks: the live run on a real container, the full suite, ruff and a `find` for src changes. Facts not named as coordinator-checked are the engineer's report only. No acceptance criterion is marked as met by this step.

- This step changed tests only; no file under `src/` changed. The engineer compared `git status --short src` before and after (engineer's report); the coordinator confirmed with `find src -newer <step 5 suite output>` that no src file is newer (coordinator).
- Files: `tests/test_storage_relational.py` (parametrised fixture `backend` with `sqlite` and `pytest.param("postgres", marks=pytest.mark.live)`; a rig fixture with private classes `_Rig`, `_SqliteRig`, `_PostgresRig`; all dialect differences live in the rig: per-dialect migrations (`INTEGER PRIMARY KEY` for SQLite, `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` for Postgres), raw SQL access, column listing, unique names `notes<12 hex>` / `notes<12 hex>_item` / `other<12 hex>_t`, the one backend-specific assertion for "a failing first migration leaves nothing" (SQLite: no `schema_versions` table as before; Postgres: the test's table does not exist and no row for its namespace, because the shared table may exist), `database_error` (`sqlite3.Error` or `psycopg.Error`), and a Postgres teardown that drops only the test's tables and deletes only its namespace rows, never `schema_versions` itself); NEW `tests/live_postgres.py` (a non-test module holding the shared live helpers: the two environment variable names, the 3 s reachability check, the safety rule that skips unless the database name contains `live` or `test`, `live_target()`, `scratch_target(reason)`, `ledger_target()`); `tests/test_live_postgres.py` (only the helper move).
- 12 tests were converted to run on both backends: migrate and record the version; a second migrate is a no-op; a longer list applies only the new steps; namespaces versioned independently; execute and query round trip with bound parameters; an invalid namespace rejected (9 parameters; SQLite asserts no file or directory is made, Postgres asserts the connection factory is never reached); a failing statement rolls everything back; a failing first migration leaves nothing; a recorded newer version is refused; two threads migrating one fresh namespace both succeed; twenty concurrent calls succeed; concurrent async migrations succeed. Kept SQLite-only: construction creates nothing, the three adoption tests, `a recorded row wins over user_version`, and the 0700/0600 modes test (Postgres ignores `adopt_user_version`, covered in `tests/test_storage_postgres.py`). Two test tables changed from `(a)` to `(a INTEGER)` because Postgres rejects an untyped column; no SQLite assertion was weakened.
- Counts: `tests/test_storage_relational.py` collected 26 items before the change; after it, `-m "not live"` collects 26 and `-m live` collects 20 (46 in total), so the SQLite side did not shrink.
- LIVE RUN (coordinator, 2026-11-03): container `llm-harness-live-postgres` (`postgres:17`, POSTGRES_DB=llm_harness_live, 127.0.0.1:15432), `LLM_HARNESS_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:15432/llm_harness_live LLM_HARNESS_LIVE_POSTGRES_PASSWORD=<throwaway> timeout 180 uv run pytest -m live tests/test_storage_relational.py tests/test_live_postgres.py -v -rs` -> 42 passed in 3.13 s, exit 0 (the 20 Postgres items of the shared contract tests and the 22 live tests of steps 3 to 5). Afterwards the live database held only the empty table `schema_versions` (the contract tests left no table and no row behind); container removed, 0 leftovers, port free. The Postgres rig SQL had never run before this.
- Verification (coordinator): the engineer's ordered runs (wiring file first): 378 passed, 42 deselected; without it 352 passed. Full default suite `timeout 420 uv run pytest -q -rs` -> 3591 passed, 51 deselected (the earlier 31 live tests plus the 20 new Postgres items), 1 warning (the pre-existing google-genai DeprecationWarning), 0 failed, 0 skipped, 144.01 s, exit 0 (the passed count is unchanged: the new items are deselected by default); `ruff check src tests` clean, `ruff format --check src tests` 150 files already formatted.
- Unverified: with the url unset the engineer saw all live items skip with a printed reason, and with a closed port `no postgres answered within 3 s: OperationalError` (engineer's report only); the safety rule rejecting a database whose name lacks `live` or `test` was not exercised (it needs a reachable server and a differently named database); no mutation check was run (time rule).

Next: B4 step 3 (constitution proposals need the human's approval), then B5 (CHANGELOG, memory entries, roadmap Now line removal, closeout checklist); the README is being written and verified in parallel.

Done 2026-11-03.

## Validation

B3 record, 2026-11-03. Sources: the coordinator's runs and the independent tester, which ran every README command exactly as written. The technical-writer read the tester's `errs.out` (38 blocks, every one `LEAK: False`) from `/private/tmp/claude-501/-Users-jprusaczyk-Documents-Sources-DPurge-llm-harness/29f66909-a605-46a7-ac77-5833f8c6905c/scratchpad/readme-pg/run1/` (scratchpad, not in the repo); the other files there (`errs.py`, `ledger.py`, `ledger2.py`, `sqlite_ledger.py`) were not read by the technical-writer and their figures are as reported.

### Full default suite (coordinator, after the last code change and after the documentation edits)

Command: `timeout 420 uv run pytest -q -rs`

| Item | Result |
| --- | --- |
| Passed | 3591 |
| Deselected | 51 (9 earlier live tests: 7 older plus 2 qdrant; 22 Postgres live tests; 20 Postgres items of the shared relational contract tests) |
| Failed, errors, skipped | 0, 0, 0 |
| Warnings | 1, pre-existing: google-genai `AiohttpClientSession` DeprecationWarning |
| Time, exit | 143.50 s, exit 0 |

- Baseline before any change for this feature: 3365 passed, 9 deselected. Progression: 3466 (step 2), 3542 (step 3), 3574 (step 4), 3591 (step 5, unchanged after step 6 because the new items are deselected).
- `ruff check src tests` clean; `ruff format --check src tests`: 150 files already formatted.

### Live runs on a real `postgres:17` container (PostgreSQL 17.11, psycopg 3.3.7)

- Step 3: 10 passed. Step 4: 17 passed. Step 5: 22 passed.
- Step 6 and the tester's run of `pytest -m live tests/test_live_postgres.py tests/test_storage_relational.py`: 42 passed (3.13 s and 4.22 s), 0 skipped.
- Containers removed and ports free each time.

### Real CLI runs (coordinator, scratch HOME, no model called): `llm-harness --storage-status`

- Fresh database: `schema: nothing to read yet`. After the ledger was created: `schema: ledger version 1 (the code expects 1)`.
- Wrong password, no password, closed port: each exactly one `  cannot connect (OperationalError)` line, exit 0, password text 0 occurrences.

### Independent README test (tester, 2026-11-03)

- Part 1: server start; `pg_isready` printed `no response` (exit 2) while starting, then `accepting connections` (exit 0); data survived `docker stop && docker rm` and a second `docker run` (a row came back; `postgres_data` held 24 entries).
- Part 2: four runs for `psycopg`: `uv sync --no-dev` + `uv run --no-dev`: False; `--no-dev --extra postgres`: True; `uv sync --extra postgres` + plain `uv run`: True; plain `uv sync`: True. Finding: after `uv sync --no-dev`, a plain `uv run` silently installs the dev group again (README corrected; the same correction was made for qdrant-client after the coordinator verified it in a scratch copy: `--no-dev` False, plain `uv run` installs 16 packages, then True).
- Part 3: the step 5 block as written (mode 600, content as shown); `--storage-status` lines; `psql` before the ledger (`ERROR:  relation "llm_calls" does not exist`, exit 1) and after (count 1); the usage notice against a stopped server `usage database unavailable (OperationalError): postgres; usage and backoff are not saved` (shown once); every error row of the README table compared with the real text: all MATCH (config messages with the `error: invalid config.toml: storage.relational: ` prefix, the memory refusal, the secrets messages, `cannot parse <path>: <position>`).
- Part 4: `--plain < /dev/null` with the server stopped: exit 0, empty output, no files created (nothing connects at startup).
- Part 5: the README Tests commands: 42 passed.
- Part 6: switching backends: the SQLite file untouched (size, mtime, row count, checksum) after `--storage-status` and `--plain` with the Postgres config, and shown again after switching back.
- Part 7: the README example storage block is byte-identical to `EXAMPLE_CONFIG_TOML`; no stale statement.
- README round 2 applied the tester's corrections (step 3 and the missing-extra row, the step 5 sentence, `pg_isready` and `psql` outputs, the exception type of the usage notice, the "new, empty database" qualifier, a Known limits bullet).

### Call-site check (tester, grep over src, non-test uses)

`PostgresRelationalStore` (`storage/__init__.py`, `storage/status.py`); `read_schema_versions_sync` and `PostgresConnectError` (`storage/status.py`); `load_secrets` (`config.py`); `_postgres_driver_installed` and `_check_postgres_url` (`config.py`); `database_error_classes` and `.dialect` (`store.py`); `translate_sql` and `ADVISORY_LOCK_KEY` (`storage/postgres.py` itself, used by the connection wrapper); `relational=` (`cli.py` and `storage/__init__.py`); `postgres_password` (`config.py`, `cli.py`, `storage/status.py`, `storage/__init__.py`). Every new function, type and config key has a non-test use.

### Acceptance criteria evidence

| Criterion | Evidence |
| --- | --- |
| 1 | `tests/test_config_postgres.py`, `tests/test_secrets_file.py`; the tester's `errs.out` rows (missing url, path, url with sqlite, scheme, host, database, port, several hosts, password, query parameters) all matched |
| 2 | `tests/test_config_postgres.py`; README part 2 runs (`uv sync --no-dev`: psycopg False) |
| 3 | `pyproject.toml` and `uv.lock` review in Documentation Review; wheel METADATA check |
| 4 | `tests/test_secrets_file.py`; `errs.out` secrets rows |
| 5 | password-leak tests; `errs.out` 38 of 38 `LEAK: False`; password text 0 occurrences in the real CLI runs |
| 6 | `tests/test_storage_postgres.py` |
| 7 | `tests/test_storage_postgres.py` |
| 8 | `tests/test_storage_relational.py`: offline on SQLite; live `postgres` parameter, 42 passed |
| 9 | `tests/test_store_postgres.py` plus the live ledger tests (42 passed) |
| 10 | `tests/test_store_postgres.py`; the tester's stopped-server usage notice (`OperationalError`, shown once) |
| 11 | `tests/test_status_postgres.py`, the live status tests and the real CLI runs above |
| 12 | the memory refusal test; the tester's error row for the memory refusal |
| 13 | `tests/test_config_postgres.py` `open_storage` tests; call-site check (`relational=` in `cli.py`) |
| 14 | full suite 3591 passed, 51 deselected, 0 failed; live 42 passed on a real container |

All 14 met.

### Unverified

- A successful TLS connection and any server other than PostgreSQL 17.11.
- `schema: not readable (<ExceptionType>)` and `schema: ledger not set up yet` against a live server (fakes only).
- No mutation check was ever run, so it is not proven that the lost-update tests fail without the advisory lock.
- No real model call was made.
- The "starts empty" claim was not tested on a database that never held a ledger, beyond the fresh-database status run.

## Documentation Review

Reviewed 2026-11-03 (B4). The writes that followed are recorded in `## Documentation Updates`.

### README

- Status: DONE and verified. The README Postgres documentation was written and then verified by an independent tester that ran every command as written (see `## Validation`, "Independent README test"); round 2 applied the tester's corrections, and a wording fix for qdrant-client (`--no-dev` and plain `uv run`) was made in the same README pass.
- `src/llm_harness/config.py` `EXAMPLE_CONFIG_TOML` has carried the relational postgres lines since step 2; the README example storage block is now byte-identical to it (tester, part 7).

### CHANGELOG

- Changelogs: `specs/tech-stack.md` has no `### Artifacts` subsection (grep for `### Artifacts` found nothing), so the implicit default applies: one artifact, root `.`, changelog `CHANGELOG.md`. `CHANGELOG.md` is the only changelog.
- Proposed entries (user-facing only; written in B5, not now), as `{changelog path, category, proposed entry text}`:
  - `{CHANGELOG.md, Added, "Postgres relational backend: `[storage.relational] backend = \"postgres\"` with a `url`, the optional extra `postgres` and an optional password in `~/.config/llm-harness/secrets.toml` under `[postgres]` keep the usage ledger and schema versions on a Postgres server; `--storage-status` reports it read-only; `[tools] memory = true` is refused with it (planned follow-up `storage-backends-postgres-memory`); see the README section \"Postgres\"."}`
  - `{CHANGELOG.md, Changed, "The unknown-backend error for `[storage.relational]` now lists `sqlite, postgres` (postgres was recognised but rejected before)."}`
- Considered and judged internal (no entry): `Store` taking a relational object, the `dialect` attribute, the adapter module `src/llm_harness/storage/postgres.py`, the test rigs and live helpers, the `dev` dependency group addition, `load_secrets` replacing `load_qdrant_api_key`, and the removal of `_PLANNED_BACKENDS`. None changes a command, a config key or a message a user sees (the one user-visible message change is listed under Changed).

### CI, build and release

- `{pyproject.toml, needs change: yes, reason: done in step 2: optional extra `postgres` = ["psycopg[binary]>=3.3.7"] and the same requirement in the `dev` group (pyproject lines 30 and 46)}`
- `{uv.lock, needs change: yes, reason: done in step 2: regenerated by `uv lock` only; added psycopg 3.3.7 and psycopg-binary 3.3.7; `provides-extras` is ["remote", "postgres"]}`
- Wheel contents check: done. Coordinator, 2026-11-03: `uv build --wheel --out-dir <scratch folder>` produced a wheel that contains `llm_harness/storage/postgres.py` and `llm_harness/secrets_file.py` (and the changed `storage/status.py` and `store.py`), whose METADATA has `Provides-Extra: postgres`, `Provides-Extra: remote` and `Requires-Dist: psycopg[binary]>=3.3.7 ; extra == 'postgres'` (and the qdrant-client line for `remote`); no `dist/` was left in the project.
- No CI workflow, Dockerfile, Makefile, task-runner or release script exists in the project (same `find` as for the qdrant feature, repeated 2026-11-03: none found; `.github` does not exist), so no path filter or release step needs a change.

### Constitution drift (proposals; approved and applied)

Files read: `specs/tech-stack.md` and `specs/mission.md`, searched for `postgres`, `psycopg`, `storage-backends-postgres`, `Storage interfaces`, `[storage`, `secrets`, `RelationalStore`, `PostgresRelationalStore`, `planned`. Status of every proposal: approved by the human 2026-11-03 ("Approve all nine"); applied.

1. `{specs/tech-stack.md, line 61, current: "`psycopg` 3.3.6 (PyPI latest checked 2026-10-28, requires Python >= 3.10) remains an optional uv extra candidate (not decided, not installed).", proposed: "`psycopg[binary]` (`>=3.3.7`, verified against PostgreSQL 17.11 with psycopg 3.3.7) is built: it is the optional uv extra `postgres` and is also in the `dev` dependency group.", status: approved by the human 2026-11-03; applied (tech-stack.md version 28, mission.md version 18)}`
2. `{specs/tech-stack.md, line 86, current: "`postgres` is recognised and rejected with a message naming `storage-backends-postgres`;", proposed: "`postgres` is accepted and needs `url` (`postgresql://` or `postgres://`, one host, a database name, no password in it, only `sslmode` and `connect_timeout` as query parameters; `path` is rejected with it; `url` is rejected for `sqlite`); nothing is planned-but-rejected any more;", status: approved by the human 2026-11-03; applied (tech-stack.md version 28, mission.md version 18)}`
3. `{specs/tech-stack.md, line 86, current: "An optional `<config dir>/secrets.toml` (mode 0600, `[qdrant] api_key`, known tables only) is read only for backend `qdrant` and loaded into `HarnessConfig.qdrant_api_key` as a `Secret`.", proposed: "An optional `<config dir>/secrets.toml` (mode 0600, `[qdrant] api_key` and `[postgres] password`, known tables only) is read once, only when relational is `postgres` or vector is `qdrant`; each secret is loaded only for its own backend (`Secrets` dataclass, `load_secrets`) into `HarnessConfig.qdrant_api_key` or `HarnessConfig.postgres_password` as a `Secret`.", status: approved by the human 2026-11-03; applied (tech-stack.md version 28, mission.md version 18)}`
4. `{specs/tech-stack.md, line 86, current: "and a Postgres DSN would be a `Secret` that prints as `[REDACTED:...]`.", proposed: "and the Postgres password is a `Secret` that prints as `[REDACTED:...]`.", status: approved by the human 2026-11-03; applied (tech-stack.md version 28, mission.md version 18)}`
5. `{specs/tech-stack.md, line 95, current: "Persistence: one SQLite file, by default `~/.local/share/llm-harness/harness.db` (path settable with `[storage.relational] path`), with tables `llm_calls`, `model_backoff` and `schema_versions`. It is reached through `SqliteRelationalStore` (`src/llm_harness/storage/relational.py`);", proposed: "Persistence: by default one SQLite file, `~/.local/share/llm-harness/harness.db` (path settable with `[storage.relational] path`), or a Postgres server with `[storage.relational] backend = \"postgres\"`, with tables `llm_calls`, `model_backoff` and `schema_versions`. It is reached through `SqliteRelationalStore` (`src/llm_harness/storage/relational.py`) or `PostgresRelationalStore` (`src/llm_harness/storage/postgres.py`: sync psycopg 3, one connection per call, async methods through `asyncio.to_thread`, `?` translated to `%s`, `BEGIN IMMEDIATE` mapped to `BEGIN` plus one advisory lock, migrations atomic with DDL rollback, `database_error_classes()`, `RelationalStoreError`); the Postgres ledger uses IDENTITY and DOUBLE PRECISION, `--storage-status` reads it read-only with a connect timeout of at most 5 s and prints one `cannot connect (<Type>)` line when the server is down, and `[tools] memory = true` is refused with it (planned follow-up `storage-backends-postgres-memory`: Postgres full-text search replacing the SQLite FTS5 table);", status: approved by the human 2026-11-03; applied (tech-stack.md version 28, mission.md version 18)}`
6. `{specs/tech-stack.md, line 96, current: "`RelationalStore` (SQLite),", proposed: "`RelationalStore` (`sqlite`, `postgres`),", status: approved by the human 2026-11-03; applied (tech-stack.md version 28, mission.md version 18)}`
7. `{specs/tech-stack.md, line 96, current: "Still planned: Postgres (`storage-backends-postgres`), pgvector (`storage-backends-pgvector`) and the roadmap item `memory-reindex-all`;", proposed: "Still planned: pgvector (`storage-backends-pgvector`, depends on the Postgres store), `storage-backends-postgres-memory` and the roadmap item `memory-reindex-all`;", status: approved by the human 2026-11-03; applied (tech-stack.md version 28, mission.md version 18)}`
8. `{specs/mission.md, line 21, current: "a local storage layer (an SQLite relational store, a file store and", proposed: "a local storage layer (a relational store, SQLite or an optional Postgres server (optional extra `postgres`), a file store and", status: approved by the human 2026-11-03; applied (tech-stack.md version 28, mission.md version 18)}`
9. `{specs/mission.md, line 21, current: "the usage ledger and agent memory use it;", proposed: "the usage ledger and agent memory use it (agent memory needs the SQLite relational store; with Postgres, `[tools] memory = true` is refused);", status: approved by the human 2026-11-03; applied (tech-stack.md version 28, mission.md version 18)}`

Checked and found not stale: `specs/tech-stack.md` line 13 and line 53 (the word `planned` refers to other features; line 53 says memory is "in SQLite", which stays true), line 66, 68, 80 (unrelated `planned`).

### Memory candidates for B5 (not written now)

Timestamps are placeholders to be replaced with `date -u` at the append. Duplicate check: `grep -n -i -E "psycopg|postgres|LLM_HARNESS_LIVE|BEGIN IMMEDIATE|DOUBLE PRECISION|exception type" specs/memory.md` returned no match, so candidates 1 to 3 have no duplicate. For candidate 4 the near-duplicate is the existing `[build]` entry about `qdrant-client` and the extra `remote` (memory.md line 70 on 2026-11-03); it has a different subject (another package and extra), so it is not a duplicate and the two are not merged.

- `2026-11-03T16:22:02Z [gotcha] Against PostgreSQL 17.11 and psycopg 3.3.7 the SQLite ledger DDL needs changes: INTEGER PRIMARY KEY does not autoincrement and REAL is 32-bit (use IDENTITY and DOUBLE PRECISION); ? placeholders fail and a bare % fails when parameters are passed; BEGIN IMMEDIATE is a syntax error (src/llm_harness/storage/postgres.py maps it to BEGIN plus an advisory lock).`
- `2026-11-03T16:22:02Z [gotcha] psycopg error text names host, port, user and database and constraint violations quote the failing row, so only exception type names may be shown (PostgresConnectError, the Store notice in src/llm_harness/store.py, --storage-status).`
- `2026-11-03T16:22:02Z [env] The Postgres live tests (tests/test_live_postgres.py, tests/test_storage_relational.py[postgres], helpers in tests/live_postgres.py) skip unless LLM_HARNESS_LIVE_POSTGRES_URL is set and the database name contains live or test, because the ledger tests drop llm_calls, model_backoff and the ledger version row.`
- `2026-11-03T16:22:02Z [build] The dev dependency group in pyproject.toml lists psycopg[binary], so a plain uv sync already installs it; only uv sync --no-dev needs the extra postgres.`

### Not reviewed

- `CHANGELOG.md` content beyond confirming it is the single changelog (written in B5).
- `specs/roadmap.md` and `specs/memory.md` (bookkeeping is written later by instruction).
- Any CI or release workflow: none exists, so there was nothing to read.
- Behaviour against a successful TLS connection, a blackholed host and servers other than PostgreSQL 17.11 (unverified in steps 1 to 6; not re-run in B4).

## Documentation Updates

B5, 2026-11-03.

### What was changed where

- `README.md`: written by another agent and verified by the independent tester; round 2 applied the tester's corrections (step 3 and the missing-extra row, the step 5 sentence, `pg_isready` and `psql` outputs, the exception type of the usage notice, the "new, empty database" qualifier, a Known limits bullet) and fixed the qdrant-client wording (`--no-dev` and plain `uv run`). Not edited by the technical-writer.
- Constitution (all nine proposals approved by the human 2026-11-03, applied exactly as recorded in Documentation Review, "Constitution drift"): `specs/tech-stack.md` version 27 -> 28 (seven edits), `specs/mission.md` version 17 -> 18 (two edits); both `status: approved`, `updated: 2026-11-03`.
- `CHANGELOG.md`, under `## [Unreleased]`: one line at the end of `### Added` and one line at the end of `### Changed`, both as proposed in Documentation Review, "CHANGELOG". The older `### Changed` line "`[storage.relational] backend = "postgres"` is still rejected, but the message now names `storage-backends-postgres` (it named `storage-backends-remote`)." (from the qdrant feature) was deleted on the coordinator's instruction because it contradicted the new Added entry. Lines added (category as in the file): under `### Added` the line starting "Postgres relational backend:"; under `### Changed` the line starting "The unknown-backend error for `[storage.relational]` now lists `sqlite, postgres`".
- `specs/roadmap.md`: the `## Now` line for this feature was removed; the section body is now "Nothing in progress." `version` is unchanged (Now-line edits do not bump it). No other roadmap line was touched.
- `specs/memory.md`: four entries appended, none under 400 characters, no existing line changed. Entries appended, verbatim:

```
2026-11-03T16:25:02Z [gotcha] Against PostgreSQL 17.11 and psycopg 3.3.7 the SQLite ledger DDL needs changes: INTEGER PRIMARY KEY does not autoincrement and REAL is 32-bit (use IDENTITY and DOUBLE PRECISION); ? placeholders fail and a bare % fails when parameters are passed; BEGIN IMMEDIATE is a syntax error (src/llm_harness/storage/postgres.py maps it to BEGIN plus an advisory lock).
2026-11-03T16:25:03Z [gotcha] psycopg error text names host, port, user and database and constraint violations quote the failing row, so only exception type names may be shown (PostgresConnectError, the Store notice in src/llm_harness/store.py, --storage-status).
2026-11-03T16:25:04Z [env] The Postgres live tests (tests/test_live_postgres.py, tests/test_storage_relational.py[postgres], helpers in tests/live_postgres.py) skip unless LLM_HARNESS_LIVE_POSTGRES_URL is set and the database name contains live or test, because the ledger tests drop llm_calls, model_backoff and the ledger version row.
2026-11-03T16:25:05Z [build] The dev dependency group in pyproject.toml lists psycopg[binary], so a plain uv sync already installs it; only uv sync --no-dev needs the extra postgres.
```

- Memory count after the appends: 70 entries (`grep -cE '^[0-9]{4}-' specs/memory.md`), below the 200 threshold, so no maintenance pass ran.

### B5 closeout checklist

| Item | State | Evidence |
| --- | --- | --- |
| Validation of steps recorded | true | `## Implementation Notes`: full suite 3591 passed, 51 deselected, 0 failed, 0 skipped after step 6; live tests 42 passed on a real container. |
| README written and verified by the independent tester | true | `## Validation`, "Independent README test": every command run as written, every error row MATCH, example block byte-identical to `EXAMPLE_CONFIG_TOML`; round 2 corrections applied. |
| Full-suite re-run after the documentation changes | true | Coordinator: `timeout 420 uv run pytest -q -rs` -> 3591 passed, 51 deselected, 1 warning, 0 failed, 0 errors, 0 skipped, 143.50 s, exit 0; ruff check clean, ruff format check 150 files already formatted. |
| `## Now` line removed | true | `specs/roadmap.md` `## Now` reads "Nothing in progress." |
| CI and release review recorded | true | Documentation Review, "CI, build and release" (pyproject.toml, uv.lock, wheel contents, no CI or release files). |
| Every new function, type and config key has a non-test use | true | `## Validation`, "Call-site check (tester, grep over src, non-test uses)". |
| `validate-specs` passes | true | exit 0 after the last edit (run by the technical-writer; "specs validation successful"). |

Every item is true, so `status` is `done`.
