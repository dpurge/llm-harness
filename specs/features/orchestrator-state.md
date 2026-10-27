---
title: Orchestrator with typed state and no full chat history
kind: feature
status: draft
version: 1
updated: 2026-10-27
---

## Problem / Motivation

DRAFT, not approved. Written so that a later session can start from it without asking the author again. Every field name below is a PROPOSAL until the author confirms it.

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

Standing rules from the author that apply: desktop, interactive, single user; the GUI uses the full browser width and viewport height with large text (base at least 18px, secondary at least 15px); no screenshots are used to check layout; the dev install is editable (`uv sync`, `uv run llm-harness`); the author forbids git operations by the assistant (the project is not a git repository, so no `branch` field).

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

STATUS: DRAFT. The author must answer the open questions below at approval time; each has a safe default so work can start from the default if the author says "use the defaults".

Reply contract: the model sends one JSON object with two keys, `state_patch` and `action` (the constitution fixes the two keys). `state_patch` follows JSON Merge Patch style semantics (an object merged into the state, `null` deletes a key); lists are replaced as a whole only through an explicit form (proposal `{"$replace": [...]}`), otherwise new items are added through an append form (proposal `{"$append": [...]}`), which avoids the patch-overwrite error where a model resends a short list and loses items. Validation is by pydantic models for the state and the action union; unknown keys are rejected everywhere.

State schema, FIELD NAMES ARE PROPOSALS needing the author's confirmation before implementation:

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

OPEN QUESTIONS (each with a safe default):
1. Are the proposed state field names (`goal`, `goal_history`, `findings`, `plan`, `environment`, `turn`, `step`) and the action tag names (`call_tool`, `delegate`, `ask_user`, `respond`) acceptable? Default: use them as proposed.
2. Is `orchestrator` the right tag for the orchestrator model? Default: `orchestrator`.
3. Does "not in full" mean the previous assistant reply may be included as part of the latest observation? Default: no, only the latest user message plus state (strictest reading).
4. Patch list semantics: `$append` and `$replace` forms, or a different shape? Default: as proposed.
5. Step cap (8), retry cap (2) and state size cap (8000 characters): values? Default: as proposed.
6. Is the orchestrator the only chat path, or does `--plain` keep a direct single-model chat mode for testing? Default: the orchestrator is the only path; tests use the scripted model.
7. Does the model selector in the UIs remain (picking the orchestrator model)? Default: yes, it selects the orchestrator model.
8. Is the transcript shown in the UI saved anywhere? Default: no persistence in this feature (the suspended run state is serializable, but no store).
9. Is a `StatusEvent` the right way to show steps, or should status ride on the context-inspector later? Default: add the minimal `StatusEvent` now.

Unverified:
- That small local models (for example `gemma4:12b`) can follow the two-key JSON contract reliably (needs a live probe).
- Whether any provider adapter offers a JSON-mode or structured-output switch that helps; not used in this draft.
- The exact way the three front ends are refactored (not yet designed in detail).

Assumptions:
- Python 3.12 and the pydantic-based typing already used in the repository.
- The step model is the same provider interface (`ChatProvider`), so the adapters do not change.
- The reply is parsed after the stream ends; partial JSON is not shown (thinking and status are shown live).
- Environment values come from standard library calls (`datetime`, `getpass`, `platform`/`socket`) in this feature; the choice of calls is a detail to verify when implementing.
- The `model-registry` spec is approved and implemented first.

Next step: human review of the open questions, then approval (B1).

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

Not started.

## Validation

Not started.

## Documentation Review

Not started.

## Documentation Updates

Not started.
