---
title: "Shared memory notes: one global set visible to every user, published by admins"
kind: feature
status: done
version: 1
updated: 2026-11-04
branch: main
---

## Problem / Motivation

Agent memory is strictly per user. Every query filters by `user_id` (`src/llm_harness/memory.py:166-181` the two search statements, `:386-393` `get`, `:412-422` `list`), the vector search filters on `{"user_id": user_id}` (`src/llm_harness/memory_index.py:287-292`), and the vector payload holds only `user_id` (`memory_index.py:276`, `:325`). There is no way to give every user the same set of notes (house rules, shared conventions, team facts), so each person would have to save the same note by hand.

Human decisions (2026-11-04, recorded as decided):

- One global set of shared notes, visible to every user.
- Normal users see shared notes READ-ONLY, mixed into `/memory list`.
- Option A: no agent tool ever writes a shared note. `memory_save` gets no scope parameter.
- This one spec covers the scope, the admin configuration and `/memory publish ID` / `/memory unpublish ID` (own notes only), so the feature is runnable end to end.
- Admins come from a config list of `user_id` values.
- Vector read is TWO searches (`{"user_id": u}` and `{"shared": 1}`) merged by `fuse_ranks`; a shared note carries `"shared": 1` in the vector payload only (no `VectorStore` interface change).
- In automatic recall a shared note competes by rank inside the existing `RECALL_K = 5` and is marked `[shared]`.

Assumptions (the human may change them):

- (a) Migration step 3 on BOTH dialect tuples: `ALTER TABLE memories ADD COLUMN shared INTEGER NOT NULL DEFAULT 0`. `Memory.shared: bool = False` is the LAST dataclass field. `user_id` stays the creator of the note.
- (b) Reads become `(user_id = ? OR shared = 1)`: `get`, the SQLite and Postgres search statements, `list`, and the `_reindex` selection. `update` and `forget` stay owner-only.
- (c) New store method `set_shared(user_id, id, shared: bool)`, owner-only. It sets the flag and deletes the `memory_vectors` markers of that id; `IndexedMemoryStore` also re-upserts that one note's vector payload (see Approach).
- (d) Config key `[tools] memory_admins = ["alice", ...]`: a list of `user_id` values validated like `user_id`; default empty, so nobody can publish.
- (e) `/memory publish ID` and `/memory unpublish ID`: only a user listed in `memory_admins` (else `only memory admins may publish or unpublish shared memories`); an own note only (another id answers `no memory #N`); a note already in the target state is reported; success answers `published memory #N` / `unpublished memory #N`. A plain `MemoryStore` (vectors off) also works.
- (f) `/memory delete ID` of a shared note: the owner is refused with `memory #N is shared; unpublish it first`; another user gets `memory #N is shared and read-only`. An admin forgetting someone else's shared note is the next roadmap item (`memory-shared-admin`).
- (g) The marker `[shared]` appears in `/memory list`, `/memory show`, `/memory search`, the recall block, the `memory_search` tool block and the reflection prompt's list of notes.
- (h) `/memory reindex` covers shared notes for every user's run, but a note is embedded once per collection (markers are per note id, not per user).
- (i) `update` of a shared note by a non-owner fails as today (`no memory #N`). The reflection prompt says shared notes cannot be changed.
- (j) README Known limits get the downgrade trap and the cost of two vector queries.
- (k) Added by the technical writer, NOT in the brief, needs the human's yes: the store's `update` and `forget` also refuse a shared note for its OWNER (SQL `AND shared = 0`), so a model tool or the reflection step can never alter or delete a published note, even when the model runs as an admin who owns it. The owner unpublishes first. Without this, `memory_forget` called by a model running as the owner would delete a shared note for everyone, which breaks option A.
- (l) Added by the technical writer: the "already in that state" answers are `memory #N is already shared` and `memory #N is not shared` (wording not given in the brief).

## Acceptance Criteria

Step 1 (storage) covers criteria 1 to 19, 25 and 27; step 2 (config, commands, docs) covers 20 to 24, 26 and 28. All run offline with the existing fakes: `FakeVectorStore`, `ConceptEmbedder`, a real `SqliteRelationalStore` on `tmp_path`, and the `Rig` and seed helpers of `tests/test_memory_reindex.py`. Postgres is covered by statement-text tests only.

