"""Inspector tabs of the web chat: `Chat | State | Trace`.

The State tab shows the orchestrator state and a preview of the next request,
built by the pure `build_request` (never a second implementation). Every
model-derived string goes through `sanitize_text` and is shown in a `ui.label`,
which sends a text node; no HTML or markdown element is used here.
"""

from collections.abc import Callable, Sequence
from typing import Any

from nicegui import events, ui

from llm_harness.config import AgentConfig, ModelConfig
from llm_harness.display import sanitize_text
from llm_harness.inspect_state import (
    OPEN_BY_DEFAULT,
    agents_text,
    build_preview,
    counters_text,
    environment_text,
    finding_text,
    goal_text,
    long_field_keys,
    long_field_text,
    plan_step_text,
    span_children,
    span_details_text,
    span_title,
    tools_text,
)
from llm_harness.orchestrator import Orchestrator
from llm_harness.state import OrchestratorState
from llm_harness.state_io import (
    EXPORT_FILENAME,
    EXPORT_MEDIA_TYPE,
    MAX_IMPORT_BYTES,
    StateImportError,
    check_size,
    export_yaml,
    import_yaml,
)
from llm_harness.trace import Span

CHAT_TAB = "chat"
STATE_TAB = "state"
TRACE_TAB = "trace"

# Quasar's own tab label is 14px; raise it to the 18px page base.
INSPECTOR_CSS = """
.llm-page .q-tab__label { font-size: 18px; }
.llm-trace-text { overflow-wrap: anywhere; }
"""

# Panels fill the page width and the height left under the tab strip; a panel
# never grows the page, its content scrolls inside it. `min-h-0` lets a flex
# child shrink below its content so the scroll area can take over.
PANELS_CLASSES = "w-full grow min-h-0"
PANEL_CLASSES = "w-full h-full no-wrap"
_PRE_WRAP = "whitespace-pre-wrap"
_SECONDARY = "text-grey llm-secondary"


def build_tabs() -> ui.tabs:
    """The tab strip; the caller builds a `ui.tab_panels` on it with `*_TAB` names."""
    with ui.tabs().classes("w-full") as tabs:
        ui.tab(CHAT_TAB, label="Chat").mark("tab-chat")
        ui.tab(STATE_TAB, label="State").mark("tab-state")
        ui.tab(TRACE_TAB, label="Trace").mark("tab-trace")
    return tabs


def _section(title: str, marker: str, *, is_open: bool) -> ui.expansion:
    """A collapsible section; built once, so its open state survives a refresh."""
    return ui.expansion(title, value=is_open).classes("w-full").mark(marker)


def pick_files_handler(upload_id: int) -> str:
    """Browser-side click handler that opens the file picker of a `ui.upload`.

    `getElement` is NiceGUI's own lookup of a Vue component by element id;
    `$refs.qRef` is the QUploader inside it, whose `pickFiles` clicks its file
    input synchronously, inside the user's click.
    """
    return f"() => getElement({upload_id}).$refs.qRef.pickFiles()"


class StateTransfer:
    """`Export state` and `Import state` in the header row of the State tab.

    Import order: size (before the bytes are read), parse, validate, and
    `Orchestrator.restore` last, so any failure before it changes nothing. The
    status line is a `ui.label` (a text node); nothing from the file reaches
    an html or markdown element, and the file content is never logged.
    """

    def __init__(
        self,
        get_orchestrator: Callable[[], Orchestrator | None],
        on_imported: Callable[[], None],
    ) -> None:
        self._get_orchestrator = get_orchestrator
        self._on_imported = on_imported
        with ui.column().classes("w-full gap-1"):
            with ui.row().classes("w-full items-center gap-2"):
                self._export = ui.button("Export state", on_click=self._on_export).mark(
                    "export-state"
                )
                # The visible Import button looks exactly like Export; the
                # QUploader (a card with its own header) stays hidden and only
                # supplies the file picker. The picker opens from a client-side
                # click handler: browsers open a file dialog only inside the
                # user's click, not from a message that arrives later over the
                # websocket (which is what `run_method` would be).
                self._upload = (
                    ui.upload(
                        auto_upload=True,
                        max_file_size=MAX_IMPORT_BYTES,
                        on_upload=self._on_upload,
                        on_rejected=self._on_rejected,
                    )
                    .props('accept=".yaml,.yml"')
                    .classes("hidden")
                    .mark("import-state")
                )
                self._import_button = (
                    ui.button("Import state")
                    .on("click", js_handler=pick_files_handler(self._upload.id))
                    .mark("import-state-button")
                )
            self._status = (
                ui.label("")
                .classes(f"w-full {_PRE_WRAP} {_SECONDARY}")
                .mark("transfer-status")
            )

    def set_busy(self, is_busy: bool) -> None:
        for control in (self._export, self._import_button, self._upload):
            control.set_enabled(not is_busy)

    def _on_export(self) -> None:
        orchestrator = self._get_orchestrator()
        if orchestrator is None:
            self._status.set_text("export failed: no session")
            return
        ui.download.content(
            export_yaml(orchestrator.snapshot()), EXPORT_FILENAME, EXPORT_MEDIA_TYPE
        )
        self._status.set_text("state exported")

    def _on_rejected(self) -> None:
        # The widget's own limit (client side): too large, or not a .yaml file.
        self._fail("file is too large or not a .yaml file")

    async def _on_upload(self, event: events.UploadEventArguments) -> None:
        try:
            # Size first: NiceGUI has already received the file (a large one sits in
            # a temp file), but it is never read into memory over the cap.
            check_size(event.file.size())
            self._import(await event.file.read())
        except StateImportError as exc:
            self._fail(str(exc))
        finally:
            self._upload.reset()

    def _import(self, data: bytes) -> None:
        orchestrator = self._get_orchestrator()
        if orchestrator is None:
            raise StateImportError("no session")
        run_state = import_yaml(data)
        try:
            orchestrator.restore(run_state)
        except RuntimeError:
            raise StateImportError("a turn is running, try again after it") from None
        self._status.set_text("state loaded")
        self._on_imported()

    def _fail(self, reason: str) -> None:
        # The reasons come from state_io (already one line); this keeps it so.
        self._status.set_text(
            f"import failed: {' '.join(sanitize_text(reason).split())}"
        )


