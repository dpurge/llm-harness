"""The Trace tab of the terminal chat: the last turn's spans as nested collapsibles.

Spans appear while a turn runs, so the tree is rebuilt on each refresh. The new
tree is mounted before the old one is removed (no empty frame), and the rebuilds
run one at a time on the panel's own message queue. Which collapsibles the user
opened or closed is remembered by span id and field and applied again; the
memory is reset when the turn or the orchestrator changes, because span ids
repeat from one turn to the next.

Every model-derived string is sanitised in `inspect_state` and shown in a
`Static(..., markup=False)`. A `Collapsible` title is a `str` that Textual parses
as markup, so titles are passed as `Content`, which is taken literally.
"""

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from llm_harness.inspect_state import (
    OPEN_BY_DEFAULT,
    long_field_keys,
    long_field_text,
    span_children,
    span_details_text,
    span_title,
)
from llm_harness.orchestrator import Orchestrator
from llm_harness.trace import Span

_NO_TRACE = "No trace yet."


class TraceCollapsible(Collapsible):
    """A span or a long field; `key` is what the open state is remembered by."""

    def __init__(
        self, *children: Widget, key: str, title: Content, collapsed: bool
    ) -> None:
        super().__init__(*children, title=title, collapsed=collapsed)  # type: ignore[arg-type]
        self.key = key


def _body(text: str, classes: str | None = None) -> Static:
    return Static(text, markup=False, classes=classes)


class TracePanel(VerticalScroll):
    """The span tree of the last turn; `No trace yet.` when there is none."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._is_open: dict[str, bool] = {}
        self._turn_key: tuple[Orchestrator, int] | None = None

    def compose(self) -> ComposeResult:
        yield Static(_NO_TRACE, markup=False, id="trace-empty", classes="secondary")
        yield Vertical(id="trace-tree")

    def refresh_trace(self, orchestrator: Orchestrator | None) -> None:
        """Rebuild the tree from `orchestrator.last_trace`, after earlier rebuilds."""
        self.call_later(self._rebuild, orchestrator)

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        self._remember(event.collapsible, is_open=True)

    def on_collapsible_collapsed(self, event: Collapsible.Collapsed) -> None:
        self._remember(event.collapsible, is_open=False)

    def _remember(self, collapsible: Collapsible, *, is_open: bool) -> None:
        if isinstance(collapsible, TraceCollapsible):
            self._is_open[collapsible.key] = is_open

    async def _rebuild(self, orchestrator: Orchestrator | None) -> None:
        # Read the trace now, not when it was asked for: the latest wins.
        spans = orchestrator.last_trace if orchestrator is not None else None
        self.query_one("#trace-empty", Static).display = not spans
        tree = self.query_one("#trace-tree", Vertical)
        old = list(tree.children)
        new: list[Collapsible] = []
        if spans and orchestrator is not None:
            turn_key = (orchestrator, orchestrator.state.turn)
            if not self._is_same_turn(turn_key):
                self._is_open.clear()
                self._turn_key = turn_key
            grouped = span_children(spans)
            new = [self._span(root, grouped) for root in grouped.get(None, [])]
        else:
            self._is_open.clear()
            self._turn_key = None
        if new:
            await tree.mount(*new)
        if old:
            await tree.remove_children(old)

    def _is_same_turn(self, turn_key: tuple[Orchestrator, int]) -> bool:
        previous = self._turn_key
        return (
            previous is not None
            and previous[0] is turn_key[0]
            and previous[1] == turn_key[1]
        )

    def _collapsible(
        self, title: str, key: str, body: list[Widget], *, default: bool
    ) -> TraceCollapsible:
        is_open = self._is_open.get(key, default)
        # Content, not str: a str title would be parsed as markup.
        return TraceCollapsible(
            *body, key=key, title=Content(title), collapsed=not is_open
        )

    def _span(
        self, span: Span, grouped: dict[str | None, list[Span]]
    ) -> TraceCollapsible:
        body: list[Widget] = [_body(span_details_text(span), classes="trace-details")]
        body += [self._span(child, grouped) for child in grouped.get(span.id, [])]
        body += [self._long_field(span, key) for key in long_field_keys(span)]
        collapsible = self._collapsible(
            span_title(span), span.id, body, default=span.kind in OPEN_BY_DEFAULT
        )
        collapsible.add_class("trace-span", f"trace-{span.status}")
        return collapsible

    def _long_field(self, span: Span, key: str) -> TraceCollapsible:
        collapsible = self._collapsible(
            key,
            f"{span.id}/{key}",
            [_body(long_field_text(span, key), classes="trace-text")],
            default=False,
        )
        collapsible.add_class("trace-field")
        return collapsible
