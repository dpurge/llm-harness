---
title: "/help lists the commands of the current front end"
kind: feature
status: done
version: 1
updated: 2026-11-04
branch: main
---

## Problem / Motivation

A person has no way to ask the chat which commands exist. Today they must read the README or guess from the input placeholder, and the three front ends do not support the same commands:

- Plain mode (`src/llm_harness/cli.py`): `_EXIT_COMMANDS = frozenset({"/exit", "/quit"})` (line 68), checked in `_chat_loop` (line 251); `/memory` answers come from the `memory_command` callable (lines 255-260), wired in `main` with `partial(handle_memory_text, ...)` (lines 609-614). No placeholder exists in line mode.
- Terminal UI (`src/llm_harness/tui/app.py`): `_QUIT_COMMANDS = frozenset({"/quit", "/exit"})` (line 67), quit check (line 278), `/export` and `/import` through `parse_command` (line 291) and `_run_state_command` (lines 315-334), `/memory` through `handle_memory_text` (lines 295-301); placeholder `_PLACEHOLDER = "Message, Enter to send, /export, /import <path>, /quit to exit"` (line 69).
- Web GUI (`src/llm_harness/gui/app.py`): only `_QUIT_COMMAND = "/quit"` (line 42; no `/exit`), quit check (line 376), `/memory` through `handle_memory_text` (lines 379-385); placeholder `"Message, Enter to send, /quit to exit"` (line 186). No slash `/export` or `/import` command (verified: `state_files` is imported only by `src/llm_harness/tui/app.py`, grep of `src`). The GUI does export and import orchestrator state, through the "Export state" and "Import state" buttons in the State tab (`src/llm_harness/gui/inspector.py:89` docstring, `ui.button("Export state", on_click=self._on_export)` at line 106, `ui.upload(... on_upload=self._on_upload)` at line 116, `ui.button("Import state")` at line 127, handlers `_on_export` line 141 and `_on_upload` line 155).

`/memory` has its own usage text, `USAGE` (`src/llm_harness/memory_command.py:27-35`), shown only when a person types a malformed `/memory` line (`handle_memory_text`, lines 207-208). There is no overview of all commands, and a message such as `/help` is today sent to the model as an ordinary chat message in every front end (it matches no command in any of them).

Human decisions (2026-11-04):

- A. `/help` lists only the commands the CURRENT front end supports. One command table marks each command with the front ends that support it; today the slash commands `/export` and `/import` exist only in the TUI; the GUI has the same feature as State tab buttons. The human names the GUI the primary front end (coordinator message, 2026-11-04).
- B. `/help` prints an overview, one line per command (name, arguments, what it does). `/help COMMAND` (for example `/help memory`, with or without the slash) prints the detail; for memory the full existing `USAGE` text.

## Acceptance Criteria

GUI first (the web GUI is the primary front end):

1. GUI handler (mandatory test). In `_on_send` (`src/llm_harness/gui/app.py`), `/help` and `/help COMMAND` show the answer in the chat, clear the prompt, make no model call and add no `you:` line. The test drives the input like the existing GUI tests: `open_chat(user, Factory(REPLY))`, then `user.find(marker="prompt").type(text).trigger("keydown.enter")` (`send` in `tests/test_gui.py:254`, which waits for a `user` echo and so cannot be used as is for a command that adds none; a wait on the answer element is needed), with `nicegui.testing.user_plugin` enabled by `addopts` in `pyproject.toml:51`.
2. GUI overview. `help_overview("gui")` has one line each for `/memory`, `/quit`, `/help`, and one line each for the buttons "Export state" and "Import state" (State tab), marked as buttons and not as slash commands; it has no `/export`, `/import` or `/exit` line.
3. GUI `/help memory` shows exactly `memory_command.USAGE`.
4. GUI placeholder mentions `/help`; the pinned string in `tests/test_gui.py:1410` is updated.

All front ends:

5. Command table. A new module holds one table of commands; each entry has the name, the arguments, a one-line description, and the set of front ends (`plain`, `tui`, `gui`) that support it. Entries today: `/memory` (all three), `/quit` (all three), `/exit` (plain and TUI), `/help` (all three), and export/import, described as "slash command in the TUI; in the web GUI the State tab buttons "Export state" and "Import state"" (the plain mode has neither).
6. Overview per front end. `help_overview(front_end)` returns one line per command available in that front end (name, arguments, description) and none for a command it lacks: no `/export` or `/import` line for `plain`; no `/exit` for `gui`; the TUI lists `/export` and `/import <path>` as slash commands.
7. Detail per command. `help_detail(front_end, name)` accepts the name with or without the leading slash, in any case; for `memory` it returns exactly `memory_command.USAGE`; for the other commands their own detail text. For `export` and `import` in the GUI it names the buttons and says there is no slash command.
8. Extra words. `/help extra words` gives the detail for the first word after `/help` (`extra`), so it reports an unknown name unless that word is a command.
9. Unknown name. An unknown name, or a command the current front end lacks (for example `/help exit` in the GUI), answers `no help for "<escaped name>"; type /help`; the name is escaped like `_escape` in `src/llm_harness/memory_command.py:86` does for `/memory`.
10. `/help` is case-insensitive (`/HELP`, `/Help`) and is recognised only as the first word of the whole input; `/helpx` is an ordinary message.
11. Plain mode handler. In `_chat_loop`, `/help` and `/help COMMAND` write the answer to stdout and make no model call.
12. TUI handler. `/help` and `/help COMMAND` show the answer as a note, clear the prompt, make no model call and add no `you:` line; `/help memory` shows `USAGE` in plain mode and the TUI too.
13. TUI placeholder mentions `/help`; the test at `tests/test_tui_inspector.py:1161` is updated.
14. Drift test. For each front end, every command constant it dispatches on (quit and exit words, `COMMAND_WORD`, `EXPORT_COMMAND`, `IMPORT_COMMAND`, the help word) appears in the table for that front end, and the table has no slash command for that front end that it does not handle.
15. Memory help stays in step. The `/memory` detail is read from `memory_command.USAGE` (no copy), pinned by a test comparing the two.
16. Docs. README documents `/help` and the per-front-end command list; CHANGELOG `[Unreleased]` has an `Added` line.

## Approach

Memory consulted: grep of specs/memory.md for `slash|placeholder|front end|handle_memory_text|_QUIT|/quit|/export` — 1 relevant entry (2026-11-04T14:14:54Z, `handle_memory_text` appends `take_notice()` to every /memory action; does not apply to `/help`, which does not touch the memory store).

Assumptions (may change; the human is asked to confirm or correct):

- (a) `/help` is handled locally in each front end before anything reaches the orchestrator or model: no model call, never stored in the chat history. It is shown the way `/memory` answers are shown today: stdout line in plain mode (`cli.py:257-260`), a `note` in the TUI (`tui/app.py:298-301`), a `memory`-marked line with class `w-full` in the GUI (`gui/app.py:382-385`). The GUI marker `memory` for a help answer is unverified as the right choice; a separate `help` marker may suit better.
- (b) One new module, for example `src/llm_harness/commands.py`, holds the table and the pure functions `help_overview(front_end)` and `help_detail(front_end, name)`. The memory detail reuses `memory_command.USAGE` so it cannot drift.
- (c) `/help` is case-insensitive; `/help extra words` gives the detail for the first word; an unknown name answers `no help for "<escaped name>"; type /help`, escaped like `_escape` does for `/memory`. Note that `/memory` itself matches `COMMAND_WORD` case-sensitively (`parse_memory_command`, `memory_command.py:59`), while quit and `/export` and `/import` are case-insensitive; the help overview shows the lower-case forms.
- (d) The TUI and GUI input placeholders mention `/help`.
- (e) A drift test (criterion 14) ties the table to the constants each front end dispatches on.
- (f) Slash `/export` and `/import` are NOT added to the CLI or GUI; that is a separate feature, to be asked of the human.
- (g) The table is where later features (`memory-add-command` and others) add their commands; their detail text still comes from their own usage constant.