class StatePanel:
    """The State tab: sections and labels are built once, `refresh` sets their text.

    Findings and plan are rebuilt inside their (kept) sections on each refresh
    because their length changes. The sections are never rebuilt, so a section
    the user opened or closed stays that way.
    """

    def __init__(
        self,
        get_orchestrator: Callable[[], Orchestrator | None],
        on_imported: Callable[[], None],
        agents: Sequence[AgentConfig],
    ) -> None:
        self._agents = agents
        with ui.tab_panel(STATE_TAB).classes(PANEL_CLASSES).mark("panel-state"):
            self.transfer = StateTransfer(get_orchestrator, on_imported)
            with (
                ui.scroll_area().classes("w-full grow").mark("state-scroll"),
                ui.column().classes("w-full"),
            ):
                self._build_state_section()
                self._build_agents_section()
                self._build_tools_section()
                self._build_preview_section()

    def _build_state_section(self) -> None:
        self._empty = ui.label("No session.").classes(_SECONDARY).mark("state-empty")
        self._counters = ui.label("").classes(_PRE_WRAP).mark("state-counters")
        with _section("Goal", "section-goal", is_open=True):
            self._goal = ui.label("").classes(f"w-full {_PRE_WRAP}").mark("state-goal")
        with _section("Findings", "section-findings", is_open=True):
            self._findings = ui.column().classes("w-full").mark("state-findings")
        with _section("Plan", "section-plan", is_open=True):
            self._plan = ui.column().classes("w-full").mark("state-plan")
        with _section("Environment", "section-environment", is_open=True):
            self._environment = (
                ui.label("").classes(f"w-full {_PRE_WRAP}").mark("state-environment")
            )

    def _build_agents_section(self) -> None:
        # Configuration, not session state: shown before the first message too.
        with _section("Agents", "section-agents", is_open=True):
            ui.label(agents_text(self._agents)).classes(f"w-full {_PRE_WRAP}").mark(
                "state-agents"
            )

    def _build_tools_section(self) -> None:
        with _section("Tools", "section-tools", is_open=True):
            self._tools = (
                ui.label("(none)").classes(f"w-full {_PRE_WRAP}").mark("state-tools")
            )

    def _build_preview_section(self) -> None:
        ui.label("Next request").classes("text-bold")
        self._counts = {}
        self._messages = {}
        # The user message carries the state JSON and the observation; the long
        # system prompt and the state JSON start collapsed.
        parts = (
            ("system", "System message", "system", False),
            ("state", "State JSON (user message)", "user", False),
            ("observation", "Observation", None, True),
        )
        for part, title, role, is_open in parts:
            with _section(title, f"section-preview-{part}", is_open=is_open):
                self._counts[part] = (
                    ui.label("").classes(_PRE_WRAP).mark(f"count-{part}")
                )
                if role is not None:
                    self._messages[role] = (
                        ui.label("")
                        .classes(f"w-full {_PRE_WRAP} {_SECONDARY}")
                        .mark(f"preview-{role}")
                    )

    def refresh(
        self, orchestrator: Orchestrator | None, model: ModelConfig, typed: str
    ) -> None:
        """Show `orchestrator`'s state and the request its next send would build."""
        self._empty.set_visibility(orchestrator is None)
        if orchestrator is None:
            return
        self._show_state(orchestrator.state, orchestrator.pending_question)
        self._tools.set_text(tools_text(orchestrator.available_tools()))
        self._show_preview(orchestrator, model, typed)

    def _show_state(self, state: OrchestratorState, question: str | None) -> None:
        self._counters.set_text(counters_text(state))
        self._goal.set_text(goal_text(state))
        self._findings.clear()
        with self._findings:
            for finding in state.findings:
                ui.label(finding_text(finding)).classes(f"w-full {_PRE_WRAP}").mark(
                    "state-finding"
                )
        self._plan.clear()
        with self._plan:
            for step in state.plan:
                ui.label(plan_step_text(step)).classes(f"w-full {_PRE_WRAP}").mark(
                    "state-plan-step"
                )
        self._environment.set_text(environment_text(state, question))

    def _show_preview(
        self, orchestrator: Orchestrator, model: ModelConfig, typed: str
    ) -> None:
        preview = build_preview(orchestrator, model, typed)
        self._counts["system"].set_text(preview.system_count)
        self._counts["state"].set_text(preview.state_count)
        self._counts["observation"].set_text(preview.observation_count)
        self._messages["system"].set_text(preview.system_text)
        self._messages["user"].set_text(preview.user_text)


