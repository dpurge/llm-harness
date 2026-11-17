"""The chat page: the state and widgets of one browser page load.

The orchestrator streams the reply (and an `ask_user` question) as text, plus one
plain-text `step N: ...` note per step."""

import asyncio
import contextlib
from functools import partial

from nicegui import app, ui

from llm_harness.a2ui.demo import DEMO_MESSAGES, DEMO_SURFACE_ID
from llm_harness.a2ui.validation import SurfaceValidator
from llm_harness.chat.commands import GUI, UI_DEMO_COMMAND, help_answer, parse_help
from llm_harness.chat.session import ChatSession
from llm_harness.config.settings import (
    HarnessConfig,
    ModelConfig,
    chat_models,
)
from llm_harness.core.approval import ApprovalRequest
from llm_harness.core.display import sanitize_text
from llm_harness.core.orchestrator import Orchestrator
from llm_harness.core.subagents import SubagentFactoryMaker
from llm_harness.core.types import UiSurface
from llm_harness.frontends.gui import inspector
from llm_harness.frontends.gui.a2ui.render import render_surface
from llm_harness.frontends.gui.styles import (
    _DIM,
    _MARKDOWN_CLASS,
    _MARKDOWN_CSS,
    _PAGE_CSS,
    _PRE_WRAP,
)
from llm_harness.frontends.gui.turn_widgets import _TurnWidgets
from llm_harness.frontends.markdown_render import sanitize_html
from llm_harness.memory.command import SLOW_ACTIONS, parse_memory_command
from llm_harness.models.router import RouterFactory
from llm_harness.storage import Storage
from llm_harness.tools.registry import ToolRegistry

_QUIT_COMMAND = "/quit"
# /exit quits as well (the tui and plain mode do); /help lists only /quit here.
_QUIT_WORDS = frozenset({_QUIT_COMMAND, "/exit"})
_NO_MODEL = "no working model: pick another and press New chat"