Design notes:

- Parsing: the `/help` check runs before `handle_memory_text` in each front end; in plain mode it goes before the `memory_command` callable, so `_chat_loop` needs the front end name or a help callable (design choice left to the engineer; `memory_command` is a `Callable[[str], Awaitable[str | None]]` today, `cli.py:240`).
- In the TUI the order today is quit, approval answer (lines 285-290), state command, memory. `/help` must go after the approval check so that a typed `/help` during an approval prompt still denies (fail closed), and it may go next to the state command.
- Risks: (1) a command added to a front end without a table entry: the drift test (criterion 14) catches only constants it knows about, so a new dispatch constant must be added to the test's list; (2) `/exit` is not a GUI command today (`gui/app.py:42`), so the table must say so rather than assume parity, and a later change to the GUI quit words must update the table; (3) a person who types `/help` today reaches the model as an ordinary message, so the change is visible to them; (4) line-mode help must not break piped use (stdout only, no prompt text).
- Fixtures: TUI tests use `make_app`, `send`, `texts`, `prompt_of` (`tests/test_tui.py:146,236,212,224`); GUI tests use `open_chat`, `send`, `texts` (`tests/test_gui.py:210,254,229`); plain mode tests call `_chat_loop` directly (`tests/test_cli.py:19,1484`). Whether any of them already tests a `/memory` answer in the TUI or GUI is unverified (grep for `/memory` in `tests/test_tui*.py` and `tests/test_gui*.py` found no match).

## Affected Areas

- `src/llm_harness/commands.py` (new): command table, `help_overview`, `help_detail`.
- `src/llm_harness/cli.py`: `_chat_loop` and its wiring in `main`.
- `src/llm_harness/tui/app.py`: `on_input_submitted`, `_PLACEHOLDER`.
- `src/llm_harness/gui/app.py`: `_on_send`, the input placeholder. `src/llm_harness/gui/inspector.py` is read only (button labels).
- `src/llm_harness/memory_command.py`: read-only reuse of `USAGE` and `_escape` (a public name for the escape may be needed; unverified).
- Tests: new `tests/test_commands.py`; additions to `tests/test_cli.py`, `tests/test_tui.py`, `tests/test_gui.py`; the two placeholder tests listed in criteria 4 and 13.
- `README.md` (the chat commands section around lines 18-26 and the `/memory` section), `CHANGELOG.md`.

## Out of Scope

- Adding commands to a front end (slash `/export` and `/import` for the CLI or GUI, `/exit` for the GUI); a separate feature, ask the human first.
- Key bindings and approval-prompt help.
- Localisation of the help text.
- Help for config keys.
- Completion or suggestions while typing.

## Implementation Notes

- New module `src/llm_harness/commands.py`: table `COMMANDS` of `Command` entries (name, arguments, description, front ends), front-end constants `PLAIN`, `TUI`, `GUI`, `FRONT_ENDS`, `HELP_COMMAND`, dataclass `HelpCommand`, and functions `parse_help`, `help_overview`, `help_detail`, `help_answer`. The memory detail is `memory_command.USAGE` itself (no copy).
- `src/llm_harness/memory_command.py`: one line `escape_text = _escape`, a public alias used by `help_detail` for the unknown-name text.
- Wiring: `src/llm_harness/gui/app.py` `_on_send` (after the quit check, before `handle_memory_text`; answer shown with `self._show(..., "help", "w-full")`, prompt cleared; placeholder now `Message, Enter to send, /help for commands, /quit to exit`); `src/llm_harness/tui/app.py` (after the approval check, before `parse_command`; shown as a note; `_PLACEHOLDER` includes `/help`); `src/llm_harness/cli.py` `_chat_loop` (stdout; `/help` takes priority over the `memory_command` callable).
- Deviation from assumption (a): the GUI uses its own `help` marker, not `memory`.
- Tests: new `tests/test_commands.py`; additions in `tests/test_gui.py`, `tests/test_tui.py`, `tests/test_tui_approval.py`, `tests/test_cli.py`; placeholder pins updated in `tests/test_gui.py` and `tests/test_tui_inspector.py`.

