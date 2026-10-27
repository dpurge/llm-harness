"""Terminal chat UI (Textual): streams one reply at a time from a ChatSession."""

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
)
from textual.worker import Worker, WorkerCancelled

from llm_harness.config import ConfigError, HarnessConfig, ModelConfig
from llm_harness.display import format_switch, format_usage_report, sanitize_text
from llm_harness.markdown_render import parser_factory
from llm_harness.providers.base import ChatProvider
from llm_harness.redact import redact
from llm_harness.router import RouterFactory
from llm_harness.session import ChatSession
from llm_harness.types import (
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    TextDelta,
    ThinkingDelta,
    UsageReport,
)

_QUIT_COMMANDS = frozenset({"/quit", "/exit"})
_PLACEHOLDER = "Message, Enter to send, /quit to exit"


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
    ) -> None:
        super().__init__()
        self._config = config
        self._model = model
        self._router_factory = router_factory
        # Whether the current model was picked by the user (--model or selector).
        self._is_manual = is_manual
        self._router: ChatProvider | None = None
        self._session: ChatSession | None = None
        self._turn: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Select(
            # Text, not str: Select parses a str prompt as markup, so a name such
            # as "[/]" would raise and "GPT [beta]" would lose its brackets.
            [(Text(model.name), model.id) for model in self._config.models],
            allow_blank=False,
            value=self._model.id,
            id="model",
        )
        yield VerticalScroll(id="log")
        yield Input(placeholder=_PLACEHOLDER, id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        await self._start_session(self._model, self._is_manual)
        self.query_one("#prompt", Input).focus()

    async def on_unmount(self) -> None:
        if self._router is not None:
            await self._router.aclose()

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
        self._session = ChatSession(router, model.model)
        if old_router is not None:
            await old_router.aclose()
        return True

    def _is_history_empty(self) -> bool:
        return self._session is None or not self._session.history

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
        if not self._is_history_empty():
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
        if self._turn_is_running():
            return
        prompt = self.query_one("#prompt", Input)
        if self._session is None:
            await self._show("no working model: pick another and press Ctrl+N", "error")
            return
        prompt.value = ""
        prompt.disabled = True
        await self._show(f"you: {sanitize_text(text)}", "user")
        self._turn = self.run_worker(self._run_turn(self._session, text), group="turn")

    def action_cancel_turn(self) -> None:
        if self._turn is not None and self._turn.is_running:
            self._turn.cancel()

    def _end_turn(self, restore_text: str | None = None) -> None:
        prompt = self.query_one("#prompt", Input)
        if restore_text is not None:
            prompt.value = restore_text
        prompt.disabled = False
        prompt.focus()

    async def _run_turn(self, session: ChatSession, text: str) -> None:
        report: UsageReport | None = None
        thinking: Static | None = None
        thinking_text = ""
        reply: Markdown | None = None
        reply_text = ""
        try:
            async for event in session.send(text):
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
) -> int:
    app = ChatApp(config, model, router_factory, is_manual)
    app.run()
    return app.return_code or 0
