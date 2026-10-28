"""Orchestrator state, patch merge and reply contract (offline, fixed clock)."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from llm_harness.state import (
    AskUserAction,
    CallToolAction,
    DelegateAction,
    Environment,
    Finding,
    GoalChange,
    OrchestratorState,
    PatchError,
    PlanStep,
    ReplyError,
    RespondAction,
    apply_patch,
    parse_reply,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_state(**overrides: object) -> OrchestratorState:
    environment = Environment(now=NOW, user_name="u", computer_name="c")
    return OrchestratorState(environment=environment, **overrides)  # type: ignore[arg-type]


FINDING = {"text": "t", "source": "s"}


def test_models_are_frozen_and_forbid_extra() -> None:
    state = make_state()
    with pytest.raises(ValidationError):
        state.goal = "x"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Finding(text="t", source="s", extra=1)  # type: ignore[call-arg]


@pytest.mark.parametrize("source", ["", "   "])
def test_finding_without_source_rejected(source: str) -> None:
    with pytest.raises(ValidationError):
        Finding(text="t", source=source)
    with pytest.raises(PatchError, match="findings"):
        apply_patch(
            make_state(), {"findings": {"$append": [{"text": "t", "source": source}]}}
        )


def test_finding_without_source_key_rejected() -> None:
    with pytest.raises(PatchError):
        apply_patch(make_state(), {"findings": {"$append": [{"text": "t"}]}})


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        Environment(now=datetime(2026, 1, 1), user_name="u", computer_name="c")


@pytest.mark.parametrize("key", ["environment", "goal_history", "turn", "step"])
def test_protected_key_rejected(key: str) -> None:
    with pytest.raises(PatchError, match="protected"):
        apply_patch(make_state(), {key: 1})


def test_a_patch_of_the_environment_user_id_is_protected() -> None:
    with pytest.raises(PatchError, match="protected"):
        apply_patch(make_state(), {"environment": {"user_id": "x"}})


def test_unknown_key_rejected() -> None:
    with pytest.raises(PatchError, match="Unknown state key `mood`"):
        apply_patch(make_state(), {"mood": "x"})


def test_non_dict_patch_rejected() -> None:
    for bad in (None, [], "goal", 3):
        with pytest.raises(PatchError, match="object"):
            apply_patch(make_state(), bad)


def test_null_deletes() -> None:
    state = make_state(
        goal="g",
        findings=[Finding(**FINDING)],
        plan=[PlanStep(text="p", status="todo")],
    )
    new = apply_patch(state, {"goal": None, "findings": None, "plan": None})
    assert (new.goal, new.findings, new.plan) == ("", [], [])


def test_goal_must_be_string() -> None:
    with pytest.raises(PatchError, match="goal"):
        apply_patch(make_state(), {"goal": 5})


def test_goal_list_form_error_says_plain_string() -> None:
    with pytest.raises(PatchError) as info:
        apply_patch(make_state(), {"goal": {"$replace": ["x"]}})
    message = str(info.value)
    assert "`goal`" in message
    assert "plain string" in message
    assert "$append" in message
    assert "`findings` and `plan`" in message


def test_append_keeps_old_items() -> None:
    state = make_state(findings=[Finding(**FINDING)])
    new = apply_patch(
        state, {"findings": {"$append": [{"text": "t2", "source": "s2"}]}}
    )
    assert [f.text for f in new.findings] == ["t", "t2"]


def test_replace_replaces() -> None:
    state = make_state(plan=[PlanStep(text="old", status="todo")])
    new = apply_patch(
        state, {"plan": {"$replace": [{"text": "new", "status": "doing"}]}}
    )
    assert new.plan == [PlanStep(text="new", status="doing")]


def test_bare_list_rejected_naming_both_forms() -> None:
    with pytest.raises(PatchError) as info:
        apply_patch(make_state(), {"plan": [{"text": "p", "status": "todo"}]})
    assert "$append" in str(info.value) and "$replace" in str(info.value)


def test_list_form_must_be_single_known_key() -> None:
    for bad in ({}, {"$append": [], "$replace": []}, {"$other": []}):
        with pytest.raises(PatchError):
            apply_patch(make_state(), {"plan": bad})


def test_bad_plan_status_rejected() -> None:
    with pytest.raises(PatchError, match="plan"):
        apply_patch(make_state(), {"plan": {"$append": [{"text": "p", "status": "x"}]}})


def test_goal_change_records_one_entry() -> None:
    state = make_state(goal="a", turn=3)
    new = apply_patch(state, {"goal": "b"})
    assert new.goal_history == [GoalChange(turn=3, old="a", new="b")]


def test_first_goal_is_recorded() -> None:
    new = apply_patch(make_state(turn=1), {"goal": "first"})
    assert new.goal_history == [GoalChange(turn=1, old="", new="first")]


def test_unchanged_goal_records_nothing() -> None:
    new = apply_patch(make_state(goal="a"), {"goal": "a"})
    assert new.goal_history == []


def test_size_cap_rejected_and_boundary_accepted() -> None:
    patch = {"goal": "x" * 50}
    size = len(apply_patch(make_state(), patch).model_dump_json())
    assert apply_patch(make_state(), patch, max_chars=size).goal == "x" * 50
    with pytest.raises(PatchError, match="limit"):
        apply_patch(make_state(), patch, max_chars=size - 1)


def test_rejected_patch_leaves_state_unchanged() -> None:
    state = make_state(goal="a", findings=[Finding(**FINDING)])
    before = state.model_dump()
    # The goal part is valid; the bad findings part must stop it applying.
    with pytest.raises(PatchError):
        apply_patch(state, {"goal": "b", "findings": {"$append": [{"text": "t"}]}})
    assert state.model_dump() == before


ACTIONS = [
    ({"type": "respond", "text": "hi"}, RespondAction),
    ({"type": "ask_user", "question": "which?"}, AskUserAction),
    ({"type": "call_tool", "tool": "echo", "arguments": {"a": 1}}, CallToolAction),
    ({"type": "delegate", "subagent": "r", "brief": "look"}, DelegateAction),
]


@pytest.mark.parametrize(("action", "cls"), ACTIONS)
def test_parse_reply_happy_path(action: dict, cls: type) -> None:
    reply = parse_reply(json.dumps({"state_patch": {"goal": "g"}, "action": action}))
    assert isinstance(reply.action, cls)
    assert reply.state_patch == {"goal": "g"}


def test_call_tool_arguments_default_empty() -> None:
    reply = parse_reply(
        '{"state_patch": {}, "action": {"type": "call_tool", "tool": "t"}}'
    )
    assert isinstance(reply.action, CallToolAction) and reply.action.arguments == {}


RESPOND = '{"type": "respond", "text": "x"}'


BODY = '{"state_patch": {"goal": "g"}, "action": ' + RESPOND + "}"


@pytest.mark.parametrize(
    "text",
    [
        "```json\n" + BODY + "\n```",
        "```\n" + BODY + "\n```",
        "  ```JSON \n" + BODY + "\n```  \n",
        "```json\n" + BODY + "```",
    ],
)
def test_parse_reply_accepts_one_wrapping_code_fence(text: str) -> None:
    reply = parse_reply(text)
    assert reply.state_patch == {"goal": "g"}
    assert reply.action.type == "respond"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("not json", "not valid JSON"),
        (
            'Here you go:\n```json\n{"state_patch": {}, "action": '
            + RESPOND
            + "}\n```",
            "not valid JSON",
        ),
        (
            '```json\n{"state_patch": {}, "action": ' + RESPOND + "}\n```\nDone.",
            "not valid JSON",
        ),
        (
            '```json\n{"state_patch": {}}\n```\n```json\n{"action": '
            + RESPOND
            + "}\n```",
            "extra content",  # a second fence inside the body fails the JSON parse
        ),
        ("```json\n```", "not valid JSON"),
        ("[]", "JSON object"),
        ('{"state_patch": {}, "action": ' + RESPOND + ', "x": 1}', "unexpected: ['x']"),
        ('{"action": ' + RESPOND + "}", "missing: ['state_patch']"),
        ('{"state_patch": {}, "action": {"type": "dance"}}', "valid types"),
        (
            '{"state_patch": {}, "action": {"type": "respond", "text": "x", "z": 1}}',
            "z",
        ),
        ('{"state_patch": [], "action": ' + RESPOND + "}", "state_patch"),
    ],
)
def test_parse_reply_rejects(text: str, fragment: str) -> None:
    with pytest.raises(ReplyError) as info:
        parse_reply(text)
    assert fragment in str(info.value)


def test_parse_reply_extra_data_explains_brace_balance() -> None:
    # state_patch closed one brace too early, so `plan` landed at the top level.
    text = (
        '{"state_patch": {"goal": "g"}}, "plan": {"$replace": []}, "action": '
        + RESPOND
        + "}"
    )
    with pytest.raises(ReplyError) as info:
        parse_reply(text)
    message = str(info.value)
    assert "extra content after the first JSON object at character 30" in message
    assert "braces are balanced" in message
    assert "`goal`, `findings` and `plan` go INSIDE `state_patch`" in message
    assert "only top-level keys" in message
    assert "not valid JSON" not in message


def test_parse_reply_other_json_errors_keep_the_generic_message() -> None:
    with pytest.raises(ReplyError) as info:
        parse_reply('{"state_patch": ')
    assert "not valid JSON" in str(info.value)
    assert "extra content" not in str(info.value)


def test_goal_history_keeps_only_the_newest_twenty_records() -> None:
    state = make_state()
    for number in range(300):
        state = apply_patch(state, {"goal": f"goal-{number}"})
    assert len(state.goal_history) == 20
    assert state.goal_history[-1].new == "goal-299"
    assert state.goal_history[0].new == "goal-280"
    assert state.goal == "goal-299"


def test_whitespace_goal_is_rejected_with_advice() -> None:
    with pytest.raises(PatchError, match="must not be blank; use null to clear it"):
        apply_patch(make_state(goal="x"), {"goal": " \t"})


def test_empty_string_goal_is_still_accepted() -> None:
    assert apply_patch(make_state(goal="x"), {"goal": ""}).goal == ""


def test_parse_reply_accepts_backticks_inside_the_fenced_body() -> None:
    text = (
        '```json\n{"state_patch":{"goal":"g"},'
        '"action":{"type":"respond","text":"run `ls`"}}\n```'
    )
    assert parse_reply(text).action.text == "run `ls`"  # type: ignore[union-attr]


@pytest.mark.parametrize("field", ["text", "source"])
def test_whitespace_only_finding_fields_are_rejected(field: str) -> None:
    item = {"text": "t", "source": "s", field: "   "}
    with pytest.raises(ValidationError):
        Finding(**item)
