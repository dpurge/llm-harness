"""Command line: argument parsing and the line-based `--plain` chat."""

import argparse
import asyncio
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from llm_harness import store as store_module
from llm_harness.config import (
    CONFIG_FILE,
    EXAMPLE_CONFIG_TOML,
    EXAMPLE_MODELS_YAML,
    MODELS_FILE,
    ConfigError,
    HarnessConfig,
    MissingConfigError,
    ModelConfig,
    default_config_dir,
    load_config,
    permission_warnings,
    resolve_think,  # noqa: F401 - still importable from here
    select_model,
    write_examples,
)
from llm_harness.display import format_switch, format_usage_report, sanitize_text
from llm_harness.providers import ProviderFactory, build_provider
from llm_harness.providers.base import ChatProvider
from llm_harness.router import RouterFactory, make_router_factory
from llm_harness.session import ChatSession
from llm_harness.store import Store
from llm_harness.types import (
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    TextDelta,
    ThinkingDelta,
    UsageReport,
)

_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_EXAMPLE_TEXT = {CONFIG_FILE: EXAMPLE_CONFIG_TOML, MODELS_FILE: EXAMPLE_MODELS_YAML}
_EXIT_COMMANDS = frozenset({"/exit", "/quit"})

# (config, initial model, router factory, was --model given) -> exit code
TuiRunner = Callable[[HarnessConfig, ModelConfig, RouterFactory, bool], int]
# The web GUI runner has the same shape as the terminal UI runner.
GuiRunner = TuiRunner


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-harness")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plain", action="store_true", help="line-based chat; works over pipes"
    )
    mode.add_argument("--gui", action="store_true", help="web GUI in the browser")
    parser.add_argument("--model", metavar="ID", help="model id from models.yaml")
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument(
        "--think",
        dest="think",
        action="store_const",
        const=True,
        default=None,
        help="ask the model to think (overrides the model's thinking setting)",
    )
    thinking.add_argument(
        "--no-think",
        dest="think",
        action="store_const",
        const=False,
        default=None,
        help="turn thinking off (overrides the model's thinking setting)",
    )
    return parser


def _handle_missing_config(
    exc: MissingConfigError, config_dir: Path, stdin: TextIO, stderr: TextIO
) -> int:
    print(exc, file=stderr)
    for path in exc.missing:
        print(f"\nExample {path.name}:\n{_EXAMPLE_TEXT[path.name]}", file=stderr)
    # Never prompt or write without a person at the terminal.
    if not stdin.isatty():
        return 1
    stderr.write(f"Write example files to {config_dir}? [y/N] ")
    stderr.flush()
    if not stdin.readline().strip().lower().startswith("y"):
        return 1
    for path in write_examples(config_dir, exc.missing):
        print(f"wrote {path}", file=stderr)
    print("Review these files before use.", file=stderr)
    return 0


