---
title: "Agent memory: vector (meaning-based) search"
kind: feature
status: done
version: 1
updated: 2026-11-03
branch: main
---

## Problem / Motivation

Keyword search (FTS5 with bm25, stop words dropped, `MemoryStore.search` at `src/llm_harness/memory.py:269`) misses matches by meaning. Sample: the message "what can I cook for her, she has a diet" becomes the query `"cook" OR "diet"` (checked 2026-11-03 with `_match_expression`) and does not find the note "Partner is allergic to peanuts" (keywords: allergy, partner). This is a known weak point already recorded in `specs/features/memory.md`. That feature's Out of Scope lists "Vector or embedding retrieval (a separate later feature)"; this is that feature.

Prerequisites are done: `embeddings` (`Embedder`) and `storage-config` (`VectorStore`, qdrant-edge). No production code uses the `Embedder` yet; this feature is its first consumer. So the deferred item from `specs/features/embeddings.md` Out of Scope ("Storing the embedding model id per vector collection and refusing a model mismatch: moves to the first consumer feature") lands here.

No embedding model is active by default: the `bge-m3-local` entry is commented out in the example `models.yaml` (`src/llm_harness/config.py:854-860`). `ollama list` on the user's machine shows `bge-m3` pulled.

Decisions made by the user (2026-11-03):

1. Ranking: hybrid. FTS5 and vector search both run and are merged by reciprocal rank fusion (score `1/(60+rank)`, keyword rank wins ties). This applies to all three readers: the `memory_search` tool, automatic recall, and reflection's closest 5. `k` max 10, `RECALL_K` 5 and the 6000-character cap are unchanged.
2. Switch: a new `[tools] memory_vectors` (StrictBool, default false) that needs an `embedding` model. Any failure (no embedding model, provider error, vector lock held by another process, disabled backend, model mismatch) shows one notice and search continues with keywords only. A memory write never fails because of the index.
3. Consistency: SQLite is the truth and the vector index is rebuildable. A failed vector write leaves the entry unindexed. A `/memory reindex` command and lazy repair re-embed missing or stale entries. The embedding model id is stored per collection and a mismatch is refused.
4. Scope of the first feature: index, hybrid search, flag, fallback and reindex. Tests are offline only (fake embedder, in-test fake vector store). There is no minimum-score threshold and no real-model measurement; both are recorded as limits. A real `bge-m3` check happens only if the user asks.

Open points (assumptions, the user may change them):

- (a) Lazy repair embeds at most 20 unindexed rows of the user per process, on the first search.
- (b) The vector collection name is `memories_` plus the first 16 hex characters of `sha256(model id)`. Changing the embedding model therefore creates a new collection that a reindex fills (the `VectorStore` has no drop; the dimension is fixed on disk).
- (c) When the off-reason arises during `/memory`, the one-line notice is also appended to that command's answer.
- (d) `memory_vectors = true` with `memory = false` gives a registry note (shown like the existing memory notes), not a `ConfigError`.
- (e) After the first failure the index stays off for the rest of the process.

## Acceptance Criteria

1. Flag off (the default) or `user_id` is None: no index, no embed call, behaviour and all existing memory tests unchanged.
2. On save, update and forget with the flag on: SQLite first, then one vector upsert or delete. The point id is `str(memory id)`; the payload holds only `user_id` as a str; the embedded text is `<text> [<keywords joined by ', '>]`. Secrets are refused before anything is embedded (existing check).
3. Failure of the index on a write: the memory is still stored and returned, the entry stays unindexed, and one notice is shown.
4. Unindexed tracking lives in SQLite (memory migration 2): a marker table `memory_vectors(collection, memory_id)`. Triggers delete the marker when `text` or `keywords` change or the row is deleted, so stale-marking is atomic with the write.
5. The model id per collection is stored in SQLite `memory_vector_meta(collection, model_id, dim)`. A different model id or dimension for an existing collection is refused with a notice. The storage layer is not changed.
6. Search takes up to 10 keyword ids and up to 10 vector hits (filter `user_id`), fuses them by RRF and cuts to the clamped `k`. Vector-only ids are loaded through SQLite `get(user_id, id)`, so orphans and other users' rows are dropped.
7. Pinned sample: with the fake concept embedder, the message "what can I cook for her, she has a diet" recalls "Partner is allergic to peanuts", and base keyword search returns nothing for it.
8. Isolation: every vector query carries the user filter. A two-user test shows neither user sees the other's entries.
9. Failure of the index on a read, no embedding model, lock held (`VectorStoreLockedError`) or disabled backend gives a keywords-only result and exactly one notice `memory vector search is off: <reason>; keyword search only`, shown in the CLI, TUI and GUI as a Notice after recall or reflection.
10. `/memory reindex` re-embeds every unindexed or stale entry of the user and answers `reindexed N of M memories`. With the flag off it answers `vector search is off: set [tools] memory_vectors = true`. It is idempotent. Lazy repair follows assumption (a).
11. The embedder is built lazily on first use from `select_embedding_model(config, None)`; startup never fails because of it.
12. Tests are offline, fast and deterministic: real SQLite on `tmp_path`, plus a fake concept embedder and a fake vector store in `tests/fakes.py` that mirror the real rules (`ensure_collection` before use, dimension check, bool filters refused, scripted `VectorStoreLockedError` and `VectorStoreError`).

