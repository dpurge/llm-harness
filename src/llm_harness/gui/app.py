"""Web chat UI (NiceGUI): every browser client owns its router and Orchestrator.

The orchestrator streams the reply (and an `ask_user` question) as text, plus one
plain-text `step N: ...` note per step."""

import asyncio
import contextlib

from nicegui import app, ui

from llm_harness.config import (
    ConfigError,
    HarnessConfig,
    ModelConfig,
    chat_models,
    find_orchestrator_agent,
)
from llm_harness.display import format_switch, format_usage_report, sanitize_text
from llm_harness.gui import inspector
from llm_harness.gui.security import LoopbackOnlyMiddleware
from llm_harness.markdown_render import pygments_css, render_html, sanitize_html
from llm_harness.orchestrator import Orchestrator
from llm_harness.providers.base import ChatProvider
from llm_harness.redact import redact
from llm_harness.router import RouterFactory
from llm_harness.storage import Storage
from llm_harness.subagents import SubagentFactoryMaker
from llm_harness.tools import ToolRegistry
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

_QUIT_COMMAND = "/quit"
_PRE_WRAP = "whitespace-pre-wrap"
# Secondary text (thinking, usage line, notes) is a little smaller than the 18px base.
_DIM = "text-grey llm-secondary"
_MARKDOWN_CLASS = "llm-md"

# Full-viewport chat layout: the page never scrolls, only the message area does.
# Quasar's own sizes (14px buttons, inputs, expansion header) are raised to match.
_PAGE_CSS = """
html, body { overflow: hidden; }
.nicegui-content { height: 100dvh; padding: 12px; }
.llm-page { font-size: 18px; }
.llm-page .q-field__native, .llm-page .q-field__input,
.llm-page .q-field__label, .llm-page .q-field__marginal { font-size: 20px; }
.llm-page .q-btn { font-size: 18px; }
.llm-page .q-item__label, .llm-secondary { font-size: 16px; }
.llm-page .q-scrollarea__content { width: 100%; overflow-wrap: anywhere; }
"""

# The model reply is markdown rendered to sanitized HTML. Wide tables and code
# scroll inside their own box, so the page itself never grows sideways.
_MARKDOWN_CSS = """
.llm-md { line-height: 1.5; }
.llm-md > :first-child { margin-top: 0; }
.llm-md > :last-child { margin-bottom: 0; }
.llm-md h1, .llm-md h2, .llm-md h3, .llm-md h4, .llm-md h5, .llm-md h6 {
  font-weight: 600; line-height: 1.25; margin: 1em 0 0.4em;
}
.llm-md h1 { font-size: 1.5em; }
.llm-md h2 { font-size: 1.35em; }
.llm-md h3 { font-size: 1.2em; }
.llm-md h4, .llm-md h5, .llm-md h6 { font-size: 1.05em; }
.llm-md p, .llm-md ul, .llm-md ol, .llm-md blockquote { margin: 0.5em 0; }
.llm-md ul, .llm-md ol { padding-left: 1.6em; }
.llm-md ul { list-style: disc; }
.llm-md ol { list-style: decimal; }
.llm-md li.task-list-item { list-style: none; margin-left: -1.2em; }
.llm-md input[type=checkbox] { margin-right: 0.4em; }
.llm-md blockquote {
  border-left: 4px solid #bdbdbd; color: #616161; padding: 0 0 0 0.8em;
}
.llm-md table {
  display: block; width: auto; max-width: 100%; overflow-x: auto;
  border-collapse: collapse; margin: 0.6em 0;
}
.llm-md th, .llm-md td { border: 1px solid #bdbdbd; padding: 4px 10px; text-align: left; }
.llm-md th { background: #f5f5f5; font-weight: 600; }
.llm-md pre {
  background: #f5f5f5; border-radius: 4px; margin: 0.6em 0; padding: 8px 12px;
  overflow-x: auto; white-space: pre; overflow-wrap: normal;
}
.llm-md code, .llm-md pre {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 16px;
}
.llm-md :not(pre) > code { background: #f0f0f0; border-radius: 3px; padding: 1px 4px; }
.llm-md a { color: #1565c0; text-decoration: underline; }
.llm-md hr { border: 0; border-top: 1px solid #bdbdbd; margin: 1em 0; }
.llm-md .footnotes { font-size: 16px; }
""" + pygments_css(_MARKDOWN_CLASS)


