---
title: "Shared memory notes: an admin forgets a shared note for everyone"
kind: feature
status: done
version: 1
updated: 2026-11-04
branch: main
---

## Problem / Motivation

A shared note (`memory-shared-scope`, done) can be removed today only by its creator, after unpublishing it: `/memory delete ID` of a shared note answers `memory #N is shared; unpublish it first` for the owner and `memory #N is shared and read-only` for everyone else (`src/llm_harness/memory_command.py:165-168`). The store refuses to delete a shared note for any user, the owner included (`DELETE ... AND user_id = ? AND shared = 0`, `src/llm_harness/memory.py:390-398`). So when the creator is gone, or a bad note was published, no one can remove it for everyone; an admin cannot even unpublish another user's note, because `_publish` answers `no memory #N` unless the note is the caller's own (`memory_command.py:189-191`).

Human decisions (2026-11-04, recorded as decided):

- New command `/memory forget ID`: admins only (a `user_id` listed in `[tools] memory_admins`), and it works on any shared note, whoever created it.
- It deletes IMMEDIATELY. The answer echoes the removed text, like `/memory delete`. There is no confirmation step.
- `/memory delete ID` keeps its meaning and its existing shared-note answers (`memory #N is shared; unpublish it first` for the owner, `memory #N is shared and read-only` for others).
- Still holds: no model tool may ever write or delete a shared note. The admin identity is the self-chosen `user_id` listed in `[tools] memory_admins`.

Assumptions (the human may change them):

