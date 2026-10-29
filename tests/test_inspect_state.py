"""`environment_text`: the Environment lines shared by the GUI and TUI state tabs."""

from datetime import UTC, datetime

from llm_harness.config import AgentConfig
from llm_harness.inspect_state import (
    LONG_FIELDS,
    OPEN_BY_DEFAULT,
    agents_text,
    environment_text,
    long_field_keys,
    long_field_text,
    span_children,
    span_details_text,
    tools_text,
)
from llm_harness.state import Environment, OrchestratorState
from llm_harness.trace import Span
from tests.test_orchestrator import model_cfg

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def state_with(user_id: str | None) -> OrchestratorState:
    return OrchestratorState(
        environment=Environment(
            now=NOW, user_name="u", computer_name="c", user_id=user_id
        )
    )


def test_environment_text_shows_the_user_id_after_the_computer_name() -> None:
    assert environment_text(state_with("ann"), None) == (
        f"now: {NOW.isoformat()}\nuser_name: u\ncomputer_name: c\nuser_id: ann"
    )


def test_environment_text_says_when_the_user_id_is_not_set() -> None:
    assert environment_text(state_with(None), None).endswith("\nuser_id: (not set)")


def test_environment_text_puts_the_pending_question_after_the_user_id() -> None:
    text = environment_text(state_with("ann"), "Which file?")
    assert text.endswith("\nuser_id: ann\npending question: Which file?")


def test_environment_text_sanitizes_the_user_id() -> None:
    text = environment_text(state_with("a\x1b[31mb"), None)
    assert "\x1b" not in text


WRITER = AgentConfig(
    id="writer",
    name="Writer",
    description="Drafts text.",
    tags=["writer", "cheap"],
    model_tags=[],
)
BOSS = AgentConfig(
    id="boss",
    name="Boss",
    description="Leads\x07 the work.",
    tags=["orchestrator"],
    model_tags=["cheap"],
)


def test_agents_text_lists_each_agent_and_marks_the_orchestrator() -> None:
    assert agents_text([BOSS, WRITER]) == (
        "Boss [boss] (orchestrator)\n"
        "  tags: orchestrator\n"
        "  model_tags: cheap\n"
        "  Leads the work.\n"
        "\n"
        "Writer [writer]\n"
        "  tags: writer, cheap\n"
        "  model_tags: (none)\n"
        "  Drafts text."
    )


def test_agents_text_without_agents_says_none() -> None:
    assert agents_text([]) == "(none)"


def test_the_preview_request_lists_the_tools_of_the_orchestrator() -> None:
    from llm_harness.inspect_state import build_preview
    from llm_harness.tools import ToolRegistry
    from tests.test_orchestrator import make
    from tests.test_tools import FakeTool

    tools = ToolRegistry()
    tools.register(FakeTool("alpha", "Does a."))
    orchestrator, _ = make([], tools=tools)
    preview = build_preview(orchestrator, model_cfg(), "")
    assert "## Tools\n\nalpha: Does a." in preview.user_text


def test_the_preview_request_lists_the_delegation_targets() -> None:
    from llm_harness.inspect_state import build_preview
    from tests.test_orchestrator import make

    helper = AgentConfig(
        id="helper",
        name="Helper",
        description="Helps.",
        tags=[],
        model_tags=[],
        examples=["Help me."],
    )
    orchestrator, _ = make([], subagents=[helper])
    preview = build_preview(orchestrator, model_cfg(), "")
    assert "## Subagents\n\nhelper: Helps. (e.g. Help me.)" in preview.user_text


def test_the_preview_request_without_delegation_targets_has_no_subagents_section() -> (
    None
):
    from llm_harness.inspect_state import build_preview
    from tests.test_orchestrator import make

    orchestrator, _ = make([])
    preview = build_preview(orchestrator, model_cfg(), "")
    assert "## Subagents" not in preview.user_text


def test_tools_text_lists_each_tool_and_the_two_token_totals() -> None:
    from tests.test_tools import FakeTool, QueryInput

    tools = [
        FakeTool("alpha", "Does a.\x07", ["web"], QueryInput),
        FakeTool("beta", "Does b."),
    ]
    text = tools_text(tools)
    assert text.startswith("alpha\n  tags: web\n  Does a.\n  schema ~")
    assert "\n\nbeta\n  tags: (none)\n  Does b.\n  schema ~" in text
    # The listing (with its BEL character) is 29 characters -> 8 tokens; the
    # schemas are 116 and 51 characters -> 29 + 13 = 42.
    assert text.endswith(
        "listing sent in every request: ~8 tokens\n"
        "full schemas of these tools would be: ~42 tokens"
    )


def test_tools_text_without_tools_says_none() -> None:
    assert tools_text([]) == "(none)"


def make_span(
    span_id: str,
    parent_id: str | None,
    kind: str,
    attributes: dict | None = None,
) -> Span:
    return Span(
        id=span_id,
        parent_id=parent_id,
        kind=kind,  # type: ignore[arg-type]
        name=f"{kind} {span_id}",
        start_ms=0,
        duration_ms=1,
        status="ok",
        attributes=attributes or {},
    )


def test_subagent_spans_nest_their_child_spans_in_the_inspector_grouping() -> None:
    spans = (
        make_span("s1", None, "turn"),
        make_span("s2", "s1", "step"),
        make_span("s3", "s2", "subagent"),
        make_span("s4", "s3", "step"),
        make_span("s5", "s4", "llm_call"),
    )
    grouped = span_children(spans)
    assert [s.id for s in grouped[None]] == ["s1"]
    assert [s.id for s in grouped["s2"]] == ["s3"]
    assert [s.id for s in grouped["s3"]] == ["s4"]
    assert [s.id for s in grouped["s4"]] == ["s5"]
    assert "parent: s2" in span_details_text(spans[2]).splitlines()
    assert "kind: subagent" in span_details_text(spans[2]).splitlines()
    assert "parent: s3" in span_details_text(spans[3]).splitlines()


def test_state_is_a_long_field_after_result_and_not_a_short_detail_line() -> None:
    assert LONG_FIELDS.index("state") > LONG_FIELDS.index("result")
    span = make_span(
        "s3", "s2", "subagent", {"agent": "researcher", "state": {"goal": "g"}}
    )
    assert long_field_keys(span) == ["state"]
    details = span_details_text(span)
    assert "agent: researcher" in details
    assert "state" not in details
    assert long_field_text(span, "state") == '{\n  "goal": "g"\n}'


def test_state_text_is_whole_for_a_long_value_and_sanitised() -> None:
    state = {"notes": "x" * 5000 + "\x1b[31mred", "goal": "g"}
    span = make_span("s3", "s2", "subagent", {"state": state})
    text = long_field_text(span, "state")
    assert "x" * 5000 in text
    assert '[31mred",\n  "goal": "g"\n}' in text
    assert "\x1b" not in text
    # a JSON string value is shown as is, whole, and sanitised
    as_text = make_span("s4", "s2", "subagent", {"state": "y" * 4000 + "\x07"})
    assert long_field_text(as_text, "state") == "y" * 4000


def test_subagent_spans_are_collapsed_by_default() -> None:
    # The spec makes no front-end change; this pins the current behaviour.
    assert "subagent" not in OPEN_BY_DEFAULT
