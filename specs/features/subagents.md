---
title: "Subagents: delegate runs one child agent"
kind: feature
status: done
version: 1
updated: 2026-10-29
branch: main
---

## Problem / Motivation

Umbrella plan: `specs/features/environment-and-identity.md`, item `subagents` and contracts B4, B5. It depends on `agent-registry` and `tool-registry`, both done.

Today the orchestrator can emit `delegate` (a `DelegateAction` with `subagent` and `brief`), but the loop answers it with the observation "delegate is not available yet", and the system prompt tells the model the same. `agents.yaml` can already define more agents than the orchestrator, yet nothing runs them: the `examples` field of an agent is validated and never read. The trace has no subagent span kind, the `llm_calls` table has no agent column, and each model router keeps its own session cost, so a second router would make the shown session total jump.

This feature makes `delegate` real in the smallest useful slice: the orchestrator hands a brief to one child agent, the child works with its own typed state and its own tools, and its result comes back to the parent as an observation. The umbrella's per-item plan names this item `subagents`.

Decisions confirmed by the author on 2026-10-29:

- Slice 1: `delegate` runs ONE child agent from `agents.yaml`. Depth is 1 only. The child's step cap is 5 (the orchestrator stays at 8).
- The child has its own typed state with the goal pre-set from the brief, so the empty-goal rule does not trip. It receives the brief, the parent's environment block and its tool listing from `tools_for(child)`.
- A child `ask_user` ends the delegation and returns `needs: <question>` to the parent. A child `respond` ends it.
- A child error or reaching the step cap becomes an observation for the parent, never a parent error.
- A child `delegate` is refused with `delegate is not available to subagents`.
- The result observation is the child's `respond` text plus its findings as `- text (source: s)` lines, capped by the existing `_truncate`.
- Delegation targets are agents that are not the orchestrator and have non-empty `examples`. They are listed under `## Subagents` in the request as `id: description (e.g. examples)`. An unknown subagent id gives an observation that lists the valid ids.
- An example `researcher` agent is added to `EXAMPLE_AGENTS_YAML`: tags `[research]`, `model_tags` `[chat]`, tools `[tool_search]` (that tool is tagged only `orchestrator`, so it needs the explicit entry) and two examples.
- A new `select_agent_model(config, agent)` in `config.py` picks the top-priority chat model that carries all the agent's `model_tags`, the same pattern as `select_orchestrator_model`. The child router is built with `required_tags=frozenset(agent.model_tags)`.
- `build_request` gains an optional `instructions` argument and a new `prompts/subagent.md` is added.
- A new `subagent` span kind is added; the child's step and llm_call spans nest under it, and the child's final state is stored in a `state` attribute (added to `LONG_FIELDS` in `inspect_state.py`). The front-end inspectors do not change.
- One shared session-cost accumulator serves the parent and child routers, so the shown session total never decreases. It is passed through `make_router_factory`. The ledger schema does not change.
- The wiring lives in a new `src/llm_harness/subagents.py`, used by `cli.py`, `tui/app.py` and `gui/app.py`.
- The prompt line in `prompts/orchestrator.md` is updated. `README.md`, `CHANGELOG.md` and `specs/tech-stack.md` are updated later, in B4 and B5, not now.

## Acceptance Criteria

