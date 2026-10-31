---
title: "Tool approval: a human gate for write and exec tools"
kind: feature
status: done
version: 3
updated: 2026-10-30
branch: main
---

## Problem / Motivation

Feature 1 of 4 that split the roadmap item `tools-first-set` (order: `tool-approval`, `web-tools`, `command-execution`, `memory`; a Docker backend comes later). `tool-registry` (done) made tools typed and discoverable.

`Orchestrator._call_tool` (`src/llm_harness/orchestrator.py`, about lines 451-480) runs a tool as soon as the allowed-set check and pydantic validation pass. There is no approval hook. The only access control is static agent config (`AgentConfig.tools` and `deny`). The upcoming tools (`web_fetch`, `run_command`, memory writes) need a human gate before they run.

`ask_user` cannot carry that gate. It sets `pending_question`, yields Done and ends the turn (`orchestrator.py:340-346`); the next `send` wraps the user's text with `answer_observation` (`orchestrator.py:249-255`). An approval must block in the middle of a turn, so it needs its own mechanism.

Decisions confirmed by the author (resolved assumptions):

1. `risk` is a required field on the `Tool` protocol, with no default. `register()` refuses a missing or unknown value.
2. A harmless built-in tool `dev_echo` with risk `write` is shipped. It is registered only when enabled by a new boolean config key (name to be confirmed at implementation, default false), so the prompt can be tried by hand before any real write or exec tool exists.
3. Allow-once only. There is no "allow for session".
4. When no approver is injected, the call is denied.

## Acceptance Criteria

All tests run offline and deterministically, with `ScriptedProvider`, a stub write tool and a fake approver; no network.

1. The `Tool` protocol gains `risk: Literal["read", "write", "exec"]`. `ToolRegistry.register` rejects a tool whose risk is missing or not one of the three. `tool_search` has risk `read`. The three test stubs (areas of `tests/test_orchestrator.py:348`, `tests/test_subagents.py:62`, `tests/test_tools.py:37`) declare `risk = "read"`.
2. New `src/llm_harness/approval.py` defines `ApprovalRequest` (fields: tool, risk, agent_id, `arguments_text`, escaped and not truncated; see item 11) and `Approver = Callable[[ApprovalRequest], Awaitable[bool]]`. The constructor becomes `Orchestrator(..., approver: Approver | None = None)`.
3. In `_call_tool` the order is: allowed-set check, argument validation, approval (only when risk is not `read`), run. Invalid arguments are never sent for approval.
4. A `read` tool never asks. A `write` or `exec` tool that is approved runs. A denied call does not run and returns `tool X was not run: the user denied it`. With no approver the result is `tool X was not run: it needs the user's approval and none is available here`. An approver that raises counts as a deny (the error text is redacted). `CancelledError` propagates.
5. The `tool_call` span records the attribute `approval` with one of `not_required`, `approved`, `denied`, `unavailable`. A deny sets span status `rejected` (already used at `orchestrator.py:305`). Spans are masked with `redact` on close.
6. Subagents built by `SubagentRun` (`subagents.py`, about lines 99-114) get no approver, so their write and exec calls are denied. Passing the parent's approver is out of scope.
7. CLI: the prompt goes to stderr, `y` or `yes` (case and surrounding spaces ignored) allows, anything else denies, and the answer is read with the existing `_read_line` inside the turn. When stdin is not a terminal the CLI passes NO approver (calls needing approval are denied with the "none is available here" text), because a buffered or piped line must not answer an approval. On a terminal, input typed ahead is flushed before the prompt is written.
8. TUI: a `Future` plus an approval mode in `on_input_submitted`, checked before the turn-running early return (`tui/app.py`, about line 281); the prompt is rendered with `Static(markup=False)`. The same y/yes rule as the CLI applies. Stop is the Escape key (`Binding("escape", "cancel_turn")` in `tui/app.py`), not Ctrl-C. An empty Enter keeps waiting; any other non-empty text denies, including a typed `/export`.
9. GUI: Approve and Deny buttons and a plain `ui.label` (never `ui.html` or markdown) in a full-width log. Project rule: full browser width and viewport height, base text at least 18px, secondary text at least 15px, no narrow centred columns, no fixed-height boxes; pinned by tests on the code and classes, no screenshots. The approver is bound to the page that owns the orchestrator (`gui/app.py`, about lines 170-172).
10. Stop (Escape in the TUI, the Stop button in the GUI) or Ctrl-C in the CLI works while an approval waits, and the TUI and GUI clear their pending `Future` in a `finally`.
11. The prompt shows tool name, risk, calling `agent_id` and the arguments. The arguments text is built from `arguments.model_dump(mode="json")` of the VALIDATED model that will run (so defaults and coercion are visible), as `json.dumps(sort_keys=True, ensure_ascii=True)`, so control, format, bidi and non-ASCII characters appear as visible `\uXXXX` escapes (escape, never strip). There is NO heuristic `redact` and no `sanitize_text` stripping on this text (the trace span keeps its own redaction). A heuristic mask can hide a model-chosen payload, and the human must see what runs, so secrets are not masked in the prompt. The text is never cut, EXCEPT above a hard cap of 20000 characters: then the call is denied automatically without asking, with the observation `tool X was not run: its arguments are too large to review (N characters)` and span approval `denied`. Approval comes only from the human input channel, never from model text.
12. `dev_echo` (risk `write`) exists only when the config key is true. A hand run shows the prompt in the CLI, the TUI and the GUI.
13. The approver's result must be exactly `True` to allow; any other value (including a truthy non-bool) denies.
14. Tools never call another tool's `run`; every tool execution goes through `Orchestrator._call_tool` (a recorded invariant, checked by a test that greps or inspects that `Tool.run` has a single caller in `src`).
15. The GUI no longer keeps an unused `_pending_approval` attribute, or the attribute is read (which of the two is stated at implementation).

