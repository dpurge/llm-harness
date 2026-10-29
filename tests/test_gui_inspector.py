"""GUI inspector tabs: Chat | State | Trace, the State tab and its layout rules.

Same limits as tests/test_gui.py: the `user` simulation proves page logic and the
element props the server sends, not how the page looks. Layout is pinned from
classes and CSS text.
"""

import asyncio
import datetime as dt
import re
from pathlib import Path

import pytest
import yaml
from nicegui import ElementFilter, ui
from nicegui.elements.upload import Upload
from nicegui.testing import User

import llm_harness.gui.app as gui_app
from llm_harness.builtin_tools import build_tool_registry
from llm_harness.config import AgentConfig
from llm_harness.context import MAX_OBSERVATION_CHARS, build_request
from llm_harness.gui import inspector
from llm_harness.gui.inspector import span_children, span_title
from llm_harness.inspect_state import preview_observation
from llm_harness.orchestrator import Orchestrator
from llm_harness.state import (
    Environment,
    Finding,
    OrchestratorState,
    PlanStep,
    RunState,
)
from llm_harness.state_io import MAX_IMPORT_BYTES, export_yaml, import_yaml
from llm_harness.trace import Span
from llm_harness.types import (
    Done,
    ErrorEvent,
    TextDelta,
    ThinkingDelta,
)
from tests import test_gui
from tests.fake_steps import ScriptedSteps, reply_events
from tests.fakes import ScriptedProvider, router_factory_for
from tests.test_gui import (
    ASK_FILE,
    CONFIG,
    MODEL_1,
    MODEL_2,
    REPLY,
    RESPOND_HELLO,
    Factory,
    StepFactory,
    choose,
    elements,
    log_texts,
    open_chat,
    send,
    texts,
    wait_for,
    widget,
)
from tests.test_orchestrator import CALL_TOOL, model_cfg

# Fixtures of tests/test_gui.py, shared by name (an import would shadow the parameters).
no_leftover_pages = test_gui.no_leftover_pages
orchestrators = test_gui.orchestrators

CHAT_WIDGETS = ("model", "log", "prompt", "send", "stop", "new-chat")
GUI_DIR = Path(inspector.__file__).parent

# Step 1 sets the state and calls an unknown tool; step 2 answers.
FINDING = {"text": "port is 8080", "source": "config.yaml"}
PLAN = [{"text": "read config", "status": "doing"}]
TWO_STEPS = [
    reply_events(
        CALL_TOOL,
        {
            "goal": "find the port",
            "findings": {"$append": [FINDING]},
            "plan": {"$replace": PLAN},
        },
    ),
    reply_events({"type": "respond", "text": "done"}),
]


def label_text(user: User, marker: str) -> str:
    return widget(user, marker).text


def count_of(user: User, part: str) -> int:
    match = re.search(r"(\d+) characters", label_text(user, f"count-{part}"))
    assert match, label_text(user, f"count-{part}")
    return int(match.group(1))


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


# Tabs


