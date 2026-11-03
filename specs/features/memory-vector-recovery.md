---
title: "Memory vector search: /memory reindex switches the index back on"
kind: feature
status: done
version: 1
updated: 2026-11-03
branch: main
---

## Problem / Motivation

After the first index failure, `IndexedMemoryStore` stays off for the rest of the process. `_guarded` returns None while `_off_reason` is set (`src/llm_harness/memory_index.py:258-267`, the check at 260-261), and nothing clears `_off_reason`: the only assignments are `None` in `__init__` (line 118) and the failure branch (265). `/memory reindex` calls `store.reindex`, which goes through `_guarded` (`memory_index.py:206`), gets None and answers `vector search is off: <reason>` (`src/llm_harness/memory_command.py:148-150`). So a transient Ollama blip disables meaning search until the user restarts the program. This is assumption (e) of `specs/features/memory-vector-search.md` (line 31) and the `[gotcha]` entry at `specs/memory.md:60`. The README says the same (`README.md:742-745`).

The human asked on 2026-11-03 to make `/memory reindex` the recovery path.

The decision (from the human's request): `/memory reindex` on an indexed store that is off first resets the off state, then runs the normal reindex. The defaults below are the author's and are listed as assumptions.

Open points (assumptions, the user may change them):

- (a) Only `/memory reindex` resets the state. Searches and writes stay off until then: no automatic retry, no backoff.
- (b) A failed retry leaves the index off with the NEW reason, and the answer is `vector search is off: <new reason>`. The one-time notice for that new failure is not shown a second time: `handle_memory_text` already drops the notice when a `reindex` answer contains `vector search is off` (`memory_command.py:194-196`), and `take_notice` has already cleared its pending flag by then (`memory_index.py:126-131`).
- (c) The reset also forgets what the process learned: the ensured collection (`_collection`, `memory_index.py:117`, set at 316) and the cached embedder (`_embedder`, line 116, built at 293-296). A retry therefore rebuilds the embedder through the factory and calls `ensure_collection` again (`memory_index.py:315`), so a locked or missing store is retried honestly. The model-id and dimension check (`_check_model`, 319-333) runs again, so a model mismatch keeps failing with its reason.
- (d) The lazy-repair per-user set `_repaired_users` (line 120) is not cleared. The reindex that follows the reset covers the whole user anyway.
- (e) A plain `MemoryStore` (flag off) still answers `vector search is off: set [tools] memory_vectors = true` (`VECTORS_OFF`, `memory_command.py:25`, returned at 146-147).

## Acceptance Criteria

Tests are offline: the existing fakes `ConceptEmbedder`, `FakeVectorStore` (with `fail_on`) and `FakeEmbedder` from `tests/fakes.py`, and a real `SqliteRelationalStore` on `tmp_path`.

1. `IndexedMemoryStore` gets a new public method `resume()` that returns None. It clears the off reason, the pending-notice flag, the ensured-collection state and the cached embedder. `/memory reindex` calls it when `index_off_reason` is not None.
2. After a transient failure (an embed error, then fixed), `/memory reindex` answers `reindexed N of M memories` with the right counts. The index is on again, a later save indexes immediately, and a later search uses the vectors.
3. After a `VectorStoreLockedError` that is then cleared, the same as criterion 2.
4. A persistent failure (still failing) answers `vector search is off: <reason>` with the new reason. The index is off again and no duplicate notice is shown.
5. A model or dimension mismatch is still refused after the reset, with the same reason.
6. A store that is on is untouched by `/memory reindex`: no reset and no embed calls beyond the normal reindex.
7. Searches and writes while the index is off still make no embed or vector call and never fail.
8. `CancelledError` passes through `resume` and the retry.
9. `README.md` and `CHANGELOG.md` say that `/memory reindex` also switches vector search back on. The existing `/memory reindex` line in `CHANGELOG.md` (line 49) is edited, not duplicated, because the feature is unreleased. The "until you restart the program" sentences in `README.md` (lines 742-745, and the `/memory reindex` rule at 900-904) are corrected. In `specs/memory.md`, the `[gotcha]` entry at line 60 is not edited (the file is append-only per the `memory-format` skill): a new `[gotcha]` entry with a later timestamp is appended that supersedes it (maintenance criterion 2, "Superseded: a later entry, same tag, same subject, contradicts or subsumes it"); the old line is removed only by a later maintenance pass.
10. The text of `specs/features/memory-vector-search.md` is not edited (history). The user-visible difference is recorded in this spec's Implementation Notes.

## Approach

Memory consulted: grep of specs/memory.md for reindex — 2 relevant entries (the `[gotcha]` that the index stays off, to be superseded; the `[build]` collection-name entry), none blocking

One slice, the smallest change:

- `src/llm_harness/memory_index.py`: add `resume()` next to `reindex` (lines 197-206). It is synchronous and has no `await`: it sets `_off_reason = None`, `_notice_pending = False`, `_collection = None` and `_embedder = None`.
- `src/llm_harness/memory_command.py`: in `_reindex` (lines 145-152), after the `isinstance` check, call `store.resume()` first when `store.index_off_reason is not None`, then `store.reindex(user_id)` as now. The `None` branch (149-150) stays: it is what a failed retry answers.
- `tests/test_memory_reindex.py`: new tests for criteria 1 to 8 (the file already has the failure and `/memory reindex` answer tests).
- Docs: `README.md`, `CHANGELOG.md`, `specs/memory.md` as in criterion 9.
- No change to `USAGE` text (`memory_command.py:26-32`) or the notice texts.

Risks:

- A retry re-embeds and may hit a still-broken provider. It is bounded to one `reindex` call per command (batches of `REINDEX_BATCH_SIZE = 16`, `memory_index.py:38`). The Ollama adapter builds `ollama.AsyncClient(host=config.base_url)` with no timeout argument (`src/llm_harness/providers/ollama.py:124`), and the installed library's `timeout` default is `None` (`.venv/lib/python*/site-packages/ollama/_client.py:90`). So a server that accepts the connection and never answers would make the command wait until the user cancels. The OpenAI-compatible and Vertex adapters were not checked for timeouts: I do not know. This is no worse than the first embed that caused the failure.
- Resetting `_collection` while another coroutine writes. `resume()` has no `await`, so it runs atomically on the event loop. A coroutine suspended inside `_embed_batch_and_ensure` (`memory_index.py:274-280`) that continues after the reset only calls `_ensure_collection` again, which re-checks and re-sets the same value. `MemoryTools.store_for` keeps one store per `Storage` object (`src/llm_harness/memory_tools.py:102-106`) and the orchestrator reads it through `recall_store` (`src/llm_harness/orchestrator.py:513`, `529`, `569`). That one orchestrator uses a store at a time is not verified here (the GUI and TUI sessions were not checked): the risk is a write that lands between the reset and the retry, which at worst indexes one entry normally.
- A user who runs `/memory reindex` repeatedly against a broken provider pays one failed call each time; there is no backoff by decision (assumption (a)).

Security: unchanged. The payload still holds only `user_id`, and the off reason is still redacted (`memory_index.py:265`) and escaped for display (`memory_command.py:150`).

## Affected Areas

- `src/llm_harness/memory_index.py` (`resume`).
- `src/llm_harness/memory_command.py` (`_reindex`).
- `tests/test_memory_reindex.py`; `tests/test_memory_command.py` only if the command text changes (it should not).
- `README.md`, `CHANGELOG.md`, `specs/memory.md`.

## Out of Scope

- Automatic retry or backoff on search or write.
- A `/memory vectors on|off` command.
- Changing the notice texts.
- A minimum score.
- Closing the embedding provider.
- Any change to the `VectorStore` or `Embedder` interfaces.

## Implementation Notes

Spec approved 2026-11-03. Baseline before the build: full suite 3202 passed, 0 failed, 0 skipped, 7 deselected (marker live), run by the coordinator earlier the same day; only specs, docs and a scratch script changed since. Stayed on main. The single slice is built, validated and documented; the feature is done (final delivery approved by the human on 2026-11-03).

### The build (2026-11-03)

Files: `src/llm_harness/memory_index.py` (`resume`, line 126), `src/llm_harness/memory_command.py` (`_reindex`, call at line 149), `tests/test_memory_reindex.py`, and the docs `README.md`, `CHANGELOG.md`, `specs/memory.md`.

Code facts (read 2026-11-03):

- `IndexedMemoryStore.resume()` (`memory_index.py:126-137`) is synchronous, returns None and has no `await`. It clears the off reason, the pending-notice flag, the ensured collection and the cached embedder. It keeps `_repaired_users` and does not touch SQLite.
- `_reindex` (`memory_command.py:145-154`): a plain store answers `VECTORS_OFF` (146-147); an `IndexedMemoryStore` whose `index_off_reason` is not None gets `store.resume()` first (148-149), then the normal `reindex`. A failed retry answers `vector search is off: <new reason>` (151-152). `handle_memory_text` still drops the duplicate notice (196-198).
- Searches and writes still go through `_guarded`, which returns None while the index is off (`memory_index.py`, `_guarded`): nothing else calls `resume`. `grep resume src/llm_harness/*.py` finds only the definition and the one call.
- A model mismatch still raises from `_check_model` ("embedding model or dimension changed for this collection"), and `resume` clears `_collection`, so the check runs again.

Tests added in `tests/test_memory_reindex.py`: the file now collects 31 tests (`uv run pytest tests/test_memory_reindex.py --collect-only -q`) against the earlier 21, so 10 new by count; the engineer reported 11, and I do not know the reason for the difference. They use a `Wired` helper that builds the store through `vector_registry`, `recall_store` and `handle_memory_text`. They prove: recovery after a transient embed failure, after a locked store, a persistent failure answering the new reason with one notice, a model mismatch refused with the same reason, no reset and no extra call when the store is on, no embed or vector call for searches and writes while off, cancellation passing through the retry, `resume` on a store that is on keeping `_repaired_users`, no second lazy repair after recovery, and the factory and `ensure_collection` being called again on retry.

Mutation check (engineer): with the `resume()` call disabled, 6 tests failed (transient embed failure, locked store, persistent failure, cancellation, lazy-repair, factory and `ensure_collection` rebuild); the other new tests check what must not change. The call was restored; the coordinator re-read the code and re-ran the full suite.

Decisions beyond the spec: the `Wired` helper; helper parameters named `opened` to avoid ruff F811.

| # | Criterion | Check against the code | Result |
| --- | --- | --- | --- |
| 1 | `resume()` public, returns None, clears four items; reindex calls it when off | `memory_index.py:126-137`; `memory_command.py:148-149` | same |
| 2 | Transient failure then fixed: reindexed N of M, index on | `memory_command.py:150,154`; test `test_reindex_after_a_transient_embed_failure_switches_the_index_on` | same |
| 3 | Locked store then cleared | test `test_reindex_after_a_locked_vector_store_switches_the_index_on` | same |
| 4 | Persistent failure: new reason, one notice | `memory_command.py:151-152,196-198`; test `test_a_persistent_failure_answers_with_the_new_reason_and_one_notice` | same |
| 5 | Mismatch still refused | `memory_index.py` `_check_model`; test `test_a_model_mismatch_is_still_refused_with_the_same_reason` | same |
| 6 | Store that is on untouched | `memory_command.py:148` guard; test `test_a_store_that_is_on_is_not_resumed_and_makes_no_extra_call` | same |
| 7 | Off: searches and writes make no call | `memory_index.py` `_guarded`; test `test_searches_and_writes_while_off_make_no_embed_or_vector_call` | same |
| 8 | `CancelledError` passes through | `_guarded` catches `Exception` only; test `test_cancellation_inside_the_retry_passes_through_and_a_later_reindex_works` | same |
| 9 | Docs | `README.md` Vector search section and `/memory reindex` rules and table row, `CHANGELOG.md` line 49 edited in place, new `[gotcha]` appended to `specs/memory.md`, old one left; checked after my edits (grep `restart`, `stays off`, `until` in README finds no remaining recovery-by-restart claim) | same |
| 10 | `memory-vector-search.md` not edited | not edited by me; the difference is recorded here | same |

## Validation

Commands run by the coordinator on 2026-11-03:

- `timeout 290 uv run pytest -q`: 3212 passed, 0 failed, 0 skipped, 7 deselected (marker `live`; `addopts = -m "not live"` in `pyproject.toml`; the live tests were not run).
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: 135 files clean.
- Engineer's narrow run (memory, store, storage, cli, gui, tui tests): 1265 passed, 0 skipped, 0 failed; the 4 memory test files: 131 passed.
- Baseline before the build: 3202 passed; the difference of 10 equals the new tests counted above.

Not verified: nothing ran against a real Ollama server or the real vector backend. The Ollama embedding client has no timeout (`src/llm_harness/providers/ollama.py` ~124), so a retry against a server that hangs blocks until cancelled; out of scope (the human did not ask for it). The README says only fake models and stores were used in tests.

Known limits: no automatic retry or backoff (decision (a)); a user who runs `/memory reindex` repeatedly against a broken provider pays one failed call each time.

## Documentation Review

Checked: `README.md`, `CHANGELOG.md`, `specs/memory.md`, `specs/mission.md`, `specs/tech-stack.md`, `specs/roadmap.md` not edited.

Drift found and fixed:

| path | category | text |
| --- | --- | --- |
| `README.md` (Vector search, notice paragraph) | Correctness, High | said keyword-only "until you restart the program" and that `/memory reindex` does not switch the index back on; now says reindex switches it on and what a failed retry answers |
| `README.md` (`/memory reindex` rule) | Correctness, Medium | said the off answer is only for "switched itself off"; now describes resume and retry |
| `README.md` (`/memory` table row) | Consistency, Low | row now says it also switches vector search back on after a failure |
| `CHANGELOG.md` line 49 | Correctness, Medium | existing `/memory reindex` line edited in place to say it switches vector search back on after a failure |
| `specs/memory.md` line 60 | Correctness | the `[gotcha]` "nothing switches it back on" is false; left in place (append-only file) and superseded by a new `[gotcha]` entry appended at the end (maintenance criterion 2) |

CI review: not applicable (no `.github/workflows/`: `ls -a` shows no `.github` and no CI file, and `find . -maxdepth 3 \( -name '*.yml' -o -name '*.yaml' \) -not -path './.venv/*' -not -path './specs/*'` found nothing).

Constitution drift (proposals only): none needed. `specs/mission.md` mentions `/memory` reindex and falling back to keyword search on any problem, both still true; `specs/tech-stack.md` mentions reindex and lazy repair only as features. Neither file contains `until the process restarts`, `stays off` or `sticky`.

## Documentation Updates

- `README.md`: Vector search notice paragraph, `/memory reindex` rule and the `/memory` table row.
- `CHANGELOG.md`: existing `/memory reindex` line under `## [Unreleased]` / `### Added` extended; no new line.
- `specs/memory.md`: one `[gotcha]` entry appended; the old entry stays for a later maintenance pass.
- No constitution change.

Closeout checklist (2026-11-03):

- Validation recorded: done. Section Validation lists 3212 passed, 0 failed, 0 skipped, 7 deselected (marker `live`, not run; `addopts = -m "not live"`), run by the coordinator.
- README, CHANGELOG and every changelog the review resolved: done. README Vector search paragraph and `/memory reindex` rule and table row; CHANGELOG `/memory reindex` line (the only changelog; `specs/tech-stack.md` has no Artifacts table).
- `## Now` line removed from `specs/roadmap.md`: done; the section reads `Nothing in progress.`
- CI review recorded: not applicable (no `.github/workflows/`; see Documentation Review).
- The new function has a non-test use: done. `grep -rn "def resume\|\.resume(" src | cut -c1-110` prints `src/llm_harness/memory_index.py:126:    def resume(self) -> None:` (definition) and `src/llm_harness/memory_command.py:149:        store.resume()  # a failed retry turns the index off again with` (call).
- `validate-specs` passes: see the run recorded in the delivery report (exit 0).
