---
title: Context inspector in the GUI and the TUI (state, call trace, state export and import)
kind: feature
status: done
version: 3
updated: 2026-10-28
branch: main
---

## Problem / Motivation

The author wants to see what the orchestrator is thinking at any moment. Words from `orchestrator-state` (2026-10-27): "I want to be able to the context in the UI to understand what the orchestrator is thinking at any moment" and "this tool will be interactive, so I'd like to see think traces etc."

Today the GUI shows the chat, the thinking text in a collapsed block, one `step N: ...` note per step and the usage line. It does not show the `OrchestratorState` (goal, findings, plan, environment), what exactly was sent to the model, what the model answered before a reply was rejected, or what a tool returned. The pieces exist: `build_request` is a pure function (`src/llm_harness/context.py`), `StatusEvent` streams one line per step, `Orchestrator.snapshot()` and `restore()` give a serialisable `RunState` (`src/llm_harness/orchestrator.py`), and `orchestrator-state` named this feature as the owner of the UI for them.

Background (one sentence): a reference notebook tracks per-iteration token categories (system, tool definition, user, tool input, tool output, agent response) and prints each iteration's input, but it has no span tree, so the span model here is our own.

Decisions made by the author for this spec (2026-10-28), recorded here:

1. The GUI came first. After the author saw it, the terminal UI tabs were brought into this feature as slice 6 (2026-10-28), in the order 6a TUI tabs and State tab, 6b Trace tab, 6c `/export` and `/import`.
2. The trace is a tree `turn -> step -> llm_call | tool_call` of frozen pydantic spans, kept in memory for the LAST turn only.
3. No truncation of trace data by default (the author debugs with it); secrets are still masked.
4. The GUI gets tabs `Chat | State | Trace`, and buttons to export and import the state as YAML.
5. A small defect is fixed first: after a turn ends, the GUI prompt is enabled but not focused.

## Acceptance Criteria

All criteria are testable offline with the scripted step model (`tests/fake_steps.py`, `tests/fakes.py`) and the NiceGUI `user` fixture; layout criteria are pinned by tests that read classes and CSS text, never screenshots. Names in backticks are proposals.

Slice 1, focus fix
1. After a turn ends with `Done`, with an `ErrorEvent`, or with a cancel (Stop button), the GUI prompt input receives focus by calling `run_method("focus")` on it from `_end_turn` in `src/llm_harness/gui/app.py`. A test spies on the prompt's `run_method` and sees exactly one `"focus"` call for each of the three endings.
2. When the page is already closed (`_is_closed` is true), `_end_turn` does not call `run_method` (a test closes the page, ends a turn, and sees no call).
3. The existing behaviour is unchanged: the prompt is re-enabled, and the typed text is restored after an error or cancel (existing tests in `tests/test_gui.py` stay green).

Slice 2, trace recorder
4. A new module (proposal `src/llm_harness/trace.py`) defines frozen pydantic spans (same `_Frozen` base as `src/llm_harness/types.py`: `frozen=True`, `extra="forbid"`) with the fields `id`, `parent_id` (none for the turn), `kind` (`turn`, `step`, `llm_call`, `tool_call`), `name`, `start_ms` (offset from the turn start), `duration_ms`, `status` (`ok`, `error`, `rejected`, `cancelled`) and `attributes` (a plain dict).
5. The tree shape is `turn -> step -> llm_call | tool_call`: one `turn` span per `send`, one `step` span per loop step, one `llm_call` per provider attempt (a rejected reply that is retried gives several `llm_call` children of the same step), one `tool_call` per `call_tool` action. A test with a scripted run of one rejected reply, then a valid `call_tool`, then a `respond` checks the exact tree (kinds, parents, order).
6. An `llm_call` span holds in `attributes`: the full request messages (system and user, role and content as sent), the thinking text, the raw reply text, the rejection reason when the reply was rejected (the text of the `ReplyError` or `PatchError`), the usage tokens (input, output, reasoning) when the provider reported them, and the model id. The span status is `rejected` for a rejected reply, `error` for an `ErrorEvent`, `cancelled` for a cancel, otherwise `ok`.
7. A `tool_call` span holds the tool name, the arguments and the result text (the same text that becomes the next observation, including "unknown tool ..." and "tool ... failed: ..."). Status is `error` for an unknown or failing tool, otherwise `ok`.
8. The orchestrator fills the trace for the LAST turn only, in memory: a new `send` replaces the previous trace (a test runs two turns and sees only turn 2 spans, with `turn` equal to 2 in the turn span). The trace is exposed read-only (proposal: `Orchestrator.last_trace`, none before the first turn).
9. A turn that is cancelled, fails with an `ErrorEvent`, hits the step cap, or is closed early by the consumer keeps its partial trace: every span that was open is closed with its real status (`cancelled` or `error`) and a duration up to that moment. A test cancels a turn mid-stream and checks the open `llm_call`, `step` and `turn` spans are `cancelled`.
10. Thinking text stays display-only: a test shows that the thinking text recorded in the trace does not appear in any later request (`build_request` output) and not in `Orchestrator.state` or `snapshot()` (the same rule as criterion 20 of `orchestrator-state`). The trace is never read by `build_request`.
11. Data policy: the trace stores request, thinking, reply and tool text WITHOUT truncation (a test sends a 20000-character reply and finds all 20000 characters in the span, except masked secrets). Secrets are masked with `redact` from `src/llm_harness/redact.py` before they are stored (a test puts an `sk-...` shaped key in a tool result and finds `[REDACTED:api_key]` in the span and not the key). `redact` truncates to `limit` (default 300), so the trace calls it with a very large limit (a named constant, proposal `sys.maxsize`); passing `None` is not supported.
12. The text of a request in the trace is the request as it was sent, so the existing observation cap in `build_request` (4000 characters) still applies to what the model saw; the trace does not add a second cut.
13. The recording does not change behaviour: all existing tests in `tests/test_orchestrator.py` pass unchanged, and the events yielded by `send` are identical with and without a recorder (a test compares both event lists).
14. A real local Ollama run (marker `live`, no cost) with a small model records a turn and dumps the trace as JSON to a scratch file for the author to read; the dump is an artifact of the slice, not a test assertion.