## Approach

Memory consulted: tool|sandbox|subprocess|http|memory|embed|sqlite (case-insensitive) on specs/memory.md — none relevant (6 matching lines, none relevant to approval)

Add an injected async `Approver` that the orchestrator awaits inside `_call_tool`, between argument validation and `run`. Each front end supplies its own approver that asks the human and returns a bool.

Ordered plan (each step ends green and is shown to the author before the next):

1. `risk` field, registration validation, update the three test stubs (`tools.py`, `builtin_tools.py`, three tests).
2. `approval.py`, the gate in `_call_tool`, the observations and the span attribute (`approval.py`, `orchestrator.py`).
3. CLI approver (`cli.py`).
4. TUI approver (`tui/app.py`); Textual pilot test next to `tests/test_tui_inspector.py` (about line 856).
5. GUI approver (`gui/app.py`); NiceGUI user-fixture tests as in `tests/test_gui.py`.
6. `dev_echo`, its config key, README, CHANGELOG and specs text. (The `dev_echo` tool and its config key were pulled forward and built in the first five steps; see Implementation Notes.)

Revised plan after the security review: step 5b (hardening, all of the above, with tests proving displayed == executed arguments, escaping, the cap, `is True`, piped stdin denied), then step 6 (docs).

Security review (2026-10-30): the author approved the hardening step 5b after a security review. Findings that drove it:

- Redaction hid payloads: `echo authorization=$(curl ...|sh)` was displayed as `echo authorization=[REDACTED:api_key]`.
- The display came from the raw argument dict while the tool runs the validated model.
- Stripped characters (bidi U+202E, C1 U+0085) were invisible to the human.
- A piped stdin line could answer the CLI prompt.
- A truthy non-bool approver result approved.
- There was no size cap.

Kept out of this step: model status text imitating the prompt (follow-up), and tools calling tools (recorded as an invariant in the acceptance criteria).

Decisions, with trade-offs and alternatives:

