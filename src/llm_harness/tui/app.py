"""Terminal chat UI (Textual): streams one reply at a time from an Orchestrator."""

import asyncio
import contextlib
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import (
    Collapsible,
    Footer,
    Header,
    Input,
    Markdown,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker, WorkerCancelled

from llm_harness.config import ConfigError, HarnessConfig, ModelConfig, chat_models
from llm_harness.display import format_switch, format_usage_report, sanitize_text
from llm_harness.markdown_render import parser_factory
from llm_harness.orchestrator import Orchestrator
from llm_harness.providers.base import ChatProvider
from llm_harness.redact import redact
from llm_harness.router import RouterFactory
from llm_harness.state_files import (
    EXPORT_COMMAND,
    IMPORT_USAGE,
    StateCommand,
    StateFileError,
    parse_command,
    read_import,
    resolve_path,
    safe_line,
    write_export,
)
from llm_harness.state_io import StateImportError, export_yaml, import_yaml
from llm_harness.storage import Storage
from llm_harness.tui.state_tab import StatePanel
from llm_harness.tui.trace_tab import TracePanel
from llm_harness.types import (
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    StatusEvent,
    TextDelta,
    ThinkingDelta,
    UsageReport,
)

_QUIT_COMMANDS = frozenset({"/quit", "/exit"})
_IMPORT_BUSY = "import failed: a turn is running, try again after it"
_PLACEHOLDER = "Message, Enter to send, /export, /import <path>, /quit to exit"
CHAT_TAB = "tab-chat"
STATE_TAB = "tab-state"
TRACE_TAB = "tab-trace"


def _unexpected_error_line(exc: Exception) -> str:
    """One safe line for an unexpected exception: never a traceback or locals."""
    return (
        f"error (unexpected): {type(exc).__name__}: {redact(sanitize_text(str(exc)))}"
    )


class ChatApp(App[int]):
    TITLE = "llm-harness"
    CSS = """
    .user { margin-top: 1; text-style: bold; }
    .thinking { color: $text-muted; text-style: dim; }
    .usage { color: $text-muted; text-style: dim; }
    .note { color: $text-muted; }
    .error { color: $error; text-style: bold; }
    .secondary { color: $text-muted; }
    .heading { text-style: bold; margin-top: 1; }
    /* Tabs, panes and panels fill the width and the height left under the tab
       strip; the log and the state panel scroll inside, nothing has a fixed size. */
    TabbedContent { width: 1fr; height: 1fr; }
    TabbedContent > ContentSwitcher { width: 1fr; height: 1fr; }
    TabPane { width: 1fr; height: 1fr; padding: 0; }
    #log { width: 1fr; height: 1fr; }
    #state-panel { width: 1fr; height: 1fr; }
    #trace-panel { width: 1fr; height: 1fr; }
    #prompt { width: 1fr; }
    #trace-tree { width: 1fr; height: auto; }
    .trace-error > CollapsibleTitle { color: $error; }
    .trace-rejected > CollapsibleTitle { color: $warning; }
    .trace-cancelled > CollapsibleTitle { color: $text-muted; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel_turn", "Cancel"),
        Binding("ctrl+n", "new_chat", "New chat"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config: HarnessConfig,
        model: ModelConfig,
        router_factory: RouterFactory,
        is_manual: bool,
        storage: Storage | None = None,
    ) -> None:
        super().__init__()
        self._storage = storage
        self._config = config
        self._model = model
        self._router_factory = router_factory
        # Whether the current model was picked by the user (--model or selector).
        self._is_manual = is_manual
        self._router: ChatProvider | None = None
        self._orchestrator: Orchestrator | None = None
        self._turn: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        # The TabPane ids are the tab ids the criteria name (`tab-chat`, ...).
        # Textual gives the clickable Tab widget the id `--content-tab-<pane id>`
        # and `TabbedContent.active` is the pane id, so the pane id is the one
        # to look up and to compare.
        with TabbedContent(initial=CHAT_TAB):
            with TabPane("Chat", id=CHAT_TAB):
                yield Select(
                    # Text, not str: Select parses a str prompt as markup, so a name
                    # as "[/]" would raise and "GPT [beta]" would lose its brackets.
                    [
                        (Text(model.name), model.id)
                        for model in chat_models(self._config)
                    ],
                    allow_blank=False,
                    value=self._model.id,
                    id="model",
                )
                yield VerticalScroll(id="log")
                yield Input(placeholder=_PLACEHOLDER, id="prompt")
            with TabPane("State", id=STATE_TAB):
                yield StatePanel(id="state-panel")
            with TabPane("Trace", id=TRACE_TAB):
                yield TracePanel(id="trace-panel")
        yield Footer()

    async def on_mount(self) -> None:
        await self._start_session(self._model, self._is_manual)
        self.query_one("#prompt", Input).focus()

    async def on_unmount(self) -> None:
        if self._router is not None:
            await self._router.aclose()
        if self._storage is not None:
            await self._storage.close()

    # Sessions

    async def _start_session(self, model: ModelConfig, is_manual: bool) -> bool:
        """Swap in a session for `model`; on any error keep the current one."""
        try:
            router = self._router_factory(model, is_manual)
        except ConfigError as exc:
            await self._show(f"error: {sanitize_text(str(exc))}", "error")
            return False
        except Exception as exc:  # noqa: BLE001 - shown as one safe line, never a traceback
            await self._show(_unexpected_error_line(exc), "error")
            return False
        old_router = self._router
        self._router = router
        self._model = model
        self._is_manual = is_manual
        self._orchestrator = Orchestrator(
            router, model, user_id=self._config.user_id, storage=self._storage
        )
        if old_router is not None:
            await old_router.aclose()
        self._refresh_inspector()
        return True

    def _is_selector_unlocked(self) -> bool:
        # A failed or cancelled first turn leaves the selector free: only a
        # completed turn locks the model for this chat.
        return self._orchestrator is None or not self._orchestrator.has_completed_turn

    def _model_by_id(self, model_id: str) -> ModelConfig:
        return next(m for m in self._config.models if m.id == model_id)

    def _select_model_in_widget(self, model: ModelConfig) -> None:
        # Without prevent(), setting the value would fire Changed and recurse.
        select = self.query_one("#model", Select)
        with select.prevent(Select.Changed):
            select.value = model.id

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.value is Select.NULL or self._turn_is_running():
            return
        model = self._model_by_id(str(event.value))
        if model.id == self._model.id:
            return
        if not self._is_selector_unlocked():
            await self._show("applies to the next new chat (Ctrl+N)", "note")
        elif not await self._start_session(model, True):
            self._select_model_in_widget(self._model)

    async def action_new_chat(self) -> None:
        if self._turn_is_running():
            return
        model = self._model_by_id(str(self.query_one("#model", Select).value))
        # A changed selector is a pick by the user; otherwise keep the old flag.
        is_manual = self._is_manual or model.id != self._model.id
        if await self._start_session(model, is_manual):
            await self._chat_log.remove_children()

    # Turns

    @property
    def _chat_log(self) -> VerticalScroll:
        return self.query_one("#log", VerticalScroll)

    def _turn_is_running(self) -> bool:
        return self._turn is not None and self._turn.is_running

    async def _show(self, text: str, css_class: str) -> Static:
        # markup=False: model text must never be parsed as console markup.
        widget = Static(text, markup=False, classes=css_class)
        await self._chat_log.mount(widget)
        self._chat_log.scroll_end(animate=False)
        return widget

    async def _show_reply(self) -> Markdown:
        # open_links=False: a link in model text must not open anything. Raw HTML
        # is off in parser_factory, so tags show as text; Textual builds styles
        # from tokens, never from Rich markup, so "[bold]" stays literal too.
        widget = Markdown(
            "", parser_factory=parser_factory, open_links=False, classes="reply"
        )
        await self._chat_log.mount(widget)
        self._chat_log.scroll_end(animate=False)
        return widget

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        if not text.strip():
            return
        if text.strip().lower() in _QUIT_COMMANDS:
            self.action_cancel_turn()
            # The cancelled turn still writes to the log; let it finish first.
            with contextlib.suppress(WorkerCancelled):
                await self.workers.wait_for_complete()
            await self.action_quit()
            return
        command = parse_command(text)
        if command is not None:
            await self._run_state_command(command)
            return
        if self._turn_is_running():
            return
        prompt = self.query_one("#prompt", Input)
        if self._orchestrator is None:
            await self._show("no working model: pick another and press Ctrl+N", "error")
            return
        prompt.value = ""
        prompt.disabled = True
        await self._show(f"you: {sanitize_text(text)}", "user")
        self._turn = self.run_worker(
            self._run_turn(self._orchestrator, text), group="turn"
        )

    async def _run_state_command(self, command: StateCommand) -> None:
        """`/export` and `/import`: local, never sent to the model, never echoed.

        Success clears the prompt; a failure keeps the typed command so the path
        can be fixed. Export only reads the snapshot, so it may run during a turn;
        import is refused then.
        """
        prompt = self.query_one("#prompt", Input)
        if command.name == EXPORT_COMMAND:
            outcome = self._export_state(command.argument)
        else:
            outcome = self._import_state(command.argument)
        succeeded, line = outcome
        if not succeeded:
            await self._show(line, "error")
            return
        await self._show(line, "note")
        prompt.value = ""
        if not prompt.disabled:
            prompt.focus()

    def _export_state(self, argument: str) -> tuple[bool, str]:
        if self._orchestrator is None:
            return False, "export failed: no session"
        path = resolve_path(argument)
        try:
            write_export(path, export_yaml(self._orchestrator.snapshot()))
        except StateFileError as exc:
            return False, f"export failed: {exc}"
        return True, f"state exported to {safe_line(str(path))}"

    def _import_state(self, argument: str) -> tuple[bool, str]:
        if not argument:
            return False, IMPORT_USAGE
        orchestrator = self._orchestrator
        if orchestrator is None:
            return False, "import failed: no session"
        if self._turn_is_running():
            return False, _IMPORT_BUSY
        path = resolve_path(argument)
        try:
            run_state = import_yaml(read_import(path))
            orchestrator.restore(run_state)
        except (StateFileError, StateImportError) as exc:
            return False, f"import failed: {safe_line(str(exc))}"
        except RuntimeError:
            return False, _IMPORT_BUSY
        self._refresh_inspector()
        return True, f"state loaded from {safe_line(str(path))}"

    def action_cancel_turn(self) -> None:
        if self._turn is not None and self._turn.is_running:
            self._turn.cancel()

    def _refresh_inspector(self) -> None:
        """Redraw the State and Trace tabs from the CURRENT orchestrator."""
        prompt = self.query_one("#prompt", Input)
        self.query_one("#state-panel", StatePanel).refresh_state(
            self._orchestrator, self._model, prompt.value
        )
        self.query_one("#trace-panel", TracePanel).refresh_trace(self._orchestrator)

    def _end_turn(self, restore_text: str | None = None) -> None:
        prompt = self.query_one("#prompt", Input)
        if restore_text is not None:
            prompt.value = restore_text
        prompt.disabled = False
        prompt.focus()
        self._refresh_inspector()

    async def _run_turn(self, orchestrator: Orchestrator, text: str) -> None:
        report: UsageReport | None = None
        thinking: Static | None = None
        thinking_text = ""
        reply: Markdown | None = None
        reply_text = ""
        try:
            async for event in orchestrator.send(text):
                if isinstance(event, ThinkingDelta):
                    thinking_text += sanitize_text(event.text)
                    if thinking is None:
                        thinking = await self._show_thinking()
                    thinking.update(thinking_text)
                elif isinstance(event, TextDelta):
                    reply_text += sanitize_text(event.text)
                    if reply is None:
                        reply = await self._show_reply()
                    await reply.update(reply_text)
                    self._chat_log.scroll_end(animate=False)
                elif isinstance(event, ModelSwitch):
                    await self._show(format_switch(event), "note")
                elif isinstance(event, StatusEvent):
                    self._refresh_inspector()
                    await self._show(
                        f"step {event.step}: {sanitize_text(event.text)}", "note"
                    )
                elif isinstance(event, Notice):
                    await self._show(sanitize_text(event.text), "note")
                elif isinstance(event, UsageReport):
                    report = event
                elif isinstance(event, Done):
                    if report is not None:
                        await self._show(format_usage_report(report), "usage")
                    self._end_turn()
                elif isinstance(event, ErrorEvent):
                    message = sanitize_text(event.message)
                    await self._show(f"error ({event.kind}): {message}", "error")
                    self._end_turn(restore_text=text)
        except asyncio.CancelledError:
            # Only synchronous work here: the task is already being cancelled.
            self._chat_log.mount(Static("cancelled", markup=False, classes="note"))
            self._end_turn(restore_text=text)
            raise
        except Exception as exc:  # noqa: BLE001 - shown as one safe line, never a traceback
            # Unhandled, the worker would crash the app and Textual's crash
            # report prints frame locals (prompt text, history, the bearer token).
            await self._show(_unexpected_error_line(exc), "error")
            self._end_turn(restore_text=text)

    async def _show_thinking(self) -> Static:
        body = Static("", markup=False, classes="thinking")
        await self._chat_log.mount(Collapsible(body, title="Thinking", collapsed=True))
        return body


def run_tui(
    config: HarnessConfig,
    model: ModelConfig,
    router_factory: RouterFactory,
    is_manual: bool,
    *,
    storage: Storage | None = None,
) -> int:
    app = ChatApp(config, model, router_factory, is_manual, storage)
    app.run()
    return app.return_code or 0