## Approach

Memory consulted: grep of specs/memory.md for qdrant|vector|embed|edge — 10 relevant entries (string ids rejected, second process fails with WouldBlock, open_storage must not open the shard, bool filter refused), none blocking

A subclass `IndexedMemoryStore(MemoryStore)` in the new `src/llm_harness/memory_index.py` overrides save, update, forget and search. `MemoryTools.store_for` (`src/llm_harness/memory_tools.py:90`) returns it when the flag is on. Every reader and `apply_reflection` already gets its store there (`memory_tools.py:131`, `orchestrator.py:506` and `:553`, `memory_command.py:170`), so they do not change except for passing `storage` instead of `storage.relational`.

- A pure function `fuse_ranks(keyword_ids, vector_ids, k)` does the RRF merge.
- A one-time notice is exposed through `take_notice()`; the orchestrator yields it after `_recall` and `_reflect`.
- `cli.py` builds the lazy `embedder_factory`: `select_embedding_model`, the provider factory, a check that the provider has `embed`, wrapped in `Embedder`.
- `build_tool_registry` gets `memory_vectors` and `embedder_factory`.

Ordered slices, each runnable and tested alone:

- S1: fakes (`ConceptEmbedder`, `FakeVectorStore` in `tests/fakes.py`), `fuse_ranks` and tests.
- S2: migration 2, index writes, model-id check and tests. A v1 database migrates; the tests also confirm that the trigger syntax `AFTER UPDATE OF text, keywords` coexists with the FTS triggers `memories_au`.
- S3: hybrid search, the pinned sample and the failure fallback.
- S4: flag and wiring (`config.py` after the `memory` flag near line 315, `builtin_tools.py`, `memory_tools.py`, the three call sites, `cli.py`, the orchestrator notice) and a State tab note.
- S5: the reindex command and lazy repair in `memory_command.py` (`USAGE` gets one line).
- S6: docs (README, CHANGELOG, example config comment).

Risks:

- The first save or search adds embed latency (a real network call; lazy repair is capped at 20). Not measured here.
- Recall feeds vector-only hits to the model. Stored text is untrusted: `scrub_block_text` is unchanged and the user filter applies on both sides.
- Orphan vectors after a failed delete, and old-model collections, stay on disk (no cleanup; orphans are dropped on load).
- There is no minimum score, so a weak vector match can fill recall slots.
- A transient provider error switches the index off until the process restarts (assumption (e)); `/memory reindex` does not switch it back on and answers `vector search is off: <reason>`. The next process repairs the unindexed entries.
- There is no atomicity between SQLite and the vector write; a crash leaves an unindexed entry that is repaired later.
- Qdrant thread-safety is unverified; the adapter serialises calls with one lock (`src/llm_harness/storage/qdrant_edge.py:112`).
- A second process holding the shard lock disables vectors there (accepted: "One process per vector collection", `specs/features/storage-config.md:109`).

Security: the payload holds only `user_id`; secrets are refused before embedding (`_check_secret_free`, `src/llm_harness/memory.py:116-122`).

Both were proven by the S2 tests (see S2).

## Affected Areas

- New: `src/llm_harness/memory_index.py`, `tests/test_memory_index.py`.
- Edited: `src/llm_harness/memory.py` (migration only), `memory_tools.py`, `memory_command.py`, `orchestrator.py`, `builtin_tools.py`, `config.py`, `cli.py`, `tests/fakes.py`; `inspect_state.py` or the TUI/GUI state notes if needed.
- Docs: `README.md`, `CHANGELOG.md`.
- Constitution `specs/mission.md` and `specs/tech-stack.md` change only through the normal B4 review, not now.

## Out of Scope

- A minimum-score threshold.
- Real-model measurement and tuning (including multilingual quality).
- Cleanup or deletion of old-model collections.
- Per-user caps.
- Qdrant server or other backends.
- Changing the `VectorStore` or `Embedder` interfaces.
- Automatic reindex at startup.
- Reflection or recall prompt changes.

## Implementation Notes

Spec approved 2026-11-03. Baseline before S1: full suite 3086 passed, 0 failed, 0 skipped, 7 deselected (marker live), from the closeout of the memory feature the same day; no code changed between that run and this start. Stayed on main, as for the memory feature. S1 to S6 done, see the steps below.

### S1, fakes and rank fusion (2026-11-03)

Files:

