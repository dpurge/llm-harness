---
title: "Agent registry: agents declared in agents.yaml"
kind: feature
status: done
version: 1
updated: 2026-10-29
branch: main
---

## Problem / Motivation

Umbrella plan: `specs/features/environment-and-identity.md` (done), item `agent-registry` and contract B4. This is the first of `agent-registry`, `tool-registry`, `subagents`.

Today there is no agent concept. The orchestrator's identity is hard-coded in `src/llm_harness/orchestrator.py:317-322` (`agent_id="orchestrator"`, tags `{"orchestrator"}`, with the comment "Agent ids and tags will come from the planned agent registry"). Models are declared in `models.yaml` and loaded by `load_config` (`src/llm_harness/config.py:376`); nothing declares agents.

This feature adds `agents.yaml` next to `models.yaml`: loading, validation, the missing-file flow, the orchestrator taking its id and tags from the registry, and a read-only view in the inspector. It does not run subagents and does not match tools (those are `subagents` and `tool-registry`).

Decisions confirmed by the author on 2026-10-29: a missing `agents.yaml` behaves like a missing `models.yaml` (stop, offer an example); the registry gives the orchestrator its id and tags only (its model is still chosen by the model tag `orchestrator`); `model_tags` must be satisfiable by `models.yaml`.

## Acceptance Criteria

1. `~/.config/llm-harness/agents.yaml` has the top level `agents:` (a list; nothing else at the top level). Each entry has `id`, `name`, `description` (non-empty strings), `tags` (list), `model_tags` (list) and optional `examples` (list of strings, default empty), `tools` and `deny` (lists of strings, default empty). An unknown key in an entry is rejected. Errors name the file and the agent.
2. `tags` and `model_tags` entries are lowercase, contain no spaces, and have no duplicates within the list. Agent ids are unique; duplicates are rejected naming the id.
3. Every agent's `model_tags` must be carried together by at least one chat model in `models.yaml` (a model tagged `embedding` does not count); otherwise rejected with a message naming the agent and the tags.
4. A name in both `tools` and `deny` of one agent is rejected. Tool names are not checked against a registry yet (no real tools exist; `tool-registry` adds that check).
5. Exactly one agent carries the tag `orchestrator`: zero or more than one is rejected with a message naming the tag (and the ids, when more than one).
6. `agents.yaml` missing: `load_config` raises `MissingConfigError` listing it, and the existing flow (`cli._handle_missing_config`) offers to write an example `agents.yaml` containing one orchestrator agent whose `model_tags` match the example `models.yaml`. The example is written like the others (`O_EXCL`, never overwrites). An existing `config.toml` and `models.yaml` are untouched. `--storage-status` keeps working without a terminal as today (it does not offer examples).
7. `HarnessConfig` exposes `agents`; `find_orchestrator_agent(config)` returns the one orchestrator agent, or None for a config built without agents (tests only; `load_config` always yields one). The orchestrator builds its `ToolContext` with that agent's `id` and `tags` instead of the hard-coded values; the three front ends (`cli.py`, `gui/app.py`, `tui/app.py`) pass it. Without an agent (tests only) the orchestrator keeps today's values.
8. The model of the orchestrator is still chosen as today (`select_orchestrator_model`); there is no change to routing, fallback or the usage ledger.
9. The inspector (GUI and terminal UI) lists the agents with id, name, tags, model tags and description, marks the orchestrator, and shows model-derived strings only through the existing redaction/sanitising path. Read-only; full width and large text per the GUI rules.
10. README (config section), CHANGELOG, `specs/tech-stack.md` (built versus planned) and tests are updated. The full suite and ruff pass; every skip is explained. Tests cover each rejection above, the example file, and the orchestrator context; no test touches the real config directory.

Sample `agents.yaml` (the example the app offers to write):

```yaml
agents:
  - id: orchestrator
    name: Orchestrator
    description: Reads the user's prompt, keeps the goal, and assembles the reply.
    tags: [orchestrator]
    model_tags: [orchestrator]
```

Sample rejection: `invalid agents.yaml: agent 'writer': no chat model carries all of model_tags ['writer', 'cheap']`.

## Approach

Memory consulted: agent|registry|yaml — 2 relevant entries (pyyaml `safe_load` expands aliases by reference and `state_io.py` rejects them; every `models.yaml` entry needs a `name`); 6 lines matched in total, the rest not relevant.

Follow the pattern of models: pydantic `_Frozen` models in `config.py`, parsed with the existing `_read_yaml`, errors through `_format_validation_error`. `AgentConfig` gets the field rules (criteria 1, 2, 4); the cross-checks (duplicate ids, model_tags satisfiable, exactly one orchestrator) live in `HarnessConfig`'s `_check_references` validator, like duplicate model ids today. `HarnessConfig.agents` defaults to an empty list so existing tests that build a config directly keep working; `load_config` itself always requires a non-empty file with one orchestrator (criterion 5).