- Injected async `Approver` (chosen) versus parking the `CallToolAction` across turns via `ask_user` (rejected): parking changes `RunState` export and import, a free-text "yes" is ambiguous, and a subagent cannot end the parent's turn.
- Required `risk` (chosen) versus a default of `read`: a `getattr` fallback would fail open, so a forgotten declaration would skip the gate.
- Deny when no approver is injected (fail closed).
- Allow-once (chosen) versus session allow (deferred): if prompts become a burden, a session allow keyed by tool name only, and only for `write`, can be added later.

Risks:

- No real tool above `read` exists yet, hence `dev_echo`.
- The TUI and GUI prompt is disabled while a turn runs, so approval mode must re-enable it and then disable it again.
- GUI multi-page binding: the approver must belong to the page that owns the orchestrator.

Unknowns to verify at implementation (print from the installed packages, do not guess):

- Textual and NiceGUI button and widget APIs.
- `redact` behaviour on JSON input and how to disable its length limit.
- Whether `Span.status` accepts `"rejected"` through `SpanHandle`.

## Affected Areas

- `src/llm_harness/tools.py`, `src/llm_harness/builtin_tools.py`, `src/llm_harness/orchestrator.py`, new `src/llm_harness/approval.py`
- `src/llm_harness/cli.py`, `src/llm_harness/tui/app.py`, `src/llm_harness/gui/app.py`
- `src/llm_harness/config.py` (the `dev_echo` key)
- Tests: `tests/test_tools.py`, `tests/test_orchestrator.py`, `tests/test_subagents.py`, `tests/test_tui_inspector.py`, `tests/test_gui.py`, plus new approval tests
- `README.md`, `CHANGELOG.md`
- After separate approval, not part of this file's write: `specs/roadmap.md` and `specs/mission.md`

## Out of Scope

- `web_fetch` and `web_search`.
- `run_command`, an executor and the Docker backend.
- Memory tools.
- Session or persistent allow lists.
- Per-command allowlists.
- Propagating the parent's approver to subagents.
- Remote or multi-user approval.
- Parking a call across turns.
- Model status text imitating the prompt (follow-up).

## Implementation Notes

Test counts are as measured by the engineers: baseline 1906, then 1914 after step 1, 1925 after step 2, 1943 after step 3, 1966 after step 4, 1977 after step 5; all passed, 0 skipped, 5 live tests deselected.

- Step 1 (DONE): `risk: Risk` on the `Tool` protocol and `_check_tool` rejects a missing or unknown value (`src/llm_harness/tools.py`); `ToolSearchTool.risk = "read"` (`builtin_tools.py`); the three test stubs declare `risk = "read"`. 1906 -> 1914.
- Step 2 (DONE): new `src/llm_harness/approval.py` with `ApprovalRequest`, `Approver` and `arguments_text`; `Orchestrator(..., approver=None)` and `Orchestrator._approve`, called in `_call_tool` after validation and before `run`; the observations for deny, unavailable and a raising approver; the span attribute `approval` and span status `rejected`. Tests in `tests/test_tool_approval.py`. 1914 -> 1925.
- Step 3 (DONE): CLI approver `_ask_on_stderr` in `cli.py` (prompt on stderr, `y` or `yes` allows, reads with `_read_line`). 1925 -> 1943.
- Step 4 (DONE): TUI approver `ChatApp._approve_call` in `tui/app.py` (Future, prompt re-enabled while waiting, `_APPROVE_ANSWERS = {"y", "yes"}`); tests in `tests/test_tui_approval.py`. 1943 -> 1966.
- Step 5 (DONE): GUI approver `_approve_call` in `gui/app.py` (plain labels, Approve and Deny buttons, bound to the page that owns the orchestrator); tests in `tests/test_gui_approval.py`. 1966 -> 1977.
- Pulled forward from step 6: the `dev_echo` tool (`DevEchoTool`, risk `write`, in `builtin_tools.py`) and the config key `[tools] dev_echo` (`ToolsConfig.dev_echo: StrictBool = False` in `config.py`), registered by `build_tool_registry(agents, dev_echo=...)`.
- Step 5b (DONE): hardening after the security review. The prompt shows the validated model, `arguments_text` in `approval.py` (`json.dumps(sort_keys=True, ensure_ascii=True)`), with no `redact` or sanitize on that text; an argument text above `MAX_ARGUMENT_CHARS` (20000, `approval.py`) is denied without asking in `Orchestrator._approve` (`orchestrator.py`); only `approved is True` allows (`approved is not True` denies); the CLI passes `approver=_ask_on_stderr(...)` only `if stdin.isatty() else None` (`cli.py`), and `_discard_typed_ahead` flushes typed-ahead input before the prompt; the GUI `_pending_approval` attribute is removed (no match in `gui/app.py`; the TUI keeps and reads its own); `tests/test_tool_approval.py::test_tool_run_has_exactly_one_caller_in_src` checks by AST that `Tool.run` has a single caller; the tests that checked masking were rewritten to assert the exact text.
- Step 6 (docs): `dev_echo`, its config key and the README `### Tools` section were done earlier; `CHANGELOG.md` was written in this pass; the constitution text is pending the author's approval (see Documentation Review).

