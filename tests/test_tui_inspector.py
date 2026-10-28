"""Terminal UI tabs and the State tab (slice 6a), driven by Textual's Pilot."""

import ast
import asyncio
import os
import re
import signal
import stat
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import (
    Collapsible,
    Input,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets._collapsible import CollapsibleTitle

from llm_harness import state_files
from llm_harness.context import answer_observation, build_request
from llm_harness.display import sanitize_text
from llm_harness.inspect_state import counters_text
from llm_harness.orchestrator import Orchestrator
from llm_harness.state_io import EXPORT_FILENAME, MAX_IMPORT_BYTES, export_yaml
from llm_harness.trace import Span
from llm_harness.tui import app as tui_app
from llm_harness.tui import state_tab
from llm_harness.tui.app import ChatApp
from llm_harness.tui.trace_tab import TraceCollapsible, TracePanel
from llm_harness.types import Done, ErrorEvent, TextDelta, ThinkingDelta
from tests.fake_steps import ScriptedSteps, reply_events
from tests.fakes import router_factory_for
from tests.test_orchestrator import CALL_TOOL
from tests.test_tui import (
    ASK_FILE,
    CONFIG,
    MODEL_1,
    MODEL_2,
    MODEL_3,
    REPLY,
    RESPOND_HELLO,
    Factory,
    StepFactory,
    make_app,
    prompt_of,
    send,
    texts,
    wait_for,
)

FINDING = {"text": "port is 8080", "source": "config.yaml"}
PLAN = [{"text": "read config", "status": "doing"}]
TWO_STEPS = [
    reply_events(
        {"type": "call_tool", "tool": "search", "arguments": {}},
        {
            "goal": "find the port",
            "findings": {"$append": [FINDING]},
            "plan": {"$replace": PLAN},
        },
    ),
    # The goal patch of the LAST step is visible only after the turn ends.
    reply_events(RESPOND_HELLO, {"goal": "final goal"}),
]
TUI_DIR = Path(tui_app.__file__).parent

SECTION_DEFAULTS = {
    "section-goal": False,
    "section-findings": False,
    "section-plan": False,
    "section-environment": False,
    "section-preview-system": True,
    "section-preview-state": True,
    "section-preview-observation": False,
}


def shown(app: ChatApp, widget_id: str) -> str:
    return app.query_one(f"#{widget_id}", Static).visual.plain


def collapsed(app: ChatApp, section_id: str) -> bool:
    return app.query_one(f"#{section_id}", Collapsible).collapsed


def section_title_of(app: ChatApp, section_id: str) -> CollapsibleTitle:
    return app.query_one(f"#{section_id} > CollapsibleTitle", CollapsibleTitle)


def current_counts(app: ChatApp) -> dict[str, int]:
    out = {}
    for part in ("system", "state", "observation"):
        match = re.search(r"(\d+) characters", shown(app, f"count-{part}"))
        assert match, shown(app, f"count-{part}")
        out[part] = int(match.group(1))
    return out


# Tabs


async def test_tabs_exist_with_the_criteria_ids() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        content = app.query_one(TabbedContent)
        assert [pane.id for pane in app.query(TabPane)] == [
            "tab-chat",
            "tab-state",
            "tab-trace",
        ]
        assert content.active == "tab-chat"
        # Textual gives the clickable Tab the id "--content-tab-<pane id>".
        tabs = {tab.id: str(tab.label) for tab in content.query("ContentTab")}
        assert tabs == {
            "--content-tab-tab-chat": "Chat",
            "--content-tab-tab-state": "State",
            "--content-tab-tab-trace": "Trace",
        }


async def test_chat_widgets_are_still_found_by_id_inside_the_chat_tab() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        for selector in ("#model", "#log", "#prompt"):
            assert app.query_one(f"TabPane#tab-chat {selector}")


async def test_the_trace_tab_shows_the_empty_line_before_the_first_turn() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        assert shown(app, "trace-empty") == "No trace yet."


async def test_clicking_a_tab_switches_the_active_pane() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test() as pilot:
        await pilot.click("#--content-tab-tab-state")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "tab-state"
        await pilot.click("#--content-tab-tab-chat")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "tab-chat"


async def test_the_prompt_has_focus_on_mount_despite_the_tab_widget() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.focused is prompt_of(app)


async def test_the_prompt_has_focus_after_a_scripted_turn() -> None:
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        assert app.focused is prompt_of(app)


async def test_the_prompt_has_focus_after_an_error() -> None:
    app = make_app(
        Factory([TextDelta(text="par"), ErrorEvent(kind="auth", message="x")])
    )
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        assert app.focused is prompt_of(app)


async def test_the_prompt_has_focus_after_a_cancel() -> None:
    app = make_app(Factory([ThinkingDelta(text="par"), *REPLY], hang_after=1))
    async with app.run_test() as pilot:
        prompt_of(app).value = "hi"
        await pilot.press("enter")
        await wait_for(
            pilot, lambda: app._turn_is_running() and prompt_of(app).disabled
        )
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.focused is prompt_of(app)


# State tab: sections


async def test_section_defaults_open_and_collapsed() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        assert {s: collapsed(app, s) for s in SECTION_DEFAULTS} == SECTION_DEFAULTS
        titles = {
            section: app.query_one(f"#{section}", Collapsible).title
            for section in SECTION_DEFAULTS
        }
        assert titles["section-goal"] == "Goal"
        assert titles["section-preview-observation"] == "Observation"


async def test_a_toggled_section_stays_toggled_across_refreshes() -> None:
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await pilot.click("#--content-tab-tab-state")
        await pilot.pause()
        await pilot.click(section_title_of(app, "section-goal"))
        # The preview sections sit below the fold of the default 80x24 screen.
        section_title_of(app, "section-preview-system").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(section_title_of(app, "section-preview-system"))
        await pilot.pause()
        assert collapsed(app, "section-goal") is True
        assert collapsed(app, "section-preview-system") is False
        # Back to the chat tab: Enter must reach the prompt, not a section title.
        await pilot.click("#--content-tab-tab-chat")
        prompt_of(app).focus()
        await send(pilot, app, "hi")
        assert collapsed(app, "section-goal") is True
        assert collapsed(app, "section-preview-system") is False
        assert collapsed(app, "section-findings") is False
        assert shown(app, "state-goal") == "Goal: final goal"


# State tab: content


async def test_state_content_after_a_two_step_turn() -> None:
    factory = StepFactory(TWO_STEPS)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        orchestrator = app._orchestrator
        assert orchestrator is not None
        env = orchestrator.state.environment
        assert shown(app, "state-counters") == "turn 1, step 2"
        assert shown(app, "state-goal") == "Goal: final goal"
        assert shown(app, "state-findings") == "port is 8080 (source: config.yaml)"
        assert shown(app, "state-plan") == "[doing] read config"
        assert shown(app, "state-environment") == (
            f"now: {env.now.isoformat()}\n"
            f"user_name: {env.user_name}\n"
            f"computer_name: {env.computer_name}\n"
            "user_id: (not set)"
        )
        request = build_request(orchestrator.state, "", MODEL_1)
        system, user = request.messages
        assert current_counts(app) == {
            "system": len(system.content),
            "state": len(orchestrator.state.model_dump_json()),
            "observation": 0,
        }
        assert shown(app, "preview-user") == user.content
        assert shown(app, "preview-system") == system.content


async def test_state_shows_the_configured_user_id_and_sends_it() -> None:
    factory = StepFactory(TWO_STEPS)
    config = CONFIG.model_copy(update={"user_id": "ann"})
    app = ChatApp(config, MODEL_1, router_factory_for(config, factory), False)
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        assert shown(app, "state-environment").endswith("\nuser_id: ann")
        assert '"user_id":"ann"' in factory.provider.requests[0].messages[-1].content


async def test_state_before_any_turn_is_empty_but_shown() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        assert shown(app, "state-counters") == "turn 0, step 0"
        assert shown(app, "state-goal") == "Goal: (none)"
        assert shown(app, "state-findings") == "(none)"
        assert shown(app, "state-plan") == "(none)"
        assert app.query_one("#state-empty").display is False


async def test_the_preview_observation_is_the_typed_text_after_a_refresh() -> None:
    app = make_app(StepFactory([reply_events(ASK_FILE, {"goal": "g"})]))
    async with app.run_test() as pilot:
        await send(pilot, app, "fix it")
        assert "pending question: Which file?" in shown(app, "state-environment")
        assert current_counts(app)["observation"] == 0
        # The preview follows the typed text at the next refresh.
        prompt_of(app).value = "main.py"
        app._refresh_inspector()
        expected = answer_observation("Which file?", "main.py")
        assert current_counts(app)["observation"] == len(expected)


async def test_an_observation_over_the_cap_says_it_is_cut() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        prompt_of(app).value = "x" * 4500
        app._refresh_inspector()
        assert "4500 characters (cut to 4000 in the request)" in shown(
            app, "count-observation"
        )


# State tab: refresh hooks


async def test_each_turn_end_path_refreshes_the_state_tab() -> None:
    # Error: no StatusEvent ran, so only the end-of-turn hook shows turn 1.
    error_app = make_app(
        Factory([TextDelta(text="par"), ErrorEvent(kind="auth", message="x")])
    )
    async with error_app.run_test() as pilot:
        await send(pilot, error_app, "hello")
        assert error_app._orchestrator is not None
        assert shown(error_app, "state-counters") == counters_text(
            error_app._orchestrator.state
        )
        assert shown(error_app, "state-counters").startswith("turn 1")
    # Cancel: the turn was cut while waiting for the model.
    cancel_app = make_app(Factory([ThinkingDelta(text="par"), *REPLY], hang_after=1))
    async with cancel_app.run_test() as pilot:
        prompt_of(cancel_app).value = "hi"
        await pilot.press("enter")
        await wait_for(pilot, lambda: prompt_of(cancel_app).disabled)
        assert shown(cancel_app, "state-counters") == "turn 0, step 0"
        await pilot.press("escape")
        await cancel_app.workers.wait_for_complete()
        await pilot.pause()
        assert shown(cancel_app, "state-counters").startswith("turn 1")


async def test_the_state_tab_refreshes_after_each_status_event() -> None:
    factory = StepFactory(TWO_STEPS)
    app = make_app(factory)
    seen: list[str] = []
    original = app._refresh_inspector

    def spy() -> None:
        original()
        seen.append(shown(app, "state-counters"))

    app._refresh_inspector = spy  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        seen.clear()
        await send(pilot, app, "hi")
    # Two status events (steps 1 and 2), then the turn end.
    assert seen[:2] == ["turn 1, step 1", "turn 1, step 2"]
    assert len(seen) == 3


async def test_new_chat_resets_the_state_tab() -> None:
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        old = app._orchestrator
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert app._orchestrator is not old
        assert app._orchestrator is not None
        assert shown(app, "state-counters") == "turn 0, step 0"
        assert shown(app, "state-goal") == "Goal: (none)"
        assert shown(app, "state-findings") == "(none)"
        assert shown(app, "state-environment").startswith(
            f"now: {app._orchestrator.state.environment.now.isoformat()}"
        )


async def test_a_model_change_refreshes_the_state_tab() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test() as pilot:
        before = app._orchestrator
        app.query_one("#model", Select).value = MODEL_2.id
        await pilot.pause()
        assert app._orchestrator is not before
        assert app._orchestrator is not None
        assert shown(app, "state-environment").startswith(
            f"now: {app._orchestrator.state.environment.now.isoformat()}"
        )


# Safety


async def test_hostile_model_strings_are_shown_literally() -> None:
    hostile = "[bold]x[/]\x1b[31m"
    finding = {"text": hostile, "source": hostile}
    reply = reply_events(
        RESPOND_HELLO,
        {
            "goal": hostile,
            "findings": {"$append": [finding]},
            "plan": {"$replace": [{"text": hostile, "status": "todo"}]},
        },
    )
    app = make_app(StepFactory([reply]))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        literal = "[bold]x[/][31m"
        assert shown(app, "state-goal") == f"Goal: {literal}"
        assert shown(app, "state-findings") == f"{literal} (source: {literal})"
        assert shown(app, "state-plan") == f"[todo] {literal}"
        for widget in app.query_one("#state-panel").query(Static):
            assert "\x1b" not in widget.visual.plain
            if not isinstance(widget, CollapsibleTitle):  # fixed titles, not model text
                assert widget._render_markup is False


async def test_hostile_typed_observation_is_shown_literally() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        prompt_of(app).value = "[bold]x[/]\x1b[31m"
        app._refresh_inspector()
        assert "\x1b" not in shown(app, "preview-user")
        assert "[bold]x[/][31m" in shown(app, "preview-user")


# Trace tab

REJECTED_REPLY = [TextDelta(text="this is not json"), Done()]
# Rejected reply, retry that calls an unknown tool, then the answer.
TRACE_STEPS = [
    REJECTED_REPLY,
    reply_events(
        CALL_TOOL, {"goal": "g"}, ThinkingDelta(text="hmm "), ThinkingDelta(text="ok")
    ),
    reply_events(RESPOND_HELLO),
]
HOSTILE = "[bold]x[/]\x1b[31m"
HOSTILE_SHOWN = "[bold]x[/][31m"
SPAN_TITLE = r"(turn|step|llm_call|tool_call): .+ \| \w+ \| [\d.e+-]+ ms"


def span_boxes(app: ChatApp) -> dict[str, TraceCollapsible]:
    return {box.key: box for box in app.query(".trace-span").results(TraceCollapsible)}


def field_box(app: ChatApp, span_id: str, key: str) -> TraceCollapsible:
    return next(
        box for box in app.query(TraceCollapsible) if box.key == f"{span_id}/{key}"
    )


def title_text(box: Collapsible) -> str:
    return box.query_one(CollapsibleTitle).label.plain


def titles(app: ChatApp) -> dict[str, str]:
    return {span_id: title_text(box) for span_id, box in span_boxes(app).items()}


def parent_id_of(box: Collapsible) -> str | None:
    parent = next((a for a in box.ancestors if isinstance(a, TraceCollapsible)), None)
    return parent.key if parent is not None else None


def details_of(box: Collapsible) -> str:
    return box.query(".trace-details").first(Static).visual.plain


def field_text(app: ChatApp, span_id: str, key: str) -> str:
    return field_box(app, span_id, key).query_one(".trace-text", Static).visual.plain


def last_spans(app: ChatApp) -> dict[str, Span]:
    assert app._orchestrator is not None
    return {span.id: span for span in app._orchestrator.last_trace or ()}


async def settle(pilot, app: ChatApp, expected_spans: int) -> None:
    """Wait until the rebuilt tree shows `expected_spans` spans."""
    await app.workers.wait_for_complete()
    await wait_for(pilot, lambda: len(span_boxes(app)) == expected_spans)
    await pilot.pause()


def make_span(span_id: str, parent_id: str | None, kind: str = "step") -> Span:
    return Span(
        id=span_id,
        parent_id=parent_id,
        kind=kind,  # type: ignore[arg-type]
        name=f"n-{span_id}",
        start_ms=0.0,
        duration_ms=1.5,
        status="ok",
        attributes={},
    )


async def test_the_trace_tree_after_a_scripted_turn() -> None:
    app = make_app(StepFactory(TRACE_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app, 7)
        boxes = span_boxes(app)
        assert {i: parent_id_of(b) for i, b in boxes.items()} == {
            "s1": None,
            "s2": "s1",
            "s3": "s2",  # rejected attempt
            "s4": "s2",  # the retry that was accepted
            "s5": "s2",  # the tool call of step 1
            "s6": "s1",
            "s7": "s6",
        }
        statuses = {i: t.split(" | ")[1] for i, t in titles(app).items()}
        assert statuses == {
            "s1": "ok",
            "s2": "ok",
            "s3": "rejected",
            "s4": "ok",
            "s5": "error",  # no tool registry: "unknown tool"
            "s6": "ok",
            "s7": "ok",
        }
        for title in titles(app).values():
            assert re.fullmatch(SPAN_TITLE, title), title
        assert titles(app)["s5"].startswith("tool_call: search | error | ")
        assert titles(app)["s3"].startswith("llm_call: provider-model-1 | rejected | ")
        # Colour is a second signal only: the status word is in the title.
        assert boxes["s3"].has_class("trace-rejected")
        assert boxes["s5"].has_class("trace-error")
        assert app.query_one("#trace-empty").display is False


async def test_span_details_and_every_long_field_are_shown_whole() -> None:
    app = make_app(StepFactory(TRACE_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app, 7)
        spans = last_spans(app)
        assert field_text(app, "s3", "reply") == "this is not json"
        assert field_text(app, "s3", "rejection") == spans["s3"].attributes["rejection"]
        request = field_text(app, "s3", "messages")
        assert request.startswith("[system]\n")
        assert "\n\n[user]\n" in request
        assert field_text(app, "s4", "thinking") == "hmm ok"
        assert field_text(app, "s5", "arguments") == "{}"
        assert field_text(app, "s5", "result") == spans["s5"].attributes["result"]
        assert "unknown tool" in field_text(app, "s5", "result")
        details = details_of(span_boxes(app)["s3"])
        assert "kind: llm_call" in details and "status: rejected" in details
        assert "model: provider-model-1" in details
        assert "parent: s2" in details
        assert "start: " in details and "duration: " in details


async def test_a_twenty_thousand_character_reply_is_shown_without_truncation() -> None:
    text = "word " * 4000
    app = make_app(
        StepFactory([reply_events({"type": "respond", "text": text}, {"goal": "g"})])
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app, 3)
        spans = last_spans(app)
        reply = spans["s3"].attributes["reply"]
        assert len(reply) > 20000
        assert field_text(app, "s3", "reply") == sanitize_text(reply)


async def test_long_text_wraps_and_the_panel_does_not_scroll_sideways() -> None:
    text = "x" * 3000 + " " + "word " * 500
    app = make_app(
        StepFactory([reply_events({"type": "respond", "text": text}, {"goal": "g"})])
    )
    async with app.run_test(size=(100, 40)) as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app, 3)
        await pilot.click("#--content-tab-tab-trace")
        await pilot.pause()
        span_boxes(app)["s3"].collapsed = False  # the llm_call holding the field
        field_box(app, "s3", "reply").collapsed = False
        panel = app.query_one("#trace-panel", TracePanel)
        body = field_box(app, "s3", "reply").query_one(".trace-text", Static)
        await wait_for(pilot, lambda: body.size.height > 0)
        assert panel.size.width >= 90
        assert body.region.width <= panel.size.width
        assert body.size.height > 5  # wrapped onto many lines
        assert panel.virtual_size.width <= panel.size.width


async def test_long_fields_are_collapsed_and_turn_and_step_are_open_by_default() -> (
    None
):
    app = make_app(StepFactory(TRACE_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app, 7)
        for span_id, box in span_boxes(app).items():
            kind = titles(app)[span_id].split(": ")[0]
            assert box.collapsed is (kind not in ("turn", "step")), span_id
        fields = [b for b in app.query(TraceCollapsible) if "/" in b.key]
        assert fields
        assert all(box.collapsed for box in fields)
        assert {box.key.split("/")[1] for box in fields} == {
            "messages",
            "thinking",
            "reply",
            "rejection",
            "arguments",
            "result",
        }


async def test_orphan_spans_are_listed_at_the_root_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = (make_span("s3", "s2", "llm_call"), make_span("s4", "s2", "tool_call"))
    monkeypatch.setattr(Orchestrator, "last_trace", property(lambda self: spans))
    app = make_app(Factory(REPLY))
    async with app.run_test() as pilot:
        await settle(pilot, app, 2)
        assert list(span_boxes(app)) == ["s3", "s4"]
        assert {i: parent_id_of(b) for i, b in span_boxes(app).items()} == {
            "s3": None,
            "s4": None,
        }
        assert titles(app)["s3"] == "llm_call: n-s3 | ok | 1.5 ms"
        assert app.query_one("#trace-empty").display is False


async def test_the_empty_trace_says_so_before_a_turn_and_after_new_chat() -> None:
    app = make_app(StepFactory(TRACE_STEPS))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert shown(app, "trace-empty") == "No trace yet."
        assert app.query_one("#trace-empty").display is True
        assert not span_boxes(app)
        await send(pilot, app, "hi")
        await settle(pilot, app, 7)
        assert app.query_one("#trace-empty").display is False
        await pilot.press("ctrl+n")
        await wait_for(pilot, lambda: not span_boxes(app))
        await pilot.pause()
        assert app.query_one("#trace-empty").display is True
        assert not list(app.query(TraceCollapsible))


async def test_the_next_turn_replaces_the_trace_of_the_old_one() -> None:
    app = make_app(StepFactory([*TRACE_STEPS, reply_events(RESPOND_HELLO)]))
    async with app.run_test() as pilot:
        await send(pilot, app, "one")
        await settle(pilot, app, 7)
        await send(pilot, app, "two")
        await settle(pilot, app, 3)  # not 10: only the last turn
        assert titles(app)["s1"].startswith("turn: ")


async def test_the_open_state_is_remembered_across_a_refresh_and_reset_by_a_turn() -> (
    None
):
    app = make_app(StepFactory([*TRACE_STEPS, *TRACE_STEPS]))
    async with app.run_test() as pilot:
        await send(pilot, app, "one")
        await settle(pilot, app, 7)
        span_boxes(app)["s4"].collapsed = False  # an llm_call, closed by default
        span_boxes(app)["s2"].collapsed = True  # a step, open by default
        field_box(app, "s5", "result").collapsed = False
        await pilot.pause()
        old_s4 = span_boxes(app)["s4"]
        app._refresh_inspector()
        await wait_for(pilot, lambda: span_boxes(app)["s4"] is not old_s4)
        await pilot.pause()
        assert span_boxes(app)["s4"].collapsed is False
        assert span_boxes(app)["s2"].collapsed is True
        assert field_box(app, "s5", "result").collapsed is False
        # The ids repeat in the next turn: the old open states must not apply.
        await send(pilot, app, "two")
        await settle(pilot, app, 7)
        await wait_for(pilot, lambda: span_boxes(app)["s4"].collapsed)
        assert span_boxes(app)["s2"].collapsed is False
        assert field_box(app, "s5", "result").collapsed is True


async def test_a_cancelled_turn_shows_the_cancelled_status() -> None:
    factory = StepFactory([])
    factory.provider = ScriptedSteps(
        [[ThinkingDelta(text="par"), *reply_events(RESPOND_HELLO)]], hang_after=1
    )
    app = make_app(factory)
    async with app.run_test() as pilot:
        prompt_of(app).value = "hi"
        await pilot.press("enter")
        await wait_for(
            pilot, lambda: app._turn_is_running() and prompt_of(app).disabled
        )
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        spans = last_spans(app)
        await settle(pilot, app, len(spans))
        statuses = {i: t.split(" | ")[1] for i, t in titles(app).items()}
        assert statuses == {i: s.status for i, s in spans.items()}
        assert "cancelled" in statuses.values()
        assert titles(app)["s1"].startswith("turn: ")
        assert span_boxes(app)["s1"].has_class("trace-cancelled")


async def test_an_error_turn_shows_the_error_status() -> None:
    app = make_app(
        Factory([TextDelta(text="par"), ErrorEvent(kind="auth", message="x")])
    )
    async with app.run_test() as pilot:
        await send(pilot, app, "hello")
        spans = last_spans(app)
        await settle(pilot, app, len(spans))
        statuses = {i: t.split(" | ")[1] for i, t in titles(app).items()}
        assert statuses == {i: s.status for i, s in spans.items()}
        assert "error" in statuses.values()
        assert span_boxes(app)["s1"].has_class("trace-error")


async def test_hostile_strings_are_literal_in_titles_and_bodies() -> None:
    call = {"type": "call_tool", "tool": HOSTILE, "arguments": {"k": HOSTILE}}
    steps = [
        reply_events(call, {"goal": "g"}),
        reply_events({"type": "respond", "text": HOSTILE}),
    ]
    app = make_app(StepFactory(steps))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        await settle(pilot, app, 6)
        tool_span = next(s for s in last_spans(app).values() if s.kind == "tool_call")
        assert titles(app)[tool_span.id].startswith(f"tool_call: {HOSTILE_SHOWN} | ")
        assert f"name: {HOSTILE_SHOWN}" in details_of(span_boxes(app)[tool_span.id])
        # JSON text: the ESC is escaped by the dump, the brackets stay literal.
        assert '"k": "[bold]x[/]' in field_text(app, tool_span.id, "arguments")
        assert HOSTILE_SHOWN in field_text(app, tool_span.id, "result")
        panel = app.query_one("#trace-panel")
        for widget in panel.query(Static):
            assert "\x1b" not in widget.visual.plain
            if not isinstance(widget, CollapsibleTitle):
                assert widget._render_markup is False
        for title in app.query(CollapsibleTitle):
            assert "\x1b" not in title.label.plain


async def test_the_trace_is_refreshed_after_each_step_and_at_the_turn_end() -> None:
    app = make_app(StepFactory(TWO_STEPS))
    panel_calls: list[Orchestrator | None] = []
    original = TracePanel.refresh_trace

    def spy(self: TracePanel, orchestrator: Orchestrator | None) -> None:
        panel_calls.append(orchestrator)
        original(self, orchestrator)

    async with app.run_test() as pilot:
        TracePanel.refresh_trace = spy  # type: ignore[method-assign]
        try:
            await send(pilot, app, "hi")
        finally:
            TracePanel.refresh_trace = original  # type: ignore[method-assign]
    # Two status events (steps 1 and 2), then the turn end.
    assert len(panel_calls) == 3
    assert all(call is app._orchestrator for call in panel_calls)


async def test_the_trace_follows_a_refresh_at_the_end_of_the_turn() -> None:
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        # The last spans (the turn, the last step) close after the final status
        # event, so only the end-of-turn refresh can show the whole turn.
        await settle(pilot, app, len(last_spans(app)))
        assert "s1" in span_boxes(app)


# Imports and layout


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_the_tui_imports_neither_nicegui_nor_the_gui_package() -> None:
    for path in TUI_DIR.glob("*.py"):
        for module in imported_modules(path):
            assert module.split(".")[0] != "nicegui", (path.name, module)
            assert not module.startswith("llm_harness.gui"), (path.name, module)
    # The neutral module both front ends use is framework free too.
    neutral = Path(state_tab.__file__).parent.parent / "inspect_state.py"
    for module in imported_modules(neutral):
        assert module.split(".")[0] not in {"nicegui", "textual"}, module
        assert not module.startswith("llm_harness.gui"), module


def test_the_layout_css_fills_the_width_and_height_with_no_fixed_sizes() -> None:
    css = ChatApp.CSS
    for rule in (
        "TabbedContent { width: 1fr; height: 1fr; }",
        "TabPane { width: 1fr; height: 1fr; padding: 0; }",
        "#log { width: 1fr; height: 1fr; }",
        "#state-panel { width: 1fr; height: 1fr; }",
        "#trace-panel { width: 1fr; height: 1fr; }",
    ):
        assert rule in css
    assert not re.search(r"(width|height):\s*\d+(?!\w)", css)


async def test_the_log_and_panels_fill_the_screen_in_a_real_layout() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        assert log.size.width >= 90
        assert log.size.height >= 20
        await pilot.click("#--content-tab-tab-state")
        await pilot.pause()
        panel = app.query_one("#state-panel")
        assert panel.size.width >= 90
        assert panel.size.height >= 20
        assert isinstance(app.query_one("#prompt", Input), Input)


# Slice 6c: /export and /import in the prompt

THREE_TURNS = [*TWO_STEPS, reply_events(RESPOND_HELLO, {"goal": "third"})]


class GatedSteps(ScriptedSteps):
    """Like ScriptedSteps, but the second call waits until `gate` is set."""

    def __init__(self, scripts) -> None:
        super().__init__(scripts)
        self.gate = asyncio.Event()

    async def stream_chat(self, req):
        if len(self.requests) == 1:
            await self.gate.wait()
        async for event in super().stream_chat(req):
            yield event


class GatedFactory:
    def __init__(self, scripts) -> None:
        self.provider = GatedSteps(scripts)

    def __call__(self, config):
        return self.provider


def snapshot_of(app: ChatApp):
    assert app._orchestrator is not None
    return app._orchestrator.snapshot()


def errors_of(app: ChatApp) -> list[str]:
    return texts(app, "error")


def notes_of(app: ChatApp) -> list[str]:
    return texts(app, "note")


async def submit(app: ChatApp, text: str) -> None:
    """Submit `text` as Enter does, also while the (disabled) prompt cannot type."""
    await app.on_input_submitted(Input.Submitted(prompt_of(app), text))


async def run_and_export(pilot: Pilot, app: ChatApp, path: Path) -> None:
    await send(pilot, app, "hi")
    await send(pilot, app, f"/export {path}")
    assert notes_of(app)[-1] == f"state exported to {path}"


async def test_export_without_a_path_writes_the_default_file_in_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        await send(pilot, app, "/export")
        target = Path.cwd() / EXPORT_FILENAME
        assert notes_of(app)[-1] == f"state exported to {target}"
        assert target.read_bytes() == export_yaml(snapshot_of(app))
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert prompt_of(app).value == ""
        assert prompt_of(app).has_focus


async def test_export_to_an_explicit_path_and_a_home_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        explicit = tmp_path / "explicit.yaml"
        await send(pilot, app, f"/export {explicit}")
        assert explicit.is_file()
        await send(pilot, app, "/export ~/home.yaml")
        assert (tmp_path / "home.yaml").is_file()
        assert notes_of(app)[-1] == f"state exported to {tmp_path / 'home.yaml'}"


async def test_export_refuses_existing_missing_parent_and_directory(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "old.yaml"
    existing.write_bytes(b"old")
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, f"/export {existing}")
        await send(pilot, app, f"/export {tmp_path / 'nope' / 'x.yaml'}")
        await send(pilot, app, f"/export {tmp_path}")
        lines = errors_of(app)
        assert lines[0] == f"export failed: {existing} already exists"
        assert lines[1].startswith("export failed: ")
        assert "parent directory" in lines[1]
        assert lines[2].startswith("export failed: ") and "directory" in lines[2]
        assert len(lines) == 3
        assert existing.read_bytes() == b"old"
        assert not (tmp_path / "nope").exists()
        # A rejected command stays in the prompt so the path can be fixed.
        assert prompt_of(app).value == f"/export {tmp_path}"
        assert notes_of(app) == []


async def test_export_does_not_follow_a_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    link = tmp_path / "link.yaml"
    link.symlink_to(victim)
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, f"/export {link}")
        assert errors_of(app) == [f"export failed: {link} already exists"]
        assert victim.read_bytes() == b"precious"


async def test_export_without_a_session_says_so(tmp_path: Path) -> None:
    app = make_app(Factory(REPLY), model=MODEL_3)
    async with app.run_test() as pilot:
        assert app._orchestrator is None
        await send(pilot, app, f"/export {tmp_path / 'x.yaml'}")
        await send(pilot, app, "/import whatever")
        assert errors_of(app)[-2:] == [
            "export failed: no session",
            "import failed: no session",
        ]
        assert not (tmp_path / "x.yaml").exists()


async def test_export_then_import_round_trips_into_a_fresh_chat(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.yaml"
    first = make_app(StepFactory(TWO_STEPS))
    async with first.run_test() as pilot:
        await run_and_export(pilot, first, target)
        exported = snapshot_of(first)
    assert exported.state.environment.now.tzinfo is not None
    second = make_app(Factory(REPLY))
    async with second.run_test() as pilot:
        assert snapshot_of(second) != exported
        await send(pilot, second, f"/import {target}")
        assert notes_of(second) == [f"state loaded from {target}"]
        assert snapshot_of(second) == exported
        assert snapshot_of(second).state.environment.now.tzinfo is not None
        assert prompt_of(second).value == ""
        assert shown(second, "state-goal") == "Goal: final goal"
        assert texts(second, "user") == []  # the transcript is not restored


async def test_the_next_turn_overwrites_the_imported_environment(
    tmp_path: Path,
) -> None:
    source = make_app(StepFactory(TWO_STEPS))
    async with source.run_test() as pilot:
        await send(pilot, source, "hi")
        run_state = snapshot_of(source)
    user_name = run_state.state.environment.user_name
    text = export_yaml(run_state).decode()
    assert f"user_name: {user_name}" in text
    target = tmp_path / "evil.yaml"
    target.write_text(text.replace(f"user_name: {user_name}", "user_name: mallory"))
    app = make_app(StepFactory(THREE_TURNS))
    async with app.run_test() as pilot:
        await send(pilot, app, "warm up")  # consumes script 1 and 2
        await send(pilot, app, f"/import {target}")
        assert "user_name: mallory" in shown(app, "state-environment")
        await send(pilot, app, "again")
        assert "mallory" not in shown(app, "state-environment")
        assert snapshot_of(app).state.environment.user_name == user_name


BAD_FILES = {
    "missing": lambda tmp: tmp / "nope.yaml",
    "directory": lambda tmp: tmp,
    "unreadable": lambda tmp: _write(tmp / "locked.yaml", b"goal: x\n", mode=0),
    "not-utf8": lambda tmp: _write(tmp / "bin.yaml", b"\xff\xfe\x00bad"),
    "alias": lambda tmp: _write(tmp / "alias.yaml", b"a: &x [1]\nb: *x\n"),
    "python-tag": lambda tmp: _write(
        tmp / "tag.yaml", b"!!python/object/apply:os.system ['echo hi']\n"
    ),
    "wrong-schema": lambda tmp: _write(tmp / "schema.yaml", b"unknown: 1\n"),
    "not-a-mapping": lambda tmp: _write(tmp / "list.yaml", b"- 1\n- 2\n"),
}


def _write(path: Path, data: bytes, mode: int | None = None) -> Path:
    path.write_bytes(data)
    if mode is not None:
        path.chmod(mode)
    return path


@pytest.mark.parametrize("kind", sorted(BAD_FILES))
async def test_a_bad_import_is_one_line_and_changes_nothing(
    kind: str, tmp_path: Path
) -> None:
    if kind == "unreadable" and os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    path = BAD_FILES[kind](tmp_path)
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        before = snapshot_of(app)
        log_before = len(app.query("#log > *"))
        await send(pilot, app, f"/import {path}")
        (line,) = errors_of(app)
        assert line.startswith("import failed: ")
        assert "\n" not in line and "Traceback" not in line
        assert "os.system" not in line
        assert snapshot_of(app) == before
        assert len(app.query("#log > *")) == log_before + 1  # only the error line
        assert prompt_of(app).value == f"/import {path}"
    if kind == "unreadable":
        path.chmod(0o600)


async def test_import_without_a_path_shows_the_usage_line() -> None:
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, "/import")
        assert errors_of(app) == ["usage: /import <path>"]


async def test_an_oversized_import_is_rejected_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (MAX_IMPORT_BYTES + 1))
    real_fdopen = os.fdopen

    def guarded_fdopen(descriptor: int, mode: str):
        handle = real_fdopen(descriptor, mode)

        class Guarded:
            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> None:
                handle.close()

            def fileno(self) -> int:
                return handle.fileno()

            def read(self, *args: object) -> bytes:
                raise AssertionError("the oversized file was read")

        return Guarded()

    monkeypatch.setattr(state_files.os, "fdopen", guarded_fdopen)
    app = make_app(StepFactory(TWO_STEPS))
    async with app.run_test() as pilot:
        await send(pilot, app, f"/import {big}")
        assert errors_of(app) == [
            f"import failed: file is larger than {MAX_IMPORT_BYTES} bytes"
        ]


async def test_importing_a_fifo_does_not_hang(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    def on_alarm(signum: int, frame: object) -> None:
        raise AssertionError("/import blocked on a FIFO")

    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(20)
    try:
        app = make_app(StepFactory(TWO_STEPS))
        async with app.run_test() as pilot:
            await send(pilot, app, f"/import {fifo}")
            assert errors_of(app) == ["import failed: not a regular file"]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


async def test_import_is_refused_while_a_turn_runs_and_export_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_app(StepFactory(TWO_STEPS))
    async with source.run_test() as pilot:
        await send(pilot, source, "hi")
        other = tmp_path / "other.yaml"
        other.write_bytes(export_yaml(snapshot_of(source)))
    factory = GatedFactory(TWO_STEPS)
    app = make_app(factory)
    async with app.run_test() as pilot:
        prompt_of(app).value = "hi"
        await pilot.press("enter")
        # Step 1 is committed and step 2 waits on the gate: the state is stable.
        await wait_for(
            pilot,
            lambda: (
                len(factory.provider.requests) == 1
                and snapshot_of(app).state.goal == "find the port"
            ),
        )
        assert app._turn_is_running()
        before = snapshot_of(app)

        def must_not_read(path: Path) -> bytes:
            raise AssertionError("the file was read during a turn")

        monkeypatch.setattr(tui_app, "read_import", must_not_read)
        await submit(app, f"/import {other}")
        assert errors_of(app) == [
            "import failed: a turn is running, try again after it"
        ]
        assert snapshot_of(app) == before
        # Export only reads the snapshot, so it is allowed during a turn.
        exported = tmp_path / "during.yaml"
        await submit(app, f"/export {exported}")
        assert exported.read_bytes() == export_yaml(snapshot_of(app))
        factory.provider.gate.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert prompt_of(app).disabled is False


async def test_commands_are_neither_sent_to_the_model_nor_echoed(
    tmp_path: Path,
) -> None:
    factory = StepFactory(TWO_STEPS)
    app = make_app(factory)
    async with app.run_test() as pilot:
        target = tmp_path / "s.yaml"
        await send(pilot, app, f"/EXPORT {target}")
        await send(pilot, app, f"/Import {target}")
        await send(pilot, app, "/import nope.yaml")
        assert factory.provider.requests == []
        assert texts(app, "user") == []
        assert app._turn is None or not app._turn.is_running
        assert target.is_file()


async def test_a_word_that_only_starts_like_a_command_goes_to_the_model() -> None:
    factory = StepFactory(TWO_STEPS)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "/exportx now")
        assert len(factory.provider.requests) == 2
        assert texts(app, "user") == ["you: /exportx now"]


async def test_the_placeholder_lists_the_state_commands() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        placeholder = prompt_of(app).placeholder
        for command in ("/export", "/import", "/quit"):
            assert command in placeholder