async def test_the_page_has_the_three_tabs_with_marks_and_names(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    with user.client:
        (tabs,) = ElementFilter(kind=ui.tabs, local_scope=False)
        (panels,) = ElementFilter(kind=ui.tab_panels, local_scope=False)
    assert tabs.value == inspector.CHAT_TAB
    assert panels.value == inspector.CHAT_TAB
    names = {
        marker: widget(user, marker).props["name"]
        for marker in ("tab-chat", "tab-state", "tab-trace")
    }
    assert names == {
        "tab-chat": "chat",
        "tab-state": "state",
        "tab-trace": "trace",
    }
    assert [widget(user, m).props["label"] for m in names] == [
        "Chat",
        "State",
        "Trace",
    ]
    assert {widget(user, m).props["name"] for m in ("panel-chat", "panel-state")} == {
        "chat",
        "state",
    }
    assert widget(user, "panel-trace").props["name"] == "trace"


async def test_every_chat_widget_lives_in_the_chat_panel(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    panel = widget(user, "panel-chat")
    with user.client:
        inside = set(panel.descendants())
    for marker in CHAT_WIDGETS:
        assert widget(user, marker) in inside
    assert widget(user, "state-goal") not in inside


async def test_the_trace_tab_is_an_empty_marked_placeholder(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    with user.client:
        labels = [
            child.text
            for child in widget(user, "panel-trace").descendants()
            if isinstance(child, ui.label)
        ]
    assert labels == ["No trace yet."]


# State tab content


async def test_state_tab_shows_goal_findings_plan_environment_and_counters(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, StepFactory(TWO_STEPS))
    await send(user, "hi")
    (orchestrator,) = orchestrators
    env = orchestrator.state.environment
    assert label_text(user, "state-goal") == "Goal: find the port"
    assert texts(user, "state-finding") == ["port is 8080 (source: config.yaml)"]
    assert texts(user, "state-plan-step") == ["[doing] read config"]
    assert label_text(user, "state-counters") == "turn 1, step 2"
    assert label_text(user, "state-environment") == (
        f"now: {env.now.isoformat()}\n"
        f"user_name: {env.user_name}\n"
        f"computer_name: {env.computer_name}\n"
        "user_id: (not set)"
    )


async def test_state_tab_shows_the_configured_user_id_and_sends_it(
    user: User,
) -> None:
    factory = StepFactory(TWO_STEPS)
    gui_app.register_page(
        CONFIG.model_copy(update={"user_id": "ann"}),
        MODEL_1,
        router_factory_for(CONFIG, factory, None),
        False,
    )
    await user.open("/")
    await send(user, "hi")
    assert label_text(user, "state-environment").endswith("\nuser_id: ann")
    assert '"user_id":"ann"' in factory.provider.requests[0].messages[-1].content


async def test_state_tab_before_any_turn_shows_an_empty_state(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    assert label_text(user, "state-goal") == "Goal: (none)"
    assert label_text(user, "state-counters") == "turn 0, step 0"
    assert texts(user, "state-finding") == []
    assert widget(user, "state-empty").visible is False


async def test_a_pending_question_is_shown_in_the_environment_block(
    user: User,
) -> None:
    await open_chat(user, StepFactory([reply_events(ASK_FILE, {"goal": "g"})]))
    await send(user, "hi")
    assert label_text(user, "state-environment").endswith(
        "\npending question: Which file?"
    )


# Next request preview


async def test_preview_counts_and_messages_match_build_request(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, StepFactory(TWO_STEPS))
    await send(user, "hi")
    # Typed text is read when the tab refreshes; New chat refreshes it.
    user.find(marker="prompt").type("what next")
    user.find(marker="new-chat").click()
    await wait_for(lambda: len(orchestrators) == 2)
    await wait_for(lambda: label_text(user, "state-goal") == "Goal: (none)")
    orchestrator = orchestrators[1]
    expected = build_request(orchestrator.state, "what next", MODEL_1)
    system, user_message = expected.messages
    assert label_text(user, "preview-system") == system.content
    assert label_text(user, "preview-user") == user_message.content
    assert count_of(user, "system") == len(system.content)
    assert count_of(user, "state") == len(orchestrator.state.model_dump_json())
    assert count_of(user, "observation") == len("what next")
    assert label_text(user, "count-system").startswith(
        "system (instructions + schemas): "
    )
    assert label_text(user, "count-state").startswith("state JSON: ")
    assert label_text(user, "count-observation").startswith("observation: ")


async def test_preview_observation_is_empty_when_nothing_is_typed(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, Factory(REPLY))
    assert count_of(user, "observation") == 0
    expected = build_request(orchestrators[0].state, "", MODEL_1)
    assert label_text(user, "preview-user") == expected.messages[1].content


def test_preview_observation_wraps_the_typed_text_after_an_ask_user() -> None:
    orchestrator = Orchestrator(ScriptedProvider([]), model_cfg())
    assert preview_observation(orchestrator, "main.py") == "main.py"
    orchestrator.pending_question = "Which file?"
    assert preview_observation(orchestrator, "main.py") == (
        'You asked the user: "Which file?"\nThe user answered: main.py'
    )
    assert preview_observation(orchestrator, "") == ""


async def test_a_long_observation_is_counted_whole_and_marked_as_cut(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    long_text = "x" * (MAX_OBSERVATION_CHARS + 5)
    user.find(marker="prompt").type(long_text)
    user.find(marker="new-chat").click()
    await wait_for(
        lambda: label_text(user, "count-observation").endswith("in the request)")
    )
    assert count_of(user, "observation") == MAX_OBSERVATION_CHARS + 5
    assert f"cut to {MAX_OBSERVATION_CHARS}" in label_text(user, "count-observation")


# Refresh timing


async def test_the_state_shows_step_one_before_the_turn_ends(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    factory = GatedFactory(TWO_STEPS)
    await open_chat(user, factory)
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: label_text(user, "state-goal") == "Goal: find the port")
    # The turn is still running: the prompt is disabled and step 2 has not answered.
    assert widget(user, "prompt").enabled is False
    assert label_text(user, "state-counters") == "turn 1, step 1"
    assert texts(user, "state-finding") == ["port is 8080 (source: config.yaml)"]
    factory.provider.gate.set()
    await wait_for(lambda: widget(user, "prompt").enabled)
    assert label_text(user, "state-counters") == "turn 1, step 2"


async def test_the_preview_is_refreshed_at_the_end_of_an_error_turn(
    user: User,
) -> None:
    factory = Factory([ErrorEvent(kind="connection", message="boom")])
    await open_chat(user, factory)
    await send(user, "hello there")
    # The failed text is restored to the prompt, so it is the next observation.
    assert widget(user, "prompt").value == "hello there"
    assert count_of(user, "observation") == len("hello there")


async def test_new_chat_shows_the_new_orchestrator_state(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, StepFactory([*TWO_STEPS, *TWO_STEPS]))
    await send(user, "hi")
    assert label_text(user, "state-goal") == "Goal: find the port"
    user.find(marker="new-chat").click()
    await wait_for(lambda: label_text(user, "state-goal") == "Goal: (none)")
    assert len(orchestrators) == 2
    assert label_text(user, "state-counters") == "turn 0, step 0"
    assert texts(user, "state-finding") == []
    assert texts(user, "state-plan-step") == []
    # The next turn of the new session fills the same widgets again.
    await send(user, "again")
    assert label_text(user, "state-counters") == "turn 1, step 2"
    assert orchestrators[1].state.turn == 1


async def test_a_model_change_before_any_turn_follows_the_new_session(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, Factory(REPLY))
    choose(user, MODEL_2.id)
    await wait_for(lambda: len(orchestrators) == 2)
    await wait_for(
        lambda: count_of(user, "state") == len(orchestrators[1].state.model_dump_json())
    )
    expected = build_request(orchestrators[1].state, "", MODEL_2)
    assert label_text(user, "preview-user") == expected.messages[1].content


# Hostile text


HOSTILE = "<script>alert(1)</script>[bold]**x**\x1b[31m\x07z"
HOSTILE_CLEAN = "<script>alert(1)</script>[bold]**x**[31mz"


async def test_hostile_model_text_is_shown_literally_and_sanitised(
    user: User,
) -> None:
    patch = {
        "goal": HOSTILE,
        "findings": {"$append": [{"text": HOSTILE, "source": HOSTILE}]},
        "plan": {"$replace": [{"text": HOSTILE, "status": "todo"}]},
    }
    steps = [
        reply_events(CALL_TOOL, patch),
        reply_events({"type": "ask_user", "question": HOSTILE}),
    ]
    await open_chat(user, StepFactory(steps))
    await send(user, "hi")
    assert label_text(user, "state-goal") == f"Goal: {HOSTILE_CLEAN}"
    assert texts(user, "state-finding") == [
        f"{HOSTILE_CLEAN} (source: {HOSTILE_CLEAN})"
    ]
    assert texts(user, "state-plan-step") == [f"[todo] {HOSTILE_CLEAN}"]
    assert label_text(user, "state-environment").endswith(
        f"pending question: {HOSTILE_CLEAN}"
    )
    assert "\x1b" not in label_text(user, "state-goal")
    shown = [
        *elements(user, "state-goal"),
        *elements(user, "state-finding"),
        *elements(user, "state-plan-step"),
        *elements(user, "state-environment"),
        *elements(user, "preview-user"),
    ]
    for element in shown:
        assert isinstance(element, ui.label)
        assert "innerHTML" not in element.props
    assert "\x1b" not in label_text(user, "preview-user")


def test_the_inspector_source_uses_no_html_markdown_or_code_element() -> None:
    source = (GUI_DIR / "inspector.py").read_text("utf-8")
    for forbidden in ("ui.html", "ui.markdown", "ui.code", "ui.mermaid"):
        assert forbidden not in source


def test_every_model_string_in_the_state_panel_goes_through_sanitize_text() -> None:
    # The text building moved to the framework-free module both UIs share.
    source = (GUI_DIR.parent / "inspect_state.py").read_text("utf-8")
    for expression in (
        "state.goal",
        "finding.text",
        "finding.source",
        "step.text",
        "env.user_name",
        "env.computer_name",
        "question",
    ):
        assert f"sanitize_text({expression})" in source


# Layout


async def test_the_tabs_and_panels_use_the_full_width_and_height(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    with user.client:
        (tabs,) = ElementFilter(kind=ui.tabs, local_scope=False)
        (panels,) = ElementFilter(kind=ui.tab_panels, local_scope=False)
    assert "w-full" in tabs.classes
    assert {"w-full", "grow"} <= set(panels.classes)
    for marker in ("panel-chat", "panel-state", "panel-trace"):
        assert {"w-full", "h-full"} <= set(widget(user, marker).classes)
    state_scroll = widget(user, "state-scroll")
    assert {"w-full", "grow"} <= set(state_scroll.classes)
    assert isinstance(state_scroll, ui.scroll_area)


async def test_state_text_wraps_and_no_new_element_has_a_forbidden_size_class(
    user: User,
) -> None:
    await open_chat(user, StepFactory(TWO_STEPS))
    await send(user, "hi")
    for marker in ("state-goal", "state-finding", "state-plan-step", "preview-user"):
        assert "whitespace-pre-wrap" in widget_or_first(user, marker).classes
    with user.client:
        everything = list(ElementFilter(local_scope=False))
    for element in everything:
        for name in element.classes:
            assert not name.startswith(("max-w", "max-h"))
            assert name != "mx-auto"
            assert not re.fullmatch(r"h-\d+(\.\d+)?", name), name
        style = str(element.props.get("style", "")) + ";".join(
            f"{k}:{v}" for k, v in element._style.items()
        )
        assert not re.search(r"(?<![\w-])height:\s*\d+px", style)


def widget_or_first(user: User, marker: str):
    return elements(user, marker)[0]


def test_gui_sources_have_no_narrow_column_or_fixed_height_tokens() -> None:
    sources = sorted(GUI_DIR.glob("*.py"))
    assert {p.name for p in sources} >= {"app.py", "inspector.py"}
    forbidden = (
        r"max-w-",
        r"mx-auto",
        r"(?<![\w-])h-\d",
        r"(?<![\w-])max-h-\d",
        r"(?<![\w-])height:\s*\d+px",
    )
    for path in sources:
        text = path.read_text("utf-8")
        for pattern in forbidden:
            assert not re.search(pattern, text), f"{path.name}: {pattern}"


def font_sizes_px(css: str) -> list[int]:
    return [int(n) for n in re.findall(r"font-size:\s*(\d+)px", css)]


def test_css_has_an_18px_base_and_no_font_below_15px() -> None:
    page = gui_app._PAGE_CSS
    assert re.search(r"\.llm-page\s*\{\s*font-size:\s*(\d+)px", page)
    base = int(re.search(r"\.llm-page\s*\{\s*font-size:\s*(\d+)px", page).group(1))
    assert base >= 18
    for css in (page, gui_app._MARKDOWN_CSS, inspector.INSPECTOR_CSS):
        sizes = font_sizes_px(css)
        assert all(size >= 15 for size in sizes), sizes
    assert font_sizes_px(inspector.INSPECTOR_CSS) == [18]


def test_state_text_uses_the_existing_secondary_class() -> None:
    assert "llm-secondary" in inspector._SECONDARY
    assert ".llm-secondary" in gui_app._PAGE_CSS


# Collapsible State sections

SECTIONS_OPEN = {
    "section-goal": True,
    "section-findings": True,
    "section-plan": True,
    "section-environment": True,
    "section-preview-system": False,
    "section-preview-state": False,
    "section-preview-observation": True,
}


async def test_every_state_section_is_an_expansion_with_the_right_default(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    for marker, is_open in SECTIONS_OPEN.items():
        section = widget(user, marker)
        assert isinstance(section, ui.expansion), marker
        assert section.value is is_open, marker
    inside = {
        "section-goal": "state-goal",
        "section-findings": "state-findings",
        "section-plan": "state-plan",
        "section-environment": "state-environment",
        "section-preview-system": "preview-system",
        "section-preview-state": "preview-user",
        "section-preview-observation": "count-observation",
    }
    for section_marker, content_marker in inside.items():
        with user.client:
            descendants = set(widget(user, section_marker).descendants())
        assert widget(user, content_marker) in descendants, content_marker


async def test_a_section_the_user_toggled_keeps_its_state_across_refreshes(
    user: User,
) -> None:
    await open_chat(user, StepFactory([*TWO_STEPS, *TWO_STEPS]))
    sections = {marker: widget(user, marker) for marker in SECTIONS_OPEN}
    with user.client:
        sections["section-plan"].value = False  # open by default, closed by the user
        sections["section-preview-system"].value = True  # collapsed by default
    await send(user, "hi")  # refreshes after each step and at the turn end
    user.find(marker="new-chat").click()  # and once more for a new session
    await wait_for(lambda: label_text(user, "state-goal") == "Goal: (none)")
    expected = {**SECTIONS_OPEN, "section-plan": False, "section-preview-system": True}
    for marker, is_open in expected.items():
        # The very same elements, not rebuilt ones.
        assert widget(user, marker) is sections[marker], marker
        assert widget(user, marker).value is is_open, marker
    assert texts(user, "state-plan-step") == []  # content still refreshes when closed


# Trace tab

REJECTED_REPLY = [TextDelta(text="this is not json"), Done()]


def span_titles(user: User) -> dict[str, str]:
    return {
        next(m for m in e._markers if m.startswith("trace-span-")).removeprefix(
            "trace-span-"
        ): e.props["label"]
        for e in elements(user, "trace-span")
    }


def span_id_of(element) -> str:
    return next(
        m for m in element._markers if m.startswith("trace-span-")
    ).removeprefix("trace-span-")


def span_parents(user: User) -> dict[str, str | None]:
    parents: dict[str, str | None] = {}
    for element in elements(user, "trace-span"):
        with user.client:
            enclosing = next(
                (a for a in element.ancestors() if "trace-span" in a._markers), None
            )
        parents[span_id_of(element)] = (
            None if enclosing is None else span_id_of(enclosing)
        )
    return parents


def field_text(user: User, span_id: str, key: str) -> str:
    with user.client:
        (label,) = [
            child
            for child in widget(user, f"trace-span-{span_id}").descendants()
            if "trace-text-" + key in child._markers
        ]
    return label.text


TRACE_STEPS = [
    REJECTED_REPLY,
    reply_events(
        CALL_TOOL, {"goal": "g"}, ThinkingDelta(text="hmm "), ThinkingDelta(text="ok")
    ),
    reply_events({"type": "respond", "text": "done"}),
]


async def test_the_trace_tab_shows_the_span_tree_of_the_last_turn(user: User) -> None:
    await open_chat(user, StepFactory(TRACE_STEPS))
    await send(user, "hi")
    assert span_parents(user) == {
        "s1": None,
        "s2": "s1",
        "s3": "s2",  # rejected attempt
        "s4": "s2",  # the retry that was accepted
        "s5": "s2",  # the tool call of step 1
        "s6": "s1",
        "s7": "s6",
    }
    statuses = {
        span_id: title.split(" | ")[1] for span_id, title in span_titles(user).items()
    }
    assert statuses == {
        "s1": "ok",
        "s2": "ok",
        "s3": "rejected",
        "s4": "ok",
        "s5": "error",  # no tool registry: "not available"
        "s6": "ok",
        "s7": "ok",
    }
    for title in span_titles(user).values():
        assert re.fullmatch(
            r"(turn|step|llm_call|tool_call): .+ \| \w+ \| [\d.e+-]+ ms", title
        )
    assert span_titles(user)["s5"].startswith("tool_call: search | error | ")
    assert span_titles(user)["s3"].startswith(
        "llm_call: provider-model-1 | rejected | "
    )
    # Colour is a second signal only: the status word is in the title.
    assert widget(user, "trace-span-s3").props["header-class"] == "text-warning"
    assert widget(user, "trace-span-s5").props["header-class"] == "text-negative"
    assert widget(user, "trace-empty").visible is False


async def test_span_details_show_every_attribute_in_collapsed_fields(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, StepFactory(TRACE_STEPS))
    await send(user, "hi")
    spans = {span.id: span for span in orchestrators[0].last_trace or ()}
    # rejected llm_call: request, raw reply and the rejection reason, no thinking
    assert field_text(user, "s3", "reply") == "this is not json"
    assert field_text(user, "s3", "rejection") == spans["s3"].attributes["rejection"]
    request = field_text(user, "s3", "messages")
    assert request.startswith("[system]\n")
    assert "\n\n[user]\n" in request
    # accepted llm_call with thinking
    assert field_text(user, "s4", "thinking") == "hmm ok"
    # tool call: arguments and result
    assert field_text(user, "s5", "arguments") == "{}"
    assert field_text(user, "s5", "result") == spans["s5"].attributes["result"]
    assert "is not available" in field_text(user, "s5", "result")
    details = widget_in(user, "s3", "trace-details").text
    assert "kind: llm_call" in details and "status: rejected" in details
    assert "model: provider-model-1" in details
    for span_id in ("s3", "s4", "s5"):
        for key in (
            "messages",
            "thinking",
            "reply",
            "rejection",
            "arguments",
            "result",
        ):
            with user.client:
                fields = [
                    c
                    for c in widget(user, f"trace-span-{span_id}").descendants()
                    if f"trace-field-{key}" in c._markers
                ]
            for field in fields:
                assert field.value is False  # collapsed by default
                assert isinstance(field, ui.expansion)


def widget_in(user: User, span_id: str, marker: str):
    with user.client:
        return next(
            child
            for child in widget(user, f"trace-span-{span_id}").descendants()
            if marker in child._markers
        )


async def test_a_20000_character_reply_is_shown_whole(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    long_text = "word " * 4000
    steps = [reply_events({"type": "respond", "text": long_text})]
    await open_chat(user, StepFactory(steps))
    await send(user, "hi")
    reply = field_text(user, "s3", "reply")
    assert long_text in reply and len(reply) > 20000
    reply_label = widget_in(user, "s3", "trace-text-reply")
    assert {"whitespace-pre-wrap", "llm-trace-text", "w-full"} <= set(
        reply_label.classes
    )
    assert ".llm-trace-text { overflow-wrap: anywhere; }" in inspector.INSPECTOR_CSS


async def test_hostile_text_in_the_trace_is_literal_and_sanitised(user: User) -> None:
    steps = [
        reply_events(
            {"type": "respond", "text": HOSTILE},
            {"goal": "g"},
            ThinkingDelta(text=HOSTILE),
        )
    ]
    await open_chat(user, StepFactory(steps))
    await send(user, "hi")
    # The raw reply is the JSON text the model sent: the markup is in it literally.
    assert "<script>alert(1)</script>[bold]**x**" in field_text(user, "s3", "reply")
    assert "\x1b" not in field_text(user, "s3", "reply")
    assert "\x07" not in field_text(user, "s3", "reply")
    assert field_text(user, "s3", "thinking") == HOSTILE_CLEAN
    for element in elements(user, "trace-details"):
        assert isinstance(element, ui.label)
    for marker in ("trace-text-reply", "trace-text-thinking", "trace-text-messages"):
        for element in elements(user, marker):
            assert isinstance(element, ui.label)
            assert "innerHTML" not in element.props


async def test_a_hostile_tool_name_is_sanitised_in_title_and_details(
    user: User,
) -> None:
    call = {"type": "call_tool", "tool": HOSTILE, "arguments": {"k": HOSTILE}}
    steps = [reply_events(call), reply_events({"type": "respond", "text": "x"})]
    await open_chat(user, StepFactory(steps))
    await send(user, "hi")
    title = span_titles(user)["s4"]
    assert title.startswith(f"tool_call: {HOSTILE_CLEAN} | error | ")
    assert "\x1b" not in title
    assert HOSTILE_CLEAN in widget_in(user, "s4", "trace-details").text
    assert "\x1b" not in field_text(user, "s4", "arguments")


def make_span(span_id: str, parent_id: str | None, kind: str = "step") -> Span:
    return Span(
        id=span_id,
        parent_id=parent_id,
        kind=kind,
        name=f"n-{span_id}",
        start_ms=0.0,
        duration_ms=1.5,
        status="ok",
        attributes={},
    )


def test_a_span_with_an_unknown_parent_is_listed_under_the_root_in_order() -> None:
    # A running turn: the turn s1 and step s2 are still open, so not in the list.
    spans = (make_span("s3", "s2", "llm_call"), make_span("s4", "s2", "tool_call"))
    grouped = span_children(spans)
    assert [span.id for span in grouped[None]] == ["s3", "s4"]
    complete = (make_span("s1", None, "turn"), *spans, make_span("s2", "s1"))
    assert [s.id for s in span_children(complete)[None]] == ["s1"]
    assert [s.id for s in span_children(complete)["s2"]] == ["s3", "s4"]
    assert span_title(spans[0]).startswith("llm_call: n-s3 | ok | 1.5 ms")


async def test_orphan_spans_are_shown_under_the_root_not_hidden(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    spans = (make_span("s3", "s2", "llm_call"), make_span("s4", "s2", "tool_call"))
    monkeypatch.setattr(Orchestrator, "last_trace", property(lambda self: spans))
    await open_chat(user, Factory(REPLY))
    assert list(span_titles(user)) == ["s3", "s4"]
    assert span_parents(user) == {"s3": None, "s4": None}
    assert widget(user, "trace-empty").visible is False


async def test_the_trace_is_refreshed_after_each_step_and_keeps_open_spans(
    user: User,
) -> None:
    factory = GatedFactory(TWO_STEPS)
    await open_chat(user, factory)
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: "s3" in span_titles(user))
    # Mid-turn: the prompt is disabled, the turn span is still open (not listed),
    # and the closed child spans are shown at the root level.
    assert widget(user, "prompt").enabled is False
    assert not any(t.startswith("turn:") for t in span_titles(user).values())
    assert span_titles(user)["s3"].startswith("llm_call: ")
    with user.client:
        widget(user, "trace-span-s3").value = True  # the user opens a span
    factory.provider.gate.set()
    await wait_for(lambda: widget(user, "prompt").enabled)
    assert span_titles(user)["s1"].startswith("turn: ")
    assert widget(user, "trace-span-s3").value is True  # open state survived
    assert widget(user, "trace-span-s4").value is False  # never opened


async def test_a_cancelled_turn_shows_its_partial_trace(user: User) -> None:
    factory = StepFactory([reply_events(CALL_TOOL)])
    factory.provider = ScriptedSteps([reply_events(CALL_TOOL)], hang_after=1)
    await open_chat(user, factory)
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: widget(user, "stop").enabled)
    await asyncio.sleep(0.05)
    user.find(marker="stop").click()
    await wait_for(lambda: widget(user, "prompt").enabled)
    titles = span_titles(user)
    assert [t.split(" | ")[1] for t in titles.values()] == ["cancelled"] * 3
    assert titles["s1"].startswith("turn: ")


async def test_an_error_turn_shows_an_error_status(user: User) -> None:
    factory = Factory([ErrorEvent(kind="connection", message="boom")])
    await open_chat(user, factory)
    await send(user, "hello")
    titles = span_titles(user)
    assert titles["s1"].split(" | ")[1] == "error"
    assert titles["s3"].split(" | ")[1] == "error"
    assert "error: boom" in widget_in(user, "s3", "trace-details").text


async def test_the_trace_is_empty_before_a_turn_and_new_chat_clears_it(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, StepFactory([*TWO_STEPS, *TWO_STEPS]))
    assert widget(user, "trace-empty").text == "No trace yet."
    assert widget(user, "trace-empty").visible is True
    assert elements(user, "trace-span") == []
    await send(user, "hi")
    assert widget(user, "trace-empty").visible is False
    assert len(span_titles(user)) == 6
    user.find(marker="new-chat").click()
    await wait_for(lambda: len(orchestrators) == 2)
    await wait_for(lambda: elements(user, "trace-span") == [])
    assert widget(user, "trace-empty").visible is True


async def test_a_new_turn_replaces_the_trace_of_the_old_one(user: User) -> None:
    await open_chat(user, StepFactory([*TWO_STEPS, *TWO_STEPS]))
    await send(user, "one")
    assert len(span_titles(user)) == 6
    await send(user, "two")
    assert len(span_titles(user)) == 6  # not 12: only the last turn
    assert "turn: " in span_titles(user)["s1"]


async def test_the_trace_panel_uses_the_full_width_and_scrolls_inside(
    user: User,
) -> None:
    await open_chat(user, StepFactory(TWO_STEPS))
    await send(user, "hi")
    scroll = widget(user, "trace-scroll")
    assert isinstance(scroll, ui.scroll_area)
    assert {"w-full", "grow"} <= set(scroll.classes)
    for element in elements(user, "trace-span"):
        assert "w-full" in element.classes
    assert "w-full" in widget(user, "trace-tree").classes


def test_the_trace_panel_source_passes_every_model_string_through_sanitize_text() -> (
    None
):
    # The trace text is built in the neutral module both front ends share.
    source = (GUI_DIR.parent / "inspect_state.py").read_text("utf-8")
    assert "return sanitize_text(" in source
    # Attributes, names and message contents are only shown through `trace_text`.
    for expression in (
        "trace_text(span.name)",
        "trace_text(value)",
        "trace_text(message[",
    ):
        assert expression in source


async def test_usage_model_and_error_attributes_are_shown_in_the_details(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The router turns a provider's usage into a UsageReport, so a scripted turn
    # does not carry `usage`; show a span that does.
    span = make_span("s1", None, "llm_call").model_copy(
        update={
            "attributes": {
                "model": "m1",
                "reported_model": "m2",
                "usage": {"input_tokens": 3, "output_tokens": 7},
                "error": "boom",
            }
        }
    )
    monkeypatch.setattr(Orchestrator, "last_trace", property(lambda self: (span,)))
    await open_chat(user, Factory(REPLY))
    details = widget_in(user, "s1", "trace-details").text
    assert "model: m1\nreported_model: m2\n" in details
    assert '"input_tokens": 3' in details and '"output_tokens": 7' in details
    assert details.endswith("error: boom")


# Export and import of the state (State tab header row)


def run_state_with(**fields) -> RunState:
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=2)))
    environment = Environment(now=now, user_name="mallory", computer_name="evil-box")
    return RunState(
        state=OrchestratorState(environment=environment, **fields),
        pending_question="Which file?",
    )


async def upload_state(user: User, data: bytes, *, name: str = "state.yaml") -> None:
    """Hand a file to the Import widget as NiceGUI's own upload route would."""
    upload = widget(user, "import-state")
    await upload.handle_uploads([Upload.SmallFileUpload(name, "text/yaml", data)])


def status(user: User) -> str:
    return label_text(user, "transfer-status")


async def wait_for_status(user: User) -> str:
    await wait_for(lambda: status(user) != "")
    return status(user)


@pytest.fixture
def downloads(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[bytes, str, str]]:
    # `user.open` swaps `ui.download` for `user.download`: spy on that instance.
    calls: list[tuple[bytes, str, str]] = []
    monkeypatch.setattr(
        user.download,
        "content",
        lambda content, filename=None, media_type="": calls.append(
            (content, filename, media_type)
        ),
    )
    return calls


async def test_export_and_import_are_marked_in_the_state_tab_header_row(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    export, upload = widget(user, "export-state"), widget(user, "import-state")
    assert isinstance(export, ui.button) and isinstance(upload, ui.upload)
    with user.client:
        inside = set(widget(user, "panel-state").descendants())
        scroll_content = set(widget(user, "state-scroll").descendants())
    assert {export, upload} <= inside
    assert not {export, upload} & scroll_content
    assert export.parent_slot.parent is upload.parent_slot.parent
    assert "w-full" in export.parent_slot.parent.classes
    assert upload.props["max-file-size"] == MAX_IMPORT_BYTES == 1_000_000


_BUTTON_ONLY = {"label", "icon"}


async def test_export_and_import_buttons_look_the_same_and_the_uploader_is_hidden(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    export = widget(user, "export-state")
    button = widget(user, "import-state-button")
    upload = widget(user, "import-state")
    assert isinstance(export, ui.button) and isinstance(button, ui.button)
    assert export.props["label"] == "Export state"
    assert button.props["label"] == "Import state"
    same = lambda element: (
        {k: v for k, v in element.props.items() if k not in _BUTTON_ONLY},
        sorted(element.classes),
    )
    assert same(export) == same(button)
    assert "hidden" in upload.classes
    assert export.parent_slot.parent is button.parent_slot.parent


async def test_import_button_opens_the_picker_from_a_client_side_click_handler(
    user: User,
) -> None:
    """Pins the mechanism only: the `user` fixture runs no browser.

    A file dialog opens only inside the user's click, so the handler must run in
    the browser (`js_handler`) and reach the hidden uploader's QUploader; a
    server-side `run_method` would arrive too late. That the dialog really opens
    is checked by `tests/manual/verify_import_button.py` in a real Chromium.
    """
    await open_chat(user, Factory(REPLY))
    button = widget(user, "import-state-button")
    upload = widget(user, "import-state")
    (listener,) = [
        listener
        for listener in button._event_listeners.values()
        if listener.type == "click"
    ]
    assert listener.js_handler == (
        f"() => getElement({upload.id}).$refs.qRef.pickFiles()"
    )
    assert listener.handler is None  # no server round trip in the click


async def test_status_label_is_below_the_button_row_in_a_full_width_column(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    row = widget(user, "export-state").parent_slot.parent
    label = widget(user, "transfer-status")
    column = row.parent_slot.parent
    assert label.parent_slot.parent is column
    children = list(column)
    assert children.index(row) < children.index(label)
    assert "w-full" in column.classes and "w-full" in label.classes
    assert "whitespace-pre-wrap" in label.classes
    assert "llm-secondary" in label.classes


async def test_export_downloads_yaml_that_imports_back_to_the_same_state(
    user: User,
    orchestrators: list[Orchestrator],
    downloads: list[tuple[bytes, str, str]],
) -> None:
    await open_chat(user, StepFactory(TWO_STEPS))
    await send(user, "hi")
    (orchestrator,) = orchestrators
    user.find(marker="tab-state").click()
    user.find(marker="export-state").click()
    await wait_for(lambda: bool(downloads))
    ((content, filename, media_type),) = downloads
    assert (filename, media_type) == ("llm-harness-state.yaml", "application/yaml")
    assert import_yaml(content) == orchestrator.snapshot()
    assert b"port is 8080" in content
    assert status(user) == "state exported"


async def test_import_loads_the_state_and_refreshes_the_state_tab(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, Factory(REPLY))
    (orchestrator,) = orchestrators
    imported = run_state_with(
        goal="imported goal",
        findings=[Finding(text="f1", source="s1")],
        plan=[PlanStep(text="p1", status="todo")],
        turn=4,
    )
    await upload_state(user, export_yaml(imported))
    assert await wait_for_status(user) == "state loaded"
    assert orchestrator.snapshot() == imported
    assert label_text(user, "state-goal") == "Goal: imported goal"
    assert texts(user, "state-finding") == ["f1 (source: s1)"]
    assert texts(user, "state-plan-step") == ["[todo] p1"]
    assert label_text(user, "state-counters") == "turn 4, step 0"
    assert label_text(user, "state-environment").endswith(
        "\npending question: Which file?"
    )


async def test_the_next_turn_overwrites_the_imported_environment(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, StepFactory([reply_events(RESPOND_HELLO)]))
    (orchestrator,) = orchestrators
    harness_user = orchestrator.state.environment.user_name
    assert harness_user != "mallory"
    await upload_state(user, export_yaml(run_state_with(goal="g")))
    assert await wait_for_status(user) == "state loaded"
    assert orchestrator.state.environment.user_name == "mallory"
    await send(user, "hi")
    assert orchestrator.state.environment.user_name == harness_user
    assert orchestrator.state.environment.computer_name != "evil-box"


async def test_import_does_not_touch_the_chat_transcript(user: User) -> None:
    await open_chat(user, StepFactory(TWO_STEPS))
    await send(user, "hi")
    before = log_texts(user)
    await upload_state(user, export_yaml(run_state_with(goal="g")))
    assert await wait_for_status(user) == "state loaded"
    assert log_texts(user) == before


def dump_bytes(data: object) -> bytes:
    return yaml.safe_dump(data).encode("utf-8")


def valid_dump() -> dict:
    return run_state_with(goal="g").model_dump(mode="json")


def with_state_field(key: str, value: object) -> bytes:
    data = valid_dump()
    data["state"][key] = value
    return dump_bytes(data)


def naive_now() -> bytes:
    data = valid_dump()
    data["state"]["environment"]["now"] = "2026-10-28T13:42:13"
    return dump_bytes(data)


def blank_source() -> bytes:
    return with_state_field("findings", [{"text": "t", "source": " "}])


BAD_FILES = {
    "unknown key": with_state_field("surprise", 1),
    "wrong type": with_state_field("turn", "not a number"),
    "naive now": naive_now(),
    "blank source": blank_source(),
    "python tag": b"!!python/object/apply:os.getcwd []",
    "not utf-8": b"\xff\xfe",
    "not a mapping": b"- 1\n",
    "empty": b"",
    "oversize": b" " * (MAX_IMPORT_BYTES + 1),
}


@pytest.mark.parametrize("name", BAD_FILES)
async def test_a_bad_file_shows_one_line_and_changes_nothing(
    user: User, orchestrators: list[Orchestrator], name: str
) -> None:
    await open_chat(user, StepFactory(TWO_STEPS))
    await send(user, "hi")
    (orchestrator,) = orchestrators
    snapshot, transcript = orchestrator.snapshot(), log_texts(user)
    goal_line = label_text(user, "state-goal")
    await upload_state(user, BAD_FILES[name])
    line = await wait_for_status(user)
    assert line.startswith("import failed: ") and "\n" not in line
    assert len(line) < 300
    assert orchestrator.snapshot() == snapshot
    assert log_texts(user) == transcript
    assert label_text(user, "state-goal") == goal_line


class UnreadableUpload(Upload.SmallFileUpload):
    async def read(self) -> bytes:
        raise AssertionError("read a file over the cap")


async def test_an_oversize_file_is_refused_before_its_bytes_are_read(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    big = UnreadableUpload("big.yaml", "text/yaml", b" " * (MAX_IMPORT_BYTES + 1))
    await widget(user, "import-state").handle_uploads([big])
    assert await wait_for_status(user) == (
        "import failed: file is larger than 1000000 bytes"
    )


async def test_a_file_of_exactly_the_cap_is_judged_by_its_content(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    await upload_state(user, b"x" * MAX_IMPORT_BYTES)
    assert await wait_for_status(user) == ("import failed: top level must be a mapping")


async def test_import_is_refused_while_a_turn_runs_and_buttons_are_disabled(
    user: User, orchestrators: list[Orchestrator]
) -> None:
    await open_chat(user, Factory(REPLY, hang_after=0))
    (orchestrator,) = orchestrators
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: not widget(user, "prompt").enabled)
    assert not widget(user, "export-state").enabled
    assert not widget(user, "import-state").enabled
    assert not widget(user, "import-state-button").enabled
    snapshot = orchestrator.snapshot()
    await upload_state(user, export_yaml(run_state_with(goal="intruder")))
    assert await wait_for_status(user) == (
        "import failed: a turn is running, try again after it"
    )
    assert orchestrator.snapshot() == snapshot
    user.find(marker="stop").click()
    await wait_for(lambda: widget(user, "prompt").enabled)
    assert widget(user, "export-state").enabled
    assert widget(user, "import-state").enabled
    assert widget(user, "import-state-button").enabled


async def test_hostile_text_in_an_imported_file_is_shown_literally(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    hostile = "<img src=x onerror=alert(1)>\x1b[31m <script>x</script>"
    imported = run_state_with(
        goal=hostile, findings=[Finding(text=hostile, source=hostile)]
    )
    await upload_state(user, export_yaml(imported))
    assert await wait_for_status(user) == "state loaded"
    for marker in ("state-goal", "state-finding", "transfer-status"):
        (element,) = elements(user, marker)
        assert isinstance(element, ui.label)
    assert "<img src=x onerror=alert(1)>" in label_text(user, "state-goal")
    assert "\x1b" not in label_text(user, "state-goal")
    assert "<script>x</script>" in texts(user, "state-finding")[0]


async def test_a_hostile_key_name_in_a_bad_file_is_one_literal_sanitised_line(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    await upload_state(
        user, with_state_field(f"<img src=x>\n\x1b[31m{chr(0x202E)}evil", 1)
    )
    line = await wait_for_status(user)
    assert "<img src=x>" in line
    assert "\n" not in line and "\x1b" not in line and chr(0x202E) not in line


def test_the_transfer_code_never_puts_file_content_into_html_or_a_log() -> None:
    source = (GUI_DIR / "inspector.py").read_text("utf-8")
    for forbidden in ("ui.html", "ui.markdown", "sanitize=", "logging", "print("):
        assert forbidden not in source


AGENT = AgentConfig(
    id="boss",
    name="Boss",
    description="Leads.",
    tags=["orchestrator"],
    model_tags=[],
)


async def test_the_state_tab_lists_the_agents_before_the_first_message(
    user: User,
) -> None:
    agent = AGENT
    config = CONFIG.model_copy(update={"agents": [agent]})
    test_gui.register_page(
        config, MODEL_1, router_factory_for(config, Factory(REPLY), None), False
    )
    await user.open("/")
    assert label_text(user, "state-agents").startswith("Boss [boss] (orchestrator)")


async def test_the_state_tab_shows_the_tools_of_the_orchestrator(user: User) -> None:
    config = CONFIG.model_copy(update={"agents": [AGENT]})
    tools = build_tool_registry(config.agents)
    test_gui.register_page(
        config,
        MODEL_1,
        router_factory_for(config, Factory(REPLY), None),
        False,
        tools=tools,
    )
    await user.open("/")
    text = label_text(user, "state-tools")
    assert text.startswith(
        "tool_search\n  tags: orchestrator\n  Find the tools you may use"
    )
    assert "listing sent in every request: ~" in text
    assert "full schemas of these tools would be: ~" in text