1. Delegation then respond: with a scripted fake provider, the orchestrator emits `delegate` to an existing subagent, the child runs and responds, the parent receives the result observation and then responds to the user. The result observation is the child's `respond` text followed by its findings as `- text (source: s)` lines.
2. A result longer than the existing cap is cut by `_truncate`; a shorter one is unchanged.
3. A child that emits `delegate` receives the observation `delegate is not available to subagents` and keeps running; no grandchild is created.
4. The child stops after 5 steps. When it has not responded by then, the parent receives an observation saying the subagent reached its step limit, and the parent turn continues without raising. The orchestrator still stops at 8.
5. A child `ask_user` ends the delegation and the parent receives `needs: <question>`. The user is not asked directly by the child.
6. A child provider or tool error becomes an observation for the parent; the parent turn does not fail.
7. An unknown subagent id gives an observation that lists the valid ids, sorted. The orchestrator id is not valid.
8. An agent without `examples` is not offered: it is absent from the `## Subagents` section and a `delegate` to it gets the unknown-id observation.
9. The request carries a `## Subagents` section with one `id: description (e.g. examples)` line per valid agent, built by `build_request`. The context inspector preview shows the same section.
10. The child's tool listing equals `tools_for(child)` and its allowed tool set is the same; a tool outside that set is refused as for the orchestrator.
11. The child's state starts with the goal set from the brief, so the empty-goal rule does not stop the first step, and the child receives the parent's environment block.
12. `select_agent_model(config, agent)` returns the top-priority chat model carrying all of the agent's `model_tags`, and fails with a clear `ConfigError` when none exists. The child router is built with `required_tags=frozenset(agent.model_tags)`.
13. `build_request` accepts an optional `instructions` argument; without it the request is byte-for-byte as before. `prompts/subagent.md` is the child's system prompt.
14. The trace has a `subagent` span kind. The child's step and llm_call spans are nested under that span, and the span carries the child's final state in a `state` attribute, which is in `LONG_FIELDS`.
15. The session cost shown to the user never decreases across a turn that uses parent and child routers: both add to one shared accumulator passed through `make_router_factory`.
16. `EXAMPLE_AGENTS_YAML` loads against the example `models.yaml` without error and contains the `researcher` agent with tags `[research]`, `model_tags` `[chat]`, tools `[tool_search]` and two examples.
17. `prompts/orchestrator.md` no longer says `delegate` is not available and tells the model to use the `## Subagents` list.
18. Every new function and config key has a non-test call site (checked with `grep -rn` over `src`). Tests use scripted fake providers only; no test calls a real model or touches the real config directory. The full suite and ruff pass; every skip is explained.

## Approach

Memory consulted: [build]|[gotcha]|[convention] — 3 relevant entries (`memory.md:17` fixtures must patch functions that production calls through its module; `memory.md:31` and `memory.md:40` test doubles standing in for `Orchestrator` must accept every `Orchestrator.__init__` keyword, including `user_id=` and `storage=`).

The child is a second `Orchestrator` instance with its own typed state, its own router and the subagent prompt. The loop, the tool enforcement and the truncation are reused rather than copied. The parent's `delegate` branch builds the child through `src/llm_harness/subagents.py`, runs it, and turns its end state into one observation. The child never sees the parent's history, only the brief, the parent's environment block and its own tool listing.

Ordered plan (each step is independently testable with scripted fake providers, no real model calls; each ends green and is shown to the author before the next):

1. `select_agent_model` and the `researcher` example (`src/llm_harness/config.py`, `tests/test_config.py`): criteria 12, 16.
2. The `instructions` argument and `prompts/subagent.md` (`src/llm_harness/context.py`, `tests/test_context.py`): criterion 13.
3. The `## Subagents` listing (`src/llm_harness/context.py`, `tests/test_context.py`): criteria 8, 9.
4. The `subagent` span kind and the `state` attribute (`src/llm_harness/trace.py`, `src/llm_harness/inspect_state.py`, `tests/test_trace.py`, `tests/test_inspect_state.py`): criterion 14.
5. The `delegate` branch, the child `Orchestrator`, the refusals and the 5-step cap (`src/llm_harness/orchestrator.py`, new `src/llm_harness/subagents.py`, `tests/test_orchestrator.py`): criteria 1 to 7, 10, 11.
6. The shared session cost (`src/llm_harness/router.py`, `tests/test_router.py`): criterion 15.
7. Wiring in `cli.py`, `tui/app.py` and `gui/app.py`, with `tests/test_cli_orchestrator.py`: criterion 18 call sites.
8. The prompt line and the docs: criterion 17 and the B4, B5 updates.

Deviations from this approved text, found during implementation (2026-10-29; details in Implementation Notes):

