# Project Memory

<!-- Append-only, one entry per line: `YYYY-MM-DDTHH:MM:SSZ [tag] text`. See the `memory-format` skill. Do not read this file in full — grep it. -->

2026-10-27T18:46:20Z [gotcha] google-genai uses aiohttp automatically when aiohttp is installed (nicegui depends on it), so network failures raise aiohttp exceptions, not httpx ones; adapters must catch both, and a test against a real closed port is needed because mocks hide it.
2026-10-27T18:46:21Z [gotcha] NiceGUI 3.18: `ui.run` defaults host to all interfaces (pass the loopback host explicitly); `client.on_disconnect` also fires for sockets that reconnect (use `client.on_delete` for cleanup); call `ui.notify` through the `ui` module for the test `user` fixture; pytest needs `-p nicegui.testing.user_plugin` in addopts and `main_file = ""`.
2026-10-27T18:46:22Z [gotcha] Textual 8.2: a `str` Select option or Static content is parsed as console markup (`[/]` raises MarkupError); pass `rich.text.Text` or `Static(..., markup=False)`.
2026-10-27T18:46:23Z [build] `uv init` runs `git init` unless `--vcs none` is given.
2026-10-27T19:19:44Z [gotcha] NiceGUI 3.18: Ctrl-C in `ui.run` surfaces as KeyboardInterrupt after the server has stopped; catching it around `ui.run` and returning exit code 130 gives a clean exit without a traceback (verified in a pseudo-terminal; the uvicorn source was not read).
2026-10-27T19:19:45Z [gotcha] NiceGUI 3.18: `ui.html(content, sanitize=<callable>)` applies the callable on the server before innerHTML; only `sanitize=True` triggers browser-side DOMPurify, so a callable is the only filter.
2026-10-27T19:19:46Z [gotcha] Textual 8.2: the `Markdown` widget raises IndexError on footnote tokens and drops the inline HTML a task-list plugin emits, so leave both plugins out of its `parser_factory`; `Markdown.update()` is async.
2026-10-27T19:19:47Z [gotcha] nh3 0.3.7 (ammonia): the output for a link whose href was dropped is not idempotent in one pass (rel and target change order on a second pass), so clean twice to reach a stable form.
2026-10-27T19:19:48Z [gotcha] A shell background job ignores SIGINT, so test Ctrl-C in a pseudo-terminal, not with `kill -INT` on a background job.
2026-10-27T20:06:07Z [gotcha] `PRAGMA user_version = N` cannot take a bound parameter, so build it with an integer the code controls; `Cursor.execute` runs one statement and `executescript` commits an open transaction, so keep the migrations in `src/llm_harness/store.py` as tuples of single statements.
2026-10-27T20:06:08Z [gotcha] Work that already runs in `asyncio.to_thread` cannot be cancelled, so an `asyncio.shield` around such a database write (`src/llm_harness/router.py`) is hard to test: the thread still finishes the write when the awaiting task is cancelled.
2026-10-27T20:06:09Z [gotcha] A mutation test showed the shield around the cancelled-attempt write in `src/llm_harness/router.py` is not detectable by tests: replacing it with a plain await failed no test, so a passing suite does not prove the shield is still there.
2026-10-27T20:06:10Z [build] A test fixture must patch a function that production code calls through its module (`store.default_db_path()` in `tests/conftest.py`), not a name imported with `from ... import`, or the patch has no effect.
