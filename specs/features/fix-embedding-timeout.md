---
title: Fix embedding timeout
kind: bugfix
status: done
version: 1
updated: 2026-11-03
branch: main
---

## Problem / Motivation

An embedding call has no time limit, so a server that accepts the connection and never answers hangs the caller until the user cancels. The Ollama client is built without a timeout (`src/llm_harness/providers/ollama.py:124`, `ollama.AsyncClient(host=config.base_url)`); the installed ollama library defaults `timeout` to None (`.venv/lib/python*/site-packages/ollama/_client.py:90`). The same client serves chat streaming, so a client-wide timeout would break slow local chat. `Embedder.embed` (`src/llm_harness/embeddings.py:49-54`) is the single call path for every provider (Ollama, OpenAI-compatible, Vertex), and it awaits `self._provider.embed(...)` with no bound.

A hang would block memory writes, memory search, automatic recall, reflection's closest-notes search and `/memory reindex`, all of which reach `embedder.embed` through `IndexedMemoryStore` (`src/llm_harness/memory_index.py:287-292`). Found by reading the code during the real-model check of `specs/features/memory-vector-search.md` (Known limits); out of scope there and in `specs/features/memory-vector-recovery.md`. The human asked on 2026-11-03 to fix it. The real check measured about 2.5 s for the first embed including model load and 20-30 ms afterwards (as reported by the engineer, not re-run), so 60 s leaves a wide margin.

## Acceptance Criteria

1. `Embedder.embed` bounds the provider call with `asyncio.timeout(EMBED_TIMEOUT_SECONDS)`; the named constant is 60 and lives in `src/llm_harness/embeddings.py` next to the `Embedder`.
2. On expiry it raises `EmbeddingError` with a short message naming the model and the seconds, and no secret. DECIDED by the coordinator, announced to the human who did not object (not chosen by the human): the error kind is `connection`, because `ErrorKind` has no timeout value (`src/llm_harness/types.py:8-10`) and the Vertex adapter already classifies `TimeoutError` as a connection error (`src/llm_harness/providers/vertex.py:40-48`); a new kind would touch `types.py`, `SwitchReason` and the router, against criterion 6.
3. `asyncio.CancelledError` from the caller passes through unchanged; a cancelled embed is not turned into a timeout.
4. A fast call is unaffected: no extra await, and the result and vector-length checks (`embeddings.py:55-64`) are unchanged.
5. The memory index treats the timeout like any other embedding error: the index switches off with one notice that names the timeout, the write or search result is unchanged, and keyword search continues. No code change is expected in `memory_index.py` (`_guarded` catches `Exception` and records `redact(f"{type(error).__name__}: {error}")`, `memory_index.py:271-281`); a test through `IndexedMemoryStore` proves it.
6. Chat paths and the provider adapters are untouched.
7. Tests are offline and fast: a fake provider whose `embed` never returns (`await asyncio.Event().wait()`), with the constant patched to a small value by `monkeypatch` on the module attribute, so the test takes milliseconds.
8. README (embedding and memory section) and CHANGELOG say embeddings give up after 60 seconds. CHANGELOG: one user-facing line under `### Fixed`.

## Approach

Memory consulted: `timeout\|embedder\|embed` (case-insensitive) on `specs/memory.md` — 4 hits (lines 41, 45, 49, 61), 1 relevant: line 45 (bound test waits with `asyncio.wait_for(..., timeout=5)` so a broken gate fails fast); the other 3 do not concern embedding timeouts.

Smallest change in `src/llm_harness/embeddings.py`: add `EMBED_TIMEOUT_SECONDS = 60` above the `Embedder`, and wrap the provider call at line 52 in `async with asyncio.timeout(EMBED_TIMEOUT_SECONDS):`, catching `TimeoutError` outside the block and raising `EmbeddingError(<kind>, "model <id> gave no answer within <seconds> seconds")`. `asyncio.timeout` converts only its own expiry into `TimeoutError`, so a caller's `CancelledError` passes through. The same pattern exists in `src/llm_harness/orchestrator.py:628-631` (`async with asyncio.timeout(REFLECTION_TIMEOUT_SECONDS)`, message `no answer within {REFLECTION_TIMEOUT_SECONDS:g} seconds`, constant 60 at line 96); that constant is not changed. The timeout sits in the `Embedder`, not the Ollama adapter, so all providers are covered and the shared chat client is not given a timeout.

Tests: in `tests/test_embeddings.py` add a timeout test and a cancellation test; in `tests/test_memory_index_store.py` add one test through `IndexedMemoryStore` with the real `Embedder` over a never-answering fake provider and the constant patched small.

## Affected Areas