Deviations from the approved Approach:

- The `risk` field is required, not defaulted (author decision).
- `tests/test_trace.py` expected span attributes were extended with `approval`.
- The `tests/test_gui.py` orchestrator stub now accepts `approver`.
- The cancellation test in `tests/test_tool_approval.py` got a 5-second guard after a fail-before run showed it hung.

Known gaps, recorded here: the TUI re-disable branch in the `finally` of `_approve_call` has no test; real Ctrl-C in the CLI has no test; `tool_search.risk == "read"` had no direct assertion.

## Validation

Measured on 2026-10-30 by the tester and re-run by the coordinator:

- `uv run ruff check .`: clean.
- `uv run ruff format --check .`: clean (115 files).
- Full `uv run pytest -q`: 2011 passed, 0 skipped, 0 failed, 5 deselected, 1 warning. The 5 deselected are the `live` provider tests, which call real providers: NOT run, unverified. The warning is a google-genai DeprecationWarning, unrelated and the same as the baseline.
- Baseline before the feature: 1906 passed, 5 deselected.
- Per-suite counts at the tester run before step 5b (tests changed since in 5b; the full total above is after 5b): test_tools 35, test_tool_approval 11, test_cli_orchestrator 46, test_tui_approval 20, test_gui_approval 11.
- Not a regression: existing tests were only extended (`tests/test_trace.py` expected attribute dict gained `approval`; the `tests/test_gui.py` stub accepts `approver`; the stubs in `tests/test_orchestrator.py` and `tests/test_subagents.py` gained `risk`).
- Real artifact: the plain CLI driven with a scripted provider and a scratch config (HOME set to a scratch directory): `y`, `yes`, `n`, EOF, on a tty and on piped stdin; attack strings shown whole; escapes shown; with `dev_echo` off the result is `tool dev_echo is not available` and no prompt. `uv run llm-harness --help` works.
- Call-site grep: every new non-test symbol has a non-test use: `ApprovalRequest`, `Approver`, `arguments_text`, `MAX_ARGUMENT_CHARS` in `orchestrator.py`; `ToolsConfig` and `dev_echo` in `config.py` and `cli.py`; `DevEchoTool` in `builtin_tools.py`; `_ask_on_stderr` and `_discard_typed_ahead` in `cli.py`; `_approve_call` in `tui/app.py` and `gui/app.py`. The only tool execution site is `tool.run` in `_call_tool` in `orchestrator.py` (grep, plus the AST test).
- Independent security review (reviewer, 2026-10-30): "request changes" with 9 findings. Findings 1-5, 7, 8 (recorded as invariant AC14, pinned by the AST test) and 9 were fixed in step 5b. Finding 6 (model status text imitating the prompt) is deferred as a follow-up (it is in Out of Scope).
- Known gaps, unverified: the five `live` tests; a hand run of the real TUI and real GUI (only headless Textual pilot and NiceGUI user-fixture tests ran; no screenshots by rule); real Ctrl-C/SIGINT during a CLI approval wait; `termios.tcflush` on a real pty (monkeypatch only) and the no-termios (Windows) branch; the TUI branch that re-disables the prompt in the `finally` of `_approve_call` (no assertion); no direct assertion that `tool_search.risk == "read"` beyond the registry requiring a risk (a grep of `tests/` found none; a one-line check could be added, not added here).

