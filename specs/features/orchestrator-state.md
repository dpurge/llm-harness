---
title: Orchestrator with typed state and no full chat history
kind: feature
status: done
version: 6
updated: 2026-10-28
branch: main
---

## Problem / Motivation

Approved by the author on 2026-10-28; the field names below are confirmed. The author said to work on `main` and that the assistant makes no commits.

Today `ChatSession` (`src/llm_harness/session.py`) owns the whole chat history and sends it to the model on every turn. This is the temporary exception recorded in the constitution (`specs/mission.md` non-goals, `chat-streaming` decision 2); this feature removes it.

The author's words (2026-10-27), verbatim:

- "I want to be able to use all of them to pick and choose models that are fast and cheap but can do the job. I need configuration for models with tags and priorities, and harness will choose a model for a given task using tags and priority."
- "There will one orchestrator model responsible for reading user prompt, sending work to subagents and assembling the reply. The chat history will not be sent to the model in full."
- "In the context the orchestrator should track the current goal of the user, information it collected so far and the source of that information, and plan for achieving the goal."
- "I plan to write specific tools, and subagents will have in the configuration a list of tools that they have access to, so that we only add to context what is relevant. I want to have good control on the context. The planned tools could be: web search, RAG query, memory access, pre-programmed web scrapers or API calls."
- "Context - the examples where for services that often run batches; this tool will be interactive, so I'd like to see think traces etc."
- "The orchestrator will have to be aware of the current date and time, user name and computer name, with external memory may need to figure out that on another computer it is talking to the same user with a different name to access memories about the user, and it will need to understand what the user wants, ie. what is the goal. The goal may change, but at all times the orchestrator need to track it; if it has no clue, it needs to ask questions to the user. I want to be able to the context in the UI to understand what the orchestrator is thinking at any moment."

Why: the model should work from a small, typed, inspectable state (goal, sourced findings, plan) instead of an ever-growing transcript, which keeps requests small and cheap, gives the author control over context, and makes the context visible in the UI.

Related decisions made the same day (they matter here because models are picked by tag):

- The tag `chat` marks models that handle chat turns; `default` marks the last-resort fallback.
- A failed model backs off 1, 2, 3, 5, 10, 15, 30, 60 minutes.
- Usage and price tracking go into SQLite at `~/.local/share/llm-harness/harness.db`, which will also hold other data.
- A manual model pick overrides backoff.
- Those registry decisions belong to the separate spec `model-registry`, a prerequisite of this feature. Roadmap order: `model-registry`, `orchestrator-state`, `context-inspector`, `environment-and-identity`, `subagents`, `tools-first-set`, `a2ui-nicegui`.

Standing rules from the author that apply: desktop, interactive, single user; the GUI uses the full browser width and viewport height with large text (base at least 18px, secondary at least 15px); no screenshots are used to check layout; the dev install is editable (`uv sync`, `uv run llm-harness`); the author said to work on `main` and the assistant makes no commits.

## Acceptance Criteria

All criteria are testable offline with a scripted step model (a fake provider that replays prepared replies). Names in backticks are proposals.

State
1. A typed `OrchestratorState` (pydantic, frozen copies per step) holds: `goal` (text), `findings` (list; each item has `text` and a non-empty `source`), `plan` (list of steps, each with `text` and `status` one of `todo`, `doing`, `done`, `dropped`), and an `environment` block (`now` as an ISO 8601 timestamp with timezone offset, `user_name`, `computer_name`).
2. The `environment` block is refreshed by the harness, not by the model, at the start of every turn (a test with a fake clock shows two turns with different `now`); the model cannot patch it (protected field).
3. A finding without a source is rejected by validation.
4. After the first turn completes, `goal` is never empty: either the model set it, or the turn ended with an `ask_user` action (criterion 14).
5. When the goal changes, the change is recorded (proposal: an append-only `goal_history` list of `{turn, old, new}`); a test replaces the goal and finds one new record.
6. The serialized state has a size cap (proposal: a configurable character limit, default 8000); a patch that would exceed it is rejected as in criterion 10.

Context builder and requests
7. One component, the context builder (proposal: module `src/llm_harness/context.py`, function `build_request`), is the only code that creates a `ChatRequest` for the orchestrator; a test greps the package and finds no other construction site for the orchestrator path.
8. A request contains exactly: the instructions (data file), the reply-contract schema, the current state (JSON), and the latest observation (the latest user message, or the result of the previous action). Nothing else.
9. Canary test: turn 1 contains a canary string in the user message; on turn 5 the request sent to the model does not contain that string unless it is in the state; the size of the request for turn 20 is bounded by instructions + schema + state cap + observation cap and does not grow with the number of turns (assert on length).

Reply contract and patches
10. The model replies with a JSON object with exactly two keys, `state_patch` (object) and `action` (object). Anything else is rejected. A rejected reply changes nothing; its error text becomes the next observation.
11. Patch rules: a `null` value deletes a key; unknown keys are rejected; protected fields (`environment`, `goal_history`) cannot be patched; append-only fields (`goal_history`) only grow; an overwrite of a whole list that would silently drop items is rejected unless the patch uses an explicit replace form (proposal; see Open questions).
12. At most 2 retries after a rejected reply per step (so at most 3 attempts); after that the turn ends with a visible error event and the state is unchanged.
13. A step cap per turn (proposal: 8 steps) ends a runaway loop with a visible error event.

Actions
14. The `action` is a tagged union on `type`: `call_tool`, `delegate`, `ask_user`, `respond`. This feature implements `ask_user` and `respond`, and `call_tool` against a registered stub tool (a test-only tool in the tests). `delegate` and real tools are DEFERRED to `subagents` and `tools-first-set`: `delegate` is parsed, validated and answered with an observation saying it is not available yet.
15. When the orchestrator cannot tell what the user wants, it uses `ask_user` with a question instead of guessing (a scripted test: ambiguous first message, the model returns an empty goal and an `ask_user`; the question is shown to the user and the next user message is the next observation).
16. `ask_user` suspends the run; the run state (state, step count, pending question) is plain JSON-serializable so it can be stored and resumed (a test serializes, reloads and continues). Storing it to disk is not part of this feature.
17. `respond` carries the final text of the turn; the text is streamed to the UI as `TextDelta` events.

