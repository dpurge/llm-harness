"""Command line: argument parsing and the line-based `--plain` chat."""

import argparse
import asyncio
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, TextIO

from llm_harness import store as store_module
from llm_harness.builtin_tools import build_tool_registry
from llm_harness.config import (
    AGENTS_FILE,
    CONFIG_FILE,
    EXAMPLE_AGENTS_YAML,
    EXAMPLE_CONFIG_TOML,
    EXAMPLE_MODELS_YAML,
    MODELS_FILE,
    ConfigError,
    HarnessConfig,
    MissingConfigError,
    ModelConfig,
    default_config_dir,
    default_data_dir,
    find_orchestrator_agent,
    load_config,
    permission_warnings,
    resolve_think,  # noqa: F401 - still importable from here
    select_orchestrator_model,
    write_examples,
)
from llm_harness.display import format_switch, format_usage_report, sanitize_text
from llm_harness.orchestrator import Orchestrator
from llm_harness.providers import ProviderFactory, build_provider
from llm_harness.providers.base import ChatProvider
from llm_harness.router import RouterFactory, make_router_factory
from llm_harness.storage import Storage, open_storage
from llm_harness.storage.status import storage_status
from llm_harness.store import Store
from llm_harness.subagents import SubagentFactoryMaker, make_subagent_factory_maker
from llm_harness.tools import ToolRegistry
from llm_harness.types import (
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    StatusEvent,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    UsageReport,
)

_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_EXAMPLE_TEXT = {
    CONFIG_FILE: EXAMPLE_CONFIG_TOML,
    MODELS_FILE: EXAMPLE_MODELS_YAML,
    AGENTS_FILE: EXAMPLE_AGENTS_YAML,
}
_EXIT_COMMANDS = frozenset({"/exit", "/quit"})

# (config, initial model, router factory, was --model given) -> exit code


class TuiRunner(Protocol):
    def __call__(
        self,
        config: HarnessConfig,
        model: ModelConfig,
        router_factory: RouterFactory,
        is_manual: bool,
        *,
        storage: Storage | None = None,
        tools: ToolRegistry | None = None,
        subagent_factory_maker: SubagentFactoryMaker | None = None,
    ) -> int: ...


# The web GUI runner has the same shape as the terminal UI runner.
GuiRunner = TuiRunner


class _Sender(Protocol):
    """What the plain loop drives: an Orchestrator."""

    def send(self, text: str) -> AsyncIterator[StreamEvent]: ...


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-harness")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plain", action="store_true", help="line-based chat; works over pipes"
    )
    mode.add_argument("--gui", action="store_true", help="web GUI in the browser")
    mode.add_argument(
        "--storage-status",
        action="store_true",
        help="show the state of the three stores and exit; changes nothing",
    )
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
    exc: MissingConfigError,
    config_dir: Path,
    stdin: TextIO,
    stderr: TextIO,
    *,
    offer_examples: bool = True,
) -> int:
    print(exc, file=stderr)
    for path in exc.missing:
        print(f"\nExample {path.name}:\n{_EXAMPLE_TEXT[path.name]}", file=stderr)
    # Never prompt or write without a person at the terminal; a read-only command
    # never writes at all.
    if not offer_examples or not stdin.isatty():
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
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    session: _Sender,
) -> int:
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


async def _chat_loop_then_close(
    router: ChatProvider,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    orchestrator: Orchestrator,
    storage: Storage,
) -> int:
    """Run the chat; the storage is closed inside the loop, so Ctrl-C closes it too."""
    try:
        return await _chat_loop(router, stdin, stdout, stderr, session=orchestrator)
    finally:
        await storage.close()


async def _run_turn(
    session: _Sender, text: str, stdout: TextIO, stderr: TextIO
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
        elif isinstance(event, StatusEvent):
            status = f"[step {event.step}: {sanitize_text(event.text)}]"
            if stderr.isatty():
                status = f"{_DIM}{status}{_RESET}"
            print_note(status)
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
    *,
    storage: Storage | None = None,
    tools: ToolRegistry | None = None,
    subagent_factory_maker: SubagentFactoryMaker | None = None,
) -> int:
    # Imported here so --plain and --help never load textual.
    from llm_harness.tui.app import run_tui

    return run_tui(
        config,
        model,
        router_factory,
        is_manual,
        storage=storage,
        tools=tools,
        subagent_factory_maker=subagent_factory_maker,
    )


