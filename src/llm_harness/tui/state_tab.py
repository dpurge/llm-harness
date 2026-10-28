"""The State tab of the terminal chat: sections built once, `refresh` sets their text.

Every model-derived string comes from `inspect_state` (already through
`sanitize_text`) and is shown in a `Static(..., markup=False)`, so a hostile
string such as `[bold]` or `[/]` is never parsed as markup. The sections are
never rebuilt, so one the user opened or closed stays that way; findings and
plan are one Static each, whose text is replaced.
"""

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

from llm_harness.config import ModelConfig
from llm_harness.inspect_state import (
    build_preview,
    counters_text,
    environment_text,
    finding_text,
    goal_text,
    plan_step_text,
)
from llm_harness.orchestrator import Orchestrator

_NO_SESSION = "No session."


def _text(widget_id: str, *, classes: str | None = None) -> Static:
    return Static("", markup=False, id=widget_id, classes=classes)


class StatePanel(VerticalScroll):
    """Goal, findings, plan, environment, counters and the next-request preview."""

    def compose(self) -> ComposeResult:
        yield Static(_NO_SESSION, markup=False, id="state-empty", classes="secondary")
        yield _text("state-counters")
        yield from self._section("Goal", "goal", _text("state-goal"), is_open=True)
        yield from self._section(
            "Findings", "findings", _text("state-findings"), is_open=True
        )
        yield from self._section("Plan", "plan", _text("state-plan"), is_open=True)
        yield from self._section(
            "Environment", "environment", _text("state-environment"), is_open=True
        )
        yield Static("Next request", markup=False, classes="heading")
        # The user message carries the state JSON and the observation; the long
        # system prompt and the state JSON start collapsed.
        yield from self._section(
            "System message",
            "preview-system",
            _text("count-system"),
            _text("preview-system", classes="secondary"),
            is_open=False,
        )
        yield from self._section(
            "State JSON (user message)",
            "preview-state",
            _text("count-state"),
            _text("preview-user", classes="secondary"),
            is_open=False,
        )
        yield from self._section(
            "Observation",
            "preview-observation",
            _text("count-observation"),
            is_open=True,
        )

    @staticmethod
    def _section(title: str, name: str, *body: Static, is_open: bool) -> ComposeResult:
        yield Collapsible(
            *body, title=title, collapsed=not is_open, id=f"section-{name}"
        )

    def refresh_state(
        self, orchestrator: Orchestrator | None, model: ModelConfig, typed: str
    ) -> None:
        """Show `orchestrator`'s state and the request its next send would build."""
        self.query_one("#state-empty", Static).display = orchestrator is None
        if orchestrator is None:
            return
        state = orchestrator.state
        self._set("state-counters", counters_text(state))
        self._set("state-goal", goal_text(state))
        self._set(
            "state-findings",
            "\n".join(finding_text(f) for f in state.findings) or "(none)",
        )
        self._set(
            "state-plan", "\n".join(plan_step_text(s) for s in state.plan) or "(none)"
        )
        self._set(
            "state-environment",
            environment_text(state, orchestrator.pending_question),
        )
        preview = build_preview(orchestrator, model, typed)
        self._set("count-system", preview.system_count)
        self._set("count-state", preview.state_count)
        self._set("count-observation", preview.observation_count)
        self._set("preview-system", preview.system_text)
        self._set("preview-user", preview.user_text)

    def _set(self, widget_id: str, text: str) -> None:
        self.query_one(f"#{widget_id}", Static).update(text)