- (a) `forget ID` is parsed with the same positive-integer rule as `show` and `delete`; any other form (no id, `0`, `01`, a word, a second argument) gives `USAGE`.
- (b) Check order like `_publish` (`memory_command.py:185-188`): the admin check comes first, so a non-admin never causes a note lookup. A non-admin gets a NEW constant, `NOT_ADMIN_FORGET = "only memory admins may forget shared memories"`. Decision and reason: the alternative, generalising `NOT_ADMIN` to `only memory admins may publish, unpublish or forget shared memories`, would change an answer that four existing places pin by literal text (`tests/test_memory_command.py:470`, `tests/test_gui.py:1630`, `tests/test_tui.py:1233`, `tests/test_live_memory_postgres.py:946`) and the README answers table (`README.md:1843`); a second constant changes none of them and leaves the publish answer exactly as it is.
- (c) Only SHARED notes can be forgotten this way. After the admin check, `get` is used (it returns the caller's own notes and shared notes, `memory.py:424-433`): an unknown id or ANOTHER user's private note gives `no memory #N` (a private note is never revealed); the admin's own private note gives `memory #N is not shared; use /memory delete N`.
- (d) New store method `MemoryStore.forget_shared(id: int) -> Memory | None`: owner-agnostic, `DELETE FROM memories WHERE id = ? AND shared = 1 RETURNING <columns>`, so it cannot touch a private note even if called for a private id. It has no `user_id` parameter and does no authorisation; the admin check lives only in the command layer. The marker rows in `memory_vectors` go through the existing delete trigger (see Approach). `IndexedMemoryStore.forget_shared` also removes the vector point, mirroring `forget` exactly (see Approach).
- (e) Answer on success: `forgot shared memory #N: <text>`, the text escaped and clipped to 200 characters exactly like `/memory delete` (`_DELETED_CHARS`, `memory_command.py:42`, `:171`).
- (f) `handle_memory_text` is the only caller of `forget_shared` and receives only human input (`memory_command.py:254-266`). No tool module and no reflection module references `forget_shared`; a test pins it, like the existing `set_shared` one (`tests/test_memory_tools.py:563-568`).
- (g) `USAGE` gains `forget`; the COMMANDS table entry's argument string and description gain `forget`; `/help memory` still equals `USAGE` exactly; the drift tests stay green.
- (h) Docs: README (the command table, the Shared notes section, the answers table), a CHANGELOG `Added` line, and a `specs/memory.md` entry only if durable and new. A constitution edit to `specs/mission.md` is PROPOSED in Approach, not applied.

## Acceptance Criteria

One implementation step. All criteria 1 to 21 run offline with the existing fakes: a real `SqliteRelationalStore` on `tmp_path`, `FakeVectorStore` and `ConceptEmbedder` through the `Rig` of `tests/test_memory_index_store.py`, and the Postgres statement-text fake of `tests/test_memory_postgres.py`. Criterion 22 is the LIVE PostgreSQL run.

1. **Store: `forget_shared` deletes a shared note of another user and returns it.** On SQLite, alice publishes a note; `forget_shared(id)` returns a `Memory` equal to the row before (text, keywords, `user_id == "alice"`, `shared` True); `get` for alice and for bob then returns None, and a keyword search for bob finds nothing.
2. **Store: `forget_shared` cannot touch a private note.** For a private note and for an unknown id it returns None and the row is unchanged (`get` by the owner still returns it).
3. **Store: the marker rows are cleared through the delete trigger.** After `forget_shared` of an indexed shared note, `memory_vectors` has no row for that id and the rows of other notes stay; the full-text index no longer finds the note.
4. **Store: Postgres statement text.** On the Postgres fake, `forget_shared` issues one statement, `DELETE FROM memories WHERE id = ? AND shared = 1 RETURNING <columns>` with parameters `(id,)`, and builds the `Memory` from the returned row; no returned row gives None.
5. **Store: existing guards are untouched.** `forget(user, id)` still returns False for a shared note, for the owner and for another user (the existing `test_update_and_forget_refuse_a_shared_note_for_everyone` stays green).
6. **Indexed store: the point is removed.** On `IndexedMemoryStore` with `FakeVectorStore`, saving and publishing a note, then `forget_shared`: `rig.points()` no longer holds the id, `rig.markers()` is empty, and `rig.calls("delete")` is `[("delete", (COLLECTION, (str(id),)))]`, like `test_forget_deletes_the_vector_and_the_marker`.
7. **Indexed store: nothing to delete means no vector call.** `forget_shared` of a private or unknown id returns None and makes no vector `delete` call; in a new process (a fresh store on the same database) it deletes the old point, like `test_forget_in_a_new_process_deletes_the_old_vector`.
8. **Indexed store: index off and failing delete.** With the index already off, the SQL row is deleted, no exception escapes and no vector call is made. When the vector `delete` raises, the SQL row is still deleted, no exception escapes, `index_off_reason` is set and `take_notice` reports once.
9. **Plain store (vectors off).** `MemoryStore.forget_shared` works and touches no vector store.
10. **Command: a non-admin is refused.** With `memory_admins=[]`, with the default (no argument) and with a list that does not contain the caller, `/memory forget N` answers `NOT_ADMIN_FORGET` and the note still exists. The check comes before any lookup: an unknown id and another user's private id answer the same text for a non-admin.
11. **Command: an admin forgets another user's shared note and it disappears for everyone.** bob (creator) has a shared note; alice (admin) answers `forgot shared memory #N: <text>`; then `/memory list`, `/memory show N` and `/memory search` give no trace for alice, for bob (the creator's own list too) and for a third user; the stored row is gone.
12. **Command: a creator who is admin forgets their own shared note.** The answer is `forgot shared memory #N: <text>` and the note is gone.
13. **Command: private notes.** The admin asking for another user's PRIVATE note gets `no memory #N` and the note still exists (the owner still reads it); the admin's OWN private note gets `memory #N is not shared; use /memory delete N` and still exists; an unknown id gets `no memory #N`.
14. **Command: the echo.** The text in the answer is escaped like `/memory delete` (control and bidi characters as `\uXXXX`) and clipped to 200 characters with `...`.
15. **Command: usage forms.** `/memory forget`, `/memory forget 0`, `/memory forget 01`, `/memory forget x`, `/memory forget 1 2`, `/memory forget -1` and a superscript digit parse to the usage command; `/memory forget 3` and `/memory  forget\t12` parse to `MemoryCommand("forget", 3)` and `MemoryCommand("forget", 12)`.
16. **Command: `/memory delete` is unchanged.** The two shared-note answers (`is shared; unpublish it first`, `is shared and read-only`) and the deletion of an own private note are as before (the existing tests stay green), also for an admin.
17. **Command: `handle_memory_text` carries the admin list.** Through `handle_memory_text` without `memory_admins` the answer is `NOT_ADMIN_FORGET`; with `memory_admins=["ann"]` the answer is `forgot shared memory #N: ...`. On an indexed store the point is removed and a failed point removal appends the one-time notice to the answer.
18. **Front ends (GUI is primary).** A GUI test (NiceGUI `User`, like `test_memory_publish_in_the_gui_needs_an_admin`, `tests/test_gui.py:1641-1670`) types `/memory forget N` through `ask_memory`, which waits for the new `memory` answer element: admin gets the `forgot shared memory #N: ...` answer, non-admin and empty list get `NOT_ADMIN_FORGET`; the list afterwards has no `[shared]` note for the admin case; no user turn and no model request. A TUI test (like `test_memory_publish_in_the_tui_needs_an_admin`, `tests/test_tui.py:1244-1267`, answer read from the `note` element) and a plain-CLI test (like `test_plain_publish_needs_memory_admins_in_the_config`, `tests/test_memory_command.py:684-704`, with `memory_admins` in the config) cover the same three cases.
19. **Fail-before.** Each of these breaks at least one named test, shown by the engineer before the fix is restored: deleting the `if user_id not in admins` check of the forget branch; making `forget_shared` use `AND shared = 0` or `AND user_id = ?`; dropping the `_remove` call of `IndexedMemoryStore.forget_shared`; removing the `memory_admins=` argument in `src/llm_harness/gui/app.py`.
20. **Tool safety pin.** A test asserts that `forget_shared` does not occur in the source of `llm_harness.memory_tools` and `llm_harness.memory_reflection` (like `test_no_memory_tool_module_can_reach_set_shared`), and that among the modules of `src/llm_harness` only `memory.py`, `memory_index.py` and `memory_command.py` contain it. The `memory_forget` tool still cannot delete a shared note (`MemoryStore.forget` returns False).
21. **USAGE, COMMANDS, help, docs.** `USAGE` names `forget` on its first line (`/memory forget ID`) and has a `  forget` description line saying memory admins only and for everyone; the COMMANDS entry has arguments `[list|search|show|delete|publish|unpublish|forget|reindex]` and a description that names `forget`; `help_detail` for each front end still equals `USAGE`; `tests/test_commands.py` (`test_memory_detail_is_exactly_the_memory_usage`, `test_memory_detail_is_not_a_copy`) stays green and the existing test that pins the arguments string (`tests/test_memory_command.py:613`) is updated to the new string. README (command table, Shared notes section, answers table, the list of accepted words at `README.md:1805`) and CHANGELOG `[Unreleased]` `Added` describe the command; `uv run pytest` (default suite) and `ruff check` / `ruff format --check` are green.
22. **LIVE PostgreSQL.** `tests/test_live_memory_postgres.py` gains an end-to-end test on the real server: alice saves and publishes a note, bob (creator or other user) cannot forget (`NOT_ADMIN_FORGET`), an admin forgets it with `/memory forget N`, the row and the `memory_vectors` marker rows are gone (`live.sql(...)`), and every user's list is empty; a private note of another user gives `no memory #N` and its row still exists. A second test uses the real `PgVectorStore` (`live_vector`, like `test_publish_command_with_the_real_pgvector_store_sets_the_payload`): after the forget the point is gone (vector search for another user finds nothing, and the collection holds no point of that id). Run with the recipe in that file's docstring (docker context `rancher-desktop`, two throwaway containers `llm-harness-live-pgmem` and `llm-harness-live-pgmemvec`): `uv run pytest -m live tests/test_live_memory_postgres.py tests/test_memory_store.py` with the three `LLM_HARNESS_LIVE_*` variables, 0 failed, 0 skipped. The engineer removes both containers afterwards (`docker --context rancher-desktop rm -f ...`) and checks that ports 15436 and 15437 are free. The human already approved using the two containers for this feature family.

