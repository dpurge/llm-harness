---
title: "Tool registry: typed tools, tag matching, tool_search"
kind: feature
status: done
version: 1
updated: 2026-10-29
branch: main
---

## Problem / Motivation

Umbrella plan: `specs/features/environment-and-identity.md` (done), item `tool-registry` and contracts B4, B5. `agent-registry` (done) gave agents with `tags`, `tools` and `deny`.

Today `ToolRegistry` (`src/llm_harness/tools.py`) only maps a name to an object with `run(arguments: dict, ctx)`. Nothing registers a tool in production: `Orchestrator` defaults to an empty registry (`orchestrator.py:129`), and the system prompt says "No tools exist yet: do not use it" (`prompts/orchestrator.md:59`). A tool has no description, tags or argument schema, `agents.yaml` `tools` and `deny` names are not checked against anything, any registered tool can be called by anyone, and the model cannot find out what tools exist.

This feature makes tools typed and discoverable while keeping the model's context small (the author's requirement: only relevant tool definitions enter the context): the orchestrator sees each tool as a name plus a one-line description and asks `tool_search` for a tool's argument schema before calling it.

Decisions confirmed by the author on 2026-10-29: core scope now (grant/revoke on `delegate`, `[tools] grantable` and vector fusion come later); the context lists name and one-line description, with `tool_search` returning the schemas; typed input only, the result stays a string observation. The author also asked that nothing be committed by the assistant.

## Acceptance Criteria

1. A tool has `name` (lowercase letters, digits and underscores, starting with a letter), `description` (one non-empty line), `tags` (lowercase, no spaces, no repeats), `input_model` (a pydantic model) and `run(arguments, ctx) -> str`, where `arguments` is the validated `input_model` instance. A tool that breaks these rules is refused at registration with a message naming the tool; a duplicate name is refused as today.
2. `ToolRegistry.tools_for(agent)` returns, sorted by name, every tool whose tags intersect the agent's `tags`, plus the tools named in the agent's `tools`, minus those named in `deny` (deny wins). No model call is involved. Tested with tags only, `tools` only, `deny` over both, and an agent with nothing.
3. `ToolRegistry.search(query, k, names)` ranks the tools in `names` lexically over name (split on underscores), tags and description: a query word found in the name scores 3, in a tag 2, in the description 1, summed over distinct query words; tools with score 0 are left out; ties break by name; `k` is capped. In memory, no new dependency, deterministic.
4. The tool `tool_search` (`query` 1 to 200 characters, `k` default 5, 1 to 10) returns, for the caller's tools only, one block per match with name, description, tags, an estimated token count of its schema, and its input JSON schema generated from `input_model`. No match gives a line saying so. It carries the tag `orchestrator`, so the orchestrator receives it by criterion 2.
5. `build_tool_registry(agents)` registers the built-in tools (only `tool_search` now) and raises `ConfigError` naming the agent and the tool when an agent's `tools` or `deny` names a tool that does not exist. The three entry points use it once at startup: `cli.py` (plain mode and the arguments handed to the GUI and TUI runners), `gui/app.py` and `tui/app.py`.
6. `ToolContext` gains `allowed_tools`, the set of names the calling agent may use; the orchestrator computes it from the registry and its agent each turn.
7. `call_tool` outside the caller's set, or with a name that does not exist, is not run: the observation says `tool <name> is not available; available tools: <sorted names>` (or `none`). Arguments are validated against `input_model` first; a failure is an observation `invalid arguments for tool <name>: <field>: <message>; ...` that never echoes the offending values. Both go back to the model as a normal observation, like today's unknown-tool message, and the trace span for the call is marked `error`.
8. Every model request lists the orchestrator's tools in a `## Tools` section of the user message, one line `name: description` per tool (`(none)` when empty), built by the single context builder `build_request`. The system prompt tells the model to call `tool_search` for a tool's schema before `call_tool`, and no longer says that no tools exist. The context inspector preview shows the same section because it uses `build_request`.
9. A token estimate (characters divided by 4, rounded up; an estimate, not a tokenizer count) is computed for the tool listing and for the full schemas of the same tools. The State tab of the GUI and the terminal UI has a `Tools` section: per tool name, tags and description, plus the two totals, so the saving of listing versus sending every schema is visible. Read-only, full width, large text, plain text only.
10. README (config section and tools), CHANGELOG, `specs/mission.md` ("real tools (only an empty registry)") and `specs/tech-stack.md` (built versus planned) are updated; the full suite and ruff pass; every skip is explained. Existing tests with stub tools are updated to the typed form; no test touches the real config directory.

