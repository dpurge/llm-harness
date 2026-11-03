---
title: "Storage backends: remote qdrant vector store"
kind: feature
status: done
version: 1
updated: 2026-11-03
branch: main
---

## Problem / Motivation

The only vector backend is the local `QdrantEdgeVectorStore` (`src/llm_harness/storage/qdrant_edge.py`). `src/llm_harness/config.py` lists `qdrant` under `_PLANNED_BACKENDS` for the vector kind, so `backend = "qdrant"` in `[storage.vector]` is rejected with a message that names `storage-backends-remote`. A user who wants vectors on a shared or remote qdrant server cannot configure it. The first consumer of the vector store is the memory vector index (`src/llm_harness/memory_index.py`): one collection per embedding model, filtered by `user_id`.

Human decisions (2026-11-03):

- Split the original `storage-backends-remote` roadmap item into three slices: `storage-backends-qdrant`, `storage-backends-postgres`, `storage-backends-pgvector`. Qdrant goes first.
- Default tests are offline. Real qdrant runs only under `-m live` against a local docker container.
- Secrets live in a separate, optional file that is read only when a remote store is selected. As first decided the file held the qdrant URL and the api key; revised by the human on 2026-11-03 (see below): the URL is in config.toml and the file holds only the optional api key.

Assumptions confirmed by the human on 2026-11-03 (the secrets file layout):

- (a) Fixed path `~/.config/llm-harness/secrets.toml`, with no path override.
- (b) The file must have mode 0600; it is rejected if group- or world-readable.
- (c) Table `[qdrant]` with the single key `api_key`, which is optional. The later `storage-backends-postgres` slice will add a `[postgres]` table. A file that exists may contain only known tables (`qdrant`; later `postgres`) and only the key `api_key` in `[qdrant]`; typos are rejected by name, never silently ignored.
- (d) The file is read only when the vector backend is `qdrant`. No file means no key and is valid (for example an unauthenticated local qdrant). A file that exists must be a regular file with mode 0600 and valid TOML, otherwise it gives an actionable error. The api key never appears in logs or errors; the loaded key is held in `Secret` (from `src/llm_harness/redact.py`) as `HarnessConfig.qdrant_api_key`, set in `load_config`.

Revision decided by the human on 2026-11-03, after step 2 was done. The coordinator had noticed that the plan was inconsistent: step 2 built `url` in `[storage.vector]` of config.toml, while the old criterion 4 and old assumption (c) put `url` in the secrets file. The human's answer: "URL in config.toml, key optional in secrets file". What changed against the original wording of (c), (d) and criterion 4: `url` moved out of the secrets file into config.toml `[storage.vector]` (it is not a secret and was built in step 2), and the api key was made optional (a missing file used to be an error). Assumptions (a) and (b) are unchanged.

The remote backend must keep the same observable contract as the edge backend. That contract is recorded as `[gotcha]` entries in `specs/memory.md`: point ids derived with uuid5 from a fixed namespace with the original id kept in payload key `_id`; bool filter values refused; euclid scores negated so that higher is nearer; vector size and distance checked against stored metadata.

## Acceptance Criteria

1. `[storage.vector] backend = "qdrant"` is accepted by config validation when the optional extra is installed and the key `url` (built in step 2, no secret in it) is present. `qdrant` is no longer in `_PLANNED_BACKENDS`. `tests/test_config.py` covers: accepted, `qdrant` without the url key rejected with a message naming the missing key, an unknown backend still rejected, and `qdrant-edge` unchanged.
2. When `qdrant-client` is not installed, selecting `backend = "qdrant"` fails with a message that contains `uv sync --extra remote`. A bare install can still use `qdrant-edge`.
3. `pyproject.toml` declares `qdrant-client` in the optional extra `remote` and not in the base `dependencies`; it is also in the `dev` dependency group so offline tests can use the real request models (recorded in Step 4a).
4. Secrets file `~/.config/llm-harness/secrets.toml` (holds only the optional api key): no file is valid and gives no key; a file with mode 0600 and `[qdrant] api_key` is read and the key is loaded as `Secret` (`HarnessConfig.qdrant_api_key`); a group- or world-readable file (any mode with bits in 0o077) is rejected with an error that names the file and says `run: chmod 600 <path>`; an unknown table, an unknown key, a blank or non-string `api_key`, control characters, or broken TOML are each rejected with an error naming the problem and never the key value. With `backend = "qdrant-edge"` or `none` the file is never opened (tested with a garbage 0644 file causing no error).
5. No secret is ever printed: tests assert that the api key does not appear in `repr`, `str`, log output, exception messages or `--storage-status` output.
6. `QdrantRemoteVectorStore` in `src/llm_harness/storage/qdrant_remote.py` implements the `VectorStore` protocol (`ensure_collection`, `upsert`, `search`, `delete`, `close`) and raises `VectorStoreError` for failures, using the same id mapping as the edge backend.
7. One shared contract suite runs against both `QdrantEdgeVectorStore` and `QdrantRemoteVectorStore`. The remote one runs against a fake client, offline. The suite covers: upsert and search round trip, filter on a payload key, bool filter value refused, euclid score negated, `cosine` and `dot` ordering (higher is nearer), delete by original id, dimension or distance mismatch on `ensure_collection` refused, original id returned from search hits. It passes on both backends.
8. One test marked `live` runs the same operations against a local docker qdrant container. It is skipped by the default `-m "not live"` addopts.
9. `open_storage` builds `QdrantRemoteVectorStore` when the backend is `qdrant`, creates nothing and does not open the vector shard or connect at build time. A non-test call site exists (the production path through `open_storage`).
10. `--storage-status` output for the local backends (`qdrant-edge`) is unchanged; an existing test pins it.
11. The full default suite (`-m "not live"`) passes offline.
12. With an `api_key` set and an `http://` URL the store is built and one warning line is printed that says the key travels unencrypted; the warning never contains the key. With an `https://` URL, or no api key, no such warning is printed. Covered by a test.

## Approach

Memory consulted: grep -n -i "postgres|remote|psycopg|asyncpg|sqlalchemy" specs/memory.md and pyproject.toml - 0 hits, none relevant

Ordered steps. Each step is shown to the human before the next one starts.

