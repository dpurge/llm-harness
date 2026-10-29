"""Context builder: one bounded request per turn (offline, deterministic)."""

import json
import re
from importlib.resources import files
from pathlib import Path

from llm_harness.config import AgentConfig, ModelConfig
from llm_harness.context import (
    MAX_OBSERVATION_CHARS,
    _system_prompt,
    build_request,
    delegation_targets,
    subagent_instructions,
    subagent_lines,
)
from llm_harness.state import (
    DEFAULT_MAX_CHARS,
    Environment,
    Finding,
    OrchestratorState,
    Reply,
    reply_schema,
)
from tests.test_state import NOW
from tests.test_tools import FakeTool

CANARY = "CANARY-7f3a91"
INSTRUCTIONS = (files("llm_harness") / "prompts" / "orchestrator.md").read_text("utf-8")


def make_state(**overrides: object) -> OrchestratorState:
    environment = Environment(now=NOW, user_name="u", computer_name="c")
    return OrchestratorState(environment=environment, **overrides)  # type: ignore[arg-type]


def model_cfg(thinking: bool | None = None) -> ModelConfig:
    return ModelConfig(
        id="m1",
        name="M1",
        provider="p",
        model="provider-model-name",
        tags=["chat"],
        priority=1,
        thinking=thinking,
    )


def texts(request) -> tuple[str, str]:
    return request.messages[0].content, request.messages[1].content


def test_the_user_message_shows_the_user_id_or_null() -> None:
    def user_message(user_id: str | None) -> str:
        environment = Environment(
            now=NOW, user_name="u", computer_name="c", user_id=user_id
        )
        state = OrchestratorState(environment=environment)
        return build_request(state, "obs", model_cfg()).messages[1].content

    assert '"user_id":"ann"' in user_message("ann")
    assert '"user_id":null' in user_message(None)


def test_request_has_system_then_user_message_with_expected_content() -> None:
    state = make_state(goal="find the bug", turn=3)
    request = build_request(state, "tool said hi", model_cfg())
    assert [m.role for m in request.messages] == ["system", "user"]
    system, user = texts(request)
    assert INSTRUCTIONS.rstrip() in system
    assert "Reply schema" in system
    assert "state_patch" in system
    assert Reply.model_json_schema()["title"] in system
    assert state.model_dump_json() in user
    assert "tool said hi" in user
    # nothing but the state, the (empty) tool list and the observation, plus headings
    rest = user.replace(state.model_dump_json(), "").replace("tool said hi", "")
    assert rest.split() == ["##", "State", "##", "Tools", "(none)", "##", "Observation"]


def test_canary_in_an_old_observation_never_reaches_later_requests() -> None:
    turn1 = build_request(make_state(turn=1), f"result {CANARY}", model_cfg())
    assert CANARY in turn1.messages[1].content
    for turn in range(2, 6):
        request = build_request(
            make_state(turn=turn), f"observation {turn}", model_cfg()
        )
        assert all(CANARY not in m.content for m in request.messages)


def test_canary_is_present_when_it_is_in_the_state() -> None:
    state = make_state(turn=2, findings=[Finding(text=CANARY, source="turn 1")])
    request = build_request(state, "later", model_cfg())
    assert CANARY in request.messages[1].content


def _state_at_cap(turn: int) -> OrchestratorState:
    def with_text(length: int) -> OrchestratorState:
        finding = Finding(text="x" * length, source="s")
        return make_state(turn=turn, findings=[finding])

    # the JSON grows one character per filler character, so fill exactly to the cap
    filler = 1 + DEFAULT_MAX_CHARS - len(with_text(1).model_dump_json())
    state = with_text(filler)
    assert len(state.model_dump_json()) == DEFAULT_MAX_CHARS
    return state


def test_request_size_is_bounded_and_does_not_grow_with_turns() -> None:
    observation = "o" * MAX_OBSERVATION_CHARS
    sizes = {}
    for turn in (5, 20):
        system, user = texts(
            build_request(_state_at_cap(turn), observation, model_cfg())
        )
        sizes[turn] = len(system) + len(user)
    fixed_overhead = 200  # headings and newlines in the user message
    schema_and_instructions = len(
        texts(build_request(make_state(), "", model_cfg()))[0]
    )
    assert sizes[20] <= (
        schema_and_instructions
        + DEFAULT_MAX_CHARS
        + MAX_OBSERVATION_CHARS
        + fixed_overhead
    )
    assert sizes[5] == sizes[20]


def test_long_observation_is_cut_with_a_marker() -> None:
    extra = 123
    observation = "a" * (MAX_OBSERVATION_CHARS + extra)
    user = build_request(make_state(), observation, model_cfg()).messages[1].content
    assert "a" * MAX_OBSERVATION_CHARS in user
    assert "a" * (MAX_OBSERVATION_CHARS + 1) not in user
    assert f"[observation truncated: {extra} characters removed]" in user