Sample of the new section in the model's request (the heading is `## Tools`; shown here without the hashes):

```
Tools:

tool_search: Find the tools you may use by words and get their argument schemas.
```

Sample `tool_search` result for `query: "search"` when a `web_search` tool exists:

```
web_search - Search the web for pages about a query. tags: web, research. schema ~38 tokens.
{"properties": {"query": {"type": "string"}}, "required": ["query"], "type": "object"}
```

## Approach

Memory consulted: tool|Tool — none relevant (0 entries match in `memory.md`).

Keep `tools.py` for the protocol and registry and add `src/llm_harness/builtin_tools.py` for `tool_search` and `build_tool_registry` (assumption: the second module keeps the registry free of tool code). The registry validates registration and computes sets; the orchestrator validates arguments and enforces the set, because it already turns every tool failure into an observation (`_call_tool`, `orchestrator.py:314-334`). `build_request` (`context.py:72`) gets the tool lines as a parameter, so the orchestrator and the inspector preview (`inspect_state.build_preview`) pass the same list.

Ordered plan (each step ends green and is shown to you before the next):

1. Typed `Tool`, registration rules, `tools_for`, `search`, token estimate in `tools.py`, with tests (criteria 1 to 3, 9 estimate).
2. `ToolContext.allowed_tools`, `tool_search`, `build_tool_registry` with the agent name check, with tests (criteria 4 to 6).
3. Orchestrator: set enforcement and argument validation; update the stub tools in the existing tests (criterion 7).
4. Context: `## Tools` section and the system prompt text, passed by the orchestrator and the preview (criterion 8).
5. Wiring: `cli.py`, `gui/app.py`, `tui/app.py` build the registry once and pass it on (criterion 5 entry points).
6. Inspector `Tools` section in both front ends (criterion 9).
7. Docs and closeout (criterion 10).

Risks: step 5 changes the `run_gui` and `run_tui` signatures that tests fake; the changes stay one extra argument, and if it needs more I stop and report. The one-line listing may not be enough for a small model to choose a tool; that is a quality question for `tools-first-set` and is not measured here.

Testing strategy: offline unit tests with fake tools; no model calls; all fast.

## Affected Areas

- `src/llm_harness/tools.py`, new `src/llm_harness/builtin_tools.py`
- `src/llm_harness/orchestrator.py`, `src/llm_harness/context.py`, `src/llm_harness/prompts/orchestrator.md`
- `src/llm_harness/cli.py`, `src/llm_harness/gui/app.py`, `src/llm_harness/tui/app.py`
- `src/llm_harness/inspect_state.py`, `src/llm_harness/gui/inspector.py`, `src/llm_harness/tui/state_tab.py`
- `tests/test_tools.py`, new `tests/test_builtin_tools.py`, `tests/test_orchestrator.py`, `tests/test_context.py`, `tests/test_inspect_state.py`, `tests/test_gui_inspector.py`, `tests/test_tui_inspector.py`, `tests/test_cli.py`
- `README.md`, `CHANGELOG.md`, `specs/mission.md`, `specs/tech-stack.md`, `specs/roadmap.md`

## Out of Scope

- `delegate` with `tools: {grant, revoke}` and the `[tools] grantable` allowlist (move to `subagents`).
- Vector fusion in `search` and the `tools` vector collection (a later small slice, after `embeddings` has a first consumer).
- Typed output models (added with the first tool that needs one).
- Real tools such as web search or RAG, and a tool declaring storage namespaces (`tools-first-set`).
- Running subagents with their own tool sets (`subagents`); `tools_for` is computed for any agent but only the orchestrator uses it.
- Reading tools from configuration files; tools are code.

## Implementation Notes

Baseline before any change: 1748 passed, 5 deselected (opt-in live tests), ruff clean.

Step 1 (done, 2026-10-29): `src/llm_harness/tools.py` now holds the typed `Tool` protocol (`name`, `description`, `tags`, `input_model`, `run(arguments, ctx)`), registration rules (`_check_tool`: name pattern, one-line description, tag rules, pydantic `input_model`; an `input_model` that is not a pydantic class raises `TypeError`, the rest `ValueError`, all naming the tool), `ToolRegistry.tools_for(agent)`, `ToolRegistry.search(query, k, names)` (weights 3/2/1; a query word counts once, at the highest weight of where it is found; zero scores left out; ties by name; `k` clamped to 0..`MAX_SEARCH_K` = 10), `estimate_tokens` and `schema_text`. `tests/test_tools.py` rewritten (26 tests) and the `StubTool` in `tests/test_orchestrator.py` got the new attributes. `AgentConfig` already forbids a name in both `tools` and `deny`, so "deny wins" is only testable over a matching tag. After: 1772 passed, 5 deselected, ruff clean. None of the new functions has a non-test call site yet by design: `tool_search` and `build_tool_registry` (step 2), the orchestrator (steps 3, 4), the front ends (step 5) and the inspector (step 6) use them.