- Deviation (2026-10-29): the wiring is not "passed through `make_router_factory`" alone. `make_subagent_factory_maker` in `src/llm_harness/subagents.py` builds, per session, a subagent factory bound to that session's parent router, and the child routers feed the PARENT router's own `SessionCost` (`ModelRouter.costs`). `make_router_factory` only gained a `costs=` keyword.
- Deviation (2026-10-29): step 7c was added (human decision 2026-10-29: the shown session total resets per session; each new chat, model change or GUI client starts at $0 as before, instead of one accumulator for the whole process).
- Deviation (2026-10-29): the keyword of the runners is `subagent_factory_maker` (a maker of per-session factories), not a single factory.

Risks:

- Child text enters the parent context. It is bounded by the `_truncate` cap, and the child only has `tool_search` in the example, so the exposure is small.
- The child may fall back to a `default`-tagged model outside its `model_tags`, because `fallback_chain` also appends `default`-tagged models outside the required tags (`routing.py:35`). Accepted in this slice.
- Cost amplification: up to 8 parent steps times 5 child steps, times retries. There is no per-turn delegation cap in this slice.
- Step 5 risk, unverified: whether `tests/fake_steps.py` supports two providers (parent and child) in one test. If it does not, the step needs a small test helper first; I do not know yet.
- Test doubles for `Orchestrator` and the runners must accept every `Orchestrator.__init__` keyword, including `user_id=` and `storage=`, and fixtures must patch functions through the module production calls them through (memory entries above).

Testing strategy: offline unit tests with scripted fake providers; no model calls; all fast.

## Affected Areas

- `src/llm_harness/config.py`, `src/llm_harness/context.py`, `src/llm_harness/trace.py`, `src/llm_harness/inspect_state.py`
- `src/llm_harness/orchestrator.py`, new `src/llm_harness/subagents.py`, `src/llm_harness/router.py`
- `src/llm_harness/cli.py`, `src/llm_harness/tui/app.py`, `src/llm_harness/gui/app.py`
- `src/llm_harness/prompts/orchestrator.md`, new `src/llm_harness/prompts/subagent.md`
- `tests/test_config.py`, `tests/test_context.py`, `tests/test_trace.py`, `tests/test_inspect_state.py`, `tests/test_orchestrator.py`, `tests/test_router.py`, `tests/test_cli_orchestrator.py`
- `README.md`, `CHANGELOG.md`, `specs/tech-stack.md` (updated later, in B4 and B5)

## Out of Scope

- `delegate` with `tools: {grant, revoke}` and the `[tools] grantable` allowlist.
- Nesting beyond depth 1.
- Real tools for subagents (the example child only has `tool_search`).
- A ledger `agent_id` column or any ledger schema change.
- Per-agent cost reports.
- A child section in the State tab, or any other front-end inspector change.
- Parallel delegation.
- Persisting child state across turns.

## Implementation Notes

All steps are done; counts are the full suite (`uv run pytest`, 5 `live` tests deselected) after each step. Baseline before the feature: 1806 passed.

1. `select_agent_model(config, agent)` and the `researcher` entry of `EXAMPLE_AGENTS_YAML` (`src/llm_harness/config.py`, `tests/test_config.py`). The model is the top-priority non-embedding model carrying all of the agent's `model_tags`; none gives a `ConfigError` that names the agent; a tie at the top raises. Count: 1814.
2. `build_request(..., instructions=None, subagents=())`, `subagent_instructions(agent_id, agent_name, description)` and the prompt `src/llm_harness/prompts/subagent.md` (`src/llm_harness/context.py`, `tests/test_context.py`). Count: 1820.
3. `delegation_targets(agents)` (agents without the `orchestrator` tag that have non-empty `examples`), `subagent_lines`, and the `## Subagents` section between `## Tools` and `## Observation`, only when there is at least one target (`src/llm_harness/context.py`, `tests/test_context.py`). Count: 1829.
4. SpanKind `subagent` (`src/llm_harness/trace.py`), `state` added to `LONG_FIELDS` (`src/llm_harness/inspect_state.py`), `TraceRecorder.adopt` nests the child's spans under the `subagent` span. Tests: `tests/test_trace.py`, `tests/test_inspect_state.py`. Count: 1835.
   - Deviation: `SpanHandle.start_ms` was added (not in the approved text) so `adopt` can shift the child's offsets onto the parent's timeline.