# Trace tab

_STATUS_COLOR = {
    "ok": "text-positive",
    "error": "text-negative",
    "rejected": "text-warning",
    "cancelled": "text-grey",
}


class TracePanel:
    """The Trace tab: the span tree of the last turn as nested expansions.

    The tree is rebuilt on each refresh (spans appear and move as a turn runs);
    which expansions the user opened or closed is remembered by span id and field
    and applied again, so the view stays put while a turn streams.
    """

    def __init__(self) -> None:
        self._is_open: dict[str, bool] = {}
        self._turn_key: object = None
        with (
            ui.tab_panel(TRACE_TAB).classes(PANEL_CLASSES).mark("panel-trace"),
            ui.scroll_area().classes("w-full grow").mark("trace-scroll"),
            ui.column().classes("w-full"),
        ):
            self._empty = (
                ui.label("No trace yet.").classes(_SECONDARY).mark("trace-empty")
            )
            self._tree = ui.column().classes("w-full").mark("trace-tree")

    def refresh(self, orchestrator: Orchestrator | None) -> None:
        """Show the last turn of `orchestrator`; the empty line when it has none."""
        spans = orchestrator.last_trace if orchestrator is not None else None
        self._tree.clear()
        self._empty.set_visibility(not spans)
        if not spans:
            self._is_open.clear()
            self._turn_key = None
            return
        # A new turn or session reuses the span ids: forget the old open states.
        turn_key = (id(orchestrator), orchestrator.state.turn)
        if turn_key != self._turn_key:
            self._is_open.clear()
            self._turn_key = turn_key
        grouped = span_children(spans)
        with self._tree:
            for root in grouped.get(None, []):
                self._show_span(root, grouped)

    def _remember(self, key: str) -> Callable[[Any], None]:
        def remember(event: Any) -> None:
            self._is_open[key] = bool(event.value)

        return remember

    def _expansion(self, title: str, key: str, *, default: bool) -> ui.expansion:
        return ui.expansion(
            title,
            value=self._is_open.get(key, default),
            on_value_change=self._remember(key),
        ).classes("w-full")

    def _show_span(self, span: Span, grouped: dict[str | None, list[Span]]) -> None:
        expansion = self._expansion(
            span_title(span), span.id, default=span.kind in OPEN_BY_DEFAULT
        )
        expansion.mark("trace-span", f"trace-span-{span.id}")
        expansion.props(f"header-class={_STATUS_COLOR[span.status]}")
        with expansion:
            self._show_details(span)
            for child in grouped.get(span.id, []):
                self._show_span(child, grouped)

    def _show_details(self, span: Span) -> None:
        ui.label(span_details_text(span)).classes(
            f"w-full {_PRE_WRAP} llm-trace-text"
        ).mark("trace-details")
        for key in long_field_keys(span):
            self._show_long_field(span, key)

    def _show_long_field(self, span: Span, key: str) -> None:
        field = self._expansion(key, f"{span.id}/{key}", default=False)
        field.mark(f"trace-field-{key}")
        with field:
            ui.label(long_field_text(span, key)).classes(
                f"w-full {_PRE_WRAP} llm-trace-text"
            ).mark(f"trace-text-{key}")