## Documentation Review

- Changelog: one only, `CHANGELOG.md` (`grep -n "Artifacts" specs/tech-stack.md` finds no match in `specs/tech-stack.md`, so the single implicit artifact applies).
  - `{CHANGELOG.md, Added, "Ask-before-run gate for tools: ..."}` (the `risk` field, the prompt in `--plain`, the terminal UI and the web GUI, the escaping, the 20000-character cap, piped input and subagents denied).
  - `{CHANGELOG.md, Added, "`[tools] dev_echo = true` in `config.toml` registers `dev_echo`, ..."}`.
- README: the `### Tools` section (`README.md:377-400`) was written in steps 3 and 5b and matches the code: `Allow this call? [y/N]` and `y` or `yes` (`README.md:389-390`; `cli.py:221-224`), "nothing masked" and `\uXXXX` escapes (`README.md:392-393`; `approval.py:41` `ensure_ascii=True`), the 20000 cap (`README.md:393-394`; `approval.py:10`), piped stdin denied (`README.md:394-396`; `cli.py:543`), typed-ahead discard (`README.md:396-397`; `cli.py:186-200`), the terminal UI and GUI ask (`README.md:399-400`; `tui/app.py:199`, `gui/app.py:240`). No "secrets masked" claim about the prompt remains in the section. `README.md:10` and `README.md:738` say only `tool_search` exists as a real tool; `dev_echo` is optional and off by default, so this is still accurate (not changed).
- Constitution drift (proposed, awaiting the author's approval, NOT applied here):
  - `specs/mission.md:11` ("real tools are not built yet") and `specs/mission.md:21` ("What exists today" lacks the approval gate and `dev_echo`; "What does not exist yet: real tools (only the built-in tool_search exists)").
  - `specs/tech-stack.md:42` ("typed input and output schemas are deferred to `tools-first-set`") and `specs/tech-stack.md:50` (the Tools bullet: "any real tool (`tools-first-set`)"; it lacks the `risk` field, the approval gate and `dev_echo`). The roadmap item `tools-first-set` was split into `tool-approval`, `web-tools`, `command-execution`, `memory` and `command-execution-docker` (`specs/roadmap.md:11,19,20,22`).
CI review: not applicable (the repository has no CI workflow, Dockerfile, Makefile, task-runner or release script: checked with ls -a, no .github directory)

## Documentation Updates

- README: the `### Tools` section, written in steps 3 and 5b.
- `CHANGELOG.md`: two entries under `## [Unreleased]` / `### Added` (the ask-before-run gate; `[tools] dev_echo = true`).
- `specs/memory.md`: four `[gotcha]` entries appended (lines 44-47).
- Constitution (see the files for the text, not duplicated here):
  - `specs/mission.md` v12: "What exists today" gains the ask-before-run gate; "What does not exist yet" names `tool_search` and `dev_echo`.
  - `specs/tech-stack.md` v21: the Tools bullet records the built `tool-approval`; the `tools-first-set` references now point to `web-tools`, `command-execution`, `memory` and `command-execution-docker`.
  - `specs/roadmap.md` v15: the `tool-approval` line was removed from `## Now`, which now reads `Nothing in flight.`