# The pages that are still open, so a server shutdown can release their routers.
_open_pages: set["ChatPage"] = set()


def _unexpected_error_line(exc: Exception) -> str:
    """One safe line for an unexpected exception: never a traceback or locals."""
    return (
        f"error (unexpected): {type(exc).__name__}: {redact(sanitize_text(str(exc)))}"
    )


class ChatPage:
    """The state and widgets of ONE page load; never shared between clients.

    Every model-derived string is passed through `sanitize_text` first. All but
    the reply are shown in a `ui.label`, which sends a text node. The reply is
    markdown rendered to HTML and shown in a `ui.html` that applies our own
    server-side sanitizer (never `sanitize=True`/`False`); `ui.markdown` is not used.
    """

    def __init__(
        self,
        config: HarnessConfig,
        model: ModelConfig,
        router_factory: RouterFactory,
        is_manual: bool,
        storage: Storage | None = None,
        tools: ToolRegistry | None = None,
        subagent_factory_maker: SubagentFactoryMaker | None = None,
    ) -> None:
        self._storage = storage
        self._tools = tools
        self._subagent_factory_maker = subagent_factory_maker
        self._config = config
        self._model = model
        self._router_factory = router_factory
        # Whether the current model was picked by the user (--model or selector).
        self._is_manual = is_manual
        self._router: ChatProvider | None = None
        self._orchestrator: Orchestrator | None = None
        self._turn: asyncio.Task[None] | None = None
        self._is_closed = False

    async def build(self) -> None:
        client = ui.context.client
        ui.add_css(_PAGE_CSS)
        ui.add_css(_MARKDOWN_CSS)
        ui.add_css(inspector.INSPECTOR_CSS)
        with ui.column().classes("llm-page w-full h-full no-wrap").mark("page"):
            ui.label("llm-harness").classes("text-h4")
            tabs = inspector.build_tabs()
            with ui.tab_panels(tabs, value=inspector.CHAT_TAB).classes(
                inspector.PANELS_CLASSES
            ):
                with (
                    ui.tab_panel(inspector.CHAT_TAB)
                    .classes(inspector.PANEL_CLASSES)
                    .mark("panel-chat")
                ):
                    self._build_chat()
                self._state_panel = inspector.StatePanel(
                    lambda: self._orchestrator,
                    self._refresh_inspector,
                    self._config.agents,
                )
                self._trace_panel = inspector.TracePanel()
        # on_delete, not on_disconnect: NiceGUI 3 also calls on_disconnect for a
        # socket that is about to reconnect; on_delete fires once the reconnect
        # timeout has passed and the client is really gone.
        _open_pages.add(self)
        client.on_delete(self.close)
        await self._start_session(self._model, self._is_manual)

    def _build_chat(self) -> None:
        self._select = ui.select(
            {m.id: m.name for m in chat_models(self._config)},
            value=self._model.id,
            on_change=self._on_model_change,
        ).mark("model")
        self._select.classes("w-full")
        self._log = ui.scroll_area().classes("w-full grow border").mark("log")
        with ui.row().classes("w-full items-center gap-2"):
            self._prompt = (
                ui.input(placeholder="Message, Enter to send, /quit to exit")
                .classes("grow")
                .style("min-width: 12rem")
                .mark("prompt")
                .on("keydown.enter", self._on_send)
            )
            self._send = ui.button("Send", on_click=self._on_send).mark("send")
            self._stop = (
                ui.button("Stop", on_click=self._on_stop).mark("stop").props("flat")
            )
            self._new_chat = (
                ui.button("New chat", on_click=self._on_new_chat)
                .mark("new-chat")
                .props("flat")
            )
        self._stop.disable()

    async def close(self) -> None:
        """Cancel a running turn and release the router (the client is gone)."""
        self._is_closed = True
        _open_pages.discard(self)
        await self._cancel_turn()
        if self._router is not None:
            await self._router.aclose()
            self._router = None

    async def _cancel_turn(self) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn

    # Sessions

    async def _start_session(self, model: ModelConfig, is_manual: bool) -> bool:
        """Swap in a session for `model`; on any error keep the current one."""
        try:
            router = self._router_factory(model, is_manual)
        except ConfigError as exc:
            self._show(f"error: {sanitize_text(str(exc))}", "error", "text-negative")
            return False
        except Exception as exc:  # noqa: BLE001 - shown as one safe line, never a traceback
            self._show(_unexpected_error_line(exc), "error", "text-negative")
            return False
        old_router = self._router
        self._router = router
        self._model = model
        self._is_manual = is_manual
        self._orchestrator = Orchestrator(
            router,
            model,
            user_id=self._config.user_id,
            storage=self._storage,
            agent=find_orchestrator_agent(self._config),
            tools=self._tools,
            subagents=self._config.agents,
            subagent_factory=(
                self._subagent_factory_maker(router)
                if self._subagent_factory_maker is not None
                else None
            ),
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

    async def _on_model_change(self) -> None:
        model = self._model_by_id(str(self._select.value))
        if model.id == self._model.id or self._turn_is_running():
            return
        if not self._is_selector_unlocked():
            ui.notify("applies to the next new chat")
        elif not await self._start_session(model, True):
            self._select.value = self._model.id

    async def _on_new_chat(self) -> None:
        if self._turn_is_running():
            return
        model = self._model_by_id(str(self._select.value))
        # A changed selector is a pick by the user; otherwise keep the old flag.
        is_manual = self._is_manual or model.id != self._model.id
        if await self._start_session(model, is_manual):
            self._log.clear()

    # Turns

    def _turn_is_running(self) -> bool:
        return self._turn is not None and not self._turn.done()

    def _show(self, text: str, marker: str, classes: str = "") -> ui.label:
        with self._log:
            label = ui.label(text).classes(f"{_PRE_WRAP} {classes}").mark(marker)
        self._scroll_to_end()
        return label

    def _show_reply(self) -> ui.html:
        with self._log:
            reply = (
                ui.html("", sanitize=sanitize_html)
                .classes(f"w-full {_MARKDOWN_CLASS}")
                .mark("reply")
            )
        self._scroll_to_end()
        return reply

    def _scroll_to_end(self) -> None:
        self._log.scroll_to(percent=1.0)

    def _refresh_inspector(self) -> None:
        """Redraw the State and Trace tabs from the CURRENT orchestrator."""
        if not self._is_closed:
            self._state_panel.refresh(
                self._orchestrator, self._model, self._prompt.value or ""
            )
            self._trace_panel.refresh(self._orchestrator)

    def _set_busy(self, is_busy: bool) -> None:
        for control in (self._prompt, self._send, self._new_chat):
            control.set_enabled(not is_busy)
        self._stop.set_enabled(is_busy)
        self._state_panel.transfer.set_busy(is_busy)

    async def _on_send(self) -> None:
        text = self._prompt.value or ""
        if not text.strip():
            return
        if text.strip().lower() == _QUIT_COMMAND:
            await self._quit()
            return
        if self._turn_is_running():
            return
        if self._orchestrator is None:
            self._show(
                "no working model: pick another and press New chat",
                "error",
                "text-negative",
            )
            return
        self._prompt.value = ""
        self._set_busy(True)
        self._show(f"you: {sanitize_text(text)}", "user", "text-bold")
        self._turn = asyncio.create_task(self._run_turn(self._orchestrator, text))

    async def _quit(self) -> None:
        await self._cancel_turn()
        self._prompt.value = ""
        ui.notify("Shutting down")
        app.shutdown()

    def _on_stop(self) -> None:
        if self._turn_is_running() and self._turn is not None:
            self._turn.cancel()

    def _end_turn(self, restore_text: str | None) -> None:
        if self._is_closed:
            return
        if restore_text is not None:
            self._prompt.value = restore_text
        self._set_busy(False)
        # A disabled input loses focus; give it back so the user can type at once.
        self._prompt.run_method("focus")
        self._refresh_inspector()

    async def _run_turn(self, orchestrator: Orchestrator, text: str) -> None:
        report: UsageReport | None = None
        thinking: ui.label | None = None
        thinking_text = ""
        reply: ui.html | None = None
        reply_text = ""
        restore_text: str | None = None
        try:
            async for event in orchestrator.send(text):
                if isinstance(event, ThinkingDelta):
                    thinking_text += sanitize_text(event.text)
                    if thinking is None:
                        thinking = self._show_thinking()
                    thinking.set_text(thinking_text)
                elif isinstance(event, TextDelta):
                    reply_text += sanitize_text(event.text)
                    if reply is None:
                        reply = self._show_reply()
                    reply.set_content(render_html(reply_text))
                    self._scroll_to_end()
                elif isinstance(event, ModelSwitch):
                    self._show(format_switch(event), "note", _DIM)
                elif isinstance(event, StatusEvent):
                    self._refresh_inspector()
                    # A plain label, never markdown or html: the text is model-derived.
                    self._show(
                        f"step {event.step}: {sanitize_text(event.text)}",
                        "status",
                        _DIM,
                    )
                elif isinstance(event, Notice):
                    self._show(sanitize_text(event.text), "note", _DIM)
                elif isinstance(event, UsageReport):
                    report = event
                elif isinstance(event, Done):
                    if report is not None:
                        self._show(format_usage_report(report), "usage", _DIM)
                elif isinstance(event, ErrorEvent):
                    message = sanitize_text(event.message)
                    self._show(
                        f"error ({event.kind}): {message}", "error", "text-negative"
                    )
                    restore_text = text
        except asyncio.CancelledError:
            if not self._is_closed:
                self._show("cancelled", "note", _DIM)
            restore_text = text
            raise
        except Exception as exc:  # noqa: BLE001 - shown as one safe line, never a traceback
            # Unhandled, the task would die silently with the input disabled.
            if not self._is_closed:
                self._show(_unexpected_error_line(exc), "error", "text-negative")
            restore_text = text
        finally:
            self._end_turn(restore_text)

    def _show_thinking(self) -> ui.label:
        with self._log, ui.expansion("Thinking", value=False).classes("w-full"):
            label = ui.label("").classes(f"{_PRE_WRAP} {_DIM}").mark("thinking")
        return label


async def _close_open_pages() -> None:
    """On server shutdown: pages whose client was not deleted yet still own a router."""
    for page in list(_open_pages):
        await page.close()


def register_page(
    config: HarnessConfig,
    model: ModelConfig,
    router_factory: RouterFactory,
    is_manual: bool,
    storage: Storage | None = None,
    tools: ToolRegistry | None = None,
    subagent_factory_maker: SubagentFactoryMaker | None = None,
) -> None:
    """Register the chat page; its builder runs once per browser client."""

    @ui.page("/")
    async def chat_page() -> None:
        page = ChatPage(
            config,
            model,
            router_factory,
            is_manual,
            storage,
            tools,
            subagent_factory_maker,
        )
        await page.build()


def run_gui(
    config: HarnessConfig,
    model: ModelConfig,
    router_factory: RouterFactory,
    is_manual: bool,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    show: bool = True,
    storage: Storage | None = None,
    tools: ToolRegistry | None = None,
    subagent_factory_maker: SubagentFactoryMaker | None = None,
) -> int:
    register_page(
        config, model, router_factory, is_manual, storage, tools, subagent_factory_maker
    )
    # Before ui.run: Starlette refuses new middleware once the app has started.
    # NiceGUI mounts socket.io on this same app, so the middleware covers it too.
    app.add_middleware(LoopbackOnlyMiddleware, port=port)
    # host is explicit: NiceGUI's own default is 0.0.0.0. reload=False because
    # reload would re-import this module in a child process.
    app.on_shutdown(_close_open_pages)
    if storage is not None:
        # After the pages: their orchestrators may still be using the storage.
        app.on_shutdown(storage.close)
    try:
        ui.run(host=host, port=port, reload=False, show=show, title="llm-harness")
    except KeyboardInterrupt:
        # uvicorn finishes a graceful shutdown and then re-raises Ctrl-C.
        return 130
    return 0