Slice 3, tabs and State tab
15. The GUI page has tabs `Chat`, `State` and `Trace` built with `ui.tabs` and `ui.tab_panels`; the existing chat widgets (model selector, log, prompt, Send, Stop, New chat) live in the `Chat` panel and keep their `.mark(...)` names, so `tests/test_gui.py` keeps passing; the new tabs are marked `tab-chat`, `tab-state`, `tab-trace`.
16. The State tab shows the current `OrchestratorState` as text: `goal`, `findings` (text and source), `plan` (text and status), `environment` (`now`, `user_name`, `computer_name`), and the counters `turn` and `step`.
17. The State tab shows a "next request" preview computed with the pure `build_request` (never a second implementation): the messages as they would be sent, and the character count of each part: instructions plus schemas (the system message), state JSON (`state.model_dump_json()`), and observation (proposal: the text currently typed in the prompt, wrapped with `answer_observation` when `pending_question` is set; empty when nothing is typed). A test compares the shown messages with a direct `build_request` call on the same state.
18. The State tab refreshes at the end of every turn and after each step (on every `StatusEvent`); a test with a two-step scripted turn sees the state shown after step 1 before the turn ends.
19. Every model-derived string in the State tab (goal, findings, plan, source, question) goes through `sanitize_text` from `src/llm_harness/display.py` and is shown as a text node in a `ui.label` (a test finds no `ui.html`, `ui.markdown` or `ui.code` with model text in the new module's source; a test puts `\x1b[31m` into the goal and sees it removed).

Slice 4, Trace tab
20. The Trace tab shows the span tree of the last turn (proposal: `ui.tree`, one node per span) with each span's name, `duration_ms` and status. Before the first turn it shows an empty-state line. It refreshes at the end of every turn, after each step, and after a cancel or error.
21. Selecting a span shows its details: kind, name, start offset, duration, status and every attribute. Long fields (full request messages, thinking, raw reply, tool result) are shown in full inside collapsed `ui.expansion` blocks, each with a scrollable, wrapped, non-fixed-height text area (class `whitespace-pre-wrap`, no `h-<number>` or fixed pixel height), never cut.
22. Every model-derived string in the Trace tab (request text, thinking, reply, rejection reason, tool arguments and result, span names) goes through `sanitize_text` and is shown as a text node in a `ui.label`; none uses `ui.html`, `ui.markdown` or `ui.code` (pinned by a source test and by a test that puts `<script>` text into a reply and finds it as plain text).

Slice 5, YAML export and import
23. A button `Export state` downloads a `.yaml` file (proposal name `llm-harness-state.yaml`) with the dump of `Orchestrator.snapshot()`: `RunState.model_dump(mode="json")` written by `yaml.safe_dump`. The file holds the state and `pending_question`. The trace is NOT part of the export (assumption).
24. Round trip: export, then import into a fresh `Orchestrator`, gives a `RunState` equal to the exported one (a test compares `snapshot()` before and after, including a timezone-aware `environment.now`).
25. A button `Import state` accepts one uploaded `.yaml` file. The text is read with `yaml.safe_load` only (a test uploads a file with a `!!python/object` tag and sees it rejected, nothing executed).
26. A file larger than 1,000,000 bytes (1 MB, assumption) is rejected before parsing; the check is made on the server on the received bytes, in addition to any limit set on the upload widget (a test uploads 1,000,001 bytes).
27. The loaded data is validated with the pydantic `RunState` model (`extra="forbid"` through `_Frozen`: unknown keys, a wrong type, a naive `now` or a blank finding source are all errors). A rejected file shows ONE safe line, for example `import failed: <short reason>`, built with `redact(sanitize_text(...))`, never a traceback, never a multi-line pydantic dump.
28. Import is refused while a turn is running: the GUI shows one line and `Orchestrator.restore()` is not called with a changed state (restore itself raises `RuntimeError("cannot restore while a turn is running")`; a test catches it and shows the line, and also disables the buttons while busy).
29. A failed import (any of 25 to 28) leaves the current state, `pending_question` and the chat untouched (a test compares `snapshot()` before and after a bad file).
30. After a successful import, the State tab refreshes and a status line says the state was loaded; the next turn overwrites `environment` (`now`, `user_name`, `computer_name`) with the harness values, as `Orchestrator.send` does for every turn today (a test imports a file with another `user_name` and sees the new one after the next turn).
31. The `Chat` transcript is not touched by an import: only the orchestrator state changes (a one-line note in the README says the on-screen transcript is not restored).

Layout (all slices; user's standing rules, pinned by tests that inspect classes and CSS text)
32. The tabs and every new panel use the full browser width and the full viewport height: new containers carry `w-full` and `h-full` or `grow`, and only the panels scroll, not the page (the existing `html, body { overflow: hidden; }` stays).
33. No new code in `src/llm_harness/gui/` uses `max-w-`, `mx-auto`, a fixed-height box (`h-<number>`, `height: <number>px`), or a narrow centred column (a test scans the source text of the GUI modules for these tokens).
34. Base font is at least 18px and secondary text at least 15px: new text uses the existing `_PAGE_CSS` rules (18px base in `.llm-page`, 16px in `.llm-secondary`), and a test parses `_PAGE_CSS` and every CSS block added by this feature and finds no `font-size` below 15px and the base at 18px or more.

Slice 6, TUI tabs (human-approved defaults; testable offline with Textual's pilot, in the style of `tests/test_tui.py`, and the scripted fakes)
38. The TUI (`src/llm_harness/tui/app.py`, a Textual `TabbedContent`) has tabs `Chat`, `State`, `Trace` with ids `tab-chat`, `tab-state`, `tab-trace`; all existing chat widgets and ids (`#model`, `#log`, `#prompt`) and all 62 tests in `tests/test_tui.py` keep working unchanged; the prompt keeps or regains focus after Done, error and cancel as today (a test pins it) and the Ctrl+Q, Escape and Ctrl+N bindings are unchanged.
39. State tab: collapsible sections (Textual `Collapsible`) for goal (open), findings (open), plan (open), environment (open) and the next-request preview with the character counts of system, state JSON and observation (system and state JSON collapsed, observation open); the counters `turn` and `step`; a pending question when one is set. The tab is built once and only its content is updated on refresh, so the open or closed state survives; it refreshes after each `StatusEvent`, at the end of a turn (Done, error, cancel), on New chat and on model change. The pure logic of the GUI is reused where it is not NiceGUI-specific (for example `preview_observation`) WITHOUT importing nicegui into the TUI (the TUI must not import nicegui; a test or a grep pins it); shared pure helpers move to a small neutral module only if needed.
40. Trace tab: the last turn's spans from `Orchestrator.last_trace` as nested `Collapsible`s titled `kind: name | status | N ms`; details (kind, name, start, duration, status, parent, and short attributes such as `model`, `reported_model`, `usage`, `cost_usd`, `seconds`, `error`) and one collapsed `Collapsible` per long field (messages, thinking, reply, rejection, arguments, result) with the FULL text (no truncation); orphan spans (parent not yet closed) are listed at the root; an empty trace shows `No trace yet.`; the open state is remembered across refreshes within a turn, as in the GUI.
41. All model-derived text is shown with `Static(..., markup=False)` after `sanitize_text`, never parsed as Rich markup: a hostile string such as `[bold]`, `[/]` or ESC is shown literally (tested).
42. `/export [path]` in the prompt (no new key binding) writes `export_yaml(snapshot())` to `path`, default `llm-harness-state.yaml` in the current directory. It REFUSES to overwrite an existing file (one line `export failed: <path> already exists`), refuses a path whose parent directory does not exist, writes with mode 0600, and shows `state exported to <path>`. The file is created with `os.open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)` in `state_files.write_export`, so an existing file, a symlink (dangling or not) or a symlink race fails instead of overwriting.
43. `/import <path>` goes through `state_files.read_import`: it opens the file with `O_RDONLY | O_NONBLOCK`, requires a regular file (`S_ISREG`, so a FIFO does not hang), calls `state_io.check_size` on the `fstat` size BEFORE reading (1,000,000 bytes), reads at most 1,000,001 bytes and checks the length again, then calls `import_yaml` (aliases and Python tags rejected, one safe line on error). It refuses while a turn runs (one line), leaves `snapshot()` unchanged on any failure, shows `state loaded from <path>` on success and refreshes the State tab. A missing path, a directory and an unreadable file each give one `import failed: ...` line, never a traceback. The path is used as given (`~` expanded, no globbing); the on-screen transcript is not restored.
44. `/export` and `/import` are not sent to the model (a test shows the fake provider got no request) and are listed next to `/quit` in the placeholder or a note; the Input is not cleared into the chat log as a user message.
45. Slice-6 layout and readability: TUI widgets fill the width and the height (`width: 1fr` or `100%`, `height: 1fr`), with no fixed widths or heights for the log or the panels (a CSS-text test).
46. Slice 6 tests are added to `tests/test_tui.py` or to a new `tests/test_tui_inspector.py`; existing tests are unchanged; ruff is clean; the docs (a README section for the TUI commands, a CHANGELOG `Added` line) are updated at B4/B5.

General
35. `uv run ruff check` and `uv run pytest` pass offline with zero skips; live tests carry the `live` marker.
36. Every new function, type and constant has a non-test use (as in criterion 24 of `orchestrator-state`).
37. README and CHANGELOG are updated at documentation time (B5): tabs, State and Trace, export and import, the focus fix, the 1 MB cap and the "trace is not exported" rule.

## Approach

Memory consulted: gui|nicegui|focus|orchestrator|trace — 4 relevant entries (slices 1-5); textual|tui (case-insensitive, 2026-10-28, for slice 6) — 2 hits, both relevant: `Static(..., markup=False)` for model text (criterion 41) and the Textual `Markdown` footnote and task-list gotcha. Slices 1-5 entries: "NiceGUI 3.18: `ui.run` defaults host to all interfaces (pass the loopback host explicitly); `client.on_disconnect` also fires for sockets that reconnect (use `client.on_delete` for cleanup); call `ui.notify` through the `ui` module for the test `user` fixture; pytest needs `-p nicegui.testing.user_plugin`", "NiceGUI 3.18: `ui.html(content, sanitize=<callable>)` applies the callable on the server before innerHTML; only `sanitize=True` triggers browser-side DOMPurify, so a callable is the only filter", the Ctrl-C `KeyboardInterrupt` entry (not relevant), and the entries on the Ollama response schema (not relevant to the GUI).

Ordered slices. Each one is runnable and reviewed by the author before the next starts.

1. Focus fix and test (criteria 1-3). Smallest change: in `ChatPage._end_turn` (`src/llm_harness/gui/app.py`), after `_set_busy(False)`, call `self._prompt.run_method("focus")`. The terminal UI already does this (`prompt.focus()` in `_end_turn`, `src/llm_harness/tui/app.py`). Run: `uv run llm-harness --gui`; the author types, sends, and sees the cursor back in the prompt.
2. `trace.py`, orchestrator hooks, unit tests, live dump (criteria 4-14). Design:
   - The spans are frozen. A small recorder (not frozen) holds the open spans and a clock (injectable, like `Orchestrator`'s `clock`), and creates a frozen span when one closes. The tree is a flat tuple of spans linked by `parent_id`, which keeps the model plain and serialisable.
   - `Orchestrator.send` is the only place that opens and closes spans: the turn at the start (next to the environment refresh), a step per loop iteration, an `llm_call` around each provider attempt (the request from `build_request`, thinking from forwarded `ThinkingDelta`, text from the collected `attempt.text`, the rejection text from the caught `ReplyError` or `PatchError`), and a `tool_call` around `_run_tool`. Closing happens in `finally`, so cancel, error, step cap and early close all keep a partial trace.
   - The model id of a call comes from the request (`ChatRequest.model`); the router may switch models inside a call, and the model that actually answered is in the `UsageReport` (`model_id`) when one is emitted. Which of the two to show when they differ: unverified, decide while implementing and test with a failing first model.
   - Usage tokens come from the `UsageEvent` (`Usage`: `input_tokens`, `output_tokens`, `reasoning_tokens`); whether a provider emits it for every attempt: unverified per provider.
   - Redaction: `redact(text, limit=<very large constant>)`. Verified by running it: `redact("x" * 5000, limit=sys.maxsize)` returns 5000 characters, and `limit=None` raises `TypeError` (the comparison `len(text) > limit` in `src/llm_harness/redact.py`). So a no-limit call is possible only with a large integer.
   - Tests use `ScriptedSteps` from `tests/fake_steps.py` for the tree, rejection, tool, cancel and step-cap cases. The live check runs one turn against local Ollama and writes the trace as JSON to the scratchpad.
3. GUI tabs and State tab (criteria 15-19, 32-34). Suggest a new module `src/llm_harness/gui/inspector.py` for the State and Trace panels, so `gui/app.py` (397 lines today) stays small; `ChatPage` owns the tabs and calls the inspector's refresh. The refresh is driven from `_run_turn`: on each `StatusEvent` and in `finally` of the turn. Preview: call `build_request(orchestrator.state, observation, model_cfg)` (the same call `send` makes); the character counts are `len` of the system message, of `state.model_dump_json()`, and of the observation text.
4. Trace tab (criteria 20-22). `ui.tree` for the span tree (its signature has `nodes`, `node_key`, `label_key`, `on_select`; verified with `inspect.signature`); selecting a node renders the details of that span below it. Long text goes into `ui.expansion` plus `ui.label` with `whitespace-pre-wrap`, the same pattern as the existing thinking block (`_show_thinking`).
5. YAML export and import (criteria 23-31). `pyyaml>=6.0.3` is already a dependency (`pyproject.toml` line 19) and locked at 6.0.3 (`uv.lock`, package `pyyaml`), so there is no new dependency. `RunState` is a `_Frozen` pydantic model with `state: OrchestratorState` and `pending_question: str | None` (`src/llm_harness/state.py`). Verified by running: `RunState.model_dump(mode="json")` turns `environment.now` into an ISO 8601 string with offset (for example `2026-10-28T13:42:13.977452+01:00`), `yaml.safe_dump` then `yaml.safe_load` then `RunState.model_validate` gave a value equal to the original. Download: `ui.download(src, filename, media_type)` accepts bytes (signature verified). Upload: `ui.upload(max_file_size=..., on_upload=..., on_rejected=...)` exists (signature verified); the attribute names of the upload event that give the file bytes are UNVERIFIED and must be checked in the installed NiceGUI before coding. Import order: check busy, check size, `yaml.safe_load`, `RunState.model_validate`, then `Orchestrator.restore()` last, so any failure before it changes nothing. Environment overwrite is verified in `Orchestrator.send` (the `model_copy(update={... "environment": self._environment()})` at the start of each turn).
6. Terminal UI tabs (criteria 38-46), now in scope, in three runnable steps, each reviewed by the author before the next:
   - 6a: `TabbedContent` with `Chat`, `State`, `Trace` in `src/llm_harness/tui/app.py`, the chat widgets unchanged, and the State tab (criteria 38, 39, 41, 45).
   - 6b: Trace tab (criterion 40, with 41 and 45).
   - 6c: `/export` and `/import` in the prompt (criteria 42-44), then docs and tests (criterion 46).

Assumptions (the author may revisit any of them):
- A1. No truncation of trace data by default; the author was undecided. Long fields are collapsed in the UI, not cut.
- A2. The upload size cap is 1 MB (1,000,000 bytes).
- A3. The trace is not part of the YAML export.
- A4. The next-request preview uses the text typed in the prompt as the observation (empty when nothing is typed).
- A5. Only the last turn's trace is kept; a new turn replaces it.
- A6. The names `trace.py`, `Orchestrator.last_trace`, `gui/inspector.py`, `llm-harness-state.yaml` and the span ids are proposals.
- A7. Masking happens when the trace is recorded (secrets never sit in the stored trace), not only when it is shown.
- A8. The `/export` and `/import` command syntax and the no-overwrite default are human-approved defaults that the author may revisit. The Textual `Collapsible` and `TabbedContent` API names are UNVERIFIED until the engineer prints them from the installed package.

Unverified:
- The attribute names of NiceGUI's upload event for the file bytes (see slice 5).
- The Textual `Collapsible` and `TabbedContent` API names, parameters and events used by slice 6 (see A8).
- Whether every provider adapter emits `UsageEvent` per attempt, and which model id the router reports after a switch inside one call.
- That `run_method("focus")` focuses the prompt in a real browser: the method exists (`Element.run_method(name, *args, timeout=1)` in NiceGUI 3.18.0, signature printed with `inspect`), and tests can only spy on the call; the author must confirm by hand.

Risks:
- Memory use of an untruncated trace of a long turn: bounded by 8 steps, 3 attempts per step and the 4000-character observation cap, and only one turn is kept.
- Secrets in the user's own text appear in the trace; mitigated by `redact` at record time, but `redact` only knows the shapes it has patterns for.
- A crafted YAML file: mitigated by `safe_load`, the size cap and pydantic validation with `extra="forbid"`.
- GUI refresh on every step could be slow with huge text; check by hand with a long reply.

## Affected Areas

- New: `src/llm_harness/trace.py` (spans and recorder), `src/llm_harness/gui/inspector.py` (State and Trace panels, export and import; suggested split), tests `tests/test_trace.py` and inspector tests next to `tests/test_gui.py`.
- Changed: `src/llm_harness/orchestrator.py` (`send` records spans; `last_trace` property), `src/llm_harness/gui/app.py` (tabs, focus fix in `_end_turn`, refresh calls), `src/llm_harness/state.py` only if a helper for the YAML check is needed (`RunState` itself is expected to stay as it is).
- Tests: `tests/test_orchestrator.py`, `tests/test_gui.py`, `tests/fake_steps.py` and `tests/fakes.py` (reused, extended only if needed).
- Docs at documentation time: `README.md`, `CHANGELOG.md`; `specs/roadmap.md` `Now` line (mechanical, by `/code`).

## Out of Scope

- History of earlier turns' traces; persisting traces to disk.
- Token counting with a tokenizer: the trace uses provider usage and character counts only.
- Subagents or tools UI (`subagents`, `tools-first-set`); A2UI components (`a2ui-nicegui`).
- Exporting the trace; restoring the chat transcript on import.
- Editing the state by hand in the UI (the tabs are read-only, except import).

## Implementation Notes

What was done, per slice (all on `main`, working tree, not committed):

1. Focus fix: `ChatPage._end_turn` in `src/llm_harness/gui/app.py` calls `self._prompt.run_method("focus")` after `_set_busy(False)` (not when the page is closed).
2. Trace: new `src/llm_harness/trace.py` (frozen `Span`, `SpanHandle`, `TraceRecorder`, `UNCUT = sys.maxsize`). `Orchestrator` takes `recorder: TraceRecorder | None = None` and exposes `last_trace` (closed spans only, `None` before the first turn). `llm_call` spans also record the router's `UsageReport` data: `usage`, `cost_usd`, `seconds`, `reported_model`.
3. Tabs and State tab: new `src/llm_harness/gui/inspector.py` (`build_tabs`, `StatePanel`); `gui/app.py` builds `ui.tabs` and `ui.tab_panels` and calls `_refresh_inspector` on each step and at the end of a turn.
4. Trace tab: `TracePanel` in `inspector.py`.
5. Export and import: new `src/llm_harness/state_io.py` (`export_yaml`, `import_yaml`, `check_size`, `StateImportError`) and `StateTransfer` in `inspector.py`.

Decisions and deviations from the approved Approach:

- The Trace tab uses nested `ui.expansion` blocks (turn, step, then `llm_call` or `tool_call`), not `ui.tree` with selection. Every span title reads `kind: name | status | N ms`; the details are shown inside the expansion.
- No truncation of trace data: assumption A1 confirmed by the human ("I will use traces for debugging"). Secrets are still masked by `redact` with `UNCUT`.
- Import does not use `yaml.safe_load` only (criterion 25). `pyyaml` 6.0.3 `safe_load` expands aliases by reference: a 293-byte file of nested aliases took 3.8 s in validation. `state_io.py` parses with a `SafeLoader` subclass (`_NoAliasLoader`) that rejects aliases; Python tags are still rejected by `SafeLoader`.
- Spans round `start_ms` and `duration_ms` to 3 decimals.
- `Orchestrator.last_trace` returns closed spans only; a span still open is absent, and the Trace tab lists a span whose parent is not yet closed under the root.
- The `Import state` button is a plain `ui.button`; it opens a hidden `ui.upload` through `run_method("pickFiles")` so it looks like `Export state`. The human asked for the button alignment and for the message below the buttons; both are done.
- There is no global status line (criterion 30 says "a status line"): `state loaded`, `state exported` and `import failed: ...` are shown in one label below the two buttons.
- The 1,000,000 byte limit is checked on the server with `event.file.size()` before the bytes are read, but only after NiceGUI has received the file (the widget's own `max_file_size` is client side). The GUI is loopback only; a server-side limit on the raw upload is a possible follow-up.
- Import validation: UTF-8 decode, parse, `RunState` validation, then `Orchestrator.restore()` last. A failure shows one line `import failed: ...` built only from the field path and rule (never from file values) and changes nothing. A running turn makes `restore` raise `RuntimeError`, shown as `import failed: a turn is running, try again after it`; the buttons are also disabled while busy.
- Slice 6, terminal UI (done in three steps, each its own test run):
  - 6a: `src/llm_harness/tui/app.py` uses a `TabbedContent` with `TabPane` ids `tab-chat`, `tab-state`, `tab-trace`; new `src/llm_harness/tui/state_tab.py` (`StatePanel`, built once, sections keep their open state). Pure text helpers shared with the GUI are in the new `src/llm_harness/inspect_state.py` (no NiceGUI import). Tests 1262 -> 1285 passed.
  - 6b: new `src/llm_harness/tui/trace_tab.py` (`TracePanel`, nested `Collapsible`s titled `kind: name | status | N ms`, full details and long fields collapsed, orphan spans at the root, `No trace yet.`; open state remembered per span id within a turn). Tests 1285 -> 1299 passed.
  - 6c: `/export [path]` and `/import <path>` in `ChatApp.on_input_submitted` (`src/llm_harness/tui/app.py`), with the new pure file helpers in `src/llm_harness/state_files.py` (`parse_command`, `resolve_path`, `write_export`, `read_import`, `StateFileError`). Tests 1299 -> 1351 passed.
- Slice 6 decisions and deviations:
  - Export is allowed while a turn runs (it only reads `snapshot()`); import is refused then.
  - On a failed command the typed text stays in the prompt so the path can be fixed; on success the prompt is cleared. Neither command is echoed as a user message or sent to the model; only the first word is the command (`/exportx` goes to the model).
  - Export does not use `open(path, "xb")` (criterion 42): `write_export` uses `os.open` with `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` and mode 0600, so an existing file, a symlink and a dangling symlink are all refused; a half-written file is removed.
  - Import (criterion 43) opens with `O_NONBLOCK` and checks `S_ISREG` on the path and on the descriptor, so a FIFO does not hang; the size is checked from `fstat` before reading, reads at most 1,000,001 bytes, and checks the length again.
  - Textual 8.2.8 finding: `Collapsible(title=str)` parses the title as markup, so every trace title is wrapped in `Content(...)` (`trace_tab.py`). Model text in bodies uses `Static(..., markup=False)`.
  - TabPane choice: the pane `id` is the tab id (`TabbedContent.active` returns it); the clickable `Tab` widget id is `--content-tab-<pane id>`, so tests click `#--content-tab-tab-state`. Criterion 38 names the pane ids.
  - While the State or Trace tab is active the focus is on the tabs and sections, and Enter toggles a focused `Collapsible`; the user returns to `Chat` to type (documented in the README).
  - Refresh points: each `StatusEvent`, the end of a turn (Done, error, cancel), New chat, model change, and a successful `/import`. The Trace panel rebuilds its tree on each refresh (new tree mounted before the old one is removed).
  - The placeholder reads `Message, Enter to send, /export, /import <path>, /quit to exit`.
- Mutation checks for slice 6 (each change reverted, suite re-run green): 6a hooks, `_start_session` removed 4 tests failed, `StatusEvent` refresh removed 1 failed, `_end_turn` refresh removed 3 failed; 6b `refresh_trace` removed 14 failed, plain `str` title instead of `Content` caught, ignoring the remembered open state caught, skipping the reset on a new turn caught; 6c both `check_size` calls removed caught, `O_EXCL | O_NOFOLLOW` replaced by `O_TRUNC` caught, busy guard removed caught (the busy-guard test catches it only because the test patches `read_import` to raise, since `Orchestrator.restore` also raises while busy).
- Spec version 3 (2026-10-28): criteria 42 and 43 reworded to match the code (`os.open` with `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` and mode 0600 in `state_files.write_export`; `state_files.read_import` with `O_NONBLOCK`, `S_ISREG`, `check_size` before reading). The behaviour is a strict superset of the earlier wording: a correction of implementation detail, not a scope change.

Tests added: baseline 1160 passed; 1262 passed after slice 5, and 1285, 1299 and 1351 passed after slices 6a, 6b and 6c, 4 live deselected (`uv run pytest -q`; 1351 re-run during documentation); 191 more tests in total. Slice 6 tests are in `tests/test_tui_inspector.py` (new) and `tests/test_state_files.py` (new), `tests/test_tui.py` (changed). New or extended files: `tests/test_trace.py`, `tests/test_state_io.py`, `tests/test_gui_inspector.py`, `tests/test_orchestrator.py` (new in the working tree), `tests/test_gui.py` and `tests/test_router.py` (changed).

Unverified:

- `run_method("pickFiles")` on the hidden uploader: reasoned from the Quasar source and not checked in code or tests; the human confirmed in a browser that export and import work.
- `run_method("focus")` focusing the prompt: tests only spy on the call; the human tried the GUI and said it works.
- Whether every provider adapter emits usage per attempt, and which model id the router reports after a switch inside one call (the span keeps both `model` and `reported_model`).

## Validation

Final run by the tester (record `validate3.txt`, tail confirmed): `timeout 600 .venv/bin/python -m pytest -rs -v` -> 1351 passed, 0 skipped, 0 failed, 4 deselected, 1 warning in 69.89 s. The 4 deselected are the live provider tests (marker `live`, deselected by `addopts`, not run). The warning is a google-genai `DeprecationWarning` (third party). No skip exists, so none needs an explanation. Baseline before the feature: 1160 passed.

- Per file passed: `tests/test_trace.py` 18, `tests/test_state_io.py` 18, `tests/test_state_files.py` 30, `tests/test_gui_inspector.py` 62, `tests/test_gui.py` 79, `tests/test_tui.py` 62, `tests/test_tui_inspector.py` 59 (one test was renamed after the run to `test_the_trace_tab_shows_the_empty_line_before_the_first_turn`; count unchanged; the file was re-run: 59 passed).
- `ruff check`: clean. `ruff format --check`: 85 files already formatted.
- Stage counts: 1164 (slice 1), 1180 (2), 1203 and 1221 (3-4; 1221 after the usage fix), 1259 and 1262 (5; 1262 after the button fix), 1285 (6a), 1299 (6b), 1351 (6c).

Real artifact runs (scratch HOME, no user config):

- `llm-harness --help`: exit 0.
- Scripted fake-provider turn, export, import: snapshots equal, file mode 0600, a second export refused, a 1,000,001-byte file refused.
- Real local Ollama `gemma4:12b` turns through the orchestrator: usage tokens recorded, spans `turn`, `step`, `llm_call` all `ok` (one run 45.5 s, one 63.9 s).
- Real Vertex run by the human through the GUI: trace shown with `model` and `reported_model`; the first run lacked `usage` (router `UsageReport`) and was fixed.
- Playwright check `tests/manual/verify_import_button.py`: exit 0 (file chooser fired inside the click with user activation, import loaded).
- The human confirmed in a real browser that export and import work after the browser-side click fix.

Mutation checks (each reverted, suite re-run green):

- Slice 2: removing the `tool_call` hook failed 5 tests.
- Slice 3: refresh hooks (`StatusEvent`, session start, `_end_turn`, sanitize on goal) each caught.
- Slice 4: refresh hooks failed 3, 12, 10 and 12 tests.
- Slice 5: guards `check_size`, `SafeLoader` -> `UnsafeLoader`, alias check, `set_busy`, restore busy check, and the GUI `check_size` before read each caught by a named test.
- Slice 6: see Implementation Notes.

Unverified (coverage gaps):

- Real browser look of the tabs and tree layout beyond what the human tried; real terminal look and keys of the TUI.
- The 4 live provider tests were not run in the suite.
- The model id the router reports after a switch inside one call.
- `Orchestrator(recorder=)` has no non-test caller on purpose: it is a test seam for an injectable clock or for disabling recording.

## Documentation Review

CI review: not applicable (no CI, build or release file covers the affected paths). Checked: there is no `.github/` directory and no `Makefile` or `Taskfile` in the repository root.

Checked against the code as it is now:

- `README.md` (Medium): had no mention of the `State` and `Trace` tabs, export and import, the 1,000,000 byte cap, the rejected content, the non-restored transcript or the focus fix (criterion 37). Fixed in B5.
- `CHANGELOG.md` `## [Unreleased]` (Medium): no entries for these changes. Fixed in B5.
- This spec, Approach (Medium): it says `ui.tree`, `yaml.safe_load` only, a visible `ui.upload`, "a status line", and "attribute names of the upload event unverified". The code uses nested `ui.expansion`, an alias-rejecting `SafeLoader` subclass, a hidden uploader behind a plain button, and a label below the buttons; the upload event gives `event.file.size()` and `await event.file.read()` (`src/llm_harness/gui/inspector.py`, `StateTransfer._on_upload`). Recorded under Implementation Notes; sections 1 to 5 left as approved (no version bump).
- `specs/roadmap.md` line 11 (Medium): the `Now` line stayed until B5; removed at closeout (see Documentation Updates).
- `specs/tech-stack.md` (Medium): the proposed edits (line 28 on `safe_dump` and the alias-rejecting `SafeLoader`; the layout line) were applied and the file is now version 11; nothing left to do there.
- `specs/mission.md` and `specs/tech-stack.md` line 44: still true.
- `README.md` (Medium, found at the end of slice 6): said "The terminal UI and `--plain` have no inspector yet" and listed no `/export` or `/import`; the `## Chat commands` section did not name them. Fixed.
- `CHANGELOG.md` (Medium): the two inspector lines said "in the GUI" and no line covered the terminal UI tabs or the two commands. Fixed (new lines; the GUI lines are still true).
- This spec (Medium): the Implementation Notes line "The terminal UI tabs remain a later, separate slice", the "proposed, not edited" wording for `tech-stack.md`, and the test counts stopped at 1262. Fixed.
- Criteria 42 and 43 (Low): said `open(path, "xb")` and a size check "BEFORE reading" through `state_io.check_size`; the code uses `os.open` with `O_EXCL | O_NOFOLLOW` and `read_import`. Reworded at closeout (version 3, see Implementation Notes).

## Documentation Updates

- `README.md`: section `## Context inspector (GUI)` (before `## Flags`): tabs, State and Trace content, `Export state` (`llm-harness-state.yaml`, state and pending question, no trace), `Import state` (UTF-8, 1,000,000 bytes, aliases and Python tags rejected, validation, one-line `import failed: ...`, nothing changes on failure, refused while a turn runs, environment overwritten at the next turn, transcript not restored), messages below the buttons, focus after a turn. After slice 6 the section is retitled `## Context inspector (GUI and terminal UI)` and covers both front ends: the terminal UI tabs, focus on the tabs while State or Trace is active, `/export [path]` and `/import <path>` rules; `## Chat commands` points to it; `## Security notes` has a bullet on the 0600 exclusive export and the import limits. No other file linked the old heading (grep).
- `CHANGELOG.md` `## [Unreleased]`: two `Added` lines (context inspector tabs; state export and import) and one `Changed` line (prompt focus after a turn); after slice 6 two more `Added` lines (terminal UI inspector tabs; `/export` and `/import`).
- `specs/memory.md`: three `[gotcha]` entries (pyyaml aliases; NiceGUI `user` fixture, upload event and client-side limit; `redact` limit); after slice 6 five more `[gotcha]` entries (Textual `Collapsible` title markup; TabPane id; Enter on a focused `Collapsible`; `O_EXCL | O_NOFOLLOW` export; `O_NONBLOCK` and `S_ISREG` import).
- Constitution: `specs/tech-stack.md` was updated (version 11) with the `safe_dump` and `SafeLoader` note and the new modules.

Closeout checklist:

- Validation recorded with pass/skip/fail counts: 1351 passed, 0 skipped, 0 failed, 4 live deselected; no skip, so none to explain (section Validation).
- README (`README.md`, section `## Context inspector (GUI and terminal UI)`) and CHANGELOG updated. `CHANGELOG.md` at the repo root is the only changelog (`find` found no other), and `specs/tech-stack.md` has no `Artifacts` table (grep: no match), so the implicit single artifact applies.
- `specs/roadmap.md`: this feature's `## Now` line removed at closeout (the section now reads `Nothing in flight.`); no `Next` or `Later` line for `context-inspector` exists (`Next` is "Nothing queued."; `Later` lists four other slugs); the roadmap version stays 6 (mechanical bookkeeping, no bump).
- CI review: the line `CI review: not applicable ...` is in Documentation Review; re-checked: no `.github/`, no `Makefile`, no `Taskfile` in the repo root.
- Non-test use of every new function, type and config key (criterion 36): the tester's grep found all 34 names used in `src/`. The one exception is `Orchestrator(recorder=)`, a test seam by design (see Validation, Unverified).
- `validate-specs` passes (run at closeout, output in the report).