1. **Migration of an existing v2 database keeps data.** A database migrated with the two old steps and holding rows is opened by the new code: version becomes 3, every old row has `shared = 0`, FTS search still finds the old rows.
2. **Upgrade is idempotent.** Migrating a second time is a no-op; `len(MEMORY_MIGRATIONS) == len(MEMORY_MIGRATIONS_POSTGRES) == 3`; `memory_module._MIGRATIONS_BY_DIALECT` reports 3 for both dialects; the status line says `memory version 3 (the code expects 3)`.
3. **Fresh database has the column.** A new database reaches version 3 and a saved note has `Memory.shared == False`.
4. **Private isolation is unchanged.** With two users and no shared note, neither sees, searches, lists, updates, forgets or vector-searches the other's notes (the existing isolation tests stay green).
5. **A shared note is readable by another user through `get`.**
6. **A shared note is found by another user's keyword search.** SQLite runs for real; the Postgres statement text is pinned to contain `(m.user_id = ? OR m.shared = 1)`.
7. **`list` returns own notes plus shared ones, newest-updated first, with `shared` set.**
8. **`update` and `forget` refuse a shared note.** For the owner and for another user: `update` returns None, `forget` returns False, the row is unchanged (assumption k).
9. **`update` and `forget` of another user's private note still fail** as before.
10. **`set_shared` is owner-only.** A non-owner or an unknown id returns None and changes nothing; the owner flips the flag both ways and the returned `Memory.shared` is right; `updated_at` is not changed by a flip.
11. **A flip deletes the markers of that note** in `memory_vectors` for every collection, on a plain `MemoryStore` too.
12. **The vector payload carries `shared: 1` only on shared notes.** A saved private note has payload `{"user_id": u}`; after `set_shared(True)` on an `IndexedMemoryStore` the point of that id has `{"user_id": u, "shared": 1}` and the marker is back; after `set_shared(False)` the payload is `{"user_id": u}` again.
13. **Vector read is two searches.** Each `IndexedMemoryStore.search` with a non-empty query makes exactly two vector searches, with filters `{"user_id": u}` and `{"shared": 1}` (value is the int 1, never a bool), each with limit 10, and merges the two id lists with `fuse_ranks` before the keyword fusion.
14. **A shared vector-only hit survives `_fused`; another user's private hit still drops.** Also with a vector store that ignores filters.
15. **Reindex covers shared notes.** `/memory reindex` and `--all` of user B embed user A's shared note when it has no marker, and a note is embedded once per collection (a second user's reindex finds the marker and skips it).
16. **A flip with the index off.** The SQLite flag flips, `take_notice` reports once, the markers are gone so `/memory reindex` repairs the payload; no exception escapes.
17. **A flip on a plain store (vectors off)** works and touches no vector store.
18. **`[shared]` markers appear** in `_list_line`, `show`, the `/memory search` lines, recall `_entry_line`, the `memory_search` tool block `_block` and the reflection prompt's `## Existing memories` list, and never on a private note. The text is scrubbed like the rest of the line.
19. **`memory_save` has no scope parameter.** A test pins the properties of `MemorySaveInput`'s JSON schema to exactly `text` and `keywords`; `memory_update` and `memory_forget` schemas are pinned too; no memory tool calls `set_shared`.
20. **`memory_admins` config validation.** Default is the empty list; a non-list, a non-string entry, an empty or blank entry, an entry over 128 characters, an entry with control characters and a duplicate entry are each a `ConfigError` that names `memory_admins`; a valid list is kept as written (no stripping).
21. **Publish permissions.** A user not in `memory_admins` gets `only memory admins may publish or unpublish shared memories` for both verbs, with the note unchanged; an empty list means nobody can publish.
22. **Publish and unpublish answers.** `published memory #N` and `unpublished memory #N` on success; another user's note or an unknown id gives `no memory #N`; the already-in-state answers of assumption (l); bad arguments give `USAGE`.
23. **Publish end to end.** After alice (admin) publishes, bob's `/memory list` shows the note with `[shared]`, `/memory show` shows it, `/memory search` finds it, recall for bob includes it; after `unpublish` bob sees none of that; with `memory_vectors` on, the vector payload follows criterion 12.
24. **`/memory delete` of a shared note.** Owner: `memory #N is shared; unpublish it first`; another user: `memory #N is shared and read-only`; a private own note still deletes as before; the note is never removed in the first two cases.
25. **Recall.** A shared note competes by rank inside `RECALL_K = 5` (no reserved slots): with 6 matching notes the block holds 5, and `[shared]` marks the shared ones.
26. **`USAGE`, the COMMANDS table and `/help memory`** list `publish` and `unpublish`; `help_detail` still equals `USAGE`; the table and drift tests in `tests/test_commands.py` stay green.
27. **Postgres statements.** The step-3 text of `MEMORY_MIGRATIONS_POSTGRES` and both search statements are pinned by statement-text tests. A live Postgres run is NOT part of the acceptance (label "not run" unless a live server exists).
28. **Docs match the code.** README (Agent memory, the `[tools]` example block and the sentence about the `[tools]` keys, Known limits with the downgrade trap and the two-query cost) and CHANGELOG describe the feature; the breaking-by-design tests are updated; `uv run pytest` and the project's lint are green.