1. Bounded spike, about 1 minute under a hard tool-enforced timeout. Start a local qdrant docker container, use the async `qdrant-client` to ensure a collection, upsert, run a filtered search and delete, then remove the container. Verify: the installed client version and API, point-id rules, filter and score semantics, exception types, and how a dimension or distance mismatch is detected. Record the result in Implementation Notes. A failure or timeout changes the plan.
2. Config: accept `backend = "qdrant"`, add a url-style key without secrets to `[storage.vector]`, remove qdrant from `_PLANNED_BACKENDS`. Add `qdrant-client` as optional uv extra `remote`. A missing extra gives an actionable message (`uv sync --extra remote`).
3. Secrets file (assumptions (a) to (d) above, as revised by the human on 2026-11-03): the url stays in config.toml `[storage.vector]` (step 2). The file `~/.config/llm-harness/secrets.toml` (the config dir that holds config.toml; mode 0600; no path override) holds only the optional api key as `[qdrant] api_key = "..."`. No file means no key and is valid. A file that exists must be a regular file, mode 0600, valid, with only known tables (`qdrant`; later slices add `postgres`) and only the key `api_key`; typos are rejected by name. It is read only when the vector backend is `qdrant`; the loaded key is held in `Secret` as `HarnessConfig.qdrant_api_key`, set in `load_config`.
4. `QdrantRemoteVectorStore` in `src/llm_harness/storage/qdrant_remote.py` implements the protocol and is wired in `open_storage` (a non-test call site is required). Same id mapping and error conventions as the edge backend.
5. Contract tests: one shared suite run against the edge backend and the remote backend (remote against a fake client, offline), plus one `live` test against a docker container.

Risks:

- The `qdrant-client` API is unverified until the spike (confidence Medium).
- A bare install cannot select qdrant; this is intended and covered by the missing-extra message.

Testing strategy: offline, fast and repeatable by default; live tests are opt-in with `-m live`.

## Affected Areas

- `src/llm_harness/storage/vectors.py` (protocol, value types and errors; expected unchanged)
- `src/llm_harness/storage/qdrant_edge.py` (reference contract; expected unchanged)
- `src/llm_harness/storage/qdrant_remote.py` (new)
- `src/llm_harness/storage/__init__.py` (`open_storage` wiring)
- `src/llm_harness/config.py` (`_AVAILABLE_BACKENDS`, `_PLANNED_BACKENDS`, `VectorConfig`)
- `src/llm_harness/redact.py` (`Secret`, reused)
- a new secrets-file reader module (name decided in step 3)
- `pyproject.toml` (optional extra `remote`)
- `tests/test_config.py` and new contract and live test files
- `README.md` and `CHANGELOG.md` (documented at the documentation phase)

## Out of Scope

- Postgres (slice `storage-backends-postgres`) and pgvector (slice `storage-backends-pgvector`).
- A remote file store.
- Migrating existing vectors between backends. Changing the embedding model already rebuilds the index via `memory-vector-recovery`.
- TLS and cluster tuning.

## Implementation Notes

Branch: `main`. The human chose to stay on main; uncommitted work from earlier features is already in the tree and the human commits manually.

### Step 1: spike (done 2026-11-03)

Setup: docker context `rancher-desktop`, image `qdrant/qdrant:latest` (server 1.20.0, pulled in about 10 s), container on `127.0.0.1:16333`, removed afterwards (0 leftovers, port free). `qdrant-client` 1.20.0 run via `uv run --no-project --with qdrant-client`, hard timeout 45 s, exit 0. Script: `qdrant_spike.py`, output: `spike.out` (both in the session scratchpad, not in the repo). The first run failed because of a mistake in the spike script (collection name case), not because of qdrant; it was fixed and re-run.

Findings:

- Scores: `cosine` and `dot` return higher = nearer (cosine a 1.0, b 0.9939, c 0.0; dot a 1.0, b 0.9, c 0.0). `euclid` returns a distance (a 0.0, b 0.1414, c 1.4142; nearest is 0.0). The remote backend must negate euclid scores like the edge backend.
- Point ids: a string id such as `"a"` is refused with HTTP 400 (value is not a valid point ID). uuid5 ids work, so the same uuid5 mapping with the original id in payload `_id` is needed.
- Filter: `Filter(must=[FieldCondition(key, match=MatchValue(value))])` works for a string payload (`user_id=u1` returned a, b) and, unlike the edge backend, also matched a bool payload correctly (`flag=True` returned a, c). Decision kept: the remote store still refuses bool filter values, so both backends keep one observable contract (the protocol types the filter as `str | int | bool`, but the edge backend refuses bool). Point the human may revisit.
- Dimension and distance: `get_collection(name).config.params.vectors` gives `VectorParams` with `size` and `distance` (here 3 and Cosine), so no `meta.json` is needed. `collection_exists(name)` exists (True for an existing collection, False for a missing one). A wrong-dimension upsert or search is HTTP 400 "Vector dimension error: expected dim: 3, got 2". Create on an existing collection is HTTP 409. A missing collection is HTTP 404. Deleting a missing id succeeds silently (status completed).
- Exceptions: HTTP errors raise `qdrant_client.http.exceptions.UnexpectedResponse` (status code in the message). A refused connection raises `qdrant_client.http.exceptions.ResponseHandlingException` ("All connection attempts failed"). Both map to `VectorStoreError`; messages must name the collection and never the URL's credentials or the api key.
- Distance enum values in `models.Distance` are capitalised ("Cosine", "Dot", "Euclid"); the protocol uses lowercase, so a mapping is needed.
- Using `api_key` over plain http makes the client emit a UserWarning ("Api key is used with an insecure connection").
- Not tested in the spike: TLS, a server that enforces an api key, timeouts on a slow server, the gRPC option, collection delete.

Open questions (UNVERIFIED):

- DECIDED by the human on 2026-11-03 (question: whether to require https for non-localhost URLs when an api key is set): when an api key is configured and the URL is plain `http://`, the remote backend ALLOWS it but prints a one-line warning that the key travels unencrypted; it never refuses on this ground. See acceptance criterion 12.
- EXPLAINED (coordinator, 2026-11-03; see Step 4b; the open wording was: "A UserWarning "Failed to obtain server version. Unable to check client-server compatibility." appeared for the client created with an api_key; the cause is not known."). Cause: `AsyncQdrantClient` starts a daemon thread at construction (`check_compatibility=True` is the default) that fetches the server version and emits this warning when that fails. Still open: whether a client against a reachable server ever emits it, and the decision on `check_compatibility` (Step 4b).

### Step 2: config (done 2026-11-03)

Files changed: `src/llm_harness/config.py`, `pyproject.toml`, `uv.lock`, `tests/test_config.py`.

