---
title: A2UI v1.0 in NiceGUI with custom HTML and SVG components
kind: feature
status: draft
version: 1
updated: 2026-10-27
---

## Problem / Motivation

DRAFT, not approved. Written so that a later session can start from it without asking the author again. Names of the custom catalog, components and events below are PROPOSALS until the author confirms them.

The author's words (2026-10-27), verbatim:

- "The GUI part will need to handle u2ui protocol [the human means A2UI, confirmed] because I will want to build custom GUI components (HTML and SVG)."
- "Implement u2ui v1.0 spec; since we are using NiceGUI, I think I want widgets ported to NiceGUI; compile with esbuild, use typescript if possible. Think of how to easily distribute compiled components, so that users do not need NodeJS when installing."

Decisions and facts:

- The reference is the public A2UI repository `https://github.com/a2ui-project/a2ui` (spec documents under `specification/v1_0/`, checked at commit 059b8d34) and https://a2ui.org/.
- The author chose A2UI v1.0 knowingly although a2ui.org labelled v0.9.1 "Current production release" and v1.0 "Release candidate" (checked 2026-10-27).
- Standing UI rules from the author: the GUI and the widgets use the FULL browser width and viewport height; large text (base at least 18px, secondary at least 15px); no narrow centred columns; no fixed-height boxes; this applies to every GUI page and to the planned A2UI widgets. No screenshots are used to check layout (it is judged from code and class-checking tests). The app is a desktop, interactive, single-user application. The development install must be editable (`uv sync`, `uv run llm-harness`): changes apply after a pull and restart with no manual build step. The author forbids git operations by the assistant. The terminal UI ignores or degrades `ui` events (assumption recorded earlier).
- Why: the orchestrator should be able to show richer output than markdown (forms, choices, the author's own HTML and SVG components) without letting model text become active content in the browser.

A2UI facts verified on 2026-10-27 in the public repository clone and a2ui.org:

- v1.0 spec documents: `a2ui_protocol.md`, `a2ui_custom_functions.md`, `evolution_guide.md`, `sdks_spec.md` under `specification/v1_0/docs`; JSON schemas under `specification/v1_0/json`.
- Agent-to-renderer messages: `createSurface`, `updateComponents`, `updateDataModel`, `deleteSurface`, `callRendererFunction`, `agentFunctionResponse`. Renderer-to-agent messages: `action`, `callAgentFunction`, `rendererFunctionResponse`, `error`.
- Catalogs are JSON Schema documents with `catalogId`/`$id`, `components`, `functions`, `instructions`. The basic catalog (v1) has 18 components (Text, Image, Icon, Video, AudioPlayer, Row, Column, List, Card, Tabs, Modal, Divider, Button, TextField, CheckBox, ChoicePicker, Slider, DateTimeInput) and 14 functions (required, regex, length, numeric, email, formatString, formatNumber, formatCurrency, formatDate, pluralize, openUrl, and, or, not). Custom catalogs add components and functions.
- Identifiers follow UAX #31 and the name `Surface` is reserved; child references must use `ComponentId`/`ChildList` types so validators can check structure. Messages carry structure and data: an adjacency-list component model, data-model binding with paths, and two-way binding for inputs. Transports (A2A, AG-UI, HTTP, websocket) are outside the spec.
- a2ui.org says v1.0 adds client-to-server RPC (`actionResponse`), action ids, and renames theme to surfaceProperties.
- Existing renderers: Lit, Angular, Flutter, React, Markdown-it (TypeScript packages `@a2ui/web_core` 0.14.0 and `@a2ui/lit` 0.14.0 in the repository) and the Python package `a2ui-core` (0.4.0 on PyPI, Python >= 3.10, depends on babel, jsonschema, pydantic, referencing, regex) with a v1.0 schema module and v1.0 protocol adapter, providing message processing, surface/data-model state, binding, catalog registry and validation. NOT verified: that PyPI 0.4.0 matches the repository, that it passes the v1.0 conformance cases, how it integrates with NiceGUI. There is NO NiceGUI or Python renderer; NiceGUI 3.18.0 is a Vue/Quasar app.
- Lesson from an existing A2UI integration that was compared (verified there): validate every model-produced A2UI message against the schemas BEFORE it reaches the page; send problems back to the model for a bounded number of retries; only draw components from the catalog; accept an action from the page only for a surface this conversation drew; restrict media hosts (images, video and audio come only from this server, `data:` URLs and an explicit allowlist) and set a Content-Security-Policy accordingly; rename a surface id the model reuses.

Build facts: the wheel is built with the `uv_build` backend (works today; whether it includes non-Python static files such as compiled JS is UNVERIFIED). Node v26.8.2 and npm exist on the developer machine; latest npm releases checked 2026-10-27: esbuild 0.28.2, typescript 7.0.2, lit 3.3.3. No TypeScript, JavaScript or `package.json` exists in the repository yet.

Existing GUI facts (code read): replies are sanitized markdown through `ui.html(sanitize=sanitize_html)` with an nh3 allowlist (`src/llm_harness/markdown_render.py`; images never loaded, raw HTML shown as text); `src/llm_harness/gui/security.py` (`LoopbackOnlyMiddleware`) allows only Host `127.0.0.1:<port>` or `localhost:<port>` and a matching Origin for http and websocket scopes, so any new HTTP or websocket route is covered by it.

## Acceptance Criteria

All tests are offline. Names in backticks are proposals.

Rendering
1. The NiceGUI chat page renders an A2UI v1.0 surface from a `createSurface` plus `updateComponents` (plus `updateDataModel`) message sequence; a test with the NiceGUI `user` fixture finds the expected elements.
2. All 18 basic-catalog components are ported to NiceGUI widgets, each with a test that renders a minimal valid component and checks the output.
3. All 14 basic-catalog functions are implemented where the renderer evaluates them (formatting, validation, logic, `openUrl`), each with unit tests of the documented behaviour.
4. Data-model binding works with paths, and two-way binding of inputs (TextField, CheckBox, ChoicePicker, Slider, DateTimeInput) updates the data model; a test changes an input and reads the model.
5. A custom catalog (proposal: catalog id `llm-harness/custom/v1`, to confirm) defines custom HTML and SVG components (proposal: `HtmlBlock` and `SvgImage`, to confirm); each has its own sanitizer with a test table of hostile inputs.
6. HTML sanitizer: reuses the allowlist approach of `sanitize_html` (nh3): no `script`, `style`, `iframe`, `object`, `embed`, `form` or event-handler attributes; no external resource loading.
7. SVG sanitizer allowlist: no `script`, no `foreignObject`, no event handler attributes (`on*`), no external references (`href`/`xlink:href` only to `#fragment`, no `url(...)` to other documents, no `<use>` of external files); tests with hostile samples confirm each rule.

Validation and safety
8. Every model-produced A2UI message is validated against the v1.0 JSON schemas (and the active catalog) before rendering. Nothing is rendered from an unvalidated message (a test sends an invalid message and asserts the page is unchanged).
9. A rejected message produces an error text sent back to the model as an observation, for a bounded number of retries (default 2, same as `orchestrator-state`); after that a visible error line is shown and nothing is drawn.
10. Only components of the catalog named in the surface are drawn; an unknown component is a validation error, never rendered.
11. Child references are checked (`ComponentId`/`ChildList`): a dangling or cyclic reference is rejected.
12. A surface id that the model reuses is renamed (proposal: suffix with a counter) so a new surface cannot overwrite another one silently; the rename is visible in the observation.
13. An `action` or function call from the page is accepted only for a surface this conversation drew; otherwise it is dropped and logged without content (test with a forged surface id).
14. Actions and function calls flow back to the orchestrator as observations (proposal: `ui_action` observation with surface id, action name and bound data) and only for such surfaces.
15. Media hosts are restricted: images, video and audio come only from this server, `data:` URLs and an explicit allowlist (proposal: configurable list, empty by default); a Content-Security-Policy header is set that matches (proposal: `img-src`, `media-src` limited accordingly); tests check the header and that a disallowed URL is not emitted into the page.
16. Every new HTTP or websocket route (widget bundles, action channel) passes the existing Host/Origin middleware (tests with a foreign Host and Origin get 403 or close 1008).
17. Untrusted text keeps passing through `sanitize_text` before display; model text is never put in the page as active content.

Build and distribution
18. TypeScript sources are compiled with esbuild into bundles under a package directory (proposal: `src/llm_harness/gui/static/`); a build script (proposal: `npm run build` through a documented task) exists and is the only place that needs Node.js.
19. The compiled bundles ship INSIDE the wheel: a test builds the wheel with `uv build` (or inspects the build configuration) and asserts that the bundle files are present in the archive.
20. An end-user install (`pip install` of the wheel or `uv tool install` of it) works with Node.js absent: a test or script runs the app import path with no `node` on `PATH`.
21. The dev loop needs no manual build step after a pull: the chosen mechanism (see Open questions: bundles committed, or built at app start in development) is verified by a test or documented check that a clean clone plus `uv sync` plus `uv run llm-harness --gui` serves current bundles.
22. A check compares the TypeScript source hash with the bundle (or the build is reproducible) so stale committed bundles are detected in the test run.

Layout and front ends
23. GUI pages and all widgets use the full browser width and viewport height, base text at least 18px and secondary at least 15px, no narrow centred columns and no fixed-height boxes; verified by class-checking tests (no screenshots).
24. The terminal UI and `--plain` ignore or degrade `ui` events (proposal: print a one-line note `[ui surface omitted]`); a test shows the TUI neither crashes nor draws the surface.
25. A conformance check runs the repository's own v1.0 test cases or examples where they exist (to be located during the spike); results are recorded in Validation, including any case the port does not pass.
26. `uv run ruff check` and `uv run pytest` pass offline with zero skips; every new function, type and config key has a non-test use.

## Approach

Memory consulted: orchestrator|state|context|subagent|A2UI|NiceGUI|esbuild|wheel|sanitize — 3 relevant entries: "NiceGUI 3.18: `ui.run` defaults host to all interfaces (pass the loopback host explicitly)...", "NiceGUI 3.18: `ui.html(content, sanitize=<callable>)` applies the callable on the server before innerHTML; only `sanitize=True` triggers browser-side DOMPurify, so a callable is the only filter", "NiceGUI 3.18: Ctrl-C in `ui.run` surfaces as KeyboardInterrupt after the server has stopped..." (4 lines matched; the aiohttp entry is not relevant)

STATUS: DRAFT. The author must answer the open questions at approval time; each has a safe default.

MANDATORY first step: a bounded SPIKE (proposal: at most one working session) that answers these questions before any other step. The spike delivers a short written note in Implementation Notes and no production code.

Spike questions:
1. Where do the Lit and `@a2ui/web_core` widget implementations live in the repository, and how portable are they (dependencies, build, size)?
2. Option A: wrap the existing TypeScript renderer as one custom NiceGUI element (bundled by esbuild, hosted through NiceGUI's static file and custom element mechanism, to be verified against the installed NiceGUI 3.18.0). Option B: write a Python mapping of catalog components to NiceGUI elements on top of `a2ui-core`. Which one fits the author's wish ("widgets ported to NiceGUI")? Decide with evidence: effort, safety, testability offline, how custom HTML and SVG components fit.
3. Can the `uv_build` wheel include the compiled bundles (a small experiment: build a wheel with a dummy static file and list it)?
4. Does `a2ui-core` 0.4.0 match the repository, pass the v1.0 conformance cases, validate v1.0 messages against catalogs, and track surface and data-model state in a way that suits NiceGUI?
5. Which of the repository's test cases or examples can serve as the conformance check (criterion 25)?
6. How does a NiceGUI element send an action back to Python, and does that pass through the Host/Origin middleware unchanged?

Ordered plan after the spike, each step small, with the unknown it retires and how it is verified:
1. Message validation and state in Python (retires: the validator choice; verified by offline tests with valid and invalid v1.0 messages, including child-reference checks).
2. A minimal surface with Text, Column and Button, end to end, including the action round trip into an observation (retires: the hosting mechanism and the action channel; verified by a NiceGUI `user` fixture test and a manual run).
3. The rest of the basic catalog and the functions (retires: binding and two-way inputs; verified by per-component tests and the conformance check).
4. The custom catalog with HTML and SVG components and their sanitizers (retires: the safety of custom content; verified by hostile-input tables).
5. Packaging and the dev loop (retires: static files in the wheel, bundles current after a pull without a manual build; verified by wheel inspection and a clean-clone check).
6. Security hardening: CSP, media allowlist, route checks, surface-id rules (retires: the CSP interaction with NiceGUI's own scripts, which is unverified; verified by header tests and a manual browser run).

Interplay with `orchestrator-state`: the orchestrator model emits A2UI messages inside its `ask_user` and `respond` actions (proposal: an optional `ui` key in the action holding a message list); the page returns `ui_action` observations (criterion 14) that become the next observation of the step loop. Validation errors (criterion 9) use the same retry budget as reply-contract errors. The model learns the catalog through the catalog's `instructions` field plus the instructions data file.

Dev loop decision, proposed default: commit the compiled bundles to the repository AND check their freshness in the test run (criterion 22), because it is the only option that needs no Node.js for the editable install after a pull. Alternative: build when the app starts in development when Node is present. The author decides.

Names that are PROPOSALS to confirm before implementation: custom catalog id `llm-harness/custom/v1`; custom components `HtmlBlock`, `SvgImage`; the observation name `ui_action`; the static directory `src/llm_harness/gui/static/`; the TypeScript source directory (proposal `frontend/`).

OPEN QUESTIONS (each with a safe default):
1. Option A (wrap the TypeScript renderer) or Option B (Python mapping on `a2ui-core`)? Default: decided by the spike; prefer the option with offline-testable widgets.
2. Use `a2ui-core` as a declared dependency? Default: only if the spike verifies it; otherwise write a small validator on `jsonschema` against the repository schemas.
3. Bundles committed to git, or built at development start? Default: committed plus a freshness check.
4. Names of the custom catalog, components and observation (see above). Default: as proposed.
5. Media allowlist contents (default: empty, only this server and `data:` URLs).
6. Are v1.0 and the release-candidate status still acceptable if the protocol changes before release? Default: pin the repository commit 059b8d34 for the schemas and record the pin.
7. Retry bound for invalid messages (default 2).
8. Should the TUI show a short note or nothing for `ui` events? Default: a one-line note.
9. Which transport details matter, given transports are outside the spec? Default: none; messages travel inside the orchestrator's actions in process.
10. Should the first widgets be the basic catalog only, with custom components after (criteria order)? Default: yes.
11. TypeScript version: use the npm latest (typescript 7.0.2) or the version the A2UI packages use? Default: decided by the spike, the version that compiles the ported sources.

Unverified:
- That the Lit and `web_core` sources are portable to a NiceGUI custom element.
- NiceGUI 3.18.0 custom element and static file hosting mechanism for compiled bundles.
- Whether `uv_build` includes non-Python static files in the wheel.
- That `a2ui-core` 0.4.0 matches the repository, passes v1.0 conformance cases, and integrates with NiceGUI.
- That a CSP can be set without breaking NiceGUI's own scripts.
- Whether the repository's test cases or examples are usable as a conformance check.
- That the existing `sanitize_html` allowlist, designed for markdown, suits custom HTML components without changes.

Assumptions:
- Messages travel in process; no remote A2A or AG-UI transport.
- The page keeps one A2UI surface registry per browser client, like one `ChatSession` per client.
- The orchestrator (`orchestrator-state`) exists first, so `ask_user`/`respond` actions can carry UI messages; before that, a test harness feeds messages directly.
- Theming beyond the layout rules is not needed.
- Node.js and npm are needed on the developer and release machines only.

Next step: human review of the open questions, then approval (B1).

## Affected Areas

- New: TypeScript sources and `package.json` (outside `src/llm_harness/` or under a frontend directory; location to confirm), compiled bundles in `src/llm_harness/gui/static/`, A2UI Python modules (validation, surface state, catalog registry, sanitizers; proposal `src/llm_harness/a2ui/`), the custom catalog JSON, tests.
- Changed: `src/llm_harness/gui/app.py` (render surfaces, action round trip), `src/llm_harness/gui/security.py` only if the spike shows routes escape the middleware (expected: no change), `src/llm_harness/types.py` (a `ui` stream event), `src/llm_harness/tui/app.py` and `cli.py` (ignore or degrade `ui`), `pyproject.toml` (static files in the wheel; dependencies only after the spike).
- Constitution at documentation time: `specs/tech-stack.md` (A2UI section: spike results, dependency decisions, dev loop), README and CHANGELOG.

## Out of Scope

- A2UI in the terminal UI beyond ignoring or degrading `ui` events.
- A remote A2A (or AG-UI) transport.
- Theming beyond the layout rules.
- The orchestrator itself (`orchestrator-state`), the context inspector, subagents and tools.
- Rendering A2UI in `--plain`.
- A2UI versions other than v1.0.
- Authoring tools for the author's own components beyond the catalog definition.

## Implementation Notes

Not started.

## Validation

Not started.

## Documentation Review

Not started.

## Documentation Updates

Not started.
