---
title: "/memory search QUERY shows the memories a search finds, without a model call"
kind: feature
status: done
version: 1
updated: 2026-11-04
branch: main
---

## Problem / Motivation

`/memory` can list, show, delete and reindex (`src/llm_harness/memory_command.py:27-34`, `USAGE`), but a person cannot search their own memories by hand: the only search is the agent's own, behind `MemoryStore.search(user_id, query, k)` (`src/llm_harness/memory.py:395`). To find a note the person pages through `/memory list` (20 per page, newest first). The roadmap item is `memory-search-command` (quick win, decided 2026-11-04).

## Acceptance Criteria

1. `parse_memory_command("/memory search")` and `"/memory search   "` give `MemoryCommand("usage")`; `"/memory search peanut butter"` gives action `search` with the query `peanut butter` (words joined by single spaces, so extra whitespace is collapsed).
2. `/memory search QUERY` answers one line per hit in the exact format of `/memory list` (`_list_line`), best first, at most `MAX_SEARCH_RESULTS` (10) lines.
3. With no hit the answer is `no memories match "<query>"`, the query escaped by `_escape` and clipped.
4. The command calls `store.search(user_id, query, MAX_SEARCH_RESULTS)`; a test with a recording store asserts `k == MAX_SEARCH_RESULTS`.
5. Another user's notes are never shown (two users, same words).
6. A hostile query and hostile stored text (control, escape, bidi, newline) never reach the answer raw: the answer has no control or bidi character and no newline inside a hit line.
7. A plain `MemoryStore` answers with keyword hits and never with `VECTORS_OFF`.
8. An `IndexedMemoryStore` whose index is off answers with keyword hits (and the one-time notice, see Approach).
9. The command makes no write and no model call (the test fixtures have no model; only the embedder the vector search already uses may be called, and a plain store calls none).
10. `USAGE` mentions `search`.
11. Docs: the README command table has a `/memory search QUERY` row and a note; `CHANGELOG.md` has an `Added` entry under `[Unreleased]`.

## Approach

Memory consulted: grep of specs/memory.md for `search` and `/memory` — 3 and 6 matching lines; 3 relevant (the `IndexedMemoryStore` index-off entries at lines 60 and 62, and the Postgres search entry at line 80), none about the command itself.

Verified in code:

- Parsing: `parse_memory_command` splits the line on whitespace and dispatches on the first word (`memory_command.py:52-73`); anything unknown returns `_usage()`. `MemoryCommand` has `action` and an int `number` (`memory_command.py:41-44`), so `search` needs a text field (a new `query: str = ""`).
- Formatting: `_list_line(memory)` builds `#id [keywords] (updated ...) preview` with `_escape` and clipping to `_PREVIEW_CHARS` (`memory_command.py:102-107`); `_list` uses it (`memory_command.py:120`).
- Dispatch: `run_memory_command` branches on `command.action` (`memory_command.py:161-179`).
- Notice: `handle_memory_text` returns `USAGE` for a usage command before looking up the store, then `store.take_notice()` is appended to the answer for every action; only a reindex answer that already says `vector search is off` drops it (`memory_command.py:192-212`). So a search appends the one-time notice like list, show and delete.
- Store: `MemoryStore.search` returns at most `max(1, min(MAX_SEARCH_RESULTS, k))` hits, best first (bm25 on SQLite, ts_rank on Postgres), and `[]` for an empty or stop-word-only query (`memory.py:395-408`; `MAX_SEARCH_RESULTS = 10` at `memory.py:139`).
- Index: `IndexedMemoryStore.search` takes keyword hits, fuses them with vector hits by rank, and returns the keyword hits only when the vector call yields none (index off) (`memory_index.py:184-197`); vector hits go through `self.get(user_id, id)`, which drops other users' rows (`memory_index.py:207-213`).

Design assumptions (the human may change any of them):

- (a) `/memory search QUERY`: QUERY is all remaining words joined by single spaces; empty gives usage.
- (b) One line per hit through the existing `_list_line` (no second formatter), up to `MAX_SEARCH_RESULTS`, best first, or `no memories match "<escaped query clipped>"`.
- (c) It calls `store.search(user_id, query, MAX_SEARCH_RESULTS)`, so it shows what the agent's own search finds, vector hits included when the index is on.
- (d) No model call except the embedding the vector search already makes; no writes.
- (e) Only the caller's memories; shared notes come later in `memory-shared-scope`.
- (f) The query and stored text go through `_escape`.
- (g) `USAGE` gets the new form.
- (h) A plain `MemoryStore` (vectors off) works through keyword search; no `VECTORS_OFF` answer.
- (i) The one-time vector-off notice is appended as for other actions.

Test scaffolding to reuse: `store` fixture (`MemoryStore` on real SQLite, `tests/test_memory_command.py:109-111`), the `run` helper (`:114-117`), `StubStore` and `hostile_store` (`:189-210`; `StubStore` has no `search`, so add one), the parse table (`:48-87`); for the index, `Rig` and `seed_unindexed` (`tests/test_memory_reindex.py:32,48`) and `MemoryStore(Rig(db).store._relational)` for a plain store (`:290`).