## Approach

Memory consulted: grep of specs/memory.md for `IndexedMemoryStore|payload|handle_memory_text|memory_vectors` — 5 relevant entries (lines 60, 61, 62, 81, 82; line 69 is superseded by 81, and 32, 67, 76 concern other backends): the index switches itself off after the first failure and only `/memory reindex` resumes it; the vector point payload is `{"user_id": ...}`; `handle_memory_text` appends `take_notice()` to every `/memory` answer, so the new actions get that too.

Ordered plan, exactly two implementation steps; each ends with its tests green and a run the human can look at.

STEP 1: storage, with store-level tests.

1. Migration. Append one step (index 2, version 3) to both tuples (`memory.py:31-84` SQLite, `:92-130` Postgres): `ALTER TABLE memories ADD COLUMN shared INTEGER NOT NULL DEFAULT 0`. This is the first production `ALTER TABLE`: `relational.py:176-212` runs each statement of a step inside one transaction and records `len(migrations)`. Checked: on SQLite 3.50.4, with the real v2 schema from `MEMORY_MIGRATIONS`, the statement is valid, an existing row reads `shared = 0`, and an FTS match with `(m.user_id = 'b' OR m.shared = 1)` returns the row. NOT run: the same statement on PostgreSQL (valid standard DDL; label unverified). The FTS table and triggers and the Postgres `search` column are unaffected by a new column.
2. `Memory.shared: bool = False` as the last field (`memory.py:198-204`); `_COLUMNS` (`:166`) gains `shared`; both search statements select `m.shared`; `_row_to_memory` (`:299`) turns the integer into a bool.
3. Reads: `get`, both searches, `list` and the `_reindex` selection (`memory_index.py:246-255`) use `(user_id = ? OR shared = 1)`. `update` and `forget` keep `user_id = ?` and add `AND shared = 0` (assumption k).
4. `set_shared(user_id, id, shared)` on `MemoryStore`: `UPDATE memories SET shared = ? WHERE id = ? AND user_id = ? RETURNING ...`, then `DELETE FROM memory_vectors WHERE memory_id = ?`. The marker triggers fire only on `UPDATE OF text, keywords` (`memory.py:76-79`), so a flip alone does NOT clear markers; the explicit delete is needed. `updated_at` is left alone (the text did not change). `IndexedMemoryStore.set_shared` then calls `_add` for that note, under `_guarded`: this re-embeds one note (one embedding call) and upserts the point with the new payload, which replaces the old payload. Decision and reason: re-upserting makes the vector filter right at once, whereas leaving it to `/memory reindex` would hide a published note from vector search (and keep an unpublished one visible) until someone runs it. If the index is off or the upsert fails, the markers are already gone, so the plain `/memory reindex` repairs the point, and the notice says the index is off. A store with the index off, or a plain `MemoryStore`, flips only the SQLite row and the markers.
5. Vector payload and read: a helper builds the payload `{"user_id": u}` plus `"shared": 1` only when `memory.shared`; `_add` (`memory_index.py:325`) and `_index_batch` (`:276`) use it. `_vector_ids` (`:287-292`) runs two searches, `{"user_id": u}` and `{"shared": 1}`, each limit `VECTOR_SEARCH_LIMIT`, and merges them with `fuse_ranks(own_ids, shared_ids, VECTOR_SEARCH_LIMIT)`; that list is the vector list of the outer keyword fusion. The filter is AND-equality only (`storage/vectors.py:58-60`) and bools are refused on every backend (`storage/vector_common.py:32-43`), so the value is the int 1. `_fused` (`:199-215`) drops a vector-only hit unless `self.get(user_id, id)` returns it, so `get` MUST accept shared notes, otherwise a shared vector hit is silently lost.
6. Markers: a shared note shows `[shared]` in `_list_line` (`memory_command.py:111`), `_show` (`:136`), the search lines (`:158`, which reuse `_list_line`), recall `_entry_line` (`memory_recall.py:27`) and the tool `_block` (`memory_tools.py:65`). The reflection prompt builds its list of notes with `format_recall_block` (`memory_reflection.py:25`, `:276`), so the `_entry_line` marker covers it (read; no other path builds that list). `prompts/reflection.md` gets one sentence: a note marked `[shared]` cannot be changed or forgotten.
7. `/memory delete` answers (assumption f) in `_delete` (`memory_command.py:151`): it reads the note with `get`, so a shared note of another user is found and must not answer the misleading `no memory #N`.
8. Tests of this step: store-level, `tmp_path` SQLite, fakes. The first test builds a v2 database by running the old two steps with rows, then opens it with the new code.