## Validation

- `uv run pytest -q -p no:cacheprovider`: 3895 passed, 0 failed, 0 skipped, 1 warning (pre-existing google-genai DeprecationWarning), 152.4 s; baseline before the change was 3840 passed.
- 265 tests are deselected by the project default `-m "not live"` (marker `live`: they call real services); not run, not requested.
- `ruff check src tests`: All checks passed. `ruff format --check src tests`: 160 files already formatted.
- Fail-before (engineer-reported): replacing `help_request = parse_help(text)` in `src/llm_harness/gui/app.py` with `None` made 3 of 5 `-k help` GUI tests fail; restored.
- Call sites (grep): `parse_help` at `src/llm_harness/cli.py:256`, `src/llm_harness/tui/app.py:292`, `src/llm_harness/gui/app.py:382`; `help_answer` at `cli.py:258`, `tui/app.py:294`, `gui/app.py:384`; `help_overview` and `help_detail` are called only inside `help_answer` (`src/llm_harness/commands.py`).
- Real offline run (`help_overview`, same text as `help_answer` without a name): GUI lists `/memory`, `/help`, `/quit` and two lines `[button] Export state ...` and `[button] Import state ...` (State tab), no `/exit`, `/export`, `/import`; TUI lists `/memory`, `/help`, `/quit`, `/exit`, `/export [path]`, `/import <path>`; plain lists `/memory`, `/help`, `/quit`, `/exit`. Also run: `/HELP /export` in the GUI names the button and says there is no `/export` slash command; `/help exit` in the GUI gives the unknown-name text; `parse_help("/helpx")` returns None.
- Unverified: no real browser run (no screenshots, by the user's rule); the GUI `help` marker choice (assumption (a)) is still open for the human; the drift test knows only the dispatch constants listed in it, a new dispatch constant must be added by hand.

## Documentation Review

- `CHANGELOG.md`: yes, needed and done (`Added` under `[Unreleased]`); it is the single changelog.
- `README.md`: yes, needed and done (Quick start sentence, "Chat commands" section with the `/help` text and a per-front-end table). No README text quotes the input placeholder (grep for `Enter to send` found nothing).
- Constitution files: `specs/mission.md` does not list chat commands other than `/memory` (line 21); no change needed. `specs/tech-stack.md` line 78 (Command line bullet) described `/quit` and `/exit` as chat input but not `/help`; edited on human approval 2026-11-04 (v32), phrase appended to that bullet: "As the first word of the chat input, `/help` (any case) lists the commands of the current front end and `/help COMMAND` shows one; it is not sent to the model."
CI review: not applicable (no .github, Makefile or Dockerfile exists; verified with ls -a, no release script)

## Documentation Updates

- `README.md`: one sentence after the Quick start commands; "Chat commands" section extended with the `/help` description and a table of which command exists in which front end (in the web GUI export and import are the State tab buttons; `/export` and `/import` are TUI slash commands; `/exit` exists in plain mode and the TUI only).
- `CHANGELOG.md`: one `Added` line for `/help`.
- `specs/memory.md`: one `[convention]` entry (new commands go in the `COMMANDS` table; drift test lists dispatch constants by hand).
- `specs/roadmap.md`: the `## Now` line for this feature removed (section back to `Nothing in progress.`).
- Constitution files: `specs/tech-stack.md` updated on human approval 2026-11-04 (v32): the `/help` sentence appended to the Command line bullet. Others: none edited.