async def _read_line(stdin: TextIO) -> str:
    """Read one line in a daemon thread and await it.

    `asyncio.to_thread` would leave a non-daemon executor thread blocked in
    `readline`, and closing the loop after Ctrl-C would wait for it until the
    user presses Enter.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def settle(line: str | None, error: Exception | None) -> None:
        # The future is already cancelled when the reader was interrupted.
        if future.cancelled():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(line or "")

    def read() -> None:
        line: str | None = None
        error: Exception | None = None
        try:
            line = stdin.readline()
        except Exception as exc:  # noqa: BLE001 - re-raised in the awaiting task
            error = exc
        try:
            loop.call_soon_threadsafe(settle, line, error)
        except RuntimeError:
            pass  # the loop is closed: nobody is waiting for this line any more

    threading.Thread(target=read, daemon=True, name="stdin-reader").start()
    return await future


async def _chat_loop(
    router: ChatProvider,
    model_id: str,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    session = ChatSession(router, model_id)
    try:
        while True:
            if stdin.isatty():
                stdout.write("you> ")
                stdout.flush()
            line = await _read_line(stdin)
            if not line:
                return 0
            text = line.strip()
            if text.lower() in _EXIT_COMMANDS:
                return 0
            if not text:
                continue
            if not await _run_turn(session, text, stdout, stderr):
                return 1
    finally:
        await router.aclose()


async def _run_turn(
    session: ChatSession, text: str, stdout: TextIO, stderr: TextIO
) -> bool:
    """Stream one reply; return False when the provider reported an error."""
    report: UsageReport | None = None
    # Each thinking delta is wrapped and reset on its own, so closing an open
    # line only needs the newline.
    thinking_open = False

    def close_thinking_line() -> None:
        nonlocal thinking_open
        if thinking_open:
            stderr.write("\n")
            stderr.flush()
            thinking_open = False

    def print_note(note: str) -> None:
        close_thinking_line()
        print(note, file=stderr)

    async for event in session.send(text):
        if isinstance(event, TextDelta):
            close_thinking_line()
            stdout.write(sanitize_text(event.text))
            stdout.flush()
        elif isinstance(event, ThinkingDelta):
            plain_thinking = thinking = sanitize_text(event.text)
            if not plain_thinking:
                continue
            if stderr.isatty():
                thinking = f"{_DIM}{thinking}{_RESET}"
            stderr.write(thinking)
            stderr.flush()
            if plain_thinking:
                thinking_open = not plain_thinking.endswith("\n")
        elif isinstance(event, ModelSwitch):
            print_note(format_switch(event))
        elif isinstance(event, Notice):
            print_note(sanitize_text(event.text))
        elif isinstance(event, UsageReport):
            # Printed at Done, after the reply's final newline.
            report = event
        elif isinstance(event, Done):
            close_thinking_line()
            stdout.write("\n")
            stdout.flush()
            if report is not None:
                print(format_usage_report(report), file=stderr)
        elif isinstance(event, ErrorEvent):
            close_thinking_line()
            stdout.write("\n")
            stdout.flush()
            print(f"error ({event.kind}): {sanitize_text(event.message)}", file=stderr)
            return False
    return True


def _run_tui_lazily(
    config: HarnessConfig,
    model: ModelConfig,
    router_factory: RouterFactory,
    is_manual: bool,
) -> int:
    # Imported here so --plain and --help never load textual.
    from llm_harness.tui.app import run_tui

    return run_tui(config, model, router_factory, is_manual)


def _run_gui_lazily(
    config: HarnessConfig,
    model: ModelConfig,
    router_factory: RouterFactory,
    is_manual: bool,
) -> int:
    # Imported here so --plain, --help and the TUI never load nicegui.
    from llm_harness.gui.app import run_gui

    return run_gui(config, model, router_factory, is_manual)


def _load_config_and_model(
    config_dir: Path, model_id: str | None, stdin: TextIO, stderr: TextIO
) -> tuple[HarnessConfig, ModelConfig] | int:
    """Load the config and pick the model; an int is the exit code to return."""
    try:
        config = load_config(config_dir)
    except MissingConfigError as exc:
        return _handle_missing_config(exc, config_dir, stdin, stderr)
    except ConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return 1

    for warning in permission_warnings(config_dir):
        print(warning, file=stderr)
    try:
        return config, select_model(config, model_id)
    except ConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return 1


def main(
    argv: list[str] | None = None,
    *,
    config_dir: Path | None = None,
    data_dir: Path | None = None,
    provider_factory: ProviderFactory = build_provider,
    run_tui: TuiRunner = _run_tui_lazily,
    run_gui: GuiRunner = _run_gui_lazily,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    args = _build_parser().parse_args(argv)

    # The web GUI talks to a browser, so only the terminal UI needs a TTY.
    if not args.plain and not args.gui and not (stdin.isatty() and stdout.isatty()):
        print(
            "The terminal UI needs a terminal for input and output; "
            "use --plain over pipes.",
            file=stderr,
        )
        return 2

    config_dir = config_dir if config_dir is not None else default_config_dir()
    loaded = _load_config_and_model(config_dir, args.model, stdin, stderr)
    if isinstance(loaded, int):
        return loaded
    config, model = loaded

    # One store and one router factory per process; nothing touches the disk
    # until the first turn.
    db_path = (
        store_module.default_db_path() if data_dir is None else data_dir / "harness.db"
    )
    router_factory = make_router_factory(
        config, provider_factory, args.think, Store(db_path)
    )
    is_manual = bool(args.model)

    if args.gui:
        return run_gui(config, model, router_factory, is_manual)
    if not args.plain:
        return run_tui(config, model, router_factory, is_manual)

    try:
        router = router_factory(model, is_manual)
    except ConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return 1

    try:
        return asyncio.run(_chat_loop(router, model.model, stdin, stdout, stderr))
    except KeyboardInterrupt:
        print(file=stderr)
        return 130
