---
title: "/memory reindex --all re-embeds every note into the current vector store"
kind: feature
status: done
version: 1
updated: 2026-11-04
branch: main
---

## Problem / Motivation

The SQLite table `memory_vectors` (columns `collection`, `memory_id`) holds "indexed" markers. `IndexedMemoryStore._reindex` (`src/llm_harness/memory_index.py:236-258`) selects only memories with no marker for the current collection (the `NOT EXISTS` SQL at 240-243). The collection name depends only on the embedding model (`collection_name(model_id)`, `memory_index.py:84`).

So after `[storage.vector]` changes backend or server with the same embedding model, the collection name is the same, the markers are stale, `/memory reindex` answers `reindexed 0 of 0 memories` and the new store stays empty. The README documents a manual SQL workaround (`README.md:605-612`, `1176-1194`, `2308-2315`) and says `/memory reindex --all` is planned as roadmap item `memory-reindex-all` (`specs/roadmap.md:21`, under `## Later`; also the `[gotcha]` at `specs/memory.md:69`).

Assumptions (the human may change them):

- (a) `/memory reindex --all` re-embeds EVERY note of the calling user into the current vector store, ignoring markers. The answer text is unchanged, `reindexed N of M memories`, with M = all the user's notes.
- (b) `IndexedMemoryStore.reindex` gets a keyword-only flag `rebuild: bool = False`; with it `_reindex` skips the `NOT EXISTS` filter. The upsert is idempotent (`VectorPoint` id = `str(memory.id)`, `memory_index.py` `_index_batch`) and the marker insert is `ON CONFLICT (collection, memory_id) DO NOTHING` (`memory_index.py:317-318`), so no marker is deleted and a half-finished run is safe to repeat.
- (c) Same batching (`REINDEX_BATCH_SIZE = 16`), no limit, no progress output.
- (d) Same off and resume behaviour as plain reindex (`_reindex` in `src/llm_harness/memory_command.py:146-155`): a failed run answers `vector search is off: <reason>`.
- (e) Only the calling user's notes; never other users'.
- (f) `--all` is the only accepted extra argument; `/memory reindex foo` and `/memory reindex --all x` still answer `USAGE`.
- (g) The `USAGE` text (`memory_command.py:27-33`) gets the `--all` form.
- (h) A plain `MemoryStore` (flag off) still answers `VECTORS_OFF` (`memory_command.py:25`).

## Acceptance Criteria

Tests are offline: the existing fakes `ConceptEmbedder`, `FakeVectorStore` (with `fail_on`) and `FakeEmbedder` from `tests/fakes.py`, and a real `SqliteRelationalStore` on `tmp_path` (as in `tests/test_memory_reindex.py`).

1. `parse_memory_command("reindex --all")` returns a command that means "reindex all"; `reindex` alone still returns `MemoryCommand("reindex")`; `reindex foo` and `reindex --all x` return the usage command.
2. `IndexedMemoryStore.reindex(user_id, limit=None, *, rebuild=False)` with `rebuild=True` processes every note of the user and returns `(N, N)`.
3. With markers present, after swapping in an EMPTY new `FakeVectorStore`, plain `/memory reindex` answers `reindexed 0 of 0 memories` and the store stays empty; `/memory reindex --all` fills it and answers `reindexed N of N memories`.
4. `--all` run twice is idempotent: the same vector count, and the `memory_vectors` rows unchanged.
5. Other users' notes are untouched: no vector for them, no new marker.
6. A failing embedder makes `--all` answer `vector search is off: <reason>`; markers are not removed.
7. `asyncio.CancelledError` passes through `--all`.
8. The `USAGE` text shows `--all`.
9. A plain `MemoryStore` answers `VECTORS_OFF` for `--all`.
10. Docs: `README.md` replaces the three workaround and "planned" passages with `/memory reindex --all` and adds it to the command table (~line 1796); `CHANGELOG.md` gets one coherent entry under `## [Unreleased]` / `### Added` (the existing `/memory reindex` entry is at line 49: extend it, do not duplicate); `specs/memory.md` gets a new appended `[gotcha]` superseding line 69 (old lines are never edited); `specs/roadmap.md` gets a `## Now` line at implementation and the `Later` item is removed when done.

## Approach

Memory consulted: grep of specs/memory.md for reindex — 4 relevant entries (lines 60, 61, 62, 69: gotchas about the index being off, collection naming, stale SQLite markers; line 69 is the motivation), none blocking

One slice:

- `src/llm_harness/memory_index.py`: add the keyword-only `rebuild` flag to `reindex` and `_reindex`; when set, the count and select queries omit the `NOT EXISTS` clause (`WHERE user_id = ?` only). Batching and upsert are unchanged.
- `src/llm_harness/memory_command.py`: `MemoryCommand` (`action`, `number=1`) carries the choice (for example a new action `"reindex-all"`); `parse_memory_command` accepts exactly `reindex` and `reindex --all`; `_reindex(store, user_id)` gets the flag and keeps the resume-if-off and the answer logic; `handle_memory_text` also treats the new action like `reindex` when dropping the duplicate notice.
- Tests in `tests/test_memory_reindex.py` and `tests/test_memory_command.py`.
- Docs as in criterion 10.

Risks:

- A full re-embed of a large note set costs one embedding call per 16 notes. The Ollama adapter builds `ollama.AsyncClient(host=config.base_url)` (`src/llm_harness/providers/ollama.py:124`); the call has no timeout argument there, and the sibling spec `specs/features/memory-vector-recovery.md` (Approach, Risks) records the library default as no timeout. Whether `fix-embedding-timeout` changed this elsewhere: unverified. The user types the command deliberately, so the cost is by choice.