## Approach

Memory consulted: grep of specs/memory.md for `forget|_remove|shared|trigger|handle_memory_text|payload|_fused` — 4 relevant entries (lines 82, 84, 85, 86; line 87, the live Postgres recipe, was found by its timestamp, not by this pattern): `handle_memory_text` appends `take_notice()` to every `/memory` answer, so `forget` gets it too; the vector payload and the two vector queries; `_fused` drops a vector-only hit unless `get` returns the note, which is what makes an orphan point harmless; schema step 3 added the `shared` column.

One implementation step, with its tests green and a run the human can look at.

1. Store (`src/llm_harness/memory.py`). `MemoryStore.forget_shared(self, id: int) -> Memory | None`: `await self._ensure_migrated()`, then `DELETE FROM memories WHERE id = ? AND shared = 1 RETURNING {_COLUMNS}` through `_query` with `(id,)`, like `forget` (`memory.py:390-398`); no row gives None, else `_row_to_memory(rows[0])`. `RETURNING` on `DELETE` is already used on both dialects by `forget`. The docstring says that only the human `/memory forget` command calls it, never a tool.
2. Marker rows. Verified, not assumed: the trigger `memories_vectors_gone AFTER DELETE ON memories` runs `DELETE FROM memory_vectors WHERE memory_id = old.id` on SQLite (`memory.py:82-84`), and the Postgres tuple creates `memories_clear_markers()` and the trigger `memories_vectors_gone AFTER DELETE ON memories FOR EACH ROW` (`memory.py:126-135`). So `forget_shared` needs no explicit marker delete (unlike `set_shared`, whose `UPDATE` does not fire the `UPDATE OF text, keywords` triggers, `memory.py:404-406`). Whether the Postgres delete trigger fires for `forget_shared` on a real server is NOT verified yet; criterion 22 checks it.
3. Indexed store (`src/llm_harness/memory_index.py`). `IndexedMemoryStore.forget_shared(self, id)` mirrors `forget` (`memory_index.py:186-190`): `memory = await super().forget_shared(id)`; when `memory is not None`, `await self._guarded(partial(self._remove, id))`; return `memory`. What `forget` does exactly (read): the SQL delete first, then the point delete under `_guarded` only when a row was deleted; `_guarded` returns None without running anything when the index is already off, and turns the index off with a one-time notice when the step raises (`memory_index.py:323-332`); `_remove` finds the collection from the meta row in a new process, returns without a vector call when nothing was ever indexed for the model, and otherwise calls `self._vectors.delete(collection, [str(memory_id)])` (`memory_index.py:364-375`). Index off: the SQL row is deleted, the point is left (see Risks). Plain `MemoryStore`: SQL only.
4. Command (`src/llm_harness/memory_command.py`). `parse_memory_command`: add `forget` to the tuple of id verbs at `:76`. `NOT_ADMIN_FORGET` next to `NOT_ADMIN` (`:174`). A new `_forget(store, user_id, memory_id, admins)`: if `user_id not in admins` return `NOT_ADMIN_FORGET`; `memory = await store.get(user_id, memory_id)`; None gives `no memory #N`; `not memory.shared` gives `no memory #N` when `memory.user_id != user_id` (it cannot happen through `get`, kept as the guard that never reveals a private note) and `memory #N is not shared; use /memory delete N` for the caller's own; then `forgotten = await store.forget_shared(memory_id)`; None (a concurrent change) gives `no memory #N`; success gives `forgot shared memory #N: {_escape(_clip(forgotten.text, _DELETED_CHARS))}`. `run_memory_command` gets one branch passing `memory_admins`. `handle_memory_text` needs no change: it already forwards `memory_admins` and appends the notice. `MemoryCommand.action` comment lists `forget`; `USAGE` gains `/memory forget ID` on the first line and a `  forget` line (`memory admins only: delete one shared memory for every user at once`).
5. `src/llm_harness/commands.py`: the entry's argument string becomes `[list|search|show|delete|publish|unpublish|forget|reindex]` and the description `list, search, show, delete, publish, forget and reindex your memories` (it omits `unpublish` today, `commands.py:48-49`; left as is). The detail stays `MEMORY_USAGE`, so `/help memory` equals `USAGE` by construction.
6. No change to `cli.py`, `tui/app.py`, `gui/app.py` or `config.py`: the three callers already pass `memory_admins` (verified in the shared-scope spec's call-site notes) and the answer is plain text.
7. Tests: the files listed in Affected Areas; the live tests last, run with the docstring recipe.

Risks, inside the approach:

- Irreversible for every user and immediate, by decision. Mitigation: the answer echoes the removed text so an admin can re-save it, but the echo is clipped to 200 characters and a re-saved note is a new private note with a new id that must be published again. There is NO audit log (who forgot what, when): put Out of Scope.
- Stale vector point. When the point delete fails, the index switches itself off with a notice (`_guarded`) and the SQL row is already gone; with the index already off, the point is not touched at all. `/memory reindex` does NOT remove orphan points: `_reindex` only selects rows of `memories` and upserts them (`memory_index.py:260-286`), and the only vector `delete` call in the module is `_remove` (`:375`). A leftover point is harmless to results: a vector hit of a removed id goes through `_fused`, which calls `get` and drops the hit because `get` returns None (`memory_index.py:225-234`, the comment on the call says it drops orphans). The point stays as dead data in the vector store. Not verified: whether SQLite can reuse the id of a deleted row for a new note (`id INTEGER PRIMARY KEY` without `AUTOINCREMENT`); if it does, saving that note upserts the point with the same id and overwrites the orphan (the upsert is idempotent, `memory_index.py:349-352`), so no wrong text can attach; this reading is NOT run.
- Admin identity is self-chosen. `user_id` is a name the user picks, with no authentication; anyone who sets `user_id` to an admin's name can delete every shared note. This is the same limit as publish (documented in README Known limits) and is not access control. `MemoryStore.forget_shared` itself has no authorisation, so the safety of the feature rests on the single caller `_forget` and on the pin of criterion 20.
- Prompt injection and tools. A model must never delete a shared note: no tool or reflection module may reference `forget_shared`; `memory_forget` keeps calling the owner-scoped `forget`, which refuses shared notes. A model cannot type the command either: `/memory` lines reach `handle_memory_text` only from human input before the orchestrator.
- Race. Between `get` and `forget_shared` another admin may have forgotten the note: the second call returns None and the answer is `no memory #N`, nothing is deleted twice.
- Postgres: the `DELETE ... RETURNING` with `shared = 1` and the trigger are exercised live only by criterion 22; statement text offline.

Constitution impact (proposed, NOT applied; the human must approve):

`specs/mission.md:21` (the "what exists today" paragraph) says shared notes are "published and unpublished with `/memory publish ID` and `/memory unpublish ID` by a user listed in `[tools] memory_admins`, never written by an agent tool". Proposed phrase, appended after "never written by an agent tool": "; a shared note can also be forgotten for every user at once with `/memory forget ID` by a user listed in `[tools] memory_admins`, with no confirmation and no audit log". The short form the brief asked for is "and forgotten by an admin": the sentence would then read "published and unpublished with `/memory publish ID` and `/memory unpublish ID`, and forgotten by an admin with `/memory forget ID`, by a user listed in `[tools] memory_admins`". The first, longer phrase is preferred because it states the immediacy. `specs/tech-stack.md` and `specs/roadmap.md` need no change; `specs/roadmap.md` has the item `memory-shared-admin` (to be moved to Now and then removed by the normal `/code` bookkeeping, not by this spec).

Historical spec superseded in part (not edited): `specs/features/memory-shared-scope.md` assumption (f) and Out of Scope ("Admin forgetting of another user's shared note for everyone (`memory-shared-admin`)") are now in scope here.

## Affected Areas

Source:

- `src/llm_harness/memory.py` (`MemoryStore.forget_shared`).
- `src/llm_harness/memory_index.py` (`IndexedMemoryStore.forget_shared`).
- `src/llm_harness/memory_command.py` (`USAGE`, `MemoryCommand` comment, `parse_memory_command`, `NOT_ADMIN_FORGET`, `_forget`, `run_memory_command`).
- `src/llm_harness/commands.py` (the `/memory` entry argument and description strings).

Tests:

- `tests/test_memory_store.py`, `tests/test_memory_postgres.py` (criteria 1 to 5, 9), `tests/test_memory_index_store.py` (6 to 8), `tests/test_memory_command.py` (10 to 17, 20 partly, 21; the arguments-string pin at `:613` changes by design), `tests/test_gui.py`, `tests/test_tui.py` (18), `tests/test_memory_tools.py` (20), `tests/test_commands.py` (21, only if a new assertion is needed), `tests/test_live_memory_postgres.py` (22).

Docs: `README.md` (command table near `:1775`, the accepted-words sentence near `:1805`, the Shared notes section and answers table near `:1820-1850`), `CHANGELOG.md` (`[Unreleased]` `Added`), `specs/memory.md` (an entry only if a durable new fact appears), and the proposed `specs/mission.md` edit once approved.

## Out of Scope

- A confirmation step (the deletion is immediate by decision).
- An audit log of who forgot which note.
- Listing who created a shared note.
- Undo of a forget.
- Bulk forget (several ids, all shared notes of a user).
- A way for a non-admin to request the removal of a shared note.
- Removing orphan vector points (`/memory reindex` does not do it; harmless because `_fused` drops them).
- Real authentication of `user_id`.

## Implementation Notes

Done as one step, as approved; no deviation from the Approach.

Source (the engineer reported it; the coordinator re-read the call sites):

- `src/llm_harness/memory.py`: `MemoryStore.forget_shared` (line 400), `DELETE FROM memories WHERE id = ? AND shared = 1 RETURNING <columns>`.
- `src/llm_harness/memory_index.py`: `IndexedMemoryStore.forget_shared` (line 192), SQL delete first, then `_remove` under `_guarded`.
- `src/llm_harness/memory_command.py`: `forget` added to the id verbs of `parse_memory_command` (line 79), `NOT_ADMIN_FORGET` (line 206), `_forget` (lines 209-230), the run branch (line 274), `USAGE` (lines 31 and 38), the `MemoryCommand.action` comment (line 51).
- `src/llm_harness/commands.py`: the `/memory` entry (lines 48-49).
- No change to `cli.py`, `tui/app.py`, `gui/app.py` or `config.py`, as planned.

Tests (engineer): new tests in `tests/test_memory_store.py`, `tests/test_memory_postgres.py`, `tests/test_memory_index_store.py`, `tests/test_memory_command.py`, `tests/test_gui.py`, `tests/test_tui.py`, `tests/test_memory_tools.py` (the pins `test_no_memory_tool_module_can_reach_forget_shared` and `test_only_the_store_and_the_command_modules_mention_forget_shared`, lines 571 and 579) and `tests/test_live_memory_postgres.py`. Changed by design: the usage parse list and the arguments-string pin in `tests/test_memory_command.py`, and `tests/test_memory_reindex.py::test_usage_mentions_reindex`.

Documentation (technical-writer): see Documentation Updates.

## Validation

Reported by the coordinator, who re-ran everything:

- Default suite, `uv run pytest -q -p no:cacheprovider`: 4016 passed, 0 failed, 0 skipped, 285 deselected, 1 warning (the pre-existing google-genai DeprecationWarning), 158.6 s. Baseline before this feature: 3975 passed. The 285 deselected are the `live` tests: `pyproject.toml:51` has `addopts = '-m "not live" -p nicegui.testing.user_plugin'`.
- LIVE suite on a real PostgreSQL 17 with pgvector (two throwaway docker containers on the local Rancher Desktop context, command per the docstring of `tests/test_live_memory_postgres.py`): baseline 142 passed, 0 skipped; after the feature 148 passed, 0 failed, 0 skipped. The live tests added for criterion 22: `test_forget_command_on_the_real_postgres_store` (line 1000) and `test_forget_command_with_the_real_pgvector_store_removes_the_point` (line 1051). `NOT_ADMIN_FORGET` is defined in that file at line 996 for them.
- `ruff check` and `ruff format --check`: clean (coordinator re-ran).
- Cleanup: the coordinator removed both containers and checked that ports 15436 and 15437 are free.

Fail-before proofs (reported by the engineer, not re-run by the technical-writer):

1. Removing the admin check in `_forget` broke 10 tests (offline, GUI, TUI, plain CLI).
2. Dropping `AND shared = 1` broke 3 offline tests and 1 live Postgres test, all at store level. The command-level live tests did NOT fail for this proof, because `_forget` calls `get` before `forget_shared`.
3. Dropping the `_remove` call in `IndexedMemoryStore.forget_shared` broke 4 tests.

Call-site evidence (the technical-writer ran `grep -n`):

- `grep -n "forget_shared" src/llm_harness/*.py`: `memory_command.py:223` (inside `_forget`), `memory_index.py:192-193`, `memory.py:400`. No tool or reflection module.
- `grep -n "NOT_ADMIN_FORGET" src/llm_harness/*.py`: defined `memory_command.py:206`, returned `memory_command.py:215`.
- `grep -n '"forget"' src/llm_harness/memory_command.py`: `:51` (comment), `:79` (parse), `:274` (run branch).

Real offline demo (coordinator, scratch SQLite, `memory_admins=("carol","alice")`):

- bob (non-admin) `/memory forget 1` answered `only memory admins may forget shared memories`; carol with an empty admin list, the same.
- carol `/memory forget 2` (bob's private note) answered `no memory #2`; bob's note still existed.
- carol on her own private note 3 answered `memory #3 is not shared; use /memory delete 3`.
- `/memory forget 99` answered `no memory #99`; `/memory forget` printed the usage.
- carol forgot alice's shared note: `forgot shared memory #1: Deploys go through the dev cluster first`; afterwards alice's list was `(no memories)` and bob's no longer showed it; a repeat answered `no memory #1`.

Not verified (said openly):

- Removing `memory_admins=` from `src/llm_harness/gui/app.py` was not demonstrated as a break (criterion 19, fourth item, not done).
- The TUI and plain-CLI wiring has no separate fail-before.
- SQLite row-id reuse after a delete: not run.
- No hand-run session in the real GUI or terminal UI.

## Documentation Review

- `CHANGELOG.md` `[Unreleased]` `Added`: had no line for the command; added one.
- `README.md`: the `/memory` command table, the accepted-words sentence, the Shared notes section and answers table, the `memory_admins` config comment, the Agent memory isolation sentence and Known limits named only publish and unpublish, or said admins cannot forget; all updated. The `/help` per-front-end table lists commands, not subcommands: no change needed.
- Constitution: `specs/mission.md` (version 24) was updated on human approval, 2026-11-04, with the `/memory forget ID` phrase. `specs/tech-stack.md` memory paragraph did not mention publish or forget; one sentence added, version 34 to 35. `specs/roadmap.md` needed only the `## Now` line removal.
- `specs/memory.md`: two new `[gotcha]` entries; no duplicate found by grep for `orphan|shared = 1|forget_shared`.

CI review: not applicable (no .github, Makefile or Dockerfile exists; verified with ls -a, no release script)

## Documentation Updates

- `README.md`: `memory_admins` config comment (`publish or forget`); Agent memory sentence on shared notes; `/memory delete ID` row (mentions `/memory forget`); new `/memory forget ID` row; accepted-words sentence names `forget`; Shared notes bullet on admins; four new answer rows; the closing paragraph replaced (no longer says admins cannot forget; states immediacy, no audit log, no tool can do it); Known limits entry (irreversible and immediate, no audit log, failed vector delete leaves an orphan point that searches ignore and `/memory reindex` does not remove).
- `CHANGELOG.md`: one `Added` line for `/memory forget ID`, placed after the shared-notes line.
- `specs/memory.md`: two `[gotcha]` entries appended.
- `specs/tech-stack.md`: one sentence in the memory paragraph; version 35.
- `specs/mission.md`: updated on human approval (version 24), recorded here only as a pointer.
- `specs/roadmap.md`: the `## Now` line of this feature removed (back to `Nothing in progress.`).