STEP 2: config, commands, docs.

1. `ToolsConfig` (`config.py:505-521`) gets `memory_admins: list[str] = []` with a validator that runs every entry through the `user_id` rules of `_check_user_id` (`config.py:170-198`; it returns None only for None, and its messages start with `user_id`, so the validator re-raises with the key name `memory_admins`) and rejects duplicates like `_check_command_list` (`config.py:535-551`). An unknown `[tools]` key is an error today (README), so the key must be declared. The example config text (`config.py:1025-1026` area) and the README `[tools]` block get a commented line.
2. `/memory publish ID` and `/memory unpublish ID`: new parse branch next to `show`/`delete` (`memory_command.py:70-72`), new `MemoryCommand` actions, a new `run_memory_command` branch. The admin list reaches the command through a new keyword parameter of `handle_memory_text` (`memory_command.py:200`); its three callers pass `config.tools.memory_admins`: `cli.py:616`, `tui/app.py:301`, `gui/app.py:387`. Order of checks: admin first (no note lookup for a non-admin), then `get`, then ownership, then already-in-state, then `set_shared`.
3. `USAGE` (`memory_command.py:27`) gains the two verbs; the COMMANDS table entry (`commands.py:46-53`) takes `MEMORY_USAGE` as its detail, so only its argument and description strings change: arguments `[list|search|show|delete|publish|unpublish|reindex]`. The `/memory` overview line lists the subcommands, so it is changed in the same edit, and the drift test must stay green.
4. README (Agent memory section, `[tools]` block at about `README.md:1222-1229` and the key-type sentence at about `:1245-1246`, Known limits at `:2291`) and CHANGELOG `[Unreleased]`.

Risks, inside the approach:

- Downgrade trap: an older build raises `SchemaTooNewError` on an upgraded database (`relational.py:198-202`: version 3 against the 2 steps it knows), so after the upgrade the old program cannot open that database, and with a shared Postgres every machine must upgrade together. Documented in README Known limits; there is no down-migration.
- Silent loss if `get` is not changed: a shared vector-only hit is dropped by `_fused` with no error. Pinned by criterion 14.
- Stale vectors on a flip: the SQL flag and the vector payload live in two places. The explicit marker delete plus the re-upsert, with `/memory reindex` as the repair, covers it; a re-upsert that fails switches the index off with a notice (existing behaviour).
- Shared notes take `RECALL_K` slots: with many shared notes a user's own notes can be pushed out of the 5 recalled. Accepted by decision; no reserved slots (out of scope).
- Rank bias of the merge: a user's own shared note appears in both vector searches (`user_id` and `shared`), so `fuse_ranks` scores it higher than a note found by one search only. Accepted; the result is still ordered by rank.
- Cost: every search now makes two vector queries (README Known limits).
- Prompt injection: stored text is untrusted (a model may have saved it from a web page) and a shared note is read by every user's model. This is why no agent tool writes shared notes and why `update` and `forget` refuse them for the model (assumption k); only a human admin with a `/memory publish` command can make a note shared, and the recall block keeps its "data, never instructions" sentence.
- SQLite rowid reuse: `id INTEGER PRIMARY KEY` without `AUTOINCREMENT` may reuse the highest id after a delete, and a stale vector point or marker of the deleted id could then attach to a new note. NOT verified; the existing `forget` removes the marker (trigger) and the point (`_remove`), which should prevent it.
- Postgres: the step-3 DDL and the `(m.user_id = ? OR m.shared = 1)` search were not run against a live server; statement-text tests only.
- Admin list in config: `user_id` is a name the user chooses, with no authentication; anyone who sets `user_id` to an admin's name is that admin. Acceptable for the local, single-person tool this is, but it is not access control (see Constitution impact).