def test_observation_at_the_cap_is_not_marked() -> None:
    user = (
        build_request(make_state(), "a" * MAX_OBSERVATION_CHARS, model_cfg())
        .messages[1]
        .content
    )
    assert "truncated" not in user


def test_same_inputs_give_equal_requests() -> None:
    state = make_state(goal="g")
    first = build_request(state, "obs", model_cfg())
    second = build_request(state, "obs", model_cfg())
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_request_carries_the_reply_schema_used_in_the_prompt() -> None:
    request = build_request(make_state(), "obs", model_cfg())
    assert request.response_schema == reply_schema()
    assert json.dumps(request.response_schema, sort_keys=True, indent=2) in (
        request.messages[0].content
    )
    copy = request.model_copy(deep=True)
    assert copy == request
    assert copy.response_schema is not request.response_schema


def test_model_and_think_mapping_mirrors_the_router() -> None:
    # The router sets model=ModelConfig.model and think=resolve_think(flag, model).
    state = make_state()
    request = build_request(state, "o", model_cfg())
    assert request.model == "provider-model-name"
    assert request.think is None
    assert build_request(state, "o", model_cfg(thinking=False)).think is False
    assert (
        build_request(state, "o", model_cfg(thinking=False), think=True).think is True
    )
    assert (
        build_request(state, "o", model_cfg(thinking=True), think=False).think is False
    )


def test_only_context_constructs_chat_requests() -> None:
    # The state-in-prompt design: context.py is the single place that decides what
    # a model request contains, so no module can send the chat history around it.
    root = Path(__file__).resolve().parent.parent / "src" / "llm_harness"
    found = {
        path.name
        for path in root.rglob("*.py")
        if re.search(r"\bChatRequest\(", path.read_text("utf-8"))
        and path.name != "types.py"
    }
    assert found == {"context.py"}


def test_no_full_history_module_or_class_exists() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "llm_harness"
    old_class = "Chat" + "Session"  # spelled in two parts so a grep finds no leftover
    assert [p.name for p in root.rglob("session.py")] == []
    assert [
        p.name
        for p in root.rglob("*.py")
        if re.search(rf"\bclass\s+{old_class}\b", p.read_text("utf-8"))
    ] == []


def test_prompt_file_is_packaged_data_and_not_empty() -> None:
    prompt = files("llm_harness") / "prompts" / "orchestrator.md"
    assert prompt.is_file()
    assert prompt.read_text("utf-8").strip()


def test_the_user_message_lists_the_tools_between_state_and_observation() -> None:
    tools = [FakeTool("alpha", "Does a."), FakeTool("beta", "Does b.")]
    user = (
        build_request(make_state(), "obs", model_cfg(), tools=tools).messages[1].content
    )
    assert (
        "\n\n## Tools\n\nalpha: Does a.\nbeta: Does b.\n\n## Observation\n\nobs" in user
    )
    assert (
        user.index("## State") < user.index("## Tools") < user.index("## Observation")
    )


def test_the_user_message_says_none_when_there_are_no_tools() -> None:
    user = build_request(make_state(), "obs", model_cfg()).messages[1].content
    assert "## Tools\n\n(none)\n\n## Observation" in user


def test_tool_schemas_never_enter_the_request() -> None:
    from pydantic import BaseModel

    class Secretive(BaseModel):
        hidden_field_name: str

    request = build_request(
        make_state(), "obs", model_cfg(), tools=[FakeTool("t", input_model=Secretive)]
    )
    assert "hidden_field_name" not in "\n".join(m.content for m in request.messages)


def test_the_instructions_point_to_tool_search_and_no_longer_say_no_tools_exist() -> (
    None
):
    system = build_request(make_state(), "obs", model_cfg()).messages[0].content
    assert "tool_search" in system and "## Tools" in system
    assert "No tools exist yet" not in system


def test_the_instructions_explain_delegate_and_no_longer_call_it_unavailable() -> None:
    system = build_request(make_state(), "obs", model_cfg()).messages[0].content
    assert "not available yet" not in system
    assert "## Subagents" in system and "brief" in system


SUBAGENT_PROMPT = (files("llm_harness") / "prompts" / "subagent.md").read_text("utf-8")


def _schema_part(system: str) -> str:
    return system[system.index("## Reply schema") :]


def test_without_instructions_the_system_message_is_the_orchestrator_prompt() -> None:
    default = build_request(make_state(), "obs", model_cfg())
    explicit = build_request(make_state(), "obs", model_cfg(), instructions=None)
    assert default == explicit
    assert default.messages[0].content == _system_prompt()
    assert default.messages[0].content.startswith(INSTRUCTIONS.rstrip())