def _run_gui_lazily(
    config: HarnessConfig,
    model: ModelConfig,
    router_factory: RouterFactory,
    is_manual: bool,
    *,
    storage: Storage | None = None,
    tools: ToolRegistry | None = None,
    subagent_factory_maker: SubagentFactoryMaker | None = None,
) -> int:
    # Imported here so --plain, --help and the TUI never load nicegui.
    from llm_harness.gui.app import run_gui

    return run_gui(
        config,
        model,
        router_factory,
        is_manual,
        storage=storage,
        tools=tools,
        subagent_factory_maker=subagent_factory_maker,
    )


def _load_config(
    config_dir: Path,
    stdin: TextIO,
    stderr: TextIO,
    *,
    offer_examples: bool = True,
) -> HarnessConfig | int:
    """Load the config; an int is the exit code to return."""
    try:
        config = load_config(config_dir)
    except MissingConfigError as exc:
        return _handle_missing_config(
            exc, config_dir, stdin, stderr, offer_examples=offer_examples
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return 1

    for warning in permission_warnings(config_dir):
        print(warning, file=stderr)
    return config


def _load_config_and_model(
    config_dir: Path,
    model_id: str | None,
    stdin: TextIO,
    stderr: TextIO,
) -> tuple[HarnessConfig, ModelConfig] | int:
    """Load the config and pick the model; an int is the exit code to return."""
    config = _load_config(config_dir, stdin, stderr)
    if isinstance(config, int):
        return config
    try:
        return config, select_orchestrator_model(config, model_id)
    except ConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return 1


def _relational_path(config: HarnessConfig, data_dir: Path | None) -> Path:
    """The ledger file: `[storage.relational] path`, else under the data directory."""
    if config.storage.relational.path is not None:
        return config.storage.relational.path
    if data_dir is None:
        return store_module.default_db_path()
    return data_dir / "harness.db"


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
    needs_terminal = not (args.plain or args.gui or args.storage_status)
    if needs_terminal and not (stdin.isatty() and stdout.isatty()):
        print(
            "The terminal UI needs a terminal for input and output; "
            "use --plain over pipes.",
            file=stderr,
        )
        return 2

    config_dir = config_dir if config_dir is not None else default_config_dir()
    if args.storage_status:
        # Needs no model, so a config without a default model still works.
        status_config = _load_config(config_dir, stdin, stderr, offer_examples=False)
        if isinstance(status_config, int):
            return status_config
        lines = asyncio.run(
            storage_status(
                status_config,
                relational_path=_relational_path(status_config, data_dir),
                data_dir=data_dir if data_dir is not None else default_data_dir(),
            )
        )
        print("\n".join(lines), file=stdout)
        return 0

    # Every front end (plain, terminal UI, web GUI) drives the orchestrator.
    loaded = _load_config_and_model(config_dir, args.model, stdin, stderr)
    if isinstance(loaded, int):
        return loaded
    config, model = loaded
    try:
        tools = build_tool_registry(config.agents)
    except ConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return 1

    # One storage, one ledger and one router factory per process; nothing touches
    # the disk until first use.
    db_path = _relational_path(config, data_dir)
    storage = open_storage(
        config.storage,
        data_dir if data_dir is not None else default_data_dir(),
        relational_path=db_path,
    )
    store = Store(db_path)
    router_factory = make_router_factory(
        config,
        provider_factory,
        args.think,
        store,
        required_tags=frozenset({"orchestrator"}),
    )
    subagent_factory_maker = make_subagent_factory_maker(
        config, provider_factory, args.think, store
    )
    is_manual = bool(args.model)

    if args.gui:
        return run_gui(
            config,
            model,
            router_factory,
            is_manual,
            storage=storage,
            tools=tools,
            subagent_factory_maker=subagent_factory_maker,
        )
    if not args.plain:
        return run_tui(
            config,
            model,
            router_factory,
            is_manual,
            storage=storage,
            tools=tools,
            subagent_factory_maker=subagent_factory_maker,
        )

    try:
        router = router_factory(model, is_manual)
    except ConfigError as exc:
        print(f"error: {exc}", file=stderr)
        return 1

    try:
        orchestrator = Orchestrator(
            router,
            model,
            user_id=config.user_id,
            storage=storage,
            agent=find_orchestrator_agent(config),
            tools=tools,
            subagents=config.agents,
            subagent_factory=subagent_factory_maker(router),
        )
        return asyncio.run(
            _chat_loop_then_close(router, stdin, stdout, stderr, orchestrator, storage)
        )
    except KeyboardInterrupt:
        print(file=stderr)
        return 130