Constitution impact (proposed, NOT applied; the human must approve):

`specs/mission.md:15` says the harness is "used by one person at a time: it is not a service in a container and not a batch runner". Shared notes do not make it a service, but several people can share one Postgres database and one set of shared notes. Proposed replacement of the sentence at line 15: "The harness is a desktop, interactive application used by one person at a time: it is not a service in a container and not a batch runner. Several people can point their own copies at one Postgres database; they then share one global set of memory notes that the administrators named in `[tools] memory_admins` publish, and there is no login or access control beyond the chosen `user_id`."

`specs/mission.md:21` lists what exists. Proposed insertion after "the `/memory` command to list, show, search, delete and reindex notes (`/memory search WORDS` finds notes without a model call; `/memory reindex --all` embeds every note again)": "; shared notes: one global set visible to every user, read-only in `/memory list` and marked `[shared]`, published and unpublished with `/memory publish ID` and `/memory unpublish ID` by a user listed in `[tools] memory_admins`, never written by an agent tool".

`specs/tech-stack.md`: I found no sentence that says the payload holds only `user_id`; that statement is in `specs/memory.md:61` and in `memory-vector-search.md:36,:79`. The tech-stack lines that describe the same thing are: line 96, "The `VectorStore` has its first consumer, the memory index (collection per embedding model, filter `user_id`)". Proposed replacement of the parenthesis: "(collection per embedding model; each point's payload is `user_id`, plus `shared` = 1 on a shared note; a search runs twice, filtered by `user_id` and by `shared`, and the two lists are merged by rank)". Line 53 says "memory schema version 2 with the tables `memory_vectors` and `memory_vector_meta`" and "per-user agent memory in SQLite"; proposed: "memory schema version 3 (adds the `shared` column of `memories`) with the tables `memory_vectors` and `memory_vector_meta`" and "per-user agent memory in SQLite, plus one global set of shared notes". Line 39 repeats "used by one person" and needs the same wording as mission.md:15 if that one changes.

The `memory.md:61` entry (`[build]`, payload `{"user_id": ...}`) is a log entry, not a constitution file; a new entry is appended when the feature is built, it is not edited.

Historical specs superseded in part (not edited; this spec overrides them where they conflict):

