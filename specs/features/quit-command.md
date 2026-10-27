---
title: /quit command and clean Ctrl-C
kind: bugfix
status: done
version: 1
updated: 2026-10-27
branch: none
---

## Problem / Motivation

The user's words: "I need a /quit command to end the application without seeing exception stack on crt+c". Asked which mode, the user answered: "YES, IT WAS GUI".

Reproduction (pseudo-terminal, before the fix): Ctrl-C in `llm-harness --gui` printed a traceback of about 32 lines (an asyncio `CancelledError` raised during the handling of `KeyboardInterrupt`, through `cli.py` `main` -> `_run_gui_lazily` -> `gui/app.py` `run_gui` -> NiceGUI `ui.run` -> uvicorn), and the process ended "killed by SIGINT" (exit status -2).

The other front ends were checked in the same way:

- `--plain` already exited with code 130 and printed only `^C`.
- The terminal UI ignores Ctrl-C by Textual's design (its footer shows `ctrl+q`) and prints no stack.

Decision: fix the GUI Ctrl-C, and add a `/quit` command to the chat input in all three front ends so the application can be ended without Ctrl-C.

## Acceptance Criteria

1. Ctrl-C in `llm-harness --gui` prints no traceback and the process exits with code 130.
2. `run_gui` returns 130 when `ui.run` raises `KeyboardInterrupt`.
3. On server shutdown the GUI cancels running turns and closes the providers of every page that is still open.
4. In the GUI, a message that is exactly `/quit` (after stripping whitespace, any letter case) cancels a running turn, clears the input, shows the notification "Shutting down" and shuts the server down. It makes no model call and adds nothing to the chat history.
5. The GUI input placeholder reads `Message, Enter to send, /quit to exit`.
6. In the terminal UI, `/quit` and `/exit` (stripped, any letter case) quit like Ctrl+Q. A running turn is cancelled and awaited first, then the provider is closed when the app unmounts.
7. In `--plain`, `/quit` and `/exit` end the chat, compared case-insensitively. This is a behaviour change: `/EXIT` now exits.
8. A message that only contains the command, such as `/quit now` or `please /quit`, is an ordinary message in every front end.
9. `--plain` still exits 130 on Ctrl-C and the terminal UI is unchanged for Ctrl-C.
10. Tests cover each of the above and pass offline.

## Approach

Memory consulted: NiceGUI|Textual|uvicorn|sanitize|markdown (case-insensitive) — 3 relevant entries (NiceGUI `ui.run` host default and `on_delete`; Textual markup parsing of `str` content; google-genai/aiohttp), none about shutdown.

- `run_gui` wraps `ui.run` in `try/except KeyboardInterrupt` and returns 130. The traceback came from the exception escaping `ui.run`; catching it where `run_gui` calls it gives a clean exit.
- `app.on_shutdown(_close_open_pages)` registers a handler that closes the pages that are still open. A module-level set `_open_pages` holds them: a page adds itself in `build()` and removes itself in `close()`. `close()` cancels a running turn and closes the provider.
- `/quit` in the GUI is checked in `_on_send` before the busy check and before the model is called. `_quit` cancels the turn, clears the input, calls `ui.notify("Shutting down")` and then NiceGUI's `app.shutdown()`.
- In the terminal UI, `/quit` and `/exit` call `action_cancel_turn`, wait for the workers, then `action_quit`. Waiting first matters: without it the cancelled turn crashed while mounting a note after the log widget was gone.
- In `--plain`, `_EXIT_COMMANDS = frozenset({"/exit", "/quit"})` is compared with `text.lower()`.
- Only the whole message is a command (stripped, case-insensitive), so a model prompt that merely mentions `/quit` is not swallowed.
- NiceGUI 3.18.0 facts used (read in the installed package by the coordinator): `app.shutdown()` sets `Server.instance.should_exit = True` when not in reload mode, and `app.on_shutdown(handler)` accepts sync or async handlers.

Unverified:

- `/quit` through a real browser socket: only NiceGUI's `user` simulation was used, with `app.shutdown` patched.
- That the `on_shutdown` handler runs in the real server: it is covered by calling it directly.
- `/quit` in a real terminal UI: only Textual's Pilot was used.
- An engineer measured one Ctrl-C exit of 4.6 s; the coordinator did not reproduce it (0.2 s to 0.3 s in three runs). The cause of the one slow run is not known.

Assumptions:

- A user who types `/quit` alone wants to quit; there is no confirmation step.
- Uppercase variants such as `/EXIT` may exit in `--plain`.

## Affected Areas

- `src/llm_harness/gui/app.py`: `run_gui`, `_open_pages`, `_close_open_pages`, `ChatPage` (`build`, `close`, `_on_send`, `_quit`) and the input placeholder.
- `src/llm_harness/tui/app.py`: `_QUIT_COMMANDS`, `_PLACEHOLDER`, `on_input_submitted`.
- `src/llm_harness/cli.py`: `_EXIT_COMMANDS` and its case-insensitive comparison in `_chat_loop`.
- Tests under `tests/` for the GUI, the terminal UI and `--plain`.
- Docs: `specs/tech-stack.md`, `README.md`, `CHANGELOG.md`, `specs/memory.md`.