# The pages that are still open, so a server shutdown can release their routers.
_open_pages: set["ChatPage"] = set()


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
        self._config = config
        self._session = ChatSession(
            config,
            router_factory,
            model,
            is_manual,
            storage=storage,
            tools=tools,
            subagent_factory_maker=subagent_factory_maker,
            approver=self._approve_call,
            view=_TurnWidgets(self),
            no_model=_NO_MODEL,
        )
        self._turn: asyncio.Task[None] | None = None
        # surface id -> its container in the log, for the current session only
        self._surfaces: dict[str, ui.column] = {}
        # The user's choices in a drawn surface (selected tab), kept over redraws.
        self._views: dict[str, dict[str, str]] = {}
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
        await self._session.start(self._model, self._is_manual)

    # Read by the widgets and the tests; the session owns them.

    @property
    def _orchestrator(self) -> Orchestrator | None:
        return self._session.orchestrator

    @property
    def _model(self) -> ModelConfig:
        return self._session.model

    @property
    def _is_manual(self) -> bool:
        return self._session.is_manual

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
                ui.input(
                    placeholder="Message, Enter to send, /help for commands, /quit to exit"
                )
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
        await self._session.aclose()

    async def _cancel_turn(self) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn

    # Sessions

    async def _on_model_change(self) -> None:
        model = self._session.model_by_id(str(self._select.value))
        if model.id == self._model.id or self._turn_is_running():
            return
        if not self._session.is_selector_unlocked():
            ui.notify("applies to the next new chat")
        elif not await self._session.start(model, True):
            self._select.value = self._model.id

    async def _on_new_chat(self) -> None:
        if self._turn_is_running():
            return
        model = self._session.model_by_id(str(self._select.value))
        is_manual = self._session.new_chat_is_manual(model)
        if await self._session.start(model, is_manual):
            self._log.clear()

    # Turns

    def _turn_is_running(self) -> bool:
        return self._turn is not None and not self._turn.done()

    def _show(self, text: str, marker: str, classes: str = "") -> ui.label:
        with self._log:
            label = ui.label(text).classes(f"{_PRE_WRAP} {classes}").mark(marker)
        self._scroll_to_end()
        return label

    async def _approve_call(self, request: ApprovalRequest) -> bool:
        """Ask the human on THIS page; only the two buttons can answer.

        Everything shown is a plain label (never html or markdown): the arguments
        come from the model. Stop or closing the page cancels the turn, which
        cancels the wait here; the buttons are removed in `finally`.
        """
        decision: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        with self._log:
            block = ui.column().classes("w-full gap-1 border").mark("approval")
        with block:
            ui.label("Approval needed").classes("text-bold").mark("approval-title")
            for text, marker in (
                (f"tool: {sanitize_text(request.tool)}", "approval-tool"),
                (f"risk: {request.risk}", "approval-risk"),
                (f"agent: {sanitize_text(request.agent_id)}", "approval-agent"),
            ):
                ui.label(text).classes(_PRE_WRAP).mark(marker)
            ui.label(f"arguments: {request.arguments_text}").classes(
                f"{_PRE_WRAP} w-full"
            ).mark("approval-args")
            buttons = ui.row().classes("w-full no-wrap gap-4").mark("approval-buttons")
            with buttons:
                for caption, verdict, marker, color in (
                    ("Approve", True, "approve", "positive"),
                    ("Deny", False, "deny", "negative"),
                ):
                    ui.button(
                        caption,
                        color=color,
                        on_click=lambda verdict=verdict: self._answer(
                            decision, verdict
                        ),
                    ).classes("grow").props("size=lg").mark(marker)
        self._scroll_to_end()
        try:
            approved = await decision
            if not self._is_closed:
                with block:
                    ui.label("approved" if approved else "denied").classes(
                        f"{_PRE_WRAP} {_DIM}"
                    ).mark("approval-note")
            return approved
        finally:
            if not self._is_closed:
                buttons.delete()
                self._scroll_to_end()

    @staticmethod
    def _answer(decision: asyncio.Future[bool], verdict: bool) -> None:
        """A button click; a second click or one after a cancel does nothing."""
        if not decision.done():
            decision.set_result(verdict)

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
        if text.strip().lower() in _QUIT_WORDS:
            await self._quit()
            return
        help_request = parse_help(text)
        if help_request is not None:
            self._show(help_answer(GUI, help_request), "help", "w-full")
            self._prompt.value = ""
            return
        if text.strip().lower() == UI_DEMO_COMMAND:
            self._show_ui_demo()
            self._prompt.value = ""
            return
        memory_command = parse_memory_command(text.strip())
        if memory_command is not None and memory_command.action in SLOW_ACTIONS:
            self._start_memory_add(text.strip())
            return
        memory_answer = await self._session.memory.answer(text.strip())
        if memory_answer is not None:
            self._show(memory_answer, "memory", "w-full")
            self._prompt.value = ""
            return
        if self._turn_is_running():
            return
        if self._orchestrator is None:
            self._show(_NO_MODEL, "error", "text-negative")
            return
        self._prompt.value = ""
        self._set_busy(True)
        self._show(f"you: {sanitize_text(text)}", "user", "text-bold")
        self._turn = asyncio.create_task(self._session.run_turn(text))

    def _show_ui_demo(self) -> None:
        """Draw the fixed demo surface after it passes the A2UI validator."""
        validator = SurfaceValidator()
        outcome = validator.submit(DEMO_MESSAGES)
        if not outcome.ok:
            for error in outcome.errors:
                self._show(f"error: {sanitize_text(error)}", "error", "text-negative")
            return
        with self._log:
            render_surface(validator, DEMO_SURFACE_ID, self._show_ui_action)
        self._scroll_to_end()

    def _show_ui_action(
        self, surface_id: str, action_name: str, context: dict[str, object]
    ) -> None:
        self._show(f"ui_action: surface={surface_id} action={action_name}", "ui-action")

    def _start_memory_add(self, text: str) -> None:
        """`/memory add` calls the model: it runs in the turn slot so Stop cancels it."""
        if self._turn_is_running():
            return
        self._prompt.value = ""
        self._set_busy(True)
        self._turn = asyncio.create_task(
            self._session.run_memory_add(text, self._orchestrator)
        )

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

    def _on_ui_click(
        self,
        orchestrator: Orchestrator,
        surface_id: str,
        action_name: str,
        context: dict[str, object],
    ) -> None:
        """A click on a model-drawn button starts a turn on the orchestrator that drew it."""
        if self._turn_is_running():
            self._show("[turn running: click ignored]", "note", _DIM)
            return
        if orchestrator is not self._orchestrator:
            # A model switch replaces the session without clearing the log.
            self._show("[surface of an earlier session: click ignored]", "note", _DIM)
            return
        self._set_busy(True)
        self._show(
            f"ui_action: surface={sanitize_text(surface_id)} "
            f"action={sanitize_text(action_name)}",
            "ui-action",
        )
        self._turn = asyncio.create_task(
            self._session.run_turn(
                "",
                lambda: orchestrator.send_ui_action(surface_id, action_name, context),
            )
        )

    def _draw_surfaces(self, orchestrator: Orchestrator, event: UiSurface) -> None:
        """Bring the log in line with the validator's CURRENT state of each touched surface."""
        validator = orchestrator.ui_validator
        for surface_id in dict.fromkeys(
            (*event.surface_ids, *event.deleted_surface_ids)
        ):
            container = self._surfaces.pop(surface_id, None)
            if surface_id not in validator.surface_ids:
                if container is not None:
                    container.delete()
                    self._views.pop(surface_id, None)
                continue
            if container is None:
                with self._log:
                    container = ui.column().classes("w-full gap-2").mark("a2ui-surface")
            self._surfaces[surface_id] = container
            self._fill_surface(orchestrator, surface_id, container)
        self._scroll_to_end()

    def _fill_surface(
        self, orchestrator: Orchestrator, surface_id: str, container: ui.column
    ) -> None:
        """(Re)draw one surface inside its container; also the redraw after a user edit."""
        if (
            container.is_deleted
            or surface_id not in orchestrator.ui_validator.surface_ids
        ):
            return
        container.clear()
        with container:
            render_surface(
                orchestrator.ui_validator,
                surface_id,
                partial(self._on_ui_click, orchestrator),
                on_edit=partial(
                    self._fill_surface, orchestrator, surface_id, container
                ),
                view=self._views.setdefault(surface_id, {}),
                media_hosts=tuple(self._config.gui.a2ui_media_hosts),
            )

    def _show_thinking(self) -> ui.label:
        with self._log, ui.expansion("Thinking", value=False).classes("w-full"):
            label = ui.label("").classes(f"{_PRE_WRAP} {_DIM}").mark("thinking")
        return label
