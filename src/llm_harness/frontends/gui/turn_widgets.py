"""The turn view of the chat page: draws a turn into the page's widgets."""

from typing import TYPE_CHECKING

from nicegui import ui

from llm_harness.core.types import UiSurface
from llm_harness.frontends.gui.styles import _DIM
from llm_harness.frontends.markdown_render import render_html

if TYPE_CHECKING:
    from llm_harness.frontends.gui.chat_page import ChatPage


class _TurnWidgets:
    """Draws a turn into the page's widgets.

    Every call looks the page's method up at call time, so a test that replaces
    `page._show` sees the calls. Nothing is drawn once the page is closed.
    """

    def __init__(self, page: "ChatPage") -> None:
        self._page = page
        self._thinking: ui.label | None = None
        self._reply: ui.html | None = None

    async def thinking(self, text: str, is_first: bool) -> None:
        if is_first or self._thinking is None:
            self._thinking = self._page._show_thinking()
        self._thinking.set_text(text)

    async def reply(self, text: str, is_first: bool) -> None:
        if is_first or self._reply is None:
            self._reply = self._page._show_reply()
        self._reply.set_content(render_html(text))
        self._page._scroll_to_end()

    async def note(self, text: str) -> None:
        self._page._show(text, "note", _DIM)

    async def status(self, text: str) -> None:
        # A plain label, never markdown or html: the text is model-derived.
        self._page._show(text, "status", _DIM)

    async def surface(self, event: UiSurface) -> None:
        orchestrator = self._page._orchestrator
        if orchestrator is not None:
            self._page._draw_surfaces(orchestrator, event)

    async def usage(self, text: str) -> None:
        self._page._show(text, "usage", _DIM)

    async def error(self, text: str) -> None:
        if not self._page._is_closed:
            self._page._show(text, "error", "text-negative")

    def cancelled(self) -> None:
        if not self._page._is_closed:
            self._page._show("cancelled", "note", _DIM)

    def end_turn(self, restore_text: str | None) -> None:
        self._page._end_turn(restore_text)

    def refresh_inspector(self) -> None:
        self._page._refresh_inspector()

    async def announce_proposing(self) -> None:
        self._page._show("[memory: proposing...]", "note", _DIM)

    async def memory_answer(self, text: str) -> None:
        self._page._show(text, "memory", "w-full")

    def session_reset(self) -> None:
        self._page._surfaces.clear()  # old containers stay in the log, inert
        self._page._views.clear()