Step 2 (done, 2026-10-29): new `src/llm_harness/builtin_tools.py` with `ToolSearchInput` (`query` 1 to 200 characters, `k` default 5, 1 to 10), `ToolSearchTool` (tag `orchestrator`; one block per match `name - description tags: ... schema ~N tokens.` plus the compact schema, blocks separated by a blank line; `no tool matches '<query>'` when nothing matches or nothing is allowed; only the caller's `allowed_tools`) and `build_tool_registry(agents)` (registers `tool_search`; an unknown name in an agent's `tools` or `deny` raises `ConfigError` `invalid agents.yaml: agent '<id>': <field> names unknown tool '<name>'; known tools: ...`). `ToolContext.allowed_tools` (default empty) is filled by the orchestrator from `tools_for(agent)`, or with every registered tool when it has no agent (tests only). 15 tests added (`tests/test_builtin_tools.py`, two in `tests/test_orchestrator.py`). After: 1787 passed, 5 deselected, ruff clean. `build_tool_registry` and `ToolSearchTool` still have no non-test caller (step 5 wires the front ends); `allowed_tools` is read by `ToolSearchTool.run`.

Step 3 (done, 2026-10-29): `Orchestrator._call_tool` (`src/llm_harness/orchestrator.py`) now computes the caller's allowed names once, refuses a tool that is unknown or outside them with `tool <name> is not available; available tools: <sorted allowed names or none>`, validates `action.arguments` against the tool's `input_model` and on failure answers `invalid arguments for tool <name>: <field>: <message>; ...` (`_argument_problems` uses only location and message, never the input), then runs the tool with the validated model. Both refusals set the trace span status to `error`. This replaces the old `unknown tool <name>; available tools: ...` text, so the expectations in `tests/test_orchestrator.py`, `tests/test_trace.py`, `tests/test_gui_inspector.py` and `tests/test_tui_inspector.py` were updated. 5 tests added in `tests/test_orchestrator.py` (outside the agent's set, agent with no tools, valid arguments reach the tool as the model, invalid arguments never echo a secret, refused call marks the span). Stub tools now record `arguments.model_dump()`. After: 1792 passed, 5 deselected, ruff clean. Call site: `_call_tool` is the production path of every `call_tool` action.

Step 4 (done, 2026-10-29): `build_request` (`src/llm_harness/context.py`) takes `tools` and puts a `## Tools` section between `## State` and `## Observation`: one `name: description` line per tool in name order, `(none)` when empty (`tool_lines`); schemas never enter the request. `ToolRegistry.all()` and `Orchestrator.available_tools()` (the agent's set via `tools_for`, or every tool without an agent) feed both `Orchestrator.send` and `inspect_state.build_preview`, so the preview shows the same section. `prompts/orchestrator.md` no longer says no tools exist: it points to the `## Tools` list, tells the model to call `tool_search` (`{"query": "<words>"}`, optional `k`) before a tool to get its schema, and says unlisted tools are refused and bad arguments rejected with the reason. The prompt states `tool_search`'s own arguments because the model cannot look up the schema of the tool that gives schemas. Wording note: the prompt avoids the word "first", because a TUI test uses it as a canary message. 6 tests added (context 4, orchestrator 1, preview 1); the GUI hostile-status double gained `available_tools`; one context test now expects the `## Tools` heading. After: 1798 passed, 5 deselected, ruff clean. Call sites: `orchestrator.py` (`send` and `_allowed_tool_names`) and `inspect_state.py` (preview).

Step 5 (done, 2026-10-29): `cli.main` builds the registry once after the config loads (`build_tool_registry(config.agents)`; a `ConfigError` prints `error: ...` and exits 1 before any model call) and passes it as `tools=` to the plain-mode `Orchestrator` and to the GUI and TUI runners. `TuiRunner` (and so the GUI runner), `_run_tui_lazily`, `_run_gui_lazily`, `run_tui`, `run_gui`, `register_page`, `ChatPage` and `ChatApp` gained a `tools` keyword; both front ends hand it to each `Orchestrator` they create, so one registry serves every new chat. The suite caught that the two `_run_*_lazily` defaults did not yet accept `tools` (the fake runners in tests hid it); fixed, with a test for each default runner. 6 tests added (runners receive one registry with `tool_search`, plain requests list `tool_search`, unknown tool name in `agents.yaml` stops startup naming agent and tool, both lazy runners forward the registry); `FakeTuiRunner` and the hostile-status double accept `tools`. Smoke run against a scratch HOME: `tools: [nope]` printed `error: invalid agents.yaml: agent 'orchestrator': tools names unknown tool 'nope'; known tools: tool_search` and exited 1; `tools: [tool_search]` started and exited 0. After: 1802 passed, 5 deselected, ruff clean. Call sites (`grep -n build_tool_registry src`): `cli.py` (`main`).

Step 6 (done, 2026-10-29): `tool_text` and `tools_text` in `src/llm_harness/inspect_state.py` (every string through `sanitize_text`): per tool name, tags, description and its schema estimate, then two totals, `listing sent in every request: ~N tokens` (the exact `tool_lines` text the request carries) and `full schemas of these tools would be: ~M tokens` (estimates, 4 characters per token); `(none)` without tools. The State tab of both front ends has a `Tools` section (open by default) refreshed from `orchestrator.available_tools()` with every refresh: `gui/inspector.py` (`state-tools` label) and `tui/state_tab.py` (`state-tools` Static, markup off). 4 tests added (pure text with numbers computed independently: 29 + 13 schema tokens, 8 listing tokens; empty case; GUI; TUI). After: 1806 passed, 5 deselected, ruff clean. Call sites (`grep -n tools_text src`): `gui/inspector.py`, `tui/state_tab.py`.

## Validation

Final run 2026-10-29: `uv run pytest -q` gave 1806 passed, 0 failed, 0 skipped, 5 deselected (the opt-in `live` tests, excluded by `addopts = -m "not live"` in `pyproject.toml`; they call real providers). Baseline before the work: 1748 passed, 5 deselected. No regression: every earlier failure came from tests that expected the old `unknown tool` text, the old stub or runner signatures, or the old request layout, and was fixed in the step that changed them. `uv run ruff check` clean; `uv run ruff format --check`: 106 files already formatted. Real artifact: against a scratch HOME, `tools: [nope]` in `agents.yaml` printed `error: invalid agents.yaml: agent 'orchestrator': tools names unknown tool 'nope'; known tools: tool_search` and exited 1; `tools: [tool_search]` started and exited 0. The real defaults of both runners were exercised by tests with a monkeypatched `run_gui` and `run_tui`. No real model was called with the new prompt, and the GUI and TUI were not opened by hand; whether a small model picks the right tool from the one-line listing is not measured (see Out of Scope and `tools-first-set`). Call-site grep (`grep -rn` over `src`): `build_tool_registry` at `cli.py:418`; `available_tools` at `orchestrator.py:228` and `:338`, `inspect_state.py:120`, `gui/inspector.py:269`, `tui/state_tab.py:100`; `tool_lines` at `context.py:89` and `inspect_state.py:94`; `tools_text` at `gui/inspector.py:269` and `tui/state_tab.py:100`; `tool_search` is built by `build_tool_registry`.

## Documentation Review

- `README.md`: drift found (the intro and the Orchestrator section said no tools exist, the agents rules said tool names were not checked, the State tab description and Known limits lacked tools, Troubleshooting lacked the two new observations); fixed.
- `CHANGELOG.md`: user-facing change (tools, `tool_search`, refusals, inspector section); entry needed in the only changelog (`CHANGELOG.md`, the project has no `### Artifacts` table); written.
- `specs/mission.md`: "real tools (only an empty registry)" was out of date; updated (version 10, approved by the author 2026-10-29).
- `specs/tech-stack.md`: the Tools bullet said all of it was planned; updated with built and not-built parts, and the Naming bullet now confirms tool identifiers (version 19, approved 2026-10-29).
- `specs/features/agent-registry.md`: its statement that tool names are not checked is a record of its own time and stays as written.
- `specs/roadmap.md`: `## Now` line removed at closeout.

CI review: not applicable (the repository has no CI workflow, Dockerfile, Makefile, task runner or release script: only `pyproject.toml` and `uv.lock`; checked with `ls .github Makefile`).

## Documentation Updates

- `README.md`: intro, `### Agents` rules, a `Tools:` paragraph in the Orchestrator section, the State tab description, Known limits and two Troubleshooting lines.
- `CHANGELOG.md`: one `Added` entry under `## [Unreleased]`.
- Constitution: `specs/mission.md` (version 10) and `specs/tech-stack.md` (version 19) updated with the author's approval; see those files.
- `specs/roadmap.md`: this feature's `## Now` line removed.
- `memory.md`: nothing appended.