- New `src/llm_harness/memory_index.py`: only `fuse_ranks(keyword_ids, vector_ids, k)`. Pure reciprocal rank fusion with `RRF_OFFSET` 60, rank counted from 1. Ties go to keyword order, then vector order. A duplicate id is counted once, at its first position. `k < 1` returns `[]`. Returned ids are unique.
- New `tests/test_memory_index.py`: 29 tests.
- Appended to `tests/fakes.py`: `ConceptEmbedder` (fake `EmbeddingProvider`, dimension 4: food, person, travel, bias) and `FakeVectorStore` (in-memory `VectorStore` that mirrors the rules in `src/llm_harness/storage/qdrant_edge.py`: `ensure_collection` first, dimension/distance mismatch, bool filter refused, `_id` payload key refused, user filter, scripted failures via `fail_on`, records calls). The only edit to existing lines of `tests/fakes.py` is the `collections.abc` import line, extended, plus the `math` and `re` imports.

Checked by the coordinator (not only the engineer's report) on 2026-11-03:

- `uv run pytest tests/test_memory_index.py -q`: 29 passed, 0 failed, 0 skipped.
- `uv run ruff check src tests`: all checks passed.
- `ruff format --check` on the 3 files: already formatted.
- The engineer also ran `uv run pytest tests/test_memory*.py tests/test_storage*.py -q`: 592 passed, 0 skipped, 0 failed.

Call sites: `fuse_ranks` has no non-test caller until S3 (intended; wired there). `ConceptEmbedder` and `FakeVectorStore` are test doubles used in `tests/test_memory_index.py` and in the later slices.

Not verified:

- The fake vector store was written from the source of `qdrant_edge.py` and was not compared with the real adapter at runtime.
- Criterion 7 is covered here only as a cosine comparison between the sample query and the allergy text; the "base keyword search finds nothing" half is tested in S3.
- Criteria 6 and 8 need the real index (S2/S3).

### S2, migration 2 and index writes (2026-11-03)

Files:

- `src/llm_harness/memory.py`: migration 2 (lines 62-77). Tables `memory_vectors(collection, memory_id, PK(collection, memory_id))` and `memory_vector_meta(collection PK, model_id, dim)`. Triggers `memories_vectors_stale AFTER UPDATE OF text, keywords` and `memories_vectors_gone AFTER DELETE`, both deleting the memory's marker rows.
- `src/llm_harness/memory_index.py`: `IndexedMemoryStore(MemoryStore)` overrides save, update and forget. `collection_name(model_id)` is `memories_` plus the first 16 hex characters of the sha256 of the model id. On a write: SQLite first, then embed `<text> [<keywords>]` of the stored record, `ensure_collection` once per process, upsert one point with id `str(memory id)` and payload `{"user_id": ...}`, then insert a marker row. Any index failure (embedding error, `VectorStoreError`, `VectorStoreLockedError`, mismatch, `embedder_factory` exception) records `index_off_reason` (redacted) and switches the index off for the process; `take_notice()` then returns `memory vector search is off: <reason>; keyword search only` once. A model or dimension mismatch against `memory_vector_meta` is refused with "embedding model or dimension changed for this collection". The index never changes the write result. `CancelledError` passes through.
- `tests/test_memory_index_store.py`: 22 tests (new).
- `tests/test_storage_status.py`: one expected string changed from "memory version 1" to "memory version 2", because the pin test migrates with the full `MEMORY_MIGRATIONS`. `optional_schema_versions()` in `store.py` uses `len(MEMORY_MIGRATIONS)` and needed no edit.

Deviations from the brief:

- `forget` in a fresh process builds the embedder through the factory (no embed call), reads the dimension from `memory_vector_meta`, ensures the collection and deletes the point. With no meta row it makes no vector call (tested).
- The `IndexedMemoryStore` constructor takes `clock` as an optional fourth argument.

Resolved from the "Unverified until S2" item in Approach: the collection-name hashing and the coexistence of `AFTER UPDATE OF text, keywords` with the FTS triggers `memories_ai`, `memories_ad` and `memories_au` are proven by tests. A version-1 database migrates to 2 with its row intact. A direct SQL UPDATE of text or keywords, and a DELETE, clear the marker. An UPDATE of only `updated_at` does not.

Checked by the coordinator by running the commands on 2026-11-03:

- `uv run pytest tests/test_memory*.py tests/test_store*.py tests/test_storage*.py -q`: 646 passed, 0 failed.
- The engineer's own run of the two new test files: 49 passed, 0 skipped.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: clean.

Call sites: `IndexedMemoryStore`, `take_notice` and `index_off_reason` have no non-test caller until S4 (intended).

Not verified:

- The real qdrant adapter and a real embedding provider: only fakes were used.
- The new tests were not mutation-checked.
- Real embedder factories were not confirmed cheap to call on `forget`.

### S3, hybrid search (2026-11-03)

Files:

- `src/llm_harness/memory_index.py`: `IndexedMemoryStore.search(user_id, query, k=5)`. It takes up to 10 keyword hits from `super().search`. Then, with the index on and a non-blank query, it embeds the query (capped at `MAX_QUERY_EMBED_CHARS = 1000`) and runs a vector search with `VECTOR_SEARCH_LIMIT = 10` and `filter={"user_id": user_id}`. The two lists are merged with `fuse_ranks` and cut to the clamped k (1..10, a local copy of the clamp). Vector-only ids are loaded with `get(user_id, id)` and dropped when missing (an orphan, or another user's row), so a result can be shorter than k. Any index failure turns the index off as in S2 (`CancelledError` passes) and the result is keyword-only. With the index off, no embed or vector call is made. A blank query returns no hit and makes no embed call.
- Refactor: the embed, `ensure_collection` and model-id check sequence is one private method `_embed_and_ensure(text)`, shared by the write path and search. `_guarded` now returns the step's result, or None on failure.
- `tests/test_memory_index_store.py`: 15 new tests. They cover the pinned sample ("what can I cook for her, she has a diet" finds "Partner is allergic to peanuts" while the base `MemoryStore.search` returns nothing); a keyword hit staying first when the vectors disagree; every vector call carrying `filter == {"user_id": ...}`; two users, including a fake store that ignores the filter; an orphan vector; k = 1, 0 and 50; failure via `fail_on`, an embedder error and a locked store, each giving keyword-only results and exactly one notice; the index already off; a blank query; a stop-words-only query reaching the vector side; and the 1000-character cap.
- One existing S2 test, `test_fts_still_follows_edits_next_to_the_new_triggers`, now searches through a plain `MemoryStore`: it is about the FTS triggers, and the indexed store now also returns vector hits.

Criteria 6, 7, 8 and the read half of 9 are covered by these tests with fakes.

Checked by the coordinator by running the commands on 2026-11-03:

- `uv run pytest tests/test_memory*.py tests/test_store*.py tests/test_storage*.py -q`: 661 passed, 0 failed.
- The engineer's own run of the two index test files: 64 passed, 0 skipped.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: clean.

Call sites: `IndexedMemoryStore` still has no non-test caller until S4 (intended).

Not verified:

- The real qdrant adapter and a real embedding provider: only fakes were used.
- No type checker ran (pyright is not installed).
- The new tests were not mutation-checked.
- There is no minimum score, so with the fakes any note can match a vector query (known limit, recorded above).

### S4, flag and wiring (2026-11-03)

Files:

- `src/llm_harness/config.py`: `[tools] memory_vectors: StrictBool = False`, after `memory`, plus an example line.
- `src/llm_harness/memory.py`: base `MemoryStore.take_notice()` returning None.
- `src/llm_harness/memory_tools.py`: `MemoryTools(clock=None, *, vectors=False, embedder_factory=None)`. `store_for(storage)` caches per storage object and returns `IndexedMemoryStore` only when the flag is on and a factory is given. `resolve` and `recall_store(tools, storage)` now take the `Storage`; callers are `orchestrator.py` (two places) and `memory_command.py`.
- `src/llm_harness/builtin_tools.py`: `build_tool_registry(memory_vectors, embedder_factory)` and the registry note `MEMORY_VECTORS_NEEDS_MEMORY` (flag on while memory is off or user_id is missing; no exception). The note is shown in the State tab text through `tools_text`.
- `src/llm_harness/orchestrator.py`: `_vector_notice()` drains the one-time notice after `_recall`, after any `memory_*` tool call and after `_reflect`. The CLI, TUI and GUI show the Notice through the existing rendering.
- `src/llm_harness/memory_command.py`: appends the `take_notice()` line to its answer.
- `src/llm_harness/embeddings.py`: `build_embedder_factory(config, provider_factory)` is lazy: `select_embedding_model(config, None)`, provider from `config.providers[model.provider]`, `ConfigError` naming the provider when it has no `embed`, and `typing.cast` with a why-comment.
- `src/llm_harness/cli.py`: passes the flag and the factory, the factory only when the flag is on. The registry build never calls it.
- Tests: new `tests/test_memory_vectors_wiring.py` (26 tests); `store_for` and `recall_store` calls now pass `storage` in `tests/test_memory_tools.py` and `tests/test_memory_recall.py`.

Decisions:

- The factory lives in `embeddings.py`: it keeps `cli.py` short, adds no `memory_tools` to providers import and no cycle.
- The notice text includes the exception type, e.g. `memory vector search is off: ConfigError: <msg>; keyword search only`.
- With the real `DisabledVectorStore`, the first indexed save raises `VectorStoreError: the vector store is disabled: set [storage.vector] backend = "qdrant-edge" in config.toml` (no secret). It gives exactly one notice per process, and the memory is still saved.

Checked by the coordinator by running the commands on 2026-11-03:

- `timeout 280 uv run pytest -q`: 3176 passed, 0 failed, 0 skipped, 7 deselected (marker live, `pyproject` addopts; not run), 1 DeprecationWarning from google/genai. The baseline before S1 was 3086 passed; the difference is the new tests of S1 to S4.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: 134 files clean.
- The engineer's narrow run (memory, config, orchestrator, cli, tools, gui, tui tests): 1504 passed, 0 skipped.
- Smoke: `llm-harness` has no --config or --data-dir flag; config and data come from HOME, so a scratch run uses `HOME=<scratch dir> .venv/bin/llm-harness`. `--help` and `--storage-status` against a scratch HOME exit 0 with all paths under the scratch dir. The scratch `agents.yaml` must list `memory_search` or recall and reflection stay off.

Call sites: every new name has a non-test caller: `build_embedder_factory` at `cli.py:508`, `IndexedMemoryStore` at `memory_tools.py:110`, `take_notice` in `orchestrator.py` and `memory_command.py`, `_vector_notice` at `orchestrator.py:349`, `467` and `615`.

Known limit: the embedding provider is never closed at exit. `Embedder` has no close; a shared close hook across CLI, TUI and GUI would be needed.

Not verified:

- Nothing ran against a real ollama server or the real qdrant-edge backend.
- The `ConfigError` text for a provider that cannot embed is not exercised by a real adapter.
- The new tests were written after the code and were not mutation-checked.

### S5, reindex and lazy repair (2026-11-03)

Files:

- `src/llm_harness/memory_index.py`: `IndexedMemoryStore.reindex(user_id, limit=None) -> tuple[int, int] | None` (lines 197-206, `_reindex` 208-228). It finds the user's memories with no `memory_vectors` marker row in the current collection, oldest id first, embeds them in batches of `REINDEX_BATCH_SIZE = 16` (line 38), upserts and marks each batch (`_index_batch`, 230-246). It returns `(indexed now, unindexed before)`, or None when the index is off. With nothing to do it returns `(0, 0)` after a factory call and one SQL COUNT, with no embed or vector call (lines 219-220). A failure keeps the markers already written and turns the index off with one notice (`_guarded`, 258-267); `CancelledError` passes.
- `LAZY_REPAIR_LIMIT = 20` (line 37): `search` repairs once per process per user, after the fused result is built (`_repair_once`, 189-195, called at 166-168), so the repair never changes the result. A blank query returns before the repair (line 160-161), so it does not count as the first search.
- Helpers `_embed_batch_and_ensure` (274-280, with `_embed_and_ensure` 269-272 as a thin wrapper) and `_fused` (171-187).
- `memory_index.py` imports the private `_COLUMNS`, `_check_user_id` and `_row_to_memory` from `llm_harness.memory` (lines 20-27): a known smell, private names shared inside the package.
- `src/llm_harness/memory_command.py`: `/memory reindex` (`_reindex`, 145-152). Answer `reindexed N of M memories`. A plain store answers `VECTORS_OFF` (line 25, `vector search is off: set [tools] memory_vectors = true`). An indexed store that switched off answers `vector search is off: <index_off_reason>` (escaped). `handle_memory_text` does not append the pending notice when the answer already says `vector search is off` (lines 194-196), so the reason is not shown twice. `USAGE` has the new verb (lines 26-31). The CLI `--plain`, the TUI and the GUI all go through `handle_memory_text`; no other verb list exists.
- Tests: new `tests/test_memory_reindex.py` (21 tests: counts, limit, 3 embed calls for 40 rows, idempotence, isolation, an edit clears the marker, a new model gives a new collection with all unindexed, index off, failure midway, lazy repair once per user and bounded, `/memory reindex` parsing and answers). One front-end assertion each in `tests/test_memory_command.py` (CLI) and `tests/test_memory_command_ui.py` (TUI, GUI); these use a plain store, so they check the `VECTORS_OFF` answer. The real reindex through `handle_memory_text` is tested in `test_memory_reindex.py`.

Spec versus built, per acceptance criterion (the criteria text is not edited; checked against the code on 2026-11-03):

| # | Result | Note |
| --- | --- | --- |
| 1 | same | Flag off or no factory: `store_for` returns the plain store (S4). |
| 2 | same | Point id `str(memory.id)`, payload `{"user_id": ...}`, text from `_embedded_text` (`memory_index.py:91-92`, 237, 286). |
| 3 | same | `_guarded` (258-267) keeps the write result. |
| 4 | same | Migration 2 at `memory.py:62-77`. |
| 5 | same | `_check_model`, `memory_index.py:319-333`. |
| 6 | same | `search` 156-169, `_fused` 171-187. |
| 7 | same | Covered by S3 tests with the fake concept embedder. |
| 8 | same | `_vector_ids` passes `filter={"user_id": user_id}` (250-252). |
| 9 | differs | The reason in the notice starts with the exception type: `memory vector search is off: <ExcType>: <message>; keyword search only` (`memory_index.py:265`, 131). The notice is also shown after any `memory_*` tool call and after a `/memory` answer, not only after recall and reflection. |
| 10 | differs | Four points. `M` is the count unindexed before, `N` the count indexed now. "Stale" entries are those whose marker a trigger removed, so they count as unindexed. The flag-off answer is for a store that is not indexed; an indexed store that switched itself off answers `vector search is off: <reason>`. Lazy repair is per user per process (`_repaired_users`, line 120), not per process. The criterion's wording also implies recovery; the code does not recover (see below). |
| 11 | same | `build_embedder_factory` (`embeddings.py:67-86`), called at `cli.py:508` only when the flag is on. |
| 12 | same | Offline tests with the fakes of S1. |

Other differences from the spec text (not criteria):

- Risks (line 74) says a transient provider error switches the index off "(`/memory reindex` recovers)". The code does not: `_guarded` returns None while `_off_reason` is set (`memory_index.py:260-261`), so `reindex` answers `vector search is off: <reason>` until the process restarts. This matches assumption (e); the Risks line is wrong. The README says the same as the code.
- Assumption (c): the notice is appended to the `/memory` answer, except for `/memory reindex` when the answer already says `vector search is off` (`memory_command.py:194-196`).
- Assumption (a): per user, not per process.

Checked by the coordinator by running the commands on 2026-11-03: full suite `timeout 290 uv run pytest -q -rs`: 3202 passed, 0 failed, 0 skipped, 7 deselected (marker live), 1 DeprecationWarning from google/genai; `uv run ruff check src tests` passed; `uv run ruff format --check src tests`: 135 files clean. The engineer's S5 narrow run (memory, store, storage, cli, gui, tui tests): 1255 passed, 0 skipped.

Not verified: nothing ran against a real model, a real embedding provider or the real qdrant-edge backend; the new tests were not mutation-checked.

### S6, documentation (2026-11-03)

- `README.md`: the `[tools]` sample has `memory_vectors`; the list of boolean keys names it; the Embedding models intro no longer says nothing calls an embedding model; the Agent memory intro and the "Not built yet" paragraph are replaced by a pointer to a new section `#### Vector search` (switch, requirements, what changes, the one notice, limits); the memory tables line and the `--storage-status` sample show schema version 2; the "diet" weak point says keyword-only; `/memory reindex` is in the command table and rules; the Fixed values table has five new rows.
- `CHANGELOG.md`: two lines under `## [Unreleased]` / `### Added` (meaning-based memory search behind `[tools] memory_vectors`; `/memory reindex`). No Changed entry, memory being unreleased.
- Only these two files and this spec were edited; constitution files were not (see Documentation Review).

### Real-model check, bge-m3 on local Ollama (2026-11-03, after closeout, requested by the human)

Source of the facts: as reported by the engineer, not re-run by the coordinator. Only the Known-limits timeout fact (last bullet) was read from the code by the technical writer.

Command: `cd <project root> && timeout 120 uv run python <scratchpad>/realcheck.py`. The script lives in the session scratchpad and is not kept in the repository. One run of about 20 s.

Setup:

- Local Ollama, model `bge-m3`, embedding dimension 1024.
- The real `QdrantEdgeVectorStore` and the real `IndexedMemoryStore` over SQLite in a temporary directory. The user's real config and data were not touched.
- Config loaded with `load_config` from a copy of a scratch config; embedder from `build_embedder_factory(config, build_provider)`; storage from `open_storage`.
- 33 embed calls to Ollama, no chat-model calls. 8 English notes for `check-user` plus 1 for `other-user`.

Latencies:

| Step | Time |
| --- | --- |
| First embed, including model load | about 2554 ms (first save 2964 ms) |
| Typical embed | 20-31 ms (occasionally 74-76 ms) |
| Typical save after warm-up | 63-111 ms |
| Vector-only search | 0-1 ms |
| Hybrid `search`, end to end | 25-81 ms, typically about 30 ms |

Recall (k=3; keyword-only versus hybrid; top vector score):

| Query | Keyword-only | Hybrid, first hit | Top score |
| --- | --- | --- | --- |
| "what can I cook for her, she has a diet" | none | peanuts note | 0.528 |
| "which vacation should I book" | none | Lisbon trip | 0.533 |
| "what text editor or terminal do I use" | zsh and iTerm both | (not reported) | 0.534 |
| "how do I get ready for relocating abroad" | none | Czech/Prague note | 0.533 |
| "Co mám uvařit pro partnerku, má alergii" (Czech) | none | peanuts note | 0.718 (highest of the run: bge-m3 handles it) |
| "metric" (exact word) | "metric units" first | "metric units" first | 0.673 |
| "explain how photosynthesis works" (unrelated) | spurious hit "data engineer" (probably from "works") | still three hits | best vector score 0.381 |

Score ranges, a sample of 8 notes, one model, one run: indicative, not statistical.

- Relevant top-1 hits: 0.528-0.718.
- Irrelevant rank-2 and rank-3 hits: about 0.37-0.47.
- The weak tail of relevant queries (0.39-0.48, for example Lisbon 0.484 for the relocation query, zsh 0.465) overlaps the unrelated query's 0.368-0.381.
- A threshold between roughly 0.40 and 0.50 would separate top-1 hits from the noise in this sample, but with little margin.
- This informs the known limit "no minimum score". It stays out of scope; a threshold would need a larger measured sample.

Real-adapter checks that had only been faked before:

- The `user_id` payload filter works in both directions (no leak).
- Update, then search, finds the new text.
- Forget removes the note from vector and hybrid results.
- A second `IndexedMemoryStore` on the same shard path in the same process does NOT reuse the shard: its index is switched off with `VectorStoreLockedError: vector store is in use by another llm-harness process`, and its search returns the keyword result only. This is the documented one-process limit, now seen for real.
- A search on a vector store before `ensure_collection` raises `VectorStoreError unknown collection ...: call ensure_collection first`.
- Close and reopen over the same directory keeps the points and searches fine.
- `reindex` returns (0, 0) once everything is indexed.

No failure found; the index never switched itself off in the main run.

Not verified: more than 9 notes, non-English notes beyond the one Czech query, large stores, the chat model with `memory_vectors` on, the GUI and TUI.

Known-limits fact found by reading the code (not by the run): the Ollama embedding client is built without a timeout (`src/llm_harness/providers/ollama.py:124`, `ollama.AsyncClient(host=config.base_url)`; the installed library's default is `timeout=None`). A server that accepts the connection and never answers would hang a search, a write or `/memory reindex` until the user cancels. The other providers were not checked.

Next: none, the feature is done (B5 closeout finished 2026-11-03).

## Validation

Commands run by the coordinator on 2026-11-03:

- `timeout 290 uv run pytest -q -rs`: 3202 passed, 0 failed, 0 skipped, 7 deselected (marker `live`; `addopts '-m "not live"'` in `pyproject.toml`; those 7 were not run, so they are unverified), 1 DeprecationWarning from google/genai (third party). Baseline before S1: 3086 passed.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: 135 files clean.
- The engineer's S5 narrow run (memory, store, storage, cli, gui, tui tests): 1255 passed, 0 skipped.

Smoke recipe (by hand; chat was not run): `HOME=<scratch HOME> .venv/bin/llm-harness --plain`, where `<scratch HOME>` is an empty scratch directory (config and data come from HOME; the scratch `agents.yaml` must list `memory_search`, or recall and reflection stay off). `--help` and `--storage-status` against a scratch HOME exited 0 on 2026-11-03.

Real-model check: after closeout, one script run against local Ollama (`bge-m3`) with the real `QdrantEdgeVectorStore` and `IndexedMemoryStore` found no failure; see "Real-model check, bge-m3 on local Ollama" in Implementation Notes (as reported by the engineer, not re-run by the coordinator).

Still not done and not seen: the chat itself was not run end to end with the flag on; the 7 live tests were not run; the new tests were not mutation-checked.

Known limits: the Ollama embedding client has no timeout (`src/llm_harness/providers/ollama.py:124`), so a server that never answers hangs a search, a write or `/memory reindex` until the user cancels (other providers not checked); no minimum score; the embedding provider is never closed at exit; no cleanup of old-model collections; the first search of a process may pay up to 20 embeds; `memory_index.py` imports private names from `memory.py`.

## Documentation Review

Checked: `README.md`, `CHANGELOG.md`, and the constitution files `specs/mission.md` and `specs/tech-stack.md`, against `src/llm_harness/memory_index.py`, `memory_command.py`, `builtin_tools.py`, `config.py` and `cli.py`.

README drift found and fixed:

- Agent memory intro said vector search is not built; a paragraph said "Not built yet (planned, not available)". Code: `memory_index.py` and `config.py:318`.
- Known weak points said meaning-based search "needs the planned vector search".
- Embedding models intro said nothing calls an embedding model. Code: `cli.py:506-509` and `embeddings.py:67-86`.
- The `--storage-status` sample said `memory version 1 (the code expects 1)`. Code: two memory migrations (`memory.py:62-77`), so version 2.
- The `[tools]` sample and the boolean-keys sentence lacked `memory_vectors` (`config.py:318`).
- Not in README before, now added: `/memory reindex` and the five fixed values.

Changelog entries:

- {path: `CHANGELOG.md`, category: Added, text: "Optional meaning-based (vector) memory search, switched on with `[tools] memory_vectors = true` (needs `memory`, a `user_id` and an `embedding` model): recall, `memory_search` and reflection merge keyword and vector hits, and fall back to keyword search with one notice on any problem."}
- {path: `CHANGELOG.md`, category: Added, text: "`/memory reindex` indexes the memories that have no vector yet and answers `reindexed N of M memories`."}

CI review: not applicable (no `.github` directory, no `Makefile`, no `Dockerfile`, and no `*.yml` or `*.yaml` file outside `.venv/` and `specs/` at depth 3, checked with `ls -a` and `find` on 2026-11-03; no release workflow exists to inspect).

Constitution drift, PROPOSALS ONLY (the files were not edited; they need the human's approval):

1. `specs/mission.md`, line 21. Old: "`/memory` command to list, show and delete notes; keyword search only, SQLite FTS5; off by default, switched on by `[tools] memory = true` plus a `user_id`)". New: "`/memory` command to list, show, delete and reindex notes; keyword search (SQLite FTS5) with an optional vector (meaning-based) search merged by rank, switched on by `[tools] memory_vectors = true` and falling back to keyword search on any problem; off by default, switched on by `[tools] memory = true` plus a `user_id`)".
2. `specs/tech-stack.md`, line 53, the `memory` entry. Old: "modules `src/llm_harness/memory.py` (store), `memory_tools.py` (the four tools), `memory_recall.py` (automatic recall), `memory_reflection.py` (reflection step, ...) and `memory_command.py` (`/memory`)". New: add "`memory_index.py` (`IndexedMemoryStore`: vector index, rank fusion, reindex and lazy repair; on only with `[tools] memory_vectors = true`, which needs `memory`, a `user_id` and an `embedding` model)", and "memory schema version 2 with the tables `memory_vectors` and `memory_vector_meta`".
3. `specs/tech-stack.md`, line 53, same entry. Old: "Not built: ... vector fusion in `search`, typed output, vector search over memory, and any real tool beyond ...". New: remove "vector fusion in `search`" and "vector search over memory" (built, `memory-vector-search`).
4. `specs/tech-stack.md`, line 62 (Embeddings). Old: "Embeddings (built; no non-test caller yet, by human decision)". New: "Embeddings (built; the first consumer is the memory vector index, `build_embedder_factory` in `embeddings.py`)". Also old: "Not built, moves to the first consumer feature: storing the model id and dimension per vector collection and refusing a mismatch with a re-index message". New: "Built in `memory-vector-search`: model id and dimension per collection in the SQLite table `memory_vector_meta`, a mismatch is refused; the collection is `memories_` plus 16 hex characters of the sha256 of the model id".
5. `specs/tech-stack.md`, line 96 (Storage interfaces). Add: "The `VectorStore` has its first consumer, the memory index (collection per embedding model, filter `user_id`)."

Observation: `specs/features/memory.md` and `specs/roadmap.md` were not checked or edited in this task.

## Documentation Updates

- `README.md`: new section `#### Vector search` under Agent memory; edits listed in S6 above.
- `CHANGELOG.md`: two `### Added` lines under `## [Unreleased]`.
- Constitution update, approved by the human on 2026-11-03 and applied the same day (the 5 proposals in Documentation Review, each old sentence re-checked against the file and each new statement against the code):
  - `specs/mission.md` (version 15 to 16): `## Value proposition`, the "What exists today" paragraph: the memory description now says the `/memory` command lists, shows, deletes and reindexes notes, and that keyword search (FTS5) has an optional vector search merged by rank behind `[tools] memory_vectors = true`, with fallback to keywords. Also fixed one other stale phrase found by grep: the storage layer "only the usage ledger uses it so far" now says the usage ledger and agent memory use it, and the vector store only the optional memory vector index.
  - `specs/tech-stack.md` (version 24 to 25), `## Frameworks & libraries` (Tools and Embeddings bullets) and `## Key conventions` (Storage interfaces bullet): the Tools bullet (`memory` entry) names `memory_index.py` and memory schema version 2 with `memory_vectors` and `memory_vector_meta`, and its "Not built" list no longer has "vector fusion in `search`" or "vector search over memory"; the Embeddings bullet now names the first consumer (`build_embedder_factory`) and records the per-collection model id check as built; the Storage interfaces bullet names the memory index as the first `VectorStore` consumer.
- `specs/roadmap.md`: the `## Now` line for this feature is removed; the section reads `Nothing in progress.` (no Artifacts table exists in `specs/roadmap.md` or `specs/tech-stack.md`, checked with grep). The roadmap `version` and `updated` are not changed: removing a `## Now` line is bookkeeping that does not bump them.
- `specs/memory.md`: two entries appended (`[gotcha]` the index stays off after the first failure, `/memory reindex` does not recover it; `[build]` the point id, payload, collection name and migration 2 tables). The second entry is longer than 400 characters (a validator warning, not an error).
- B5 closeout checklist, 2026-11-03:
  - Validation recorded with counts: done, see Validation (3202 passed, 0 failed, 0 skipped, 7 deselected as marker `live`; those 7 were not run).
  - README, CHANGELOG and other changelogs updated: done (`README.md` section `#### Vector search`; two `### Added` lines in `CHANGELOG.md`; no other changelog exists).
  - `## Now` removed: done (above).
  - CI review recorded: done, not applicable (see Documentation Review).
  - Every new function, type and config key has a non-test use (`grep -rn` over `src`, 2026-11-03):
    - `IndexedMemoryStore`: `memory_tools.py:110` (`store_for`), `memory_command.py:146` (`isinstance` check).
    - `fuse_ranks`: `memory_index.py:186` (in `_fused`).
    - `collection_name`: internal, used at `memory_index.py:210`, `304` and `313`.
    - `build_embedder_factory`: `cli.py:508`.
    - `memory_vectors`: `config.py:318` (key), `cli.py:506` and `509`, `builtin_tools.py:118`, `170` and `176`, `memory_command.py:25` (text).
    - `take_notice`: `orchestrator.py:530` and `memory_command.py:194`; the base method is `memory.py:230`.
    - Constants `LAZY_REPAIR_LIMIT` (`memory_index.py:194`), `REINDEX_BATCH_SIZE` (226), `MAX_QUERY_EMBED_CHARS` (249), `VECTOR_SEARCH_LIMIT` (251) and `RRF_OFFSET` (63) are intentionally internal to `memory_index.py` and used there.
  - `validate-specs` passes: run after this edit, see the closeout report.