## Affected Areas

- `src/llm_harness/memory_index.py`
- `src/llm_harness/memory_command.py`
- `tests/test_memory_reindex.py`
- `tests/test_memory_command.py`
- `README.md`
- `CHANGELOG.md`
- `specs/roadmap.md`
- `specs/memory.md`

## Out of Scope

- Deleting vectors of notes that no longer exist, or old collections in the previous store.
- Progress reporting.
- A `--user` option.
- An ANN index (`pgvector-index`).
- Automatic detection of a backend change.

## Implementation Notes

One slice, implemented directly on `main` (nothing committed).

- `src/llm_harness/memory_index.py`: `IndexedMemoryStore.reindex(user_id, limit=None, *, rebuild=False)` (about lines 226-260); with `rebuild` every note of the user is re-embedded, ignoring the markers in `memory_vectors`.
- `src/llm_harness/memory_command.py`: `/memory reindex --all` is parsed to the action `reindex-all`; the answer is unchanged, `reindexed N of M memories`, with M = all the user's notes. `USAGE` now shows `... /memory reindex [--all]` plus the line `--all re-embeds every memory into the current vector store`.
- Tests: 8 new in `tests/test_memory_reindex.py`; 2 new parse rows and 1 updated `USAGE` test in `tests/test_memory_command.py`.
- Counts: baseline before the change 3815 passed, 265 deselected (live). After: 3826 passed (engineer-reported). The coordinator re-ran the 3 targeted files: 107 passed. The coordinator re-ran the full suite `uv run pytest -q -p no:cacheprovider`: 3826 passed, 0 failed, 0 skipped, 265 deselected (marker `live`, not run: they call real services and were not requested), 1 warning, 140.9 s.
- Fail-before proof (engineer): a temporary `if False:` in place of `if rebuild:` made 4 tests fail; restored, and confirmed at `src/llm_harness/memory_index.py` `if rebuild:`. Ruff check and format clean.
- Deviations from the Approach: none known.

## Validation

- Full suite (coordinator): `uv run pytest -q -p no:cacheprovider` gave 3826 passed, 0 failed, 0 skipped, 265 deselected, 1 warning, 140.9 s. Baseline before the change: 3815 passed. Targeted 3 files: 107 passed.
- Per-suite explanation: no skips. The 265 deselected tests are `-m "not live"` from the pyproject `addopts` (the project default, offline).
- Fail-before proof (engineer): a temporary `if False:` in place of `if rebuild:` made 4 tests fail; restored, and confirmed at `src/llm_harness/memory_index.py` `if rebuild:`.
- Lint: `ruff check` and `ruff format --check` clean (engineer-reported); the coordinator re-ran `ruff check src tests`: All checks passed.
- Call-site evidence (`grep -n` over `src/llm_harness`, re-run when writing this): `rebuild` is used at `memory_index.py:226,239,242,246` and `memory_command.py:149,154,178`; the action `reindex-all` at `memory_command.py:72` (parse), `177` (dispatch) and `208` (duplicate-notice rule).
- Real-artifact run against realistic input: not run against a live vector server (no live probes were requested). The behavior is proven by offline tests that swap in an empty fake vector store on the same SQLite database (criterion 3).
- Unverified: no timeout on a large `--all` run (the embedding provider default applies); GUI and TUI are covered only by the existing tests.

## Documentation Review

- `CHANGELOG.md`: needs an entry, yes (user-facing); done at line 49 (the existing `/memory reindex` entry was extended).
- `README.md`: needs an update, yes; done.
- `specs/memory.md`: new `[gotcha]` appended, done.
- Constitution files (grep for `reindex` and `marker` in `specs/mission.md` and `specs/tech-stack.md`): `specs/tech-stack.md:96` is STALE: it says "Still planned: the roadmap item `memory-reindex-all` and the roadmap item `pgvector-index`", but `memory-reindex-all` is now built and no longer in the roadmap. Not edited here (constitution file); it needs the update path of the `constitution-format` skill. `specs/mission.md:21` mentions only "reindex notes" in the `/memory` command, which stays true. `specs/tech-stack.md:70` mentions the marker `live` (pytest), unrelated and true.
CI review: not applicable (no .github, Makefile, Dockerfile or release script exists in the repository; verified with ls)
- No `### Artifacts` table: single changelog `CHANGELOG.md`.

## Documentation Updates

- `README.md`: in Remote qdrant step 8 and pgvector step 8 the manual `sqlite3 ... DELETE FROM memory_vectors` workaround and the "planned" text were replaced by `/memory reindex --all`; the Vector search bullet on `/memory reindex`, the `/memory` command table (new `/memory reindex --all` row) and notes (new bullet), and the known-limits bullet on changing `[storage.vector]` now name `--all`.
- `CHANGELOG.md`: the existing `/memory reindex` entry under `## [Unreleased]` / `### Added` was extended with `--all` (no new entry).
- `specs/memory.md`: one new `[gotcha]` entry appended (2026-11-04T13:38:46Z) that supersedes the 2026-11-03T14:44:46Z entry.
- `specs/roadmap.md`: the `## Now` line for this feature was removed (`Nothing in progress.` left under `## Now`); the `Later` item `memory-reindex-all` was already removed at approval.
- Constitution: `specs/tech-stack.md` and `specs/mission.md` both updated on human approval 2026-11-04 (tech-stack v31, mission v21).