- `src/llm_harness/embeddings.py`
- `tests/test_embeddings.py`, `tests/test_memory_index_store.py`
- `README.md` (embedding models and vector search sections)
- `CHANGELOG.md`

## Out of Scope

- A config key for the timeout.
- A timeout on chat or on the Ollama client.
- Retries or backoff.
- A timeout for the vector store.
- Changing `REFLECTION_TIMEOUT_SECONDS`.

## Implementation Notes

Design approved 2026-11-03 (option: timeout in Embedder, 60 s, all providers; the other options were an Ollama-adapter-only timeout and a 30 s timeout in Embedder). Baseline before the build: full suite 3212 passed, 0 failed, 0 skipped, 7 deselected (marker live), run by the coordinator the same day; only specs and docs changed since. Stayed on main.

### The build (2026-11-03)

Files: `src/llm_harness/embeddings.py`, `tests/test_embeddings.py`, `tests/test_memory_index_store.py`, `tests/fakes.py` (new fake `HangingEmbedder`). No other code changed.

Code (read): `EMBED_TIMEOUT_SECONDS = 60` sits above `EmbeddingError` (`src/llm_harness/embeddings.py:20`). `Embedder.embed` wraps only the provider call in `asyncio.timeout(EMBED_TIMEOUT_SECONDS)` (`embeddings.py:59-62`). On `TimeoutError` it raises `EmbeddingError("connection", "embedding model <id> did not answer within <n> seconds")` from the original error (`embeddings.py:63-68`). `CancelledError` is not caught, so it passes through. The vector-length loop follows unchanged (`embeddings.py:69-77`).

Tests and what they prove (read):

- `tests/test_embeddings.py`: a never-answering provider gives `EmbeddingError` of kind `connection`, naming the model id and `0.05 seconds`, with a `TimeoutError` cause and one provider call; cancelling the caller raises `CancelledError`, not an embedding error; a fast provider returns its vectors and learns its dimension; the default is 60.
- `tests/test_memory_index_store.py` (`test_an_embedder_that_never_answers_turns_the_index_off`): through `IndexedMemoryStore` with the never-answering embedder, `save` still returns the memory, the index is off, exactly one single-line notice contains `did not answer within 0.05 seconds`, a second save makes no second provider call, and keyword search finds the note.
- Fake: `HangingEmbedder` records the call and waits on an `asyncio.Event` that is never set (`tests/fakes.py:152-166`).

Mutation check (engineer): with the `asyncio.timeout` line removed, the new test hung until the 20 s shell kill; after restoring it, it passed in 0.97 s. The coordinator re-read the restored code and re-ran the full suite.

Decision on the error kind (criterion 2): the brief and code disagreed (see the criterion). The coordinator decided `connection` and announced it to the human, who did not object; the human did not choose it. This criterion 2 text change, from OPEN to the decision, is the only edit to the Acceptance Criteria; `version` stays 1.

Module-object patching: the tests patch with `monkeypatch.setattr(embeddings, "EMBED_TIMEOUT_SECONDS", 0.05)` on the imported module. Patching by the string path `llm_harness.embeddings.EMBED_TIMEOUT_SECONDS` failed with `AttributeError` when `tests/test_providers*.py` ran first; that file does `del sys.modules[name]` (`tests/test_providers.py:58`). The engineer did not confirm that as the cause.

Decisions beyond the spec: none in code; the message wording is `embedding model <id> did not answer within <n> seconds` (the Approach had drafted `model <id> gave no answer within ...`).

| # | Criterion | Check against the code | Result |
| --- | --- | --- | --- |
| 1 | `asyncio.timeout(EMBED_TIMEOUT_SECONDS)`, constant 60 in `embeddings.py` | `embeddings.py:20`, `:59` | same |
| 2 | `EmbeddingError` naming model and seconds, no secret | `embeddings.py:63-68`; kind `connection` (decided, see above) | differs from the original OPEN text only by the decision |
| 3 | `CancelledError` passes through | only `TimeoutError` is caught, `embeddings.py:63`; cancellation test | same |
| 4 | Fast call unaffected, length checks unchanged | `embeddings.py:69-77`; fast-provider test | same |
| 5 | Memory index: one notice naming the timeout, results unchanged, no `memory_index.py` change | `_guarded` catches `Exception` (`memory_index.py:277-278`), `embedder.embed` at `memory_index.py:291`; index-store test | same |
| 6 | Chat paths and adapters untouched | the only `src` change is `embeddings.py`; `providers/vertex.py:40-48` was read, not edited | same |
| 7 | Offline, fast tests, constant patched on the module | tests above use `monkeypatch.setattr(embeddings, ...)` and `HangingEmbedder` | same |
| 8 | README and CHANGELOG say 60 seconds | `README.md:317-319`, `README.md:747-749`, `CHANGELOG.md` `### Fixed` | same (after the edits listed under Documentation Updates) |

