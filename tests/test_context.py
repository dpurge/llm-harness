"""Context builder: one bounded request per turn (offline, deterministic)."""

import json
import re
from importlib.resources import files
from pathlib import Path

from llm_harness.config import ModelConfig
from llm_harness.context import MAX_OBSERVATION_CHARS, build_request
from llm_harness.state import (
    DEFAULT_MAX_CHARS,
    Environment,
    Finding,
    OrchestratorState,
    Reply,
    reply_schema,
)
from tests.test_state import NOW

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
    # nothing but the state and the observation, plus their two headings
    rest = user.replace(state.model_dump_json(), "").replace("tool said hi", "")
    assert rest.split() == ["##", "State", "##", "Observation"]


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