def test_given_instructions_replace_the_prompt_and_keep_both_schemas() -> None:
    text = "# Custom\n\nDo the thing."
    custom = build_request(make_state(), "obs", model_cfg(), instructions=text)
    system = custom.messages[0].content
    default = build_request(make_state(), "obs", model_cfg()).messages[0].content
    assert system.startswith(text)
    assert INSTRUCTIONS.rstrip() not in system
    assert "## Reply schema" in system and "## State schema" in system
    assert _schema_part(system) == _schema_part(default)
    assert custom.response_schema == reply_schema()


def test_instructions_do_not_change_the_user_message() -> None:
    tools = [FakeTool("alpha", "Does a.")]
    plain = build_request(make_state(goal="g"), "obs", model_cfg(), tools=tools)
    custom = build_request(
        make_state(goal="g"), "obs", model_cfg(), tools=tools, instructions="x"
    )
    assert texts(custom)[1] == texts(plain)[1]


def test_subagent_instructions_fill_identity_and_keep_other_braces() -> None:
    text = subagent_instructions("researcher", "Researcher", "Finds facts")
    assert "subagent researcher (Researcher): Finds facts" in text
    for placeholder in ("{agent_id}", "{agent_name}", "{description}"):
        assert placeholder not in text
    assert '{"state_patch": {"findings": {"$append"' in text


def test_a_value_that_looks_like_a_placeholder_is_not_substituted_again() -> None:
    text = subagent_instructions("a", "{description}", "{agent_id}")
    assert "subagent a ({description}): {agent_id}" in text


def test_the_subagent_prompt_covers_the_rules_a_subagent_must_follow() -> None:
    assert "respond" in SUBAGENT_PROMPT
    assert "ask_user" in SUBAGENT_PROMPT
    assert "delegate is not available to subagents" in SUBAGENT_PROMPT
    assert "at most 5 steps" in SUBAGENT_PROMPT
    assert "state_patch" in SUBAGENT_PROMPT and "source" in SUBAGENT_PROMPT


def agent(**overrides: object) -> AgentConfig:
    fields: dict[str, object] = {
        "id": "a",
        "name": "A",
        "description": "An agent.",
        "tags": [],
        "model_tags": [],
    }
    return AgentConfig.model_validate(fields | overrides)


def _user(**kwargs: object) -> str:
    return build_request(make_state(), "obs", model_cfg(), **kwargs).messages[1].content  # type: ignore[arg-type]


def test_the_orchestrator_agent_is_never_a_delegation_target() -> None:
    boss = agent(id="boss", tags=["orchestrator"], examples=["plan"])
    assert delegation_targets([boss]) == []


def test_an_agent_without_examples_is_not_a_delegation_target() -> None:
    assert delegation_targets([agent(id="quiet")]) == []


def test_targets_keep_the_given_order() -> None:
    ids = ["z", "b", "m"]
    agents = [agent(id=i, examples=["x"]) for i in ids]
    agents.insert(1, agent(id="boss", tags=["orchestrator"], examples=["x"]))
    assert [a.id for a in delegation_targets(agents)] == ids


def test_subagent_lines_show_id_description_and_examples() -> None:
    agents = [
        agent(id="researcher", description="Finds facts.", examples=["look up a; b"]),
        agent(id="coder", description="Writes code.", examples=["fix a bug", "add x"]),
    ]
    assert subagent_lines(agents) == (
        "researcher: Finds facts. (e.g. look up a; b)\n"
        "coder: Writes code. (e.g. fix a bug; add x)"
    )


def test_subagent_lines_say_none_without_targets() -> None:
    assert subagent_lines([]) == "(none)"


def test_the_user_message_lists_subagents_between_tools_and_observation() -> None:
    user = _user(subagents=[agent(id="r", description="D.", examples=["e1", "e2"])])
    assert (
        "## Tools\n\n(none)\n\n## Subagents\n\nr: D. (e.g. e1; e2)\n\n## Observation"
        in user
    )
    assert (
        user.index("## Tools")
        < user.index("## Subagents")
        < user.index("## Observation")
    )


def test_no_subagents_leaves_the_user_message_unchanged() -> None:
    assert _user(subagents=[]) == _user()
    assert "## Subagents" not in _user()


def test_only_ineligible_subagents_add_no_section() -> None:
    ineligible = [
        agent(id="boss", tags=["orchestrator"], examples=["x"]),
        agent(id="q"),
    ]
    assert _user(subagents=ineligible) == _user()
    assert "## Subagents" not in _user(subagents=ineligible)


def test_subagents_do_not_change_the_system_message() -> None:
    offered = [agent(id="r", examples=["e"])]
    assert (
        texts(build_request(make_state(), "obs", model_cfg(), subagents=offered))[0]
        == texts(build_request(make_state(), "obs", model_cfg()))[0]
    )