## Validation

Commands run by the coordinator on 2026-11-03:

- `timeout 290 uv run pytest -q`: 3217 passed, 0 failed, 0 skipped, 7 deselected (marker `live`; `addopts` is `-m "not live"` in `pyproject.toml`; those tests were not run). Baseline before the build: 3212 passed, so 5 tests were added (4 in `tests/test_embeddings.py`, 1 in `tests/test_memory_index_store.py`).
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: 135 files already formatted.

Not verified: no run against a real hung server or a real Ollama for this change. The 60 s value is a judgement; the first embed measured about 2.5 s for bge-m3 in an earlier real check (as reported by the engineer, not re-run).

Known limits:

- For the memory index the 60 s wait happens once per process, because the index then switches off and later calls make no provider call (until `/memory reindex`).
- Chat and the other call paths are unchanged.
- There is no config key for the timeout.

## Documentation Review

Checked: `README.md`, `CHANGELOG.md`, `specs/memory.md`, `specs/mission.md`, `specs/tech-stack.md`.

Drift fixed:

- `README.md` (Embedding models rules and Vector search failure paragraph) did not say what happens when an embedding server never answers; it now does.
- `CHANGELOG.md` had no `### Fixed` section under `## [Unreleased]`; added after `### Removed`.
- Criterion 2 of this spec still said OPEN; updated to the decision.

Changelog entry: {path: `CHANGELOG.md`, category: `Fixed`, text: "An embedding call that gets no answer now fails after 60 seconds instead of waiting forever; it shows as a connection error, and memory vector search switches off with one notice."}

CI review: not applicable (`ls -a` shows no `.github` directory; `find . -maxdepth 3 \( -name '*.yml' -o -name '*.yaml' \) -not -path './.venv/*' -not -path './specs/*'` printed nothing, so there is no CI config and no release workflow).

Constitution drift (proposals only, nothing edited): grepped `specs/mission.md` and `specs/tech-stack.md` for `timeout`, `embedding`, `Embedder`. `specs/mission.md` had no hits. `specs/tech-stack.md` (Embeddings bullet, line 62) says `Embedder` "raises `EmbeddingError` carrying an `ErrorKind`" and "a failing embedding model is reported as an error"; both are still true. Result: none needed. Optional, not required: append to the Embeddings bullet "; a provider call with no answer within 60 seconds raises `EmbeddingError` of kind `connection`".

## Documentation Updates

- `README.md`: new rule bullet in "Embedding models" (60 seconds, connection error, no config key, chat not affected) and two sentences in the "Vector search" failure paragraph (the timeout counts as an error: one notice, keyword search continues).
- `CHANGELOG.md`: new `### Fixed` section under `## [Unreleased]` with one line.
- `specs/memory.md`: two `[gotcha]` entries (`ErrorKind` has no timeout member; patch module constants on the module object).
- This spec: criterion 2 text (OPEN to decision), Implementation Notes, Validation, Documentation Review and this section.
- `specs/tech-stack.md`: the Embeddings bullet gained "a provider call with no answer within 60 seconds raises `EmbeddingError` of kind `connection`"; `version` 25 to 26, `updated: 2026-11-03`. The human approved this optional addition on 2026-11-03 (the proposal in Documentation Review above).
- `specs/roadmap.md`: the `## Now` line for this feature removed; `Nothing in progress.` left (a `## Now` removal alone does not bump `version`).

### Closeout checklist (2026-11-03)

- [x] Validation recorded with counts: 3217 passed, 0 failed, 0 skipped, 7 deselected (marker `live`), run by the coordinator (see Validation).
- [x] README and CHANGELOG updated: `README.md` Embedding models rule and Vector search paragraph; `CHANGELOG.md` `### Fixed` line.
- [x] `## Now` removed: `specs/roadmap.md` now says `Nothing in progress.`; `grep -n "Artifacts" specs/roadmap.md` printed nothing, so there is no Artifacts table.
- [x] CI review recorded: not applicable (see Documentation Review).
- [x] Every new function, type or config key has a non-test use: there is no new function, type or config key. The only new name is the constant `EMBED_TIMEOUT_SECONDS`, used inside `embeddings.py`. `grep -rn "EMBED_TIMEOUT_SECONDS" src | cut -c1-110` printed:
  - `src/llm_harness/embeddings.py:20:EMBED_TIMEOUT_SECONDS = 60`
  - `src/llm_harness/embeddings.py:59:            async with asyncio.timeout(EMBED_TIMEOUT_SECONDS):`
  - `src/llm_harness/embeddings.py:67:                f"{EMBED_TIMEOUT_SECONDS} seconds",`
- [x] `validate-specs` passes (see the closing run in the report).