Alias note: `_read_yaml` uses `yaml.safe_load`, the same as `models.yaml`; the alias slowdown applies only to a local file the user wrote. It is not hardened here (assumption; say if you want `state_io`'s no-alias loader reused).

Ordered plan (each step ends green and is shown to you before the next):

1. `AgentConfig`, `HarnessConfig.agents`, validation and `find_orchestrator_agent` in `config.py`, with tests (criteria 1-5, 7 first half).
2. `agents.yaml` in `load_config`, the example text, `_EXAMPLES` and the missing-file flow, with tests (criterion 6).
3. Orchestrator takes the agent; `cli.py`, `gui/app.py`, `tui/app.py` pass it (criteria 7, 8), with tests.
4. Inspector view: one text-building function in `inspect_state.py` used by `gui/inspector.py` and `tui/state_tab.py` (criterion 9), with tests.
5. Docs and closeout (criterion 10).

Risk: step 4 needs the config (or the agent list) to reach the inspector; how it gets there is read from `gui/app.py` and `tui/app.py` at that step, and if it needs more than passing one argument I stop and report.

Testing strategy: offline unit tests only, all fast; no model calls.

## Affected Areas

- `src/llm_harness/config.py` (models, validation, loading, example, missing-file list)
- `src/llm_harness/orchestrator.py` (agent id and tags in `ToolContext`)
- `src/llm_harness/cli.py`, `src/llm_harness/gui/app.py`, `src/llm_harness/tui/app.py` (pass the agent; first-run flow)
- `src/llm_harness/inspect_state.py`, `src/llm_harness/gui/inspector.py`, `src/llm_harness/tui/state_tab.py` (view)
- `tests/test_config.py`, `tests/test_orchestrator.py`, `tests/test_inspect_state.py`, `tests/test_gui_inspector.py`, `tests/test_tui_inspector.py`, `tests/test_cli.py`
- `README.md`, `CHANGELOG.md`, `specs/tech-stack.md`, `specs/roadmap.md`

## Out of Scope

- Tool matching, `tool_search`, tool name validation (`tool-registry`).
- Running subagents, `delegate`, budgets, depth (`subagents`).
- Choosing the orchestrator's model from `model_tags` (author chose: id and tags only).
- Editing agents in the UI; hot reload.
- Hardening YAML aliases beyond what `models.yaml` has.

## Implementation Notes

Baseline before any change: 1702 passed, 5 deselected (opt-in live tests), ruff clean.

Step 1 (done, 2026-10-29): in `src/llm_harness/config.py` added `AgentConfig` (field rules, `tools`/`deny` clash), `_check_agents` (duplicate ids, `model_tags` carried together by one chat model, exactly one `orchestrator` agent when any agent is declared), `HarnessConfig.agents` (default empty) and `select_orchestrator_agent` (replaced in step 3 by `find_orchestrator_agent`, see below). 21 tests added to `tests/test_config.py`. After: 1723 passed, 5 deselected, ruff check and format clean. `AgentConfig` had no non-test call site yet by design: `load_config` uses it in step 2 and the orchestrator and front ends in step 3.

Step 2 (done, 2026-10-29): `AGENTS_FILE`, `EXAMPLE_AGENTS_YAML` (mode 0o644) and `_load_models`/`_load_agents` in `src/llm_harness/config.py`; `load_config` now lists `agents.yaml` as missing, rejects a wrong top level, an empty list and every cross-check with `invalid agents.yaml: ...`; `cli.py` prints the agents example in the missing-file flow (`_EXAMPLE_TEXT`). Test fixtures in `tests/test_cli.py`, `tests/test_cli_orchestrator.py`, `tests/test_live_fallback.py` and the `write_config` helper write an `agents.yaml`; 16 tests added. A model-less setup now fails earlier: an agent whose `model_tags` no chat model carries (including an embedding-only `models.yaml`) is a config error. After: 1740 passed, 5 deselected, ruff clean. Smoke run against a scratch HOME: the missing-file output includes `Example agents.yaml:`, `write_examples` writes all three files, `load_config` returns orchestrator agent `orchestrator` and model `gemma-local`, and a bad `model_tags` gives `invalid agents.yaml: agent 'orchestrator': no chat model carries all of model_tags ['nope']`. Call sites: `grep -n "_load_agents\|_check_agents" src/llm_harness/config.py` shows `load_config` and `HarnessConfig` use them; `select_orchestrator_agent` was unused outside tests, so step 3 removed it.

Step 3 (done, 2026-10-29): `Orchestrator(..., agent=)` in `src/llm_harness/orchestrator.py` keeps the agent's id and tags and puts them in every `ToolContext` (without an agent: the built-in `orchestrator` id and tag, as before). `find_orchestrator_agent` in `config.py` is passed by `cli.py` (plain mode), `gui/app.py` and `tui/app.py` (`_start_session`). The model is still chosen by `select_orchestrator_model`, unchanged. Deviation from the plan: `select_orchestrator_agent` was removed as dead code and `find_orchestrator_agent` (returns None without agents) took its place. 4 tests added (orchestrator context, plain CLI, GUI page, TUI app); the hostile-status test double in `tests/test_gui.py` gained the `agent` parameter. After: 1744 passed, 5 deselected, ruff clean. Call sites (`grep -n find_orchestrator_agent src`): `cli.py:443`, `gui/app.py:229`, `tui/app.py:185`.

Step 4 (done, 2026-10-29): `agent_text` and `agents_text` in `src/llm_harness/inspect_state.py` (every string through `sanitize_text`; orchestrator marked; `(none)` for no agents). The State tab of both front ends gets an `Agents` section (open by default) built once from `config.agents`: `StatePanel(..., agents)` in `gui/inspector.py` (passed from `gui/app.py`) and `StatePanel(agents, ...)` in `tui/state_tab.py` (passed from `tui/app.py`). It shows configuration, so it is filled before the first message. Reuses the existing `w-full`, `_PRE_WRAP` classes and plain `ui.label` / `Static(markup=False)`, so the full-width, font and no-HTML source tests still apply and pass. The plan's risk did not occur: the config reached the panels as one extra argument. 4 tests added (text format, empty, GUI, TUI incl. a hostile `[bold]` string). After: 1748 passed, 5 deselected, ruff clean. Call sites: `gui/inspector.py` and `tui/state_tab.py` use `agents_text`.

## Validation

Final run 2026-10-29: `uv run pytest -q` gave 1748 passed, 0 failed, 0 skipped, 5 deselected (the opt-in `live` tests, excluded by `addopts = -m "not live"` in `pyproject.toml`; they call real providers). Baseline before the work: 1702 passed, 5 deselected. No regression: every earlier failure came from fixtures that did not yet write an `agents.yaml`, and was fixed in the same step. `uv run ruff check` clean; `uv run ruff format --check`: 103 files already formatted. Real artifact: against a scratch HOME, the first-run output printed `Example agents.yaml:`, `write_examples` wrote the three files, `load_config` returned the agent `orchestrator` and the model `gemma-local`, and a bad `model_tags` gave the one-line `invalid agents.yaml` error. GUI and TUI were not opened by hand in a real browser or terminal; the State tab is covered by NiceGUI and Textual tests. Call-site grep (`grep -rn find_orchestrator_agent src`): `cli.py:443`, `gui/app.py:231`, `tui/app.py:185`; `agents_text`: `gui/inspector.py:226`, `tui/state_tab.py:57`; `_load_agents`/`_check_agents`: `config.py:473`, `:379`, `:514`; `EXAMPLE_AGENTS_YAML`: `config.py:805`, `cli.py:57`.

## Documentation Review

- `README.md`: drift found (only two config files described, no agents section, State tab and Troubleshooting lacked agents); fixed.
- `CHANGELOG.md`: user-facing change (new required config file, inspector section); entry needed in the only changelog (`CHANGELOG.md`, the project has no `### Artifacts` table); written.
- `specs/mission.md`: "What exists today" did not mention agents; updated (version 9, approved by the author 2026-10-29).
- `specs/tech-stack.md`: no Agent registry description; Naming bullet said agent identifiers were still to be confirmed; updated (version 18, approved 2026-10-29).
- `specs/roadmap.md`: `## Now` line removed at closeout.

CI review: not applicable (the repository has no CI workflow, Dockerfile, Makefile, task runner or release script: only `pyproject.toml` and `uv.lock`; checked with `ls -a` and `ls .github`).

## Documentation Updates

- `README.md`: new `### Agents` section (the exact `EXAMPLE_AGENTS_YAML`, rules), three config files in the intro, agents in the State tab description, an `invalid agents.yaml` entry in Troubleshooting.
- `CHANGELOG.md`: one `Added` entry under `## [Unreleased]`.
- Constitution: `specs/mission.md` (version 9) and `specs/tech-stack.md` (version 18) updated with the author's approval; see those files.
- `specs/roadmap.md`: this feature's `## Now` line removed.
- `memory.md`: nothing appended.