5a. `src/llm_harness/subagents.py` (`SubagentRun`, `SubagentResult`, `result_observation`, `SUBAGENT_MAX_STEPS = 5`, `SUBAGENT_MAX_RETRIES = 2`) and the `Orchestrator` keywords `instructions`, `can_delegate`, `goal`. Count: 1851.
5b. The `delegate` branch `Orchestrator._delegate` with the `subagents` and `subagent_factory` keywords, the refusals, child display-event forwarding and `make_subagent_factory` (`src/llm_harness/orchestrator.py`, `src/llm_harness/subagents.py`, `tests/test_orchestrator.py`, `tests/test_subagents.py`). Count: 1876.
   - Deviation: `_delegate` imports `llm_harness.subagents` locally, because `subagents.py` imports `orchestrator.py` for the child `Orchestrator` (a module cycle otherwise).
   - Deviation: an `Orchestrator.subagents` property was added so the inspector preview reads the same agents the orchestrator uses.
   - Without a factory, `delegate` still answers `not available yet`.
6. `SessionCost`, `ModelRouter.costs` and the `costs=` keywords (`src/llm_harness/router.py`, `tests/test_router.py`). Count: 1882.
7a. Plain front end: `src/llm_harness/cli.py` passes `subagents=config.agents` and a factory (`tests/test_cli_orchestrator.py`, `tests/test_cli.py`). Count: 1890.
7b. Terminal UI and GUI: `src/llm_harness/tui/app.py`, `src/llm_harness/gui/app.py`; the inspector preview uses `orchestrator.subagents` (`tests/test_tui.py`, `tests/test_gui.py`, and the inspector tests). Count: 1897.
7c. Per-session cost (human decision 2026-10-29): `make_subagent_factory_maker(...)(router)` builds the factory per session and the child's router feeds the parent router's `SessionCost`; the runner keyword is `subagent_factory_maker`. Count: 1905.
8. The prompt line in `src/llm_harness/prompts/orchestrator.md` (done by another agent): count, see Validation.

Not built (as planned in Out of Scope): grant/revoke on `delegate` and `[tools] grantable`, nesting beyond depth 1, real tools (`tools-first-set`), a ledger `agent_id` column (the ledger records every child call but cannot say which agent made it), per-agent cost reports, parallel delegation, persisting child state across turns, and a child section in the State tab (child spans are in the Trace tab, collapsed by default).

## Validation

- Full suite before the prompt change: `uv run pytest`: 1905 passed, 0 failed, 0 skipped, 5 deselected; after the prompt change: 1906 passed (the one extra test is `test_the_instructions_explain_delegate_and_no_longer_call_it_unavailable`). The 5 deselected tests are the `live` tests; they read the real config directory and call real providers, so they were not run. Baseline before the feature: 1806 passed, the same 5 deselected; +99 tests.
- `uv run ruff check` and the format check: clean.
- Mutation checks (reported by the engineers, not re-run by the coordinator): step 5a flipping can_delegate and the goal preset failed 4 tests; step 5b three mutations (sorted ids, adopt id remap, dropped yield) each failed tests; step 6 making the shared total private failed 4; step 7a three mutations each failed tests; step 7b three wirings removed failed 4 tests; step 7c two mutations failed 5 tests each.
- Call-site evidence (non-test uses, `grep -n` over `src`; I re-ran the grep and the line numbers below match the current files): `select_agent_model` `subagents.py:178`; `delegation_targets` `context.py:116,139` and `orchestrator.py:390`; `subagent_lines` `context.py:141`; `subagent_instructions` `subagents.py:111`; `make_subagent_factory_maker` `cli.py:455`; `make_subagent_factory` `subagents.py:214`; `SubagentRun` `orchestrator.py:409`; `result_observation` `orchestrator.py:430`; `TraceRecorder.adopt` `orchestrator.py:429`; `SessionCost` / `costs=` `subagents.py:185` and `router.py:347` (and the router default); `can_delegate` `subagents.py:112` and `orchestrator.py:350`; `subagent_factory_maker` `cli.py:77-496`, `tui/app.py`, `gui/app.py`; `instructions=` `subagents.py:111` and `orchestrator.py:271`; `subagents=` `inspect_state.py:124`, `cli.py:495`, `tui/app.py:193`, `gui/app.py:241`, `orchestrator.py:272`; `SpanHandle.start_ms` `trace.py:53` and the span offset in `orchestrator.py:429`.
- Honest gaps: no real-model run of delegation (every test uses scripted fake providers); no real TUI or GUI session was run; wheel packaging of `prompts/` (the new `subagent.md`) was not built or checked; the test-file lists for steps 5a, 7a and 7b in Implementation Notes are inferred, not taken from the engineers.
- Final counts after the prompt change (coordinator re-ran the full suite, 2026-10-29): 1906 passed, 0 failed, 0 skipped, 5 deselected (the `live` tests, not run because they read the real config); ruff check: all checks passed; ruff format --check: 110 files already formatted.