Model choice
18. The orchestrator model is chosen from the model registry by the tag `orchestrator` (proposal) and priority: among models carrying the tag, the highest priority wins; with none tagged, startup fails with a message naming the tag (no silent fallback to another model for this role). The `model-registry` fallback chain applies when the chosen model fails.
19. Prompts and instructions are data files in the package (proposal: `src/llm_harness/prompts/orchestrator.md`), not Python strings, and are included in the wheel.

Front ends
20. Thinking traces and status events (proposal: `StatusEvent` with step number, action type, and a short text) stream to `--plain`, the terminal UI and the GUI alongside the existing `ThinkingDelta`, `TextDelta`, `UsageEvent`, `Done`, `ErrorEvent`; thinking text is display-only and a test shows it never appears in the next request or in the state.
21. The UIs keep showing the whole chat transcript, but only the latest user message plus the state go to the model (the transcript lives in the UI/session for display only).
22. The temporary full-history exception is removed: no code path sends earlier messages to the model, and the constitution and `chat-streaming` notes are updated at documentation time (B5) to say so.
23. `uv run ruff check` and `uv run pytest` pass offline with zero skips; live tests carry the `live` marker.
24. Every new function, type and config key has a non-test use (as in `chat-streaming` criterion 24).

## Approach

Memory consulted: orchestrator|state|context|subagent|A2UI|NiceGUI|esbuild|wheel|sanitize — 3 relevant entries: "NiceGUI 3.18: `ui.run` defaults host to all interfaces (pass the loopback host explicitly)...", "NiceGUI 3.18: Ctrl-C in `ui.run` surfaces as KeyboardInterrupt after the server has stopped...", "NiceGUI 3.18: `ui.html(content, sanitize=<callable>)` applies the callable on the server before innerHTML; only `sanitize=True` triggers browser-side DOMPurify" (4 lines matched; the aiohttp entry is not relevant)

STATUS: APPROVED on 2026-10-28. The author accepted all defaults for the 9 open questions (listed at the end of this section with their resolved values) and chose the tagged-object action shape (`respond`, `ask_user`, `call_tool`, `delegate`) over the plain-string action of the paper below.

Source protocol: arXiv 2608.26263 "SKILL.state: Scalable Long-Horizon Agent Skills" (Badhe, Tiwari, Chung). We take from it: the model input is the instructions (P), the state and the latest observation; the output is exactly `state_patch` plus `action`; the patch is merged into the state with null-deletion; an invalid patch is rolled back and retried; reasoning text is discarded. Our two deliberate extensions: the tagged-object action (instead of a plain string) and the `$append`/`$replace` list forms (instead of whole-list overwrite).

Reply contract: the model sends one JSON object with two keys, `state_patch` and `action` (the constitution fixes the two keys). `state_patch` follows JSON Merge Patch style semantics (an object merged into the state, `null` deletes a key); lists are replaced as a whole only through an explicit form (proposal `{"$replace": [...]}`), otherwise new items are added through an append form (proposal `{"$append": [...]}`), which avoids the patch-overwrite error where a model resends a short list and loses items. Validation is by pydantic models for the state and the action union; unknown keys are rejected everywhere.

State schema (field names confirmed by the author on 2026-10-28):