Risk, query length: the command does not cap the query. The keyword search keeps only the first `_MAX_QUERY_TERMS` (32) meaningful words (`memory.py:140`, `:272-281`), and the vector search embeds only the first `MAX_QUERY_EMBED_CHARS` (1000) characters (`memory_index.py:34`, `:288`). A very long query therefore still works and the tail is silently ignored; the "no match" answer clips the echoed query. Unverified: the exact clip length chosen for the echo (to be decided at implementation).

## Affected Areas

- `src/llm_harness/memory_command.py`: `MemoryCommand` (query field), `parse_memory_command`, `USAGE`, `run_memory_command`, a `_search` function.
- `tests/test_memory_command.py` and `tests/test_memory_reindex.py` (index-off keyword fallback).
- `README.md` command table (around line 1759) and a note; `CHANGELOG.md` `Added`.
- `/help` (`help-command`) later reads this command from its own table.

## Out of Scope

- Shared notes (`memory-shared-scope`).
- Paging of results (at most 10 hits).
- Search filters.
- `--all`.

## Implementation Notes

Done as approved; deviations: none known. The command is spelled `/memory search WORDS` (the spec title says QUERY; the field is `query`).

- `src/llm_harness/memory_command.py`: `MemoryCommand.query` field (line 48); parse at `if verb == "search"` (line 73); `_search(store, user_id, query)` (line 155) calls `store.search(user_id, query, MAX_SEARCH_RESULTS)` and formats hits with `_list_line`; no hit answers `no memories match "<escaped query>"` clipped to `_PREVIEW_CHARS` (80: 77 characters plus `...`); dispatch `if command.action == "search"` in `run_memory_command` (line 188); `USAGE` (line 27) has `/memory search WORDS` and the line `search  the memories a search finds, best first, at most 10`.
- Tests: 4 parse rows and new tests in `tests/test_memory_command.py`; 2 tests in `tests/test_memory_reindex.py` (index off gives keyword hits and the one-time notice; a plain `MemoryStore` gives keyword hits, never `VECTORS_OFF`); `test_usage_mentions_reindex` adjusted.
- Counts: engineer-reported 3 targeted files 121 passed; coordinator-verified full suite 3840 passed (baseline before the change 3826).
- Non-test call sites: parse and dispatch only; `command.query` is read only in the dispatch.

## Validation

- Full suite, run by the coordinator: `uv run pytest -q -p no:cacheprovider` gave 3840 passed, 0 failed, 0 skipped, 265 deselected, 1 warning (pre-existing aiohttp class warning), 141.7 s. The 265 deselected carry the marker `live`, which the project default `-m "not live"` excludes; they call real services and were not requested.
- Fail-before (engineer): passing k=1 in `_search` made 4 tests fail; restored.
- `ruff check` and `ruff format --check` clean (engineer-reported, not re-run by the technical-writer).
- Real offline run (coordinator, scratch SQLite, `MemoryStore`, `run_memory_command`): `/memory search green   tea` gave one list line with ann's note only (bob's note with the same words not shown); `/memory search zebra` gave `no memories match "zebra"`; `/memory search` gave the usage text; hostile `\x1b[31mtea` gave `no memories match "\u001b[31mtea"`.
- Unverified: the `handle_memory_text` path for search (the one-time notice appended there) has no test; a long query on the vector side is untested.

## Documentation Review

- `CHANGELOG.md`: yes, needed and done (`Added` under `[Unreleased]`); it is the single changelog, and `specs/tech-stack.md` has no `### Artifacts` table.
- `README.md`: yes, needed and done (command table row, note, intro sentence and the "any other form" bullet in "The /memory command").
- Constitution files: `specs/mission.md` line 21 lists "the `/memory` command to list, show, delete and reindex notes" and does not mention search; it was proposed to the human at review time and updated on approval (v22, see Documentation Updates). `specs/tech-stack.md` names `memory_command.py` (`/memory`) in the `memory` entry without listing subcommands; no change needed.
CI review: not applicable (no .github, Makefile or Dockerfile exists; verified with ls -a); `pyproject.toml` has only `[project.scripts]` and `git ls-files` shows no release or publish script.

## Documentation Updates

- `README.md`: new row `/memory search WORDS` in the command table, a note below the table, "searches" added to the intro sentence, and `search` and `reindex` added to the "any other form prints the usage text" bullet.
- `CHANGELOG.md`: one `Added` line for `/memory search WORDS`.
- `specs/memory.md`: one `[gotcha]` entry (`handle_memory_text` appends `take_notice()` to every /memory action).
- `specs/roadmap.md`: the `## Now` line for this feature removed (section back to `Nothing in progress.`).
- `specs/mission.md` updated on human approval 2026-11-04 (v22).