- `specs/features/memory.md:41` (isolation: every query filters by `user_id`; neither user sees the other's entries) and `:103` (every query filters by `user_id`): reads now also return `shared = 1` rows; private notes keep the isolation, and `update`, `forget` stay owner-only.
- `specs/features/memory.md:89` ("Remote or shared memory across users" is out of scope): the shared part is now in scope here (remote memory exists since the Postgres backend).
- `specs/features/memory-vector-search.md:36` and `:79` (payload holds only `user_id`): a shared note's payload also holds `shared` = 1.
- `specs/features/memory-vector-search.md:40` (a search takes up to 10 vector hits, filter `user_id`; vector-only ids go through `get(user_id, id)`): there are now two vector searches, and `get` also returns shared notes.

## Affected Areas

Source:

- `src/llm_harness/memory.py`: migrations, `Memory`, `_COLUMNS`, searches, `get`, `list`, `update`, `forget`, `set_shared`.
- `src/llm_harness/memory_index.py`: payload helper, `_vector_ids`, `_reindex`, `_index_batch`, `_add`, `set_shared`.
- `src/llm_harness/memory_tools.py` (`_block` marker; no new tool parameter), `src/llm_harness/memory_recall.py` (`_entry_line`), `src/llm_harness/prompts/reflection.md`.
- `src/llm_harness/memory_command.py` (`USAGE`, parse, `_list_line`, `_show`, `_delete`, publish and unpublish, `handle_memory_text` parameter), `src/llm_harness/commands.py` (the `/memory` table entry), `src/llm_harness/config.py` (`ToolsConfig`, example config text), and the three callers `src/llm_harness/cli.py`, `src/llm_harness/tui/app.py`, `src/llm_harness/gui/app.py`.

Docs: `README.md`, `CHANGELOG.md`, `specs/roadmap.md` (reworded in this change), and the constitution edits proposed above (`specs/mission.md`, `specs/tech-stack.md`) once approved.

Tests that break by design (their asserted numbers or texts change):

- `tests/test_memory_postgres.py:158-163`: the SQLite migrations and the SQLite search statement are asserted byte-identical to the old text.
- `tests/test_memory_postgres.py:213-222`: step counts equal 2 and the schema status reports memory at version 2.
- `tests/test_memory_index_store.py:385-392`: asserts two vector searches in total for two store searches, each with only the `user_id` filter.
- `tests/test_storage_status.py:164-166`: `schema: memory version 2 (the code expects 2)`.
- `tests/test_status_postgres.py:260-262`: the served memory version 2.
- `Memory(...)` positional constructions that must still compile with the default last field (they should keep working, check): `tests/test_memory_recall.py:79`, `tests/test_memory_reflection.py:89`, `tests/test_memory_command.py:222`.

Listed by the brief but my reading says they may stay green (they index step 2 or use private notes only); confirm by running the suite: `tests/test_memory_postgres.py:261` and `:272` (they read `MEMORY_MIGRATIONS_POSTGRES[1]`, which an appended step leaves unchanged), `tests/test_memory_index_store.py:404-419` (a private hit of another user still drops), `tests/test_memory_reindex.py:118` and `:607` (other users' private rows stay untouched).

## Out of Scope

- Admin forgetting of another user's shared note for everyone (`memory-shared-admin`), and an admin command that lists all shared notes.
- `/memory add --shared` (`memory-add-command`) and yaml export or import of shared notes (`memory-yaml-transfer`).
- Groups, topics or per-team sets; roles beyond the config list `memory_admins`.
- Reserving recall slots for shared or for private notes.
- An any-of (OR) vector filter in the `VectorStore` interface.
- A Postgres live test run (labelled not run unless a live Postgres is available; none is started for this work).
- Real authentication of `user_id`.

## Implementation Notes

Both steps were built as planned; no deviation from the approved Approach. Assumptions (k) and (l) were kept as written (`update` and `forget` refuse a shared note for its owner; the already-in-state answers are `memory #N is already shared` and `memory #N is not shared`). The marker is placed as `#N [shared] [kw] (updated ...)`.

STEP 1 (storage), done by the engineer:

- `src/llm_harness/memory.py`: migration step 3 on both dialect tuples (`ALTER TABLE memories ADD COLUMN shared INTEGER NOT NULL DEFAULT 0`); `Memory.shared` as the last field; reads use `(user_id = ? OR shared = 1)` (`get`, both search statements, `list`); `update` and `forget` add `AND shared = 0`; `set_shared` is owner-only and clears the `memory_vectors` markers of the note.
- `src/llm_harness/memory_index.py`: `_payload` and `_SHARED_FILTER`, a two-search `_vector_ids`, `_reindex` selection that includes shared notes, `IndexedMemoryStore.set_shared` that re-embeds the one note under the guard.
- `src/llm_harness/memory_recall.py` (`SHARED_MARKER`, `_entry_line`), `src/llm_harness/memory_tools.py` (`_block`), `src/llm_harness/prompts/reflection.md` (one bullet).
- Tests: the changed test files, as `git status` lists them, are `tests/test_memory_store.py`, `tests/test_memory_index_store.py`, `tests/test_memory_reindex.py`, `tests/test_memory_recall.py`, `tests/test_memory_tools.py`, `tests/test_memory_reflection.py`, `tests/test_memory_postgres.py`, `tests/test_storage_status.py` and `tests/test_live_memory_postgres.py`; which test sits in which file was not re-read by the technical writer.

STEP 2 (config, commands), done by the engineer:

- `src/llm_harness/config.py`: `ToolsConfig.memory_admins: list[str] = []` with a validator (entries follow the `user_id` rules, duplicates refused, errors name `memory_admins`), and the commented example line in the example config text.
- `src/llm_harness/memory_command.py`: `NOT_ADMIN`, `_publish`, parse of `publish` and `unpublish`, the `memory_admins` keyword parameter of `run_memory_command` and `handle_memory_text`, `USAGE`, the `_delete` answers for shared notes; `src/llm_harness/commands.py`: the `/memory` entry text.
- `src/llm_harness/cli.py`, `src/llm_harness/tui/app.py`, `src/llm_harness/gui/app.py` pass `memory_admins=config.tools.memory_admins`.
- Tests: the changed or new test files, as `git status` lists them, are `tests/test_config.py`, `tests/test_memory_command.py`, `tests/test_commands.py` (new), `tests/test_cli.py`, `tests/test_gui.py`, `tests/test_tui.py`, `tests/test_tui_approval.py` and `tests/test_tui_inspector.py`.

Safety pinned by tests (engineer-reported): the `memory_save` schema is exactly `text` and `keywords`; no tool or reflection module references `set_shared` (re-checked below with grep).

## Validation

Commands and results. Rows marked "coordinator" were re-run by the coordinator; the rest are engineer-reported.

| Check | Result | Source |
| --- | --- | --- |
| `uv run pytest -q -p no:cacheprovider` (default suite) | 3975 passed, 0 failed, 0 skipped, 279 deselected, 1 warning (pre-existing google-genai DeprecationWarning), 149 s | coordinator |
| Baselines of the default suite | 3895 before step 1, 3929 after step 1, 3975 after step 2 | engineer |
| Live suite on real PostgreSQL: `uv run pytest -m live tests/test_live_memory_postgres.py tests/test_memory_store.py` with `LLM_HARNESS_LIVE_PGMEM_URL`, `LLM_HARNESS_LIVE_PGMEM_VECTOR_URL`, `LLM_HARNESS_LIVE_POSTGRES_PASSWORD` set (throwaway containers `postgres:17` and `pgvector/pgvector:pg17` on the local Rancher Desktop Docker context, database `llm_harness_live`) | baseline before the change 128 passed; after step 1 140 passed; after step 2 142 passed, 0 failed, 0 skipped | coordinator re-ran the final count |
| `ruff check` and `ruff format --check` | clean | coordinator |

- No skips in either run. The 279 deselected tests are the ones marked `live`: `addopts = -m "not live"` in `pyproject.toml` leaves them out of the default run; the live suite above runs the memory ones against a real server.
- Postgres: the step-3 DDL, both search statements, `set_shared` and the publish and unpublish commands ran on real PostgreSQL 17 in the live run, in these tests of `tests/test_live_memory_postgres.py`: `test_shared_notes_are_read_by_another_user_and_owner_only_to_change` (line 797), `test_set_shared_with_a_driver_failure_is_a_type_name_only_error` (837), `test_the_real_pgvector_store_filters_on_the_shared_payload_and_unpublish_removes_it` (849), `test_a_shared_note_unindexed_by_a_flip_is_embedded_by_another_users_reindex` (910), `test_publish_and_unpublish_commands_on_the_real_postgres_store` (938), `test_publish_command_with_the_real_pgvector_store_sets_the_payload` (975), plus `tests/test_memory_store.py` with the live Postgres parameter. This supersedes the "not run" label of the Approach (criterion 27 asked only for statement-text tests).
- Fail-before proofs (engineer-reported): making `MemoryStore.get` owner-only broke offline and live tests (a first weak test was rewritten to be vector-only so that it fails); deleting `if user_id not in admins: return NOT_ADMIN` broke 12 tests; removing the `memory_admins=` argument in `src/llm_harness/gui/app.py` broke the GUI admin test.
- Real offline demos (coordinator, scratch SQLite): bob sees alice's published note as `#1 [shared] [deploy] (updated ...) ...` in list, search and show; bob's `/memory delete 1` answers `memory #1 is shared and read-only`; alice's answers `memory #1 is shared; unpublish it first`; an update by anyone is refused; `/memory publish` as a non-admin, and with an empty admin list, answers `only memory admins may publish or unpublish shared memories`; alice (admin) publish answers `published memory #1`, again `memory #1 is already shared`; unpublish by bob is refused, by alice `unpublished memory #1`, and bob's list is empty again; `/memory publish 99` answers `no memory #99`.

Call-site evidence (`grep -n` run by the technical writer on `src/`, 2026-11-04):

- `set_shared(`: `memory.py:400` (definition), `memory_index.py:192` (override) and `:198` (`super().set_shared`), `memory_command.py:198` (the only caller). No match in `memory_tools.py` or `memory_reflection.py` (count 0).
- `memory_admins`: `config.py:530` (field), `:532` and `:545` (validator), `:1047` (example line); passed at `cli.py:620`, `tui/app.py:306`, `gui/app.py:392`; accepted at `memory_command.py:228` (`run_memory_command`) and `:260` (`handle_memory_text`), forwarded at `:242` and `:281`.
- `_publish`: `memory_command.py:177` (definition) and `:238` (the only call, in `run_memory_command`).
- `_payload`: `memory_index.py:99` (definition), `:295` (`_index_batch`) and `:351` (`_add`).
- `SHARED_MARKER`: defined `memory_recall.py:26`, used at `memory_recall.py:32`, `memory_tools.py:68`, `memory_command.py:118` and `:152`.
- Readers of `Memory.shared`: `memory_recall.py:32`, `memory_tools.py:68`, `memory_command.py:118`, `:152`, `:165`, `:192`, `memory_index.py:101`; SQL: `memory.py:176-186` select `m.shared` and filter `(m.user_id = ? OR m.shared = 1)`.

Not verified (said here, not guessed):

- The terminal UI and plain-CLI `memory_admins` wiring has no fail-before proof (the GUI has one).
- No real GUI or terminal UI session was run by hand.
- SQLite row-id reuse after a delete (Approach risk) was not checked.
- How each front end shows `SchemaTooNewError` from an older build was not checked.
- Postgres other than version 17, TLS and large collections were not tested.

## Documentation Review

Checked against the code as built:

- `README.md`: Tools block and key-type sentence, Agent memory (isolation, `--storage-status` example, the migration line near the Postgres section), Automatic recall, The /memory command, the new Shared notes section, Troubleshooting, Known limits, and the Chat commands table (its `/memory` row names no subcommand, so it needs no change; `/help memory` prints `USAGE`, which lists `publish` and `unpublish`).
- `CHANGELOG.md` `[Unreleased]`: no entry for the feature before this change.
- Constitution: `specs/mission.md` (version 23) already holds the approved wording (paragraph on several people and one Postgres database, and the shared-notes sentence in "what exists today"); it matches the built commands and was not edited. `specs/tech-stack.md` still said "planned in `memory-shared-scope`" and memory schema version 2; both were stale and are fixed (version 33 to 34).
- Historical specs `specs/memory.md` (the 2026-11-03T11:44:59Z payload entry), `specs/features/memory.md` (lines 41, 89, 103) and `specs/features/memory-vector-search.md` (lines 36, 40, 79) are superseded in part by this feature and are not edited; `specs/memory.md` got new entries instead.

CI review: not applicable (no .github, Makefile or Dockerfile exists; verified with ls -a, no release script)

## Documentation Updates

- `README.md`: `[tools]` example block gets `memory_admins`; the key-type sentence describes it; Agent memory intro and isolation paragraph mention shared notes; `--storage-status` example and the Postgres migration line say memory version 3; Automatic recall shows the `[shared]` entry shape; The /memory command table has `publish` and `unpublish` rows, the `delete` row notes the refusal, the `reindex` lines count shared notes, the usage-text list names the new verbs; new section "Shared notes" (what they are, admins, answers table, upgrade, vector behaviour); Troubleshooting entry for an older build on an upgraded database; Known limits entry (downgrade trap, admin is a chosen name, two vector queries, reindex `--all` re-embeds shared notes, two-step flip with `/memory reindex` as repair, crowding of recall).
- `CHANGELOG.md` `[Unreleased]`: one `Added` line (shared notes, publish and unpublish, `memory_admins`) and two `Changed` lines (schema version 3 and the older-build refusal; `/memory delete` answers for shared notes, two vector queries, reindex).
- `specs/memory.md`: four appended entries (payload and two vector queries, `_fused` and `get`, first production `ALTER TABLE`, live Postgres memory test recipe).
- `specs/tech-stack.md`: removed the two "planned in `memory-shared-scope`" wrappers, memory schema version 3, `version` 33 to 34 (human-approved constitution change); `specs/mission.md` was updated earlier to version 23 on the same approval and needed no further edit.
- `specs/roadmap.md`: the `## Now` line for this feature is removed (back to `Nothing in progress.`).