- `goal: str` (the user's current goal; empty only before the first turn finishes)
- `goal_history: list[{turn, old, new}]` (append-only, harness-written)
- `findings: list[{text, source}]` (`source` is a free-text origin such as a URL, a tool name plus query, or `user`)
- `plan: list[{text, status}]` with `status` in `todo|doing|done|dropped`
- `environment: {now, user_name, computer_name}` (harness-written, protected)
- `turn: int` and `step: int` (harness-written counters)

The ready-made environment values (date and time, user name, computer name) are only filled in as a simple block here; how identity works across computers is owned by `environment-and-identity`.

Context builder: `build_request(state, observation, model_cfg) -> ChatRequest` assembles instructions + schema + state + latest observation into messages (one system message, one user message holding the state and observation as JSON). It is a pure function, so a context inspector can call it to show exactly what would be sent next.

Step loop (per user turn): refresh `environment`; observation = user message; loop up to the step cap: build request, stream the reply (thinking and status events go to the UI), parse and validate; on a rejected reply retry up to 2 times with the error text as the observation; apply the patch; execute the action: `respond` ends the turn, `ask_user` suspends and returns the question as the turn's reply, `call_tool` runs a registered tool and its result becomes the next observation, `delegate` returns the "not available yet" observation. Ideas adopted from a reference design that was compared (verified there, adopted here as ideas, not as binding decisions): a delegated subagent runs as a child with its own state and limits, nested at most 2 deep, with token use counted against the parent's remaining budget, and its final state or failure returned to the parent as the next observation; a subagent is offered to the orchestrator only if it declares `intents` (a description plus example requests); every step has an `on_error` fallback state; allowed tools and next states are declared per state. These belong to `subagents` and are listed only so they are not lost.

Relation to `ChatSession` and the front ends: `ChatSession` stops owning and sending history. Proposal: introduce `Orchestrator` (module `src/llm_harness/orchestrator.py`) with the same `send(text) -> AsyncIterator[StreamEvent]` surface so the three front ends (`--plain`, terminal UI, GUI) change little; `ChatSession` is either replaced by it or reduced to a display transcript holder. The existing events are kept and one status event type is added. `resolve_think` and `ModelConfig` are reused. The model selector in the UIs is reconsidered: with an orchestrator the selector picks the orchestrator model (default by the tag `orchestrator`, `--model <id>` still overrides it).

Model choice: the harness filters `models.yaml` entries by tag and takes the highest priority (the existing rule in the constitution); the tag `orchestrator` is a PROPOSAL (the `chat` tag marks models for chat turns and `default` the last-resort fallback). The `model-registry` fallback and the manual-pick-overrides-backoff rule are used as that spec defines them; this spec does not duplicate them.

Prompts as data: instructions live in a markdown file in the package and are loaded at start; the schema section is generated from the pydantic models so the contract and the validator cannot drift.

Testing strategy: a scripted step model (a fake provider whose replies are the JSON objects of each step) drives the loop offline: valid patch, rejected patch with retry, retries exhausted, step cap, ask-user suspend and resume, canary absence of history, bounded request size, protected fields, fake clock. Tests are offline and fast; one optional live test (marker `live`) with a small local model checks that it follows the contract.

Risks:
- Small local models may not follow the JSON contract reliably. Mitigation: the bounded retries; use the `model-registry` fallback to a stronger model carrying the same tag; keep the contract minimal.
- State size growth. Mitigation: the size cap and rejection; later a summarising step (not in this feature).
- Patch-overwrite errors (a model resends a partial list and drops items). Mitigation: append/replace forms (criterion 11).
- A model puts reasoning text into the state. Mitigation: thinking is display-only and never parsed.
- Losing conversational nuance because the history is not sent. Mitigation: the state carries what matters; the author accepted this trade-off ("The chat history will not be sent to the model in full").

RESOLVED QUESTIONS (all 9 resolved by the author on 2026-10-28 as the stated defaults):
1. State field names `goal`, `goal_history`, `findings`, `plan`, `environment`, `turn`, `step` and action tags `call_tool`, `delegate`, `ask_user`, `respond`: used as proposed.
2. Orchestrator model tag: `orchestrator`.
3. The previous assistant reply is not part of the latest observation: only the latest user message plus state.
4. Patch list semantics: `$append` and `$replace` forms as proposed.
5. Step cap 8, retry cap 2, state size cap 8000 characters.
6. The orchestrator is the only chat path; tests use the scripted model.
7. The model selector in the UIs remains and selects the orchestrator model.
8. No persistence of the transcript in this feature (the suspended run state is serializable, but no store).
9. A minimal `StatusEvent` is added now.

Unverified:
- That small local models (for example `gemma4:12b`) can follow the two-key JSON contract reliably (needs a live probe).
- Whether any provider adapter offers a JSON-mode or structured-output switch that helps; not used in this draft.
- The exact way the three front ends are refactored (not yet designed in detail).

Assumptions:
- Python 3.12 and the pydantic-based typing already used in the repository.
- The step model is the same provider interface (`ChatProvider`), so the adapters do not change.
- The reply is parsed after the stream ends; partial JSON is not shown (thinking and status are shown live).
- Environment values come from standard library calls (`datetime`, `getpass`, `platform`/`socket`) in this feature; the choice of calls is a detail to verify when implementing.
- The `model-registry` spec is done (`status: done`).

## Affected Areas

- New: `src/llm_harness/orchestrator.py`, `src/llm_harness/context.py` (context builder), orchestrator state and action types (in `types.py` or a new module), `src/llm_harness/prompts/` (data files, included in the wheel), tests with a scripted step model.
- Changed: `src/llm_harness/session.py` (no longer sends history), `src/llm_harness/types.py` (status event), `src/llm_harness/cli.py`, `src/llm_harness/tui/app.py`, `src/llm_harness/gui/app.py` (display of status and `ask_user` questions), `src/llm_harness/config.py` (selection by the tag `orchestrator`).
- Constitution at documentation time: `specs/mission.md` (remove the full-history exception), `specs/tech-stack.md` (reply contract and state schema), `specs/features/chat-streaming.md` notes; README and CHANGELOG.

## Out of Scope

- `context-inspector`: the UI to see thinking traces and the orchestrator's context at any moment (this feature only streams thinking and status events and exposes the pure context builder).
- `environment-and-identity`: richer environment awareness and recognising the same user on another computer. OPEN design question; the author's wording: "with external memory may need to figure out that on another computer it is talking to the same user with a different name to access memories about the user". Candidate approaches, none chosen: a user-chosen stable identity id stored in the memory service and mapped to local user and computer names; a memory-side alias table maintained with the user's confirmation; asking the user ("are you the same person as X?") when names differ. This feature only fills the simple environment block.
- `subagents`: subagents declared in configuration with per-subagent tool lists and model tags, only relevant tool definitions in their context, a fresh small brief, a short summary with sources returned, depth cap and budget inheritance (ideas listed in Approach).
- `tools-first-set`: web search, RAG query, memory access, pre-programmed web scrapers and API calls, each with typed input and output; the local Ollama embedding models `nomic-embed-text` and `bge-m3` exist for RAG (checked earlier, planned only).
- `model-registry`: fallback chain, backoff, usage and price tracking in SQLite (prerequisite).
- `a2ui-nicegui`: UI components emitted by the orchestrator.
- Persisting chats or run state; summarising old state; multi-user support.

## Implementation Notes

Work was done on `main` with no commits, in these steps.

1. State and reply contract. `src/llm_harness/state.py` with `tests/test_state.py`: `OrchestratorState`, `Finding`, `PlanStep`, `GoalChange`, `Environment`, the four action types (`respond`, `ask_user`, `call_tool`, `delegate`) and `Reply`. `apply_patch` merges a patch: `null` deletes, the fields `environment`, `goal_history`, `turn` and `step` are protected, lists change only through `$append` or `$replace`, the serialized state is capped at 8000 characters, and a changed goal adds a `GoalChange` record. `parse_reply` validates the two-key reply.

2. Context builder. `src/llm_harness/context.py`, `src/llm_harness/prompts/orchestrator.md` and `tests/test_context.py`: pure `build_request` (system message = instructions plus the generated schemas; user message = state JSON plus the observation, capped at 4000 characters). The prompt ships as package data; a wheel built for this check lists `llm_harness/prompts/orchestrator.md`.

3. Step loop. `src/llm_harness/orchestrator.py`, `src/llm_harness/tools.py`, `StatusEvent` in `src/llm_harness/types.py`, and `tests/fake_steps.py`: step cap 8, 2 retries per step, `ask_user` and `respond`, `call_tool` through a `ToolRegistry` (an unknown or failing tool becomes an observation), `delegate` answered with "not available yet", `RunState` snapshot and restore as JSON, and `has_completed_turn`. The retry observation keeps the original observation and adds the rejection text.

4. Model choice and front ends. `src/llm_harness/cli.py`, `config.py`, `router.py`: `select_orchestrator_model` (tag `orchestrator`, highest priority, no fallback to `default`) and the router's `required_tags`. Every front end (plain, TUI, GUI) now builds `Orchestrator`. `StatusEvent` is shown as `[step N: ...]` on stderr (plain), a note line (TUI) and a muted `ui.label` (GUI; layout classes identical to the usage line, pinned by tests).

5. Reply robustness, found with the real local model (`gemma4:12b` via Ollama). `parse_reply` tolerates one code fence around the whole reply; `ChatRequest.response_schema` (optional) is sent by the Ollama adapter as `format=`; `reply_schema()` pins the `state_patch` fields. Measured: unconstrained, 8 of 10 replies parsed; the Reply schema with a free-form `state_patch`, 0 of 10 applied on a turn-2-like state (always `goal` as an object); the pinned schema, 10 of 10. The OpenAI-compatible and Vertex adapters ignore `response_schema` (Vertex `gemini-3.1-flash-lite` returned bare JSON in 2 of 2 samples).

6. Prompt iterations. Findings from facts the user states get source "user"; "no tools exist yet, do not use call_tool"; `respond` is final (no "I'm working on it"); a "How you work" section; one example reply.

7. Removal of the full-history path. `src/llm_harness/session.py` (`ChatSession`) and `tests/test_session.py` were removed; no code path sends earlier messages. The criterion 7 guard test allows `ChatRequest(` only in `context.py` (among the `src` files). The live tests use a `single_turn` helper in `tests/fakes.py` (collected, not run).

8. Independent review (`reviewer`, read-only) found 9 items; fixed: `goal_history` is capped to the newest 20 records (the state hit the 8000-character cap after 56 goal changes); the retry rejection text always survives long observations (capped at 500 characters); `ask_user` answers are sent as `You asked the user: "<q>"` and `The user answered: <text>`, and `pending_question` is cleared only when a turn ends with `respond`; a fence may contain backticks; `Finding` text and source reject blank values in the schema too (pattern `\S`); a blank goal is rejected; the final `ErrorEvent` text is capped at 300 characters; the schema test understands `oneOf`/`discriminator`. Left as is, with reasons: (a) `Orchestrator(think=...)` has only test callers because the router applies the thinking flag; (b) with several steps in one turn the UIs show the usage line of the last step only (the session total stays cumulative).

Deviations from the approved spec text, to carry into documentation:
- Criterion 10: one wrapping code fence is tolerated.
- The retry observation is the original observation plus the rejection text.
- `ChatRequest` has an optional `response_schema` field.
- The `ask_user` answer observation (step 8) is new.
- `goal_history` keeps the newest 20 records.
- Replies are shown whole after the step completes; the model's JSON is never shown (thinking still streams).
- `snapshot()`, `restore()` and `pending_question` have no non-test caller by design: criterion 16 asks only that the state be serialisable and resumable, and persistence is out of scope.

## Validation

Counts come from `uv run pytest -q -rs`.

- Baseline before this feature (`specs/features/model-registry` done): 990 passed, 4 deselected, 0 skipped, 0 failed.
- Final: 1160 passed, 4 deselected, 0 skipped, 0 failed. The 4 deselected carry the `live` marker; they were collected with `-m live --collect-only` and not run because they are metered.
- `ruff check` and `ruff format --check` are clean (71 files).
- The passed count rose by 170 net: new tests in `test_state`, `test_context`, `test_reply_schema`, `test_orchestrator`, `test_tools`, `test_cli_orchestrator` and the TUI, GUI, router and adapter tests; 21 session tests were deleted.

Real artifact runs (local Ollama `gemma4:12b`, scratch HOME, no cost):
- `llm-harness --plain` multi-turn conversations; the second turn recalled "300 euros" from the state with no history sent.
- State probe `.agent/tmp/gemma_state_probe.py`: 4 of 4 turns, with the goal change recorded in `goal_history`.
- The TUI and GUI were verified by Pilot/User tests only, not run by hand by the assistant (the user runs them).

Call-site check: every new function, type and constant has a non-test caller (`apply_patch`, `parse_reply`, `reply_schema`, `build_request`, `retry_observation`, `answer_observation`, `ToolRegistry`, `select_orchestrator_model`, `has_completed_turn`, `response_schema`, `StatusEvent`, `GOAL_HISTORY_LIMIT`), except the intentional ones named in Implementation Notes.

Not verified: Vertex Gemini behaviour beyond 2 samples; OpenAI/OpenRouter orchestrator behaviour; server-side support for the schema on providers other than Ollama; small-model reliability beyond the samples above (Medium confidence).

## Documentation Review

Checked on 2026-10-28 against the code as it is now: `README.md` (read fully), `CHANGELOG.md`, `specs/mission.md`, `specs/tech-stack.md`, `specs/roadmap.md` (Now line only), `specs/memory.md` (grep only). Nothing outside this section was edited in this phase; every item below is a proposal for the documentation update (B5), and the constitution items need human approval.

### 1. README.md drift (14 items)

Format: `{README section/line, what is stale or missing, proposed text}`. Evidence is the code that contradicts the README.

1. {intro, lines 3-7; "Today it streams chat ... The orchestrator and tools do not exist yet."} Stale: the orchestrator exists (`src/llm_harness/orchestrator.py`), every front end runs through it (`src/llm_harness/cli.py:309`, `:341`); tools exist only as an empty registry (`src/llm_harness/tools.py`, name lookup only) and `delegate` answers "not available yet" (`src/llm_harness/orchestrator.py:218`). Proposed: "... and shows the model's thinking text when the model provides it. Every message goes through an orchestrator that keeps a small typed state instead of the chat history (see Orchestrator). It falls back to another model when one fails and records what each call cost. Real tools and subagents do not exist yet." Also "streams chat" is no longer exact: replies appear whole (item 11).
2. {Quick start / models.yaml example, line 67 `tags: [default, chat, cheap]`} Missing the `orchestrator` tag. Without it, startup fails: `src/llm_harness/config.py:319-323`. The example the app writes is `tags: [default, chat, orchestrator, cheap]` (`src/llm_harness/config.py:354`). Proposed: `tags: [default, chat, orchestrator, cheap]`.
3. {Configuration, lines 77-79 "The default model is the one tagged `default` ..."} Stale: the starting model is chosen by `select_orchestrator_model` (`src/llm_harness/config.py:312-324`, called at `src/llm_harness/cli.py:276`): `--model <id>` (any id, no tag needed), otherwise the model tagged `orchestrator` with the highest priority; a tie at the top is an error (`src/llm_harness/config.py:290-297`: `several orchestrator models share the top priority <n>: <ids>`); there is no fallback to `default` for this role. `select_model` (the `default` rule, `src/llm_harness/config.py:300-309`) has no caller in `src/` any more (grep). Proposed: "The starting model is the one tagged `orchestrator` with the highest priority (a higher number wins). Two such models with the same top priority are an error. If no model carries the tag, startup stops with an error naming it."
4. {Configuration, lines 80-81 "`tags` of `chat` and `default` drive the fallback chain"} Stale: the chain for every turn uses `orchestrator` and `default` (`src/llm_harness/cli.py:320-326`); `chat` is no longer used by any front end (`"chat"` appears only as the default parameter value in `src/llm_harness/router.py:90` and `:314`). Proposed: "`tags`: `orchestrator` marks models that can run the orchestrator and, with `default`, drives the fallback chain (below); other tags are free labels." Keep the sentence about `price`.
5. {Model fallback, lines 85-89} Stale: "the other models tagged `chat`". Chain rule: first model, then models with all required tags by priority, then `default` models (`src/llm_harness/routing.py:26-46`); the required tags are `{"orchestrator"}` (`src/llm_harness/cli.py:325`). Proposed: "The chain for a turn is: the chosen model (your `--model` or selector pick, otherwise the starting model), then the other models tagged `orchestrator` by `priority` (highest first), then the models tagged `default` by `priority`."
6. {Model fallback example, lines 97 and 103 `tags: [chat, default]`} Same cause. Proposed: `tags: [orchestrator, default]` for both entries.
7. {Flags, line 151 `--model ID`} Incomplete: the id is looked up without a tag check (`src/llm_harness/config.py:316-317`, `_lookup_model`), and it becomes the first model of the chain (the rest uses the `orchestrator` tag). Proposed: "use the model with this `id` from `models.yaml` as the orchestrator model; it needs no `orchestrator` tag".
8. {Model fallback, lines 124-138, usage line} Missing: a turn can have several steps (one model call each); the front ends keep the last `UsageReport` and print one usage line per turn (`src/llm_harness/cli.py:216-224`; same pattern in `tui/app.py` and `gui/app.py`), while the session total stays cumulative (spec Implementation Notes 8b). Proposed: add "When a turn takes several steps, the usage line shows the last step's call; the session total counts every call." (the cumulative total was not re-run by me; the last-report behaviour is in the cited lines).
9. {Providers, lines 186, 189-190; Known limits line 236 "OpenRouter and OpenAI are untested"} USER-STATED, not verified by the assistant: the author said on 2026-10-28 that OpenRouter was tested the day before and works. Proposed: OpenRouter row "tested by the author (author's statement, not checked here)"; keep OpenAI "implemented from documentation, untested"; line 189 "OpenRouter and OpenAI have only offline tests ..." becomes "OpenAI has only offline tests ...". Apply only after the coordinator confirms the wording with the author.
10. {Known limits, line 232 "The full chat history is sent on every turn (temporary)."} False now: `build_request` sends instructions, schemas, state and the latest observation only (`src/llm_harness/context.py:72-91`); `src/llm_harness/session.py` is deleted. Proposed: delete the line. Replacement limit (verified): "The model sees no earlier messages, only the state; what the state does not hold is forgotten."
11. {Known limits, line 233 "No orchestrator or tools yet."} Stale. Proposed: "No tools and no subagents yet: the model is told not to use `call_tool` or `delegate`, and `delegate` is answered with `delegate is not available yet`." (`src/llm_harness/orchestrator.py:218`; `src/llm_harness/prompts/orchestrator.md`, section Actions.)
12. {new section "Orchestrator", insert after "Model fallback and usage tracking" or before "Flags"} Missing. Every statement below was checked in code. Proposed text:

   > ## Orchestrator
   >
   > Every message goes through an orchestrator. The model never sees the chat history. Each step it gets one request with four parts: the instructions (`src/llm_harness/prompts/orchestrator.md`), the generated JSON schemas of the reply and the state, the current state as JSON, and the latest observation (your latest message, or the result of the previous action). The chat transcript on screen is for display only.
   >
   > The state has these fields: `goal`; `goal_history` (the last 20 goal changes, written by the harness); `findings` (each with a `text` and a non-empty `source`); `plan` (steps with status `todo`, `doing`, `done` or `dropped`); `environment` (date and time, user name and computer name, refreshed by the harness at the start of every turn). `turn` and `step` are counters. The model can change only `goal`, `findings` and `plan`.
   >
   > The model answers with one JSON object with exactly two keys: `state_patch` and `action`. The action is one of `respond` (the answer, ends the turn), `ask_user` (a question, ends the turn; your next message is the answer), `call_tool` and `delegate`. No tools or subagents exist yet, so `call_tool` reports an unknown tool and `delegate` answers that it is not available yet.
   >
   > - Each step prints a status line: `[step 1: answering]` on stderr in `--plain`; the terminal UI and the GUI show `step 1: answering` as a note.
   > - Replies appear whole when the step has finished, because the model's reply is JSON. Thinking text still streams while the model thinks.
   > - A rejected reply is retried up to 2 times (3 attempts) with the error text as the new observation; a turn ends after 8 steps. One code fence around the whole reply is accepted; anything else around the JSON is rejected.
   > - The reply schema is sent to the model as a constraint only by the Ollama adapter (`format`); the OpenAI-compatible and Vertex adapters ignore it, so there the contract rests on the instructions and the retries.
   > - The state is capped at 8000 characters; a patch that would exceed it is rejected.

   Evidence: `src/llm_harness/context.py:72-91`; `src/llm_harness/state.py:22-24`, `:29-30`, `:63-70`, `:115-169`, `:200-202`, `:253-263`; `src/llm_harness/orchestrator.py:98-99` (cap 8, retries 2), `:137-150` (environment and counters refreshed each turn), `:199-201` (status event), `:203-214` (respond and ask_user), `:218`, `:232-237`; `src/llm_harness/cli.py:211-215`; `src/llm_harness/tui/app.py:243-246`; `src/llm_harness/gui/app.py:317-322`; `src/llm_harness/providers/ollama.py:49-51` (the only use of `response_schema` under `providers/`, by grep). Status text shapes come from `_status_text` (`src/llm_harness/orchestrator.py:76-86`: `answering`, `asking the user`, `calling tool <name>`, `delegating to <name>`); the exact example `step 1: answering` is built from that and `cli.py:212`, not run by me.
13. {Troubleshooting, lines 207-216} Missing three errors, exact text from the code. Proposed bullets (the first two print as `error: <text>` before the app starts, `src/llm_harness/cli.py:277-278`; the last two as `error (unknown): <text>`, `src/llm_harness/cli.py:229`):
    - `no model is tagged 'orchestrator'; tag one in models.yaml or use --model` (`src/llm_harness/config.py:322`): add `orchestrator` to the `tags` of at least one model, or pass `--model <id>`. Existing config files made before the orchestrator need this.
    - `several orchestrator models share the top priority <n>: <ids>` (`src/llm_harness/config.py:296`): give one of them a higher `priority`.
    - `the orchestrator reply was rejected 3 times; last error: <text>` (`src/llm_harness/orchestrator.py:192-194`): the model did not follow the JSON contract in 3 attempts; try a stronger model or `--model`. State from earlier valid steps is kept.
    - `the orchestrator reached the step cap of 8 steps without a respond or ask_user action` (`src/llm_harness/orchestrator.py:225-227`): the model kept calling tools or delegating; ask again more simply.
    The remedies for the last two are advice; the cause statements are from the code, the advice was not tested.
14. {Tests section, lines 218-228} Checked: it states no test counts and lists no test files, only commands; no drift. No change.

### 2. CHANGELOG.md entries (single implicit artifact)

Artifact resolution: `specs/tech-stack.md` has no `### Artifacts` subsection (grep for `Artifacts` returned nothing), so the implicit default applies: root `.`, changelog `CHANGELOG.md`. `CHANGELOG.md` has `## [Unreleased]` with only `### Added`. One existing line there becomes stale and should be edited in the same change: the "Model fallback" line says "the next `chat`-tagged model" (item 2 below, Changed).

Format: `{changelog path, category, one-line entry}` (10 entries).

1. {`CHANGELOG.md`, Added, "Orchestrator: every message now runs through a step loop over a small typed state (goal, goal history, sourced findings, plan, environment) with actions `respond`, `ask_user`, `call_tool` and `delegate`; the model reply is validated and a rejected reply is retried up to 2 times, with a cap of 8 steps per turn."}
2. {`CHANGELOG.md`, Added, "Model tag `orchestrator` in `models.yaml`: the highest-priority model with this tag runs the orchestrator; `--model <id>` overrides it."}
3. {`CHANGELOG.md`, Added, "Status line for each orchestrator step: `[step N: ...]` on stderr in `--plain`, a note line in the terminal UI and the GUI."}
4. {`CHANGELOG.md`, Added, "On Ollama the reply schema is sent to the model (`format`), so replies follow the JSON contract more reliably."}
5. {`CHANGELOG.md`, Changed, "All front ends now run through the orchestrator: replies appear whole when a step finishes, not word by word; thinking text still streams."}
6. {`CHANGELOG.md`, Changed, "BREAKING for existing configs: at least one model in `models.yaml` needs the `orchestrator` tag; without it startup stops with `no model is tagged 'orchestrator'; tag one in models.yaml or use --model`."}
7. {`CHANGELOG.md`, Changed, "The fallback chain uses the `orchestrator` tag instead of `chat` (chosen model, then `orchestrator`-tagged models by priority, then `default`-tagged models); also fix the existing Added line about the fallback, which names `chat`."}
8. {`CHANGELOG.md`, Changed, "The starting model is now chosen by the `orchestrator` tag instead of `default`; `default` only marks the last-resort fallback."}
9. {`CHANGELOG.md`, Removed, "The chat history is no longer sent to the model; each step sends only the instructions, the state and the latest observation."}
10. {`CHANGELOG.md`, Removed, "The `ChatSession` class (`src/llm_harness/session.py`)."} (Internal class; kept because the brief asked for it, drop if the author wants user-facing entries only.)

Sources: Acceptance Criteria 7, 8, 12, 13, 14, 18, 20-22; Implementation Notes 3-5, 7. Entry 8 follows `src/llm_harness/config.py:312-324` and `src/llm_harness/cli.py:276`; entry 7 follows `src/llm_harness/cli.py:325`.

### 3. CI / build / release review

CI review: not applicable (no `.github/`, Makefile, Taskfile, Dockerfile, `scripts/` directory or release script exists in the repository root, checked with `ls -a` and `ls`, and no tag or release step exists to wire a changelog rewrite into).

One packaging fact: `src/llm_harness/prompts/orchestrator.md` ships in the wheel; it was checked with `uv build --wheel` (build backend `uv_build`, `pyproject.toml:27-28`) and needs no `pyproject.toml` change (Implementation Notes step 2). I did not rebuild the wheel in this phase.

### 4. Constitution drift (proposals only, not applied; human approval needed)

Format: `{file, section, current text, proposed change, evidence}` (13 items).

1. {`specs/mission.md`, Problem} Current: "...the orchestrator and the context inspector are not built yet." Proposed: "...every message goes through an orchestrator that keeps a typed state instead of the chat history; subagents, real tools and the context inspector are not built yet." Evidence: `src/llm_harness/orchestrator.py:89-116`, `src/llm_harness/cli.py:341`.
2. {`specs/mission.md`, Value proposition, 2nd paragraph} Current: "What does not exist yet: the orchestrator and the context inspector." Proposed: add to "What exists today": "an orchestrator with a typed state (goal, sourced findings, plan, a simple environment block with date and time, user name and computer name) and a reply contract of a state patch plus one action; the chat history is not sent to the model". "What does not exist yet": "subagents, real tools (only an empty registry), the context inspector, and recognising the same user on another computer." Evidence: `src/llm_harness/state.py:63-70`, `src/llm_harness/tools.py`, `src/llm_harness/orchestrator.py:218`, `src/llm_harness/orchestrator.py:63-64` (identity is `getpass.getuser()` and `socket.gethostname()` only).
3. {`specs/mission.md`, Value proposition, 3rd paragraph} Current: "...choosing models per task by tags is still to come with the orchestrator." Proposed: "the orchestrator model is chosen by the tag `orchestrator` and priority; choosing models per task for subagents is still to come." Evidence: `src/llm_harness/config.py:312-324`.
4. {`specs/mission.md`, Value proposition, 4th paragraph} Current: "Goal: one orchestrator model ... The chat history is not sent to the model in full." Proposed: keep the goal sentences but say which parts exist: the chat history is not sent, the goal, sourced information and plan are tracked, an unclear goal makes the orchestrator ask the user, date and time, user name and computer name are in the context; delegation to subagents and identity across computers are still goals. Evidence: `src/llm_harness/context.py:72-91`, `src/llm_harness/orchestrator.py:209-214`, `src/llm_harness/orchestrator.py:131-135`.
5. {`specs/mission.md`, Non-goals, last bullet} Current: "Sending the full chat history to the model: the orchestrator works from its own state instead (temporary exception in `chat-streaming`, removed by `orchestrator-state`)." Proposed: "Sending the full chat history to the model: the orchestrator works from its own state instead." (the exception is gone). Evidence: `src/llm_harness/session.py` deleted (git status `D`); `src/llm_harness/context.py:72-91` is the only request builder.
6. {`specs/tech-stack.md`, Orchestrator and context paragraph} Current: "The exact reply and state-patch contract between the model and the orchestrator is decided in the feature spec." Proposed: replace with the decided contract: "The model replies with one JSON object with exactly two keys, `state_patch` and `action`. `state_patch` may contain only `goal`, `findings` and `plan`; `null` deletes a value; lists change only through `{"$append": [...]}` or `{"$replace": [...]}`; `environment`, `goal_history`, `turn` and `step` are protected; the serialized state is capped at 8000 characters; `goal_history` keeps the newest 20 records; a finding needs a non-empty `source`. `action` is a tagged union on `type`: `respond`, `ask_user`, `call_tool`, `delegate`. A rejected reply changes nothing and its error text becomes the next observation; at most 2 retries per step and 8 steps per turn. One code fence around the whole reply is tolerated. State and reply types are in `state.py`; `context.py` holds `build_request`, the only builder of the orchestrator's `ChatRequest`; `orchestrator.py` holds `Orchestrator` (`send`, `snapshot`, `restore`)." Evidence: `src/llm_harness/state.py:22-32,115-169,172-202,253-304`, `src/llm_harness/orchestrator.py:98-100`, `src/llm_harness/context.py:72-91`.
7. {`specs/tech-stack.md`, Orchestrator and context, tools} Add: "`ToolRegistry` (`tools.py`) does name lookup only; typed input and output schemas are deferred to `tools-first-set`. An unknown or failing tool becomes an observation." Evidence: `src/llm_harness/tools.py:1-30`, `src/llm_harness/orchestrator.py:232-245`.
8. {`specs/tech-stack.md`, Layout line} Current: "core in `types.py`, `session.py`, `config.py`, ..." Proposed: remove `session.py`; add `state.py` (state, patch merge and reply contract), `context.py` (request builder), `orchestrator.py` (step loop), `tools.py` (registry) and `prompts/` (`orchestrator.md`, package data included in the wheel); tests: add `tests/fake_steps.py` (scripted step model) next to `tests/fakes.py`. Evidence: `ls src/llm_harness`, `ls tests`.
9. {`specs/tech-stack.md`, model registry paragraph} Current: "The default model is the model tagged `default` with the highest priority ...; `--model <id>` or a UI selector overrides it." and "(chat turns pass `chat`)". Proposed: "The orchestrator model is the model tagged `orchestrator` with the highest priority (`select_orchestrator_model`; a tie at the top is an error, no model with the tag stops startup with a message naming the tag, and there is no fallback to `default` for this role); `--model <id>` or a UI selector overrides it. `fallback_chain` takes the required tags as a parameter; every front end passes `orchestrator` (`make_router_factory(..., required_tags=frozenset({"orchestrator"}))`); the router's own default is still `chat`." Also in the chain sentence replace "other models tagged `chat`" by "other models carrying the required tags". Evidence: `src/llm_harness/config.py:312-324`, `src/llm_harness/cli.py:320-326`, `src/llm_harness/router.py:90,314`, `src/llm_harness/routing.py:26-46`.
10. {`specs/tech-stack.md`, Infrastructure & tooling} Current: "No CI yet. The directory is not yet a git repository." Proposed: "No CI yet. The directory is a git repository (branch `main`)." Evidence: `git log --oneline | head -3` printed `ae837b0 chat shell`; `.git` exists.
11. {`specs/tech-stack.md`, Frameworks & libraries / Orchestrator paragraph} Add the structured-output fact: "`ChatRequest.response_schema` (optional JSON schema) is sent by the Ollama adapter as the `format` argument; the OpenAI-compatible and Vertex adapters ignore it." Evidence: `src/llm_harness/types.py:28`, `src/llm_harness/providers/ollama.py:49-51`; no other `response_schema` use under `src/llm_harness/providers/` (grep).
12. {`specs/tech-stack.md`, Ollama versions} Client: `ollama 0.6.3` is already stated and matches `uv.lock` (`name = "ollama"`, `version = "0.6.3"`). Server: `ollama --version` printed `ollama version 0.40.11` on this machine today, and `specs/features/chat-streaming.md:503` records the same. Proposed: add "local Ollama server 0.40.11 (checked 2026-10-28 on the dev machine)" next to the `ollama` line. Evidence is a machine fact, so the author may prefer to leave it out.
13. {`specs/tech-stack.md`, Frameworks & libraries ("OpenRouter and OpenAI paths ... untested") and Key conventions ("verified live against Ollama's `/v1` endpoint; OpenRouter and OpenAI are implemented from documentation and unverified")} USER-STATED, not verified by the assistant: the author said on 2026-10-28, in conversation, that they tested OpenRouter the day before and it works. Proposed: "OpenRouter: tested by the author on 2026-10-27 (author's statement, not verified by the assistant); OpenAI: implemented from documentation and unverified." Apply only with the author's approval of the wording.

Roadmap: the `## Now` line for this feature (`specs/roadmap.md:11`) is removed in B5; nothing else proposed.

### 5. Memory candidates for `specs/memory.md` (proposed, not appended)

Checked with grep for `gemma|global|us-central1|response_schema|format=` in `specs/memory.md`: no existing entry overlaps (17 lines in the file). Format per the `memory-format` skill; each line is at most 400 characters, no absolute path, no secret, no project or account id. Entries 1 and 2 come from this feature's measurements (Implementation Notes step 5). Entries 3 and 4 come from the author's brief and are not in the repo; I did not verify them.

```
2026-10-28T12:18:01Z [gotcha] Without a response schema, gemma4:12b through Ollama wraps its JSON reply in a code fence and can nest `state_patch` wrongly; 8 of 10 replies parsed. With `ChatRequest.response_schema` (Ollama `format=`) and the `state_patch` fields pinned, 10 of 10 were valid (`reply_schema` in `src/llm_harness/state.py`).
2026-10-28T12:18:02Z [gotcha] A free-form `state_patch` object in the Ollama `format=` schema made gemma4:12b send `goal` as an object, so 0 of 10 patches applied on a turn-2 state; list the patchable fields in the schema (`reply_schema` in `src/llm_harness/state.py`).
2026-10-28T12:18:03Z [env] Vertex Gemini 3.x models are served only from location `global`; `us-central1` returns not_found, while gemini-2.5-flash-lite works in both locations (author's statement, 2026-10-28).
2026-10-28T12:18:04Z [gotcha] Raw HTTP probes of Vertex returned misleading 404s; probe a Vertex model through the app's own adapter (`src/llm_harness/providers/vertex.py`) instead (author's statement, 2026-10-28).
```

### Not verified (left out of the proposals above or flagged)

- That the OpenRouter path works (user-stated only; items 1.9 and 4.13 are conditional on the author's approval).
- The Vertex location facts and the misleading raw 404s (memory entries 3 and 4; from the brief only, no repo evidence).
- The 8 of 10, 10 of 10 and 0 of 10 sample counts (taken from Implementation Notes step 5; I did not rerun the probes).
- The wheel contents (taken from Implementation Notes step 2; not rebuilt here).
- The exact rendered text of the TUI and GUI status note (built from the code, not run).
- The advice parts of the Troubleshooting bullets in item 1.13 (remedies were not tested).

## Documentation Updates

Applied on 2026-10-28 (B5 step 1, routine documentation part 1).

- `README.md`: review items 1.1-1.8 and 1.10-1.14 applied; item 1.9 (OpenRouter/OpenAI wording in Providers and Known limits) applied later on 2026-10-28 with the author's wording (see the last bullet); item 1.14 needed no change.
  - Intro paragraph (orchestrator, no streaming claim, real tools and subagents not yet there).
  - Quick start / `models.yaml` example: `tags: [default, chat, orchestrator, cheap]`.
  - Configuration: starting model by the `orchestrator` tag; `tags` sentence.
  - Model fallback and usage tracking: chain text, both example `tags`, one sentence on the usage line for multi-step turns.
  - Flags: `--model ID` row.
  - New section `## Orchestrator` (before `## Flags`).
  - Troubleshooting: four new error bullets.
  - Known limits: the two stale lines replaced.
- `CHANGELOG.md` under `## [Unreleased]`: 4 entries added to `### Added`; new `### Changed` (4 entries) and `### Removed` (2 entries); the stale Added "Model fallback" line now says `orchestrator`-tagged instead of `chat`-tagged.
- Release wiring: no CI or release workflow exists (Documentation Review section 3), so nothing to change.
- `specs/memory.md`: 4 entries appended (entries 3 and 4 reworded: measured by the coordinator through the app's Vertex adapter, not author statements):

```
2026-10-28T12:21:01Z [gotcha] Without a response schema, gemma4:12b through Ollama wraps its JSON reply in a code fence and can nest `state_patch` wrongly; 8 of 10 replies parsed. With `ChatRequest.response_schema` (Ollama `format=`) and the `state_patch` fields pinned, 10 of 10 were valid (`reply_schema` in `src/llm_harness/state.py`).
2026-10-28T12:21:02Z [gotcha] A free-form `state_patch` object in the Ollama `format=` schema made gemma4:12b send `goal` as an object, so 0 of 10 patches applied on a turn-2 state; list the patchable fields in the schema (`reply_schema` in `src/llm_harness/state.py`).
2026-10-28T12:21:03Z [env] Vertex Gemini 3.x models are served only from location `global`; `us-central1` returns not_found. Measured through the app's Vertex adapter: gemini-2.5-flash-lite works in `us-central1` and `global`; gemini-3.1-flash-lite and gemini-3.5-flash-lite work only in `global`.
2026-10-28T12:21:04Z [gotcha] Raw `generateContent` and `countTokens` HTTP probes of Vertex returned 404 even for a model that works; probe a Vertex model through the app's own adapter (`src/llm_harness/providers/vertex.py`) instead.
```

- Constitution changes, applied on 2026-10-28 after the author's approval of review section 4 items 1-11 and 13 (item 12, the Ollama server version, was not applied):
  - `specs/mission.md` (version 5 to 6): items 1-5 (Problem; Value proposition paragraphs 2, 3 and 4; the Non-goals bullet without the temporary exception). Two phrases "default model" in paragraphs 2 and 3 now say "orchestrator model", to agree with item 3.
  - `specs/tech-stack.md` (version 9 to 10): items 6, 7 and 11 (reply and state-patch contract, `ToolRegistry`, `response_schema`, in the Orchestrator and context paragraph); item 8 (Layout line); item 9 (model registry paragraph: orchestrator model selection and fallback chain tags); item 10 (git repository sentence); item 13 in the new wording (the OpenRouter path works; the OpenAI path is unverified), in the Frameworks & libraries sentence and the Key conventions bullet. In item 9, "the default is chosen by tag and priority" in the registry paragraph also became "the orchestrator model".
  - `specs/roadmap.md`: the `## Now` line for this feature was removed (mechanical bookkeeping, no version bump); the section now reads "Nothing in progress."
  - `README.md`: Providers table (OpenRouter "works", OpenAI "implemented from documentation, untested"), the sentence under the table (now OpenAI only) and the Known limits line ("OpenAI is untested.").