## Documentation Review

- `README.md`: drift found and fixed: the intro and the Orchestrator section said subagents do not exist and `delegate` is not available; the Agents rules said `examples` and `model_tags` are unused; Known limits said "No subagents yet". There was no description of delegation, `## Subagents`, the `subagent` span or the cost behaviour.
- `CHANGELOG.md`: no entry for subagents; entries added under Added and Changed.
- `specs/mission.md`: drift: says subagents and real tools are not built, lists "subagents" under what does not exist, says choosing models for subagents is still to come and delegation is still a goal. Applied (human approved 2026-10-29): see `specs/mission.md`.
- `specs/tech-stack.md`: drift: the Subagents bullet describes a plan; the Agent registry bullet says `model_tags`, `tools`, `deny` and `examples` are unused until `subagents`; the Tools bullet says grant/revoke moves to `subagents` (it is still not built); the naming bullet says subagent identifiers will be confirmed; the Models paragraph still called subagent tags an assumption. Applied (human approved 2026-10-29): see `specs/tech-stack.md`.
- `specs/roadmap.md`: the `## Now` line for this feature is removed (the section now reads `Nothing in flight.`) and the `## Later` list is unchanged. Applied: see `specs/roadmap.md`.
- `specs/memory.md`: no entry is made false by this feature; the three entries cited in Approach (lines 17, 31, 40) still match their text and were followed by the tests. One new `[gotcha]` entry was appended (see Documentation Updates).
CI review: not applicable (no CI, build or release files exist in this repository: no .github, no Makefile, only pyproject.toml and uv.lock, which need no change). Verified with `ls -a` (no `.github`, no `Makefile`) and `find . -name "*.yml"` outside `.venv` (no result).

## Documentation Updates

- `README.md`: intro sentence; Agents section (the `researcher` entry in the example, a rule on `examples` and `model_tags`, the "not used at run time" sentence removed); Orchestrator section (`delegate` sentence); new `## Subagents` section (declaration, context, model choice, limits, result observations, what the user sees, cost); Context inspector (`## Subagents` in the preview, `subagent` span in the Trace tab); Known limits (the "No subagents yet" bullet replaced).
- `CHANGELOG.md`: three lines under `### Added` (delegation to subagents, the `researcher` example, the `subagent` span and preview section) and three under `### Changed` (`examples` is used, `delegate` answers, session total includes subagents).
- `specs/features/subagents.md`: Approach deviations, Implementation Notes, Validation, Documentation Review and this section; `status: documenting`, `updated: 2026-10-29`.
- Constitution files, edited after the human approved the drafts on 2026-10-29: `specs/mission.md` (version 11), `specs/tech-stack.md` (version 20) and `specs/roadmap.md` (version 13, the `## Now` line removed). The text is in those files.
- `specs/memory.md`: one `[gotcha]` entry appended (`SubagentRun` closes its provider, so a factory returns a fresh router per call; a child router feeds the parent router's `SessionCost`).