- `config.py`: `_AVAILABLE_BACKENDS["vector"]` is now `("qdrant-edge", "qdrant", "none")`. `_PLANNED_BACKENDS` is now a per-kind map from backend to feature slug, holding only relational `postgres` -> `storage-backends-postgres` (the old slug `storage-backends-remote` no longer exists in the roadmap). `_check_backend` reads it.
- New `_qdrant_client_installed()` (`importlib.util.find_spec("qdrant_client")`), called from the `VectorConfig` validator for backend `qdrant` only. New `_check_qdrant_url()`. `VectorConfig` gained `url: str | None` and a model validator.
- Rules: `qdrant` requires `url` (absolute http or https with a host, valid port, no userinfo); `path` must be unset with `qdrant`; `url` must be unset with `qdrant-edge` or `none`; a missing client gives a message containing `uv sync --extra remote`. Check order in the `qdrant` branch: url missing, path set, url format, client installed. The error location is `storage.vector` (not `storage.vector.url`) and each message names the key. No message echoes the URL or any credentials.
- `pyproject.toml`: `[project.optional-dependencies] remote = ["qdrant-client>=1.20.0"]`; base dependencies unchanged.
- `uv.lock` changed only through `uv lock`: eight packages added (grpcio 1.85.1, h2 4.4.1, hpack 4.2.0, hyperframe 6.1.0, numpy 2.5.4, portalocker 4.4.2, protobuf 7.36.2, qdrant-client 1.20.0); no existing package changed. The one removed line is a `resolution-markers` entry (`python_full_version < '3.14'` split in two, needed for the new transitive dependencies). The extra was NOT installed into `.venv`.
- Verification: `uv run pytest tests/test_config.py -q` -> 368 passed, 0 failed, 0 skipped; with the storage tests (`tests/test_config.py tests/test_storage_status.py tests/test_storage*.py`) 525 passed in 6.06 s; `ruff check` clean on `config.py` and `tests/test_config.py`. The full suite was not run at this step (planned for the end; baseline 3217 passed).
- Real `find_spec` check (coordinator, 2026-11-03): `uv run python -c "from llm_harness.config import _qdrant_client_installed as f; print(f())"` printed `False` in the project venv (only `qdrant-edge-py` 0.9.0 installed); the same call under `uv run --with "qdrant-client>=1.20.0"` (a scratch overlay, project venv untouched) printed `True`. Not exercised: a real `uv sync --extra remote` into the project venv.
- Fixed in B4 (2026-11-03): `README.md` and the `EXAMPLE_CONFIG_TOML` text in `config.py` now cover the remote backend. Step 4b wired `open_storage` to build the remote store.
- Acceptance criteria 1-3 are implemented; they are recorded as met in `## Validation` (B3, 2026-11-03).
- Plan inconsistency (url in two places: config.toml in step 2, secrets file in the old criterion 4 and assumption (c)) was found by the coordinator at the start of step 3 and resolved by the human's decision of 2026-11-03 (see Problem / Motivation); the spec text was revised accordingly.

### Step 3: secrets file (done 2026-11-03)

Files changed: `src/llm_harness/secrets_file.py` (new), `src/llm_harness/config.py`, `tests/test_secrets_file.py` (new).

- `secrets_file.py`: `SECRETS_FILE = "secrets.toml"` and `load_qdrant_api_key(config_dir) -> Secret | None`. The file is opened with `os.open(O_RDONLY|O_NONBLOCK)` and the checks run on the opened descriptor (`fstat`): a file that is not a regular file, or has any mode bit in `0o077` set, is rejected with a message telling the user to run `chmod 600 <path>`. An absent file gives `None`. TOML is parsed with `tomllib`. Known tables are `{"qdrant": ("api_key",)}` (later slices add `[postgres]`); an unknown table or key is rejected by name. `api_key` must be a non-blank printable string without leading or trailing whitespace. Every failure raises `ConfigError`; no message contains the key value or file content (`tomllib` parse errors carry only a description plus line and column; the engineer tried six malformed inputs).
- `config.py`: `HarnessConfig.qdrant_api_key: Secret | None = None`. `load_config` calls `load_qdrant_api_key(config_dir)` only when `storage.vector.backend == "qdrant"`; with `qdrant-edge` or `none` the file is never opened. The import is inside `load_config` because `secrets_file` imports `ConfigError` from `config.py` (a top-level import would be circular).
- `tests/test_secrets_file.py`: 41 tests (`tmp_path`, `os.chmod`; helpers reused from `tests/test_config.py`).
- Decisions the brief did not specify (engineer): the existing `permission_warnings` was not reused (it only warns, is tied to `config.toml`, and uses other masks), so a hard check on `0o077` was written; symlinks are followed and the checks apply to the target; the directory mode is not re-checked; the whole-config masking test covers `str`, `repr` and an f-string, not `model_dump_json()` (pydantic cannot serialise `Secret`, same as `ProviderConfig.api_key`); a user-typed unknown table or key name is echoed in its error message (leaks only if someone names a key after their secret; accepted).
- Verification (coordinator, own runs): `uv run pytest tests/test_secrets_file.py tests/test_config.py tests/test_storage*.py -q` -> 564 passed, 0 failed, 0 skipped; `ruff check` clean on `secrets_file.py`, `config.py` and `tests/test_secrets_file.py`. Scratch-directory run (deleted afterwards, not `~/.config`): a 0640 file was rejected with the path and `chmod 600` and without the key; a 0600 file loaded, printed as `[REDACTED:api_key]` in `repr` and `str`, and `.reveal()` returned the value; no file gave `None`. The full suite was not re-run at this step (baseline 3217 passed; planned at the end).
- Re-checked by the technical-writer with grep on 2026-11-03: `SECRETS_FILE`, `_KNOWN_KEYS`, `load_qdrant_api_key` in `secrets_file.py`, and `qdrant_api_key` plus the conditional call in `config.py`. The remaining facts are as reported by the coordinator and were not re-run.
- Unverified: Windows (POSIX mode bits), a FIFO at the path (`O_NONBLOCK` untested), the real qdrant connection (the key is first used in step 4).
- Acceptance criteria 1-4 and 5 (key masking) are implemented in code; they are recorded as met in `## Validation` (B3, 2026-11-03).

### Step 4a: QdrantRemoteVectorStore and contract tests (done 2026-11-03)

Files changed: `src/llm_harness/storage/qdrant_remote.py` (new), `src/llm_harness/storage/vector_common.py` (new), `src/llm_harness/storage/qdrant_edge.py`, `pyproject.toml`, `uv.lock`, `tests/fake_qdrant_client.py` (new), `tests/test_qdrant_remote.py` (new), `tests/test_storage_vectors.py`, and the step 2 and step 3 tests (`tests/test_config.py`, `tests/test_secrets_file.py`; see the defect below).