## Out of Scope

- Changing Textual's Ctrl-C behaviour in the terminal UI (Ctrl+Q and `/quit` are the ways out).
- A confirmation prompt before quitting.
- A port flag or any other command-line change.

## Implementation Notes

Done:

- `gui/app.py`: `run_gui` returns 130 on `KeyboardInterrupt` and registers `app.on_shutdown(_close_open_pages)`. `_open_pages` is a module-level `set`; `ChatPage.build()` adds the page and `ChatPage.close()` removes it. `_on_send` calls `_quit()` when `text.strip().lower() == "/quit"`; `_quit()` cancels the turn, clears `_prompt`, calls `ui.notify("Shutting down")` and `app.shutdown()`.
- `tui/app.py`: `on_input_submitted` handles `/quit` and `/exit` as described under Approach.
- `cli.py`: `_EXIT_COMMANDS` holds `/exit` and `/quit`.
- 21 new tests (the coordinator's count: 665 earlier, 686 after this work).

Process note (a deviation from the /code order): the user's message served as the specification, and this spec file was written after the implementation, not before it. The approval gates of `/code` were therefore not run in order for this item.

## Validation

Commands from the technical writer's own run, in the project root with a scratch `HOME` and `UV_CACHE_DIR`:

- `uv run ruff check`: All checks passed. `uv run ruff format --check`: 50 files already formatted.
- `uv run pytest -q -rs`: 825 passed, 3 deselected, 0 skipped, 0 failed, one DeprecationWarning from google-genai (22 s). The count is higher than the coordinator's 815 because the parallel engineer added tests while this was written.
- `uv run llm-harness --help` prints the usage with `--plain`, `--gui`, `--model`, `--think` and `--no-think`; there is no new flag.

Pseudo-terminal results (coordinator):

- Before the fix: `--gui` exit -2 with 32 lines of traceback.
- After the fix, three runs: `--gui` exit 130 after 0.3 s, 0.2 s and 0.3 s; the output after Ctrl-C was only `^C` and contained no `Traceback`.
- `--plain`: exit 130 after 0.1 s, `^C` only.
- Port 8080 was free and no process was left afterwards.

Mutation checks (engineer): removing the `KeyboardInterrupt` handling made the exit-130 test abort the whole test session (the `KeyboardInterrupt` escapes); disabling `/quit` in `_on_send` failed 3 tests.

Coordinator test totals for this item: 686 passed, 3 deselected, 0 skipped (665 before, 21 new).

Not verified: see the Unverified list under Approach (real browser socket, the real `on_shutdown` run, the real terminal UI, the one 4.6 s measurement).

## Documentation Review

Reviewed 2026-10-27 against the current code.

Drift found in the constitution and README (before this work's updates):

- `specs/tech-stack.md:67` (Command line): says `/exit` or end of input ends `--plain`; the code also accepts `/quit` (`src/llm_harness/cli.py:44`, `:158`), says nothing about `/quit` in the GUI and the terminal UI, and says nothing about the GUI Ctrl-C exit.
- `README.md:14` (Quick start): "`/exit` ends it" only; `README.md:77-88` (Flags) has no mention of the chat commands.
- `specs/mission.md`: no sentence about quitting or Ctrl-C; no change needed for this item.
- `specs/features/chat-streaming.md` is a closed spec and was not edited; it describes `/exit` only and its text predates this item.

CI review: no CI, release or deploy files exist; nothing to update

- {pyproject.toml, needs change: no, it changed only through `uv add` for the `markdown-chat` item; this item adds no dependency and edits no packaging field}.

## Documentation Updates

Done 2026-10-27.

- `specs/tech-stack.md` (version 8): the Command line bullet now lists `/quit`, `/exit` and the clean GUI Ctrl-C exit with code 130. Constitution pointer: this is the only constitution change for this item; `specs/mission.md` and `specs/roadmap.md` were not changed.
- `README.md`: Quick start mentions `/quit` and the clean Ctrl-C; a "Chat commands" paragraph lists `/quit` and `/exit` (case-insensitive).
- `CHANGELOG.md`: one `Added` line for `/quit` and the clean Ctrl-C shutdown.
- `specs/memory.md`: entries for the Ctrl-C gotcha and the background-job SIGINT gotcha (see the `markdown-chat` spec for the other entries).

Closeout checklist:

- [x] Spec in the fixed shape, `status: done` (this file).
- [x] Constitution updated: `specs/tech-stack.md` (evidence: grep `/quit` in the file).
- [x] README and CHANGELOG updated (evidence: grep `/quit` in both).
- [x] Tests and lint run (see Validation).
- [x] Roadmap `## Now` has no line for this item (`Nothing in flight.`), so there is nothing to remove.
- [x] Memory entries appended (evidence: `grep -c '^20' specs/memory.md`).
- [x] No version control exists in this directory; nothing was committed.