- `qdrant_remote.py`: `QdrantRemoteVectorStore(url, api_key: Secret | None = None, *, client_factory=None, timeout=10)` implements the five `VectorStore` methods. The client and the request models are imported and created lazily at first use, so construction connects to nothing; a missing `qdrant-client` gives a `VectorStoreError` containing `uv sync --extra remote`. `ensure_collection` caches dimension and distance (both are then checked client-side), compares with the server's `VectorParams`, and handles an HTTP 409 race by re-reading. Writes use `wait=True`. Errors read `vector store failed on collection 'x': <ExceptionType> (HTTP n)`; a 404 on a data call reads "unknown collection ... call ensure_collection first". The url, the api key and response bodies never appear in a message; `repr` shows only `api_key=set|none`. Euclid scores are negated.
- `vector_common.py`: holds what both backends share (`ID_NAMESPACE`, value unchanged, `ORIGINAL_ID_KEY`, `NAME_PATTERN`, `check_name`, `point_id`, `checked_filter`). `qdrant_edge.py` now only imports them (no behaviour change; nothing else used the old private names).
- `pyproject.toml`: `qdrant-client>=1.20.0` was also added to the `dev` dependency group so offline tests can use the real request models; the user-facing `remote` extra is unchanged. `uv.lock` gained two lines for this and no package changed.
- Tests: `tests/fake_qdrant_client.py` is a fake server behaving per the Step 1 spike facts; it raises the real `UnexpectedResponse` and `ResponseHandlingException`. `store_factory` in `tests/test_storage_vectors.py` now has the params `qdrant_edge` and `qdrant_remote`, so every shared contract test runs on both backends. Edge-only tests stay edge-only (construction creates nothing, directories and `meta.json`, data path never shown, thread cancellation, second-process lock, and the renamed `test_edge_close_without_any_collection_creates_nothing`, which would have passed vacuously on the remote store). `tests/test_qdrant_remote.py` is new: `tests/test_qdrant_remote.py` has 36 test functions that collect as 40 tests (counted by the coordinator on 2026-11-03; the figures 29 and 42 first given by the engineer's report were wrong and were not checked when passed on).
- Defect found by the coordinator while verifying, and fixed: the step 2 and step 3 tests (and `tests/test_config.py`, `tests/test_secrets_file.py`) used string-target `monkeypatch.setattr("llm_harness....")`. After `tests/test_memory_vectors_wiring.py::test_the_gui_shows_the_notice` ran, nicegui's own `user` fixture teardown (`nicegui/testing/general.py` pops `llm_harness` and `llm_harness.gui*` from `sys.modules`) left a different `llm_harness` package object, so the string-target patches failed (1 failed, 4 errors in that order). The tests now patch the module object. Any other test that string-targets `llm_harness.*` would still break after that GUI test. The cause was found by the engineer with a probe and by reading nicegui's source; it was not bisected to a single line. The gap came from the coordinator's step 2 and 3 briefs asking only for narrow test runs.
- Verification (coordinator's own runs): `uv run pytest -q` (the full default suite, run after the fix) -> 3351 passed, 7 deselected, 0 failed, 0 skipped, 1 pre-existing third-party warning, in 139.24 s (baseline before any change was 3217 passed); the order that used to fail (wiring file first, then secrets file) -> 65 passed; `ruff check` clean on src and tests.
- Call-site evidence: `QdrantRemoteVectorStore` has no non-test call site yet (intentional until 4b wires it into `open_storage`); `vector_common` is used by both `qdrant_edge.py` and `qdrant_remote.py`.
- Re-checked by the technical-writer with grep on 2026-11-03: the class and the `uv sync --extra remote` message in `qdrant_remote.py`; `qdrant-client>=1.20.0` in both the `remote` extra and the `dev` group of `pyproject.toml`; no use of `QdrantRemoteVectorStore` in `src/` outside `qdrant_remote.py`; `vector_common` referenced in both store modules. The remaining facts are as reported by the coordinator and were not re-run.
- Unverified: real-server behaviour of the store (the fake mirrors the spike facts only; 4b's live test checks it); `KeyboardInterrupt` pass-through (not testable inside an asyncio task); a real `AsyncQdrantClient` built by the default factory is only tested with a monkeypatched class.
- The acceptance criteria are recorded as met in `## Validation` (B3, 2026-11-03).

### Step 4b: wiring, warning, status and live test (done 2026-11-03)

Files changed: `src/llm_harness/storage/__init__.py`, `src/llm_harness/cli.py`, `src/llm_harness/storage/qdrant_remote.py` (`insecure_key_warning`), `src/llm_harness/storage/status.py`, `tests/test_qdrant_wiring.py` (new), `tests/test_live_qdrant.py` (new). No existing test was edited.

- Wiring: `open_storage(config, data_dir, *, relational_path=None, qdrant_api_key=None)` builds `QdrantRemoteVectorStore(url=config.vector.url, api_key=qdrant_api_key)` for backend `qdrant`. It creates nothing and builds no client; a missing url raises `VectorStoreError`. `cli.py` passes `qdrant_api_key=config.qdrant_api_key` to it.
- Warning: `insecure_key_warning(url, api_key)` is a pure function in `storage/qdrant_remote.py`. It returns one line when a key is set and the scheme is http (case-insensitive), naming the host only, never the key, port or path; it returns nothing for https or when no key is set. `cli.py` prints it once to stderr right after the `open_storage` call. This implements the human's decision of 2026-11-03: allow http with a key, but warn.
- Status: for backend `qdrant`, `--storage-status` prints `vector: qdrant  <url>  (remote server, not contacted by --storage-status)`. It touches no local vector directory and never reads the key. The `qdrant-edge` and `none` branches are unchanged (existing tests pin them). The url is printed in full; this is safe because config validation rejects credentials in it.
- Other consumers: the engineer's grep over `src` found none besides `status.py` that needed a change; `memory_tools.py` and `memory_index.py` use the `VectorStore` protocol only. `VectorConfig.resolved_path` still returns `data_dir/vectors` for any backend; no caller uses it for qdrant.
- Tests: `tests/test_qdrant_wiring.py` has 13 tests (fakes only): `open_storage` wiring, no client built, the warning cases, one stderr line via the CLI with `--plain`, status output, the key absent from every output. `tests/test_live_qdrant.py` has 2 live tests (`pytestmark = pytest.mark.live`); it reads `LLM_HARNESS_LIVE_QDRANT_URL` and skips with a printed reason when it is unset or the server is unreachable; the module docstring holds the exact docker and pytest commands.
- Live run (coordinator, 2026-11-03): server qdrant 1.20.0 in a container named `llm-harness-live-qdrant` on 127.0.0.1:16333 via `docker --context rancher-desktop` (the image had to be pulled again inside `docker run`; it was no longer cached on that context). `LLM_HARNESS_LIVE_QDRANT_URL=http://127.0.0.1:16333 uv run pytest -m live tests/test_live_qdrant.py -q -s -rs` under `timeout 90` -> 2 passed, 1 warning in 0.96 s. The container was removed afterwards: 0 leftover containers, port free. This is the first check of the offline fake against a real server, and it passed.
- "Failed to obtain server version" warning, explained (coordinator, 2026-11-03; read in `.venv/lib/python3.13/site-packages/qdrant_client/async_qdrant_remote.py` around lines 205-240, plus a probe): `AsyncQdrantClient` starts a daemon thread at construction (`check_compatibility=True` is the default) that fetches the server version and emits that UserWarning when it fails. A probe with a keyless client against the unreachable url `http://127.0.0.1:1` and a 3 s wait emitted it once. Because it comes from a background thread, it can land after a `catch_warnings` block and then shows in pytest's warning summary, as it did in the live run (the live test's own capture showed only "Api key is used with an insecure connection." three times: two from the keyed stores, one from the refused-connection test). Not proven: whether a client against a REACHABLE server ever emits it (the live run does not separate this). Consequence for users: when the qdrant server is unreachable they see this library warning on stderr in addition to our error.
- Question (history, was open when step 4b was done): pass `check_compatibility=False` in the default client factory (no stray warning and no extra request at construction, but no client/server version-mismatch warning), or leave the default.
- DECIDED by the human on 2026-11-03: "yes" to `check_compatibility=False`. Implemented: `_default_client` passes `check_compatibility=False` (`src/llm_harness/storage/qdrant_remote.py` line 282). A regression test fails without it: the engineer set the flag to True and got `AssertionError: assert not ['Failed to obtain server version...']`, then restored False. The coordinator did not repeat that mutation run. The test is `test_the_real_client_emits_no_version_warning_for_an_unreachable_server`.
- Verification (coordinator's own runs): full default suite `uv run pytest -q` -> 3364 passed, 9 deselected (the 7 earlier live tests and the 2 new ones), 0 failed, 0 skipped, 1 pre-existing third-party warning, 137.80 s (3351 after step 4a, 3217 baseline). `ruff check src tests` clean; `ruff format --check src tests` reports 143 files already formatted. Call sites by grep: `open_storage` builds the store at `storage/__init__.py` line 106; `cli.py` passes the key at line 528 and calls `insecure_key_warning` at line 531; `load_qdrant_api_key` is called from `load_config` (`config.py` lines 605-608).
- Re-checked by the technical-writer with grep on 2026-11-03: `QdrantRemoteVectorStore(url=config.vector.url, api_key=qdrant_api_key)` at `storage/__init__.py:106`; `qdrant_api_key=config.qdrant_api_key` at `cli.py:528` and `insecure_key_warning` at `cli.py:531`; the "not contacted by --storage-status" text at `status.py:38`; `load_qdrant_api_key` at `config.py:605-608`; no `check_compatibility` in `src`. The remaining facts are as reported by the coordinator and were not re-run.
- Unverified: the keyed path against a server that enforces an api key; TLS; behaviour with a slow server; Windows.
- The acceptance criteria are recorded as met in `## Validation` (B3, 2026-11-03).

Pre-change test baseline (2026-11-03, before any code change): `timeout 300 uv run pytest -q` (pyproject `addopts` is `-m "not live" -p nicegui.testing.user_plugin`) exited 0. Last line of output: `3217 passed, 7 deselected, 1 warning in 130.87s (0:02:10)`. That is 3217 passed, 0 failed, 0 skipped, 0 errors; the 7 deselected tests are all marked `live` (opt-in). The one warning is a third-party DeprecationWarning from google.genai (`AiohttpClientSession`), pre-existing and unrelated. The working tree already held uncommitted changes from earlier features, so this baseline is of that tree.

Next: nothing; B5 is complete (see Documentation Updates).

Done 2026-11-03.

## Validation

B3 record, 2026-11-03. Sources: the independent `tester` report (it ran the commands) and the coordinator's own runs. The tester's logs are in `/private/tmp/claude-501/-Users-jprusaczyk-Documents-Sources-DPurge-llm-harness/29f66909-a605-46a7-ac77-5833f8c6905c/scratchpad/b3/` (scratchpad, not in the repo). The technical-writer read the last lines of `suite.log` (`3365 passed, 9 deselected, 1 warning in 139.65s (0:02:19)`, then `exit 0`) and `a.out`, `c.err`, `d1.err`, `bare.err`; the other logs were not read by the technical-writer and their figures are as reported.

### Full default suite (tester)

Command: `timeout 420 uv run pytest -q -rs`

| Item | Result |
| --- | --- |
| Passed | 3365 |
| Deselected | 9 (the 7 earlier live tests and the 2 new ones) |
| Failed, errors, skipped | 0, 0, 0 |
| Warnings | 1, pre-existing third-party (google-genai `AiohttpClientSession` DeprecationWarning in `tests/test_vertex_adapter.py`) |
| Time | 139.65 s |

- Baseline before any change: 3217 passed, 7 deselected. After step 4b: 3364 passed.
- The one extra test is `test_the_real_client_emits_no_version_warning_for_an_unreachable_server` (the `check_compatibility` change).

### Live test against a real server

- Server: qdrant 1.20.0 container on 127.0.0.1:16333 (`docker --context rancher-desktop`).
- Coordinator, after step 4b: 2 passed in 0.96 s.
- Tester, after the `check_compatibility=False` change: 2 passed in 1.11 s. The text "Failed to obtain server version" appears 0 times (`grep -c` gave 0, no pytest warning summary).
- The client library's own "Api key is used with an insecure connection." was printed 3 times (library warning, separate from our one-line warning).
- Container removed, no container left, port free (checked by both).

### Real CLI runs (tester)

Scratch HOME only, never the user's real config. Config copied from `tests/test_cli.py` constants with the provider `base_url` changed to `http://127.0.0.1:1` and `[storage.vector] backend = "qdrant"`, `url = "http://127.0.0.1:16333"`. No model was called (`--plain` reached EOF on stdin).

| Run | Setup | Exit | Result |
| --- | --- | --- | --- |
| a | `--storage-status`, no secrets.toml | 0 | line `vector: qdrant  http://127.0.0.1:16333  (remote server, not contacted by --storage-status)`; no local vector detail lines; no key |
| b | same, 0600 secrets.toml with the fake key `sk-test-SECRET-123` | 0 | same output; `SECRET` count 0 |
| c | same file at 0644 | 1 | `error: invalid secrets.toml: <path> is accessible by group or other users; run: chmod 600 <path>`; `SECRET` count 0 |
| d1 | `--plain < /dev/null`, key and http url | 0 | stderr exactly one line: `warning: the qdrant api key is sent unencrypted to 127.0.0.1 over plain http; use an https:// url in [storage.vector] to protect it` |
| d2 | https url | 0 | no such line |
| d3 | no secrets file, http url | 0 | no such line |
| e | second scratch HOME, no `[storage]` table, `--storage-status` | 0 | `vector: qdrant-edge  <data>/vectors` plus `not created yet`; no server touched |

After all CLI runs the server held no collection.

### Bare install (coordinator)

Closes the tester's one unverified gap (before it, the missing-extra path was covered only by tests that block or patch the import).

- `uv run --isolated --no-dev python -c "from llm_harness.config import _qdrant_client_installed as f; print(f())"` printed `installed: False`.
- `HOME=<scratch home1 with backend qdrant> uv run --isolated --no-dev llm-harness --storage-status` -> exit 1, stderr `error: invalid config.toml: storage.vector: backend "qdrant" needs the optional extra remote, which is not installed; run: uv sync --extra remote`.

### Call-site check (tester, grep over src)

Every new function, type and config key has a non-test use:

- `QdrantRemoteVectorStore`: `storage/__init__.py:106`.
- `insecure_key_warning`: `cli.py:531`.
- `load_qdrant_api_key`: `config.py:608`.
- `_qdrant_client_installed` and `_check_qdrant_url`: `config.py:331-332`.
- `qdrant_api_key`: `config.py`, `cli.py:528`, `storage/__init__.py`.
- `VectorConfig.url`: `storage/__init__.py`, `config.py`, `storage/status.py`, `cli.py`.
- `vector_common` helpers: both backends.
- `SECRETS_FILE` and `ID_NAMESPACE` are used only inside their own modules (intentional; module constants).

### Acceptance criteria (tester's mapping)

| # | Tests | Result |
| --- | --- | --- |
| 1 | `tests/test_config.py` (24 tests selected by `-k`) | passed |
| 2 | `tests/test_config.py` and `tests/test_qdrant_remote.py` (4) | passed |
| 3 | no test; checked by `git diff pyproject.toml` (extra `remote`, plus the `dev` group, see Step 4a) | met |
| 4 | `tests/test_secrets_file.py` (39) | passed |
| 5 | `tests/test_secrets_file.py`, `tests/test_qdrant_remote.py`, `tests/test_qdrant_wiring.py` | passed |
| 6 | `tests/test_qdrant_remote.py` | passed |
| 7 | `tests/test_storage_vectors.py` with both `store_factory` params (127 together with the remote file) | passed |
| 8 | `tests/test_live_qdrant.py` (2, run live) | passed |
| 9 | `tests/test_qdrant_wiring.py` | passed |
| 10 | `tests/test_storage_status.py` (20) | passed |
| 11 | the full suite | passed |
| 12 | `tests/test_qdrant_wiring.py` (13) plus the real CLI runs d1-d3 | passed |

State: all 12 met.

### UNVERIFIED (carry over)

- A server that enforces an api key (the test server was keyless; the key was only sent).
- TLS and https against a real server (only the absence of the warning was checked).
- A slow or hung server.
- Windows.
- The `--gui` and TUI paths (only `--storage-status` and `--plain` were run; they share the `cli.py` warning code before the branch).
- ruff was run by the coordinator on src and tests after step 4b (clean) and by the engineer on touched files after the follow-up, not by the tester.

Re-run after documentation changes (coordinator, 2026-11-03): `timeout 420 uv run pytest -q -rs` -> `3365 passed, 9 deselected, 1 warning in 145.69s (0:02:25)`, exit 0, 0 failed, 0 errors, 0 skipped (the warning is the pre-existing google-genai `AiohttpClientSession` DeprecationWarning; the 9 deselected are the 7 earlier live tests and the 2 new ones); `uv run ruff check src tests` -> All checks passed!; `uv run ruff format --check src tests` -> 143 files already formatted.

## Documentation Review

B4 record, 2026-11-03. Method: the `doc` skill, checked against the code and by running the README commands.

### a. Files already updated by B4 (done)

Verified by the coordinator with greps and by an independent tester who ran every README command exactly as written on 2026-11-03: all matched, with the corrections listed below.

- `README.md`: Configuration example block (lines 87-95, byte-identical to `EXAMPLE_CONFIG_TOML`); Storage section; new subsection `#### Remote qdrant (step by step)` (steps 1-10 with the exact commands, a table of 18 messages quoted from `src`, the backend-switch limit with the verified `sqlite3 ... "DELETE FROM memory_vectors;"` workaround, security notes, undo); Vector search (memory) section; Flags row; Security notes; Troubleshooting; Tests; Known limits.
- `src/llm_harness/config.py`: the vector lines of `EXAMPLE_CONFIG_TOML`.
- `src/llm_harness/storage/status.py`: a clarified comment, no code change.

What the tester found and what changed because of it:

- (i) README step 3 said plain `uv run` "still has the package" after `uv sync --extra remote`, with a scratch check as evidence. But `qdrant-client` is also in the `dev` group, so a plain `uv sync` already installs it. The text was rewritten and the four runs recorded: a: `uv sync --no-dev` -> False; b: `uv sync --no-dev --extra remote` -> True; c: `uv sync --extra remote` then plain `uv run` -> True; d: only `uv sync` -> True.
- (ii) The API-key container variable and the server's answers (HTTP 401 without or with a wrong key, 200 with the right one, qdrant 1.20.0) are now verified and documented, with a table row and a Troubleshooting entry for `vector store failed on collection '<name>': UnexpectedResponse (HTTP 401)`.
- (iii) `--storage-status` reads secrets.toml through config loading (a bad file makes it exit 1), and the README now says so.

Not run by the tester (kept as limits in README Known limits): https/TLS against a real server, a slow or hung server, other qdrant versions, the runtime vector-search-off and HTTP 404 messages through a live CLI, `/memory reindex` in a real chat.

### b. CHANGELOG

One changelog: `CHANGELOG.md`. `specs/tech-stack.md` has no `### Artifacts` subsection (grep for `Artifacts` found nothing), so the implicit single artifact applies (root `.`, changelog `CHANGELOG.md`). Proposed entries, to be written in B5 (not written now):

- `{CHANGELOG.md, Added, "Remote qdrant vector backend: `[storage.vector] backend = "qdrant"` with a `url`, the optional extra `remote`, an optional API key in `~/.config/llm-harness/secrets.toml` (mode 0600), one warning line for a key sent over plain http, and `--storage-status` showing the remote store without contacting it; see the README section "Remote qdrant"."}`
- `{CHANGELOG.md, Changed, "`[storage.relational] backend = "postgres"` is still rejected, but the message now names `storage-backends-postgres` (it named `storage-backends-remote`)."}`

Considered and judged internal (no entry): the shared `vector_common` module (a refactor with no behaviour change in `qdrant_edge.py`), the test fakes (`tests/fake_qdrant_client.py`), the `dev` dependency group addition (development only), the `status.py` comment (no code change).

### c. CI, build and release

- `{pyproject.toml, needs change: yes, reason: done in step 2 and 4a: optional extra `remote` with `qdrant-client>=1.20.0`, same package in the `dev` group. Verified by the coordinator on 2026-11-03 by building a wheel to a scratch folder with `uv build --wheel --out-dir <scratch>`: it contains `llm_harness/secrets_file.py`, `llm_harness/storage/qdrant_remote.py` and `llm_harness/storage/vector_common.py`, and its METADATA has `Provides-Extra: remote` and `Requires-Dist: qdrant-client>=1.20.0 ; extra == 'remote'`; no `dist/` was left in the project}`
- `{uv.lock, needs change: yes, reason: done, regenerated by `uv lock` only; additions plus one `resolution-markers` split that the human kept on 2026-11-03}`
- No CI workflow, Dockerfile, Makefile, task-runner or release script exists in the project (the coordinator ran `find . -maxdepth 2` for Makefile, Dockerfile*, *.yml, *.yaml, Jenkinsfile, tox.ini, noxfile.py, .gitlab-ci*, justfile and Taskfile* outside `.venv` and `.git` and found none; `.github` does not exist), so no path filter or release step needs a change. The validator's CI rule (`CI_DECISION_RE`, `needs change: yes|no`) is satisfied by the two records above; the "CI review: not applicable" form is not used because the project has build files.

### d. Constitution drift (all seven approved by the human 2026-11-03 and applied)

Read `specs/tech-stack.md` and `specs/mission.md` with grep for `storage-backends-remote`, `qdrant`, `[storage`, `Secret`, `secrets`. Stale statements:

1. `{file: specs/tech-stack.md, line: 61, current: "`psycopg` 3.3.6 (PyPI latest checked 2026-10-28, requires Python >= 3.10) and `qdrant-client` 1.20.0 (same check, requires Python >= 3.10) remain optional uv extras (candidates, not decided, not installed).", proposed: "`qdrant-client` (`>=1.20.0`, verified against server 1.20.0) is built: it is the optional uv extra `remote` and is also in the `dev` dependency group, so a plain `uv sync` installs it and only `--no-dev` needs the extra. `psycopg` 3.3.6 (PyPI latest checked 2026-10-28, requires Python >= 3.10) remains an optional uv extra candidate (not decided, not installed).", status: "approved by the human 2026-11-03; applied (tech-stack.md version 27, mission.md version 17)"}`
2. `{file: specs/tech-stack.md, line: 86, current: "`[storage.vector]` (`backend`, `path`)", proposed: "`[storage.vector]` (`backend`, `path`, and `url` for `qdrant` only)", status: "approved by the human 2026-11-03; applied (tech-stack.md version 27, mission.md version 17)"}`
3. `{file: specs/tech-stack.md, line: 86, current: "`postgres` and `qdrant` are recognised and rejected with a message naming `storage-backends-remote`.", proposed: "`postgres` is recognised and rejected with a message naming `storage-backends-postgres`; `qdrant` is accepted and needs `url` (http or https with a host, no credentials; `path` is rejected with it; `url` is rejected for `qdrant-edge` and `none`). An optional `<config dir>/secrets.toml` (mode 0600, `[qdrant] api_key`, known tables only) is read only for backend `qdrant` and loaded into `HarnessConfig.qdrant_api_key` as a `Secret`.", status: "approved by the human 2026-11-03; applied (tech-stack.md version 27, mission.md version 17)"}`
4. `{file: specs/tech-stack.md, line: 96, current: "Storage interfaces (built, local backends only): `RelationalStore` (SQLite), `VectorStore` (`qdrant-edge`, or `none`) and `FileStore` (`local`), in", proposed: "Storage interfaces (built): `RelationalStore` (SQLite), `VectorStore` (`qdrant-edge`, remote `qdrant` through `QdrantRemoteVectorStore` in `src/llm_harness/storage/qdrant_remote.py` with helpers shared by both stores in `storage/vector_common.py`, or `none`) and `FileStore` (`local`), in", status: "approved by the human 2026-11-03; applied (tech-stack.md version 27, mission.md version 17)"}`
5. `{file: specs/tech-stack.md, line: 96, current: "Still planned: Postgres and remote qdrant (`storage-backends-remote`);", proposed: "Still planned: Postgres (`storage-backends-postgres`), pgvector (`storage-backends-pgvector`) and the roadmap item `memory-reindex-all`;", status: "approved by the human 2026-11-03; applied (tech-stack.md version 27, mission.md version 17)"}`
6. `{file: specs/tech-stack.md, line: 96, current: "The `VectorStore` has its first consumer, the memory index (collection per embedding model, filter `user_id`).", proposed: "The `VectorStore` has its first consumer, the memory index (collection per embedding model, filter `user_id`). The default remote client is built with `check_compatibility=False` (human decision of 2026-11-03: it avoids the library's background version-check thread and its stray warning).", status: "approved by the human 2026-11-03; applied (tech-stack.md version 27, mission.md version 17)"}`
7. `{file: specs/mission.md, line: 21, current: "a qdrant-edge vector store, chosen in `[storage]` in `config.toml`", proposed: "a qdrant-edge vector store or a remote qdrant server (optional extra `remote`), chosen in `[storage]` in `config.toml`", status: "approved by the human 2026-11-03; applied (tech-stack.md version 27, mission.md version 17)"}`

Not caused by this feature, noticed while reading: `specs/mission.md` line 21 ends its "What does not exist yet" list with "and vector search over memory", which contradicts the same line's "optional vector (meaning-based) search" clause. No proposal is made here; it belongs to the earlier memory-vector features.

Checked and still true (no proposal): `specs/tech-stack.md` line 88 (`Secret` prints as `[REDACTED:api_key]`) and the `api_key_env` pattern for providers.

### e. Memory candidates for B5 (not written now)

Format check: `tail -3` and grep of `specs/memory.md` (entries are `YYYY-MM-DDTHH:MM:SSZ [tag] text`). Timestamps are placeholders to be set with `date -u` at append time.

1. `<ts> [gotcha] The nicegui `user` fixture teardown (nicegui/testing/general.py) pops `llm_harness*` from `sys.modules`, so after a GUI test that uses it a string-target `monkeypatch.setattr("llm_harness.x.Y", ...)` raises `AttributeError: module 'llm_harness' has no attribute 'config'`; patch the module object (`import llm_harness.x as m; monkeypatch.setattr(m, "Y", ...)`).`
   Duplicate check: near-duplicate at `specs/memory.md:64` (same symptom and the same remedy, but it blames `tests/test_providers*.py` and says "cause not confirmed"). This entry gives the confirmed cause; propose it as superseding line 64 in a maintenance pass (criterion 2, same tag and subject), not as an independent duplicate.
2. `<ts> [gotcha] Against qdrant server 1.20.0 and qdrant-client 1.20.0: euclid scores are distances; string point ids are refused with HTTP 400 (use uuid5 ids); `AsyncQdrantClient` starts a daemon thread at construction that emits "Failed to obtain server version" when the version check fails, so `src/llm_harness/storage/qdrant_remote.py` passes `check_compatibility=False`.` (about 330 characters; split into two lines if the validator warns above 400.)
   Duplicate check: `specs/memory.md:34` covers euclid only for qdrant-edge-py (not the server) and says nothing about string ids or the thread; no grep hit for `check_compatibility`, `Failed to obtain`, `daemon thread` or `HTTP 400`. Not a duplicate; the euclid clause overlaps in part with line 34.
3. `<ts> [gotcha] The memory index keeps "indexed" markers in SQLite (table `memory_vectors`), so changing the `[storage.vector]` backend or server leaves them stale and `/memory reindex` reports 0 of 0; the README documents a workaround and the roadmap item `memory-reindex-all` tracks the fix.`
   Duplicate check: grep for `memory_vectors`, `reindex`, `indexed` hits lines 60-62 (index off after failure; `resume()`; collection naming), none about stale markers after a backend or server change. Not a duplicate.
4. `<ts> [build] The `dev` dependency group in `pyproject.toml` lists `qdrant-client`, so a plain `uv sync` already installs it; only `uv sync --no-dev` or an installed package needs the extra `remote`.`
   Duplicate check: grep for `dev group`, `dependency-groups`, `--no-dev`, `extra remote` found no hit. Not a duplicate.

### f. Not reviewed

- `CHANGELOG.md`, `specs/memory.md`: written in B5 by instruction.
- `specs/tech-stack.md`, `specs/mission.md`: read only at review time; the proposals in section d were later approved and applied (see Documentation Updates).
- Docs of other features (`specs/features/*.md` other than this one): not read; they are history and not user-facing.
- `docs/`, `doc/`: neither directory exists (`ls` failed for both), so nothing to check.
- Windows behaviour, https/TLS against a real server, slow servers, other qdrant versions: not run by anyone (see section a).
- README line-by-line re-run: done by the independent tester, not repeated by me.

## Documentation Updates

B5, 2026-11-03.

### What was changed where

- `README.md`: updated in B4 (not touched in B5): Configuration example block, Storage, `#### Remote qdrant (step by step)`, Vector search (memory), Flags, Security notes, Troubleshooting, Tests, Known limits. See Documentation Review section a.
- `src/llm_harness/config.py`: vector lines of `EXAMPLE_CONFIG_TOML` (B4). `src/llm_harness/storage/status.py`: clarified comment, no code change (B4).
- `CHANGELOG.md`, under `## [Unreleased]`: one line at the end of `### Added` ("Remote qdrant vector backend: ...") and one line at the end of `### Changed` ("`[storage.relational] backend = "postgres"` is still rejected, but the message now names `storage-backends-postgres` (it named `storage-backends-remote`)."). Both as proposed in Documentation Review section b; `### Changed` already existed.
- Constitution (all seven proposals approved by the human 2026-11-03, applied exactly as recorded in Documentation Review section d): `specs/tech-stack.md` version 26 -> 27 (six edits), `specs/mission.md` version 16 -> 17 (one edit); both `status: approved`, `updated: 2026-11-03`. Left alone, out of scope: the "vector search over memory" wording in `specs/mission.md`.
- `specs/roadmap.md`: the `## Now` line for this feature was removed; the section body is now "Nothing in progress." (the skill gives no wording for an empty `Now`; the wording is the technical-writer's). `version` stays 22 (the skill says Now-line edits do not bump it). No other roadmap line was touched.
- `specs/memory.md`: the memory-format skill makes the file append-only and allows removing a superseded entry only in a maintenance pass (criterion 2), so the old line `2026-11-03T12:17:40Z [gotcha] A test that patches a module constant by string path ... cause not confirmed ...` was NOT replaced or deleted. The confirmed cause was appended instead, and the second new line names that older entry so a later pass can supersede it. Candidates 1 and 2 were each split in two for the 400-character limit. Entries appended, verbatim:

```
2026-11-03T14:44:43Z [gotcha] The nicegui user fixture teardown (nicegui/testing/general.py) runs sys.modules.pop for every registered page route whose function is not in tests., which removes llm_harness, llm_harness.gui and llm_harness.gui.app; later imports build a new llm_harness package object (confirmed).
2026-11-03T14:44:43Z [gotcha] After a test that used the nicegui user fixture, a string-target monkeypatch.setattr("llm_harness.x.Y", ...) raises AttributeError (llm_harness.x stays in sys.modules but is not an attribute of the new package); same symptom as the 2026-11-03T12:17:40Z entry on tests/test_providers*.py, whose cause is the fixture teardown; patch the module object instead.
2026-11-03T14:44:44Z [gotcha] Against qdrant server 1.20.0 and qdrant-client 1.20.0: euclid scores are distances, so the remote store negates them; string point ids are refused with HTTP 400, so ids are uuid5 with the original id in payload _id (src/llm_harness/storage/vector_common.py).
2026-11-03T14:44:45Z [gotcha] AsyncQdrantClient starts a daemon thread at construction that emits the warning "Failed to obtain server version" when the version check fails; src/llm_harness/storage/qdrant_remote.py passes check_compatibility=False in _default_client.
2026-11-03T14:44:46Z [gotcha] The memory index keeps indexed markers in SQLite (table memory_vectors), so changing the [storage.vector] backend or server leaves them stale and /memory reindex reports 0 of 0; the README documents a workaround and the roadmap item memory-reindex-all tracks the fix.
2026-11-03T14:44:46Z [build] The dev dependency group in pyproject.toml lists qdrant-client, so a plain uv sync already installs it; only uv sync --no-dev needs the extra remote.
```

- Memory count after the appends: 66 entries (`grep -cE '^[0-9]{4}-' specs/memory.md`), below the 200 threshold, so no maintenance pass ran.

### B5 closeout checklist

| Item | State | Evidence |
| --- | --- | --- |
| Validation recorded with counts and skips explained | true | `## Validation`: 3365 passed, 9 deselected (live, opt-in), 0 failed, 0 errors, 0 skipped. |
| Full-suite re-run after documentation edits | true | Coordinator, 2026-11-03: `timeout 420 uv run pytest -q -rs` -> 3365 passed, 9 deselected, 1 warning in 145.69s, exit 0, 0 failed, 0 errors, 0 skipped; ruff check and format check clean. Recorded in `## Validation`. |
| README, CHANGELOG and docs updated | true | README in B4 (section a); CHANGELOG lines above; `docs/` and `doc/` do not exist. |
| `## Now` line removed | true | `specs/roadmap.md` `## Now` reads "Nothing in progress." |
| CI and release review recorded | true | Documentation Review section c (pyproject.toml, uv.lock, no CI or release files). |
| Every new function, type and config key has a non-test use | true | `## Validation`, "Call-site check (tester, grep over src)". |
| `validate-specs` passes | true | exit 0 after the last edit (run by the technical-writer; "specs validation successful"). |

Every item is true, so `status` is `done`.
