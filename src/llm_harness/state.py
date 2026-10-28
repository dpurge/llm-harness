"""Orchestrator state, patch merge and the model's reply contract.

Pure: no I/O and no provider imports. Error messages are written for the model,
because a rejected reply's error text becomes its next observation.
"""

import json
import re
from functools import cache
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from llm_harness.types import _Frozen

DEFAULT_MAX_CHARS = 8000
# Old goal changes are dropped so the audit trail cannot fill the size cap.
GOAL_HISTORY_LIMIT = 20

PlanStatus = Literal["todo", "doing", "done", "dropped"]

# Keys the model may patch, and keys the harness owns (named so the error can say so).
_PATCHABLE = ("goal", "findings", "plan")
_PROTECTED = ("environment", "goal_history", "turn", "step")
_LIST_FORMS = ("$append", "$replace")
_ACTION_TYPES = ("respond", "ask_user", "call_tool", "delegate")


class Environment(_Frozen):
    """Refreshed by the harness every turn; `now` must carry a timezone offset."""

    now: AwareDatetime
    user_name: str
    computer_name: str
    # Optional so state files exported before the id existed still import.
    user_id: str | None = None


class Finding(_Frozen):
    # A finding nobody can trace is noise, so a blank source is rejected.
    # The pattern puts "not blank" into the JSON schema the model is shown.
    text: Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
    source: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"\S")
    ]


class PlanStep(_Frozen):
    text: str
    status: PlanStatus


class GoalChange(_Frozen):
    turn: int
    old: str
    new: str


class OrchestratorState(_Frozen):
    goal: str = ""
    goal_history: list[GoalChange] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    plan: list[PlanStep] = Field(default_factory=list)
    environment: Environment
    turn: int = 0
    step: int = 0


class RunState(_Frozen):
    """Everything needed to resume a run: plain JSON, no live objects."""

    state: OrchestratorState
    pending_question: str | None = None


class PatchError(ValueError):
    """A patch was rejected; the message tells the model how to fix it."""


def _items(key: str, items: object, model: type[_Frozen]) -> list:
    if not isinstance(items, list):
        raise PatchError(f"`{key}` operand must be a list, got {type(items).__name__}.")
    try:
        return TypeAdapter(list[model]).validate_python(items)  # type: ignore[valid-type]
    except ValidationError as error:
        raise PatchError(f"Invalid item in `{key}`: {_describe(error)}") from error


def _describe(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}"
        for e in error.errors()
    )


def _merge_list(key: str, old: list, value: object, model: type[_Frozen]) -> list:
    if value is None:
        return []
    if not (
        isinstance(value, dict) and len(value) == 1 and next(iter(value)) in _LIST_FORMS
    ):
        raise PatchError(
            f'`{key}` must be null, {{"$append": [...]}} or {{"$replace": [...]}} '
            f"with exactly one of those keys; a bare list is not accepted."
        )
    (form, items), *_ = value.items()
    parsed = _items(key, items, model)
    return [*old, *parsed] if form == "$append" else parsed


def apply_patch(
    state: OrchestratorState,
    patch: object,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> OrchestratorState:
    """Return a new state with `patch` merged in; raise PatchError, never half-apply."""
    if not isinstance(patch, dict):
        raise PatchError(
            f"`state_patch` must be an object, got {type(patch).__name__}."
        )
    for key in patch:
        if key in _PROTECTED:
            raise PatchError(
                f"`{key}` is protected and cannot be patched; "
                f"patchable keys: {', '.join(_PATCHABLE)}."
            )
        if key not in _PATCHABLE:
            raise PatchError(
                f"Unknown state key `{key}`; patchable keys: {', '.join(_PATCHABLE)}."
            )

    # Everything is computed into `update` first, so a late failure leaves no trace.
    update: dict[str, object] = {}
    if "goal" in patch:
        goal = patch["goal"]
        if goal is None:
            goal = ""
        if not isinstance(goal, str):
            raise PatchError(
                f"`goal` must be a plain string or null, got {type(goal).__name__}; "
                f'the {{"$append": ...}} and {{"$replace": ...}} forms are only '
                f"for `findings` and `plan`."
            )
        if goal and not goal.strip():
            raise PatchError("`goal` must not be blank; use null to clear it.")
        update["goal"] = goal
        if goal != state.goal:
            change = GoalChange(turn=state.turn, old=state.goal, new=goal)
            update["goal_history"] = [*state.goal_history, change][-GOAL_HISTORY_LIMIT:]
    if "findings" in patch:
        update["findings"] = _merge_list(
            "findings", state.findings, patch["findings"], Finding
        )
    if "plan" in patch:
        update["plan"] = _merge_list("plan", state.plan, patch["plan"], PlanStep)

    new_state = state.model_copy(update=update)
    size = len(new_state.model_dump_json())
    if size > max_chars:
        raise PatchError(
            f"Patch rejected: the state would be {size} characters, over the "
            f"limit of {max_chars}. Shorten or drop findings/plan steps."
        )
    return new_state


class RespondAction(_Frozen):
    type: Literal["respond"]
    text: str


class AskUserAction(_Frozen):
    type: Literal["ask_user"]
    question: str


class CallToolAction(_Frozen):
    type: Literal["call_tool"]
    tool: str
    arguments: dict[str, object] = Field(default_factory=dict)


class DelegateAction(_Frozen):
    type: Literal["delegate"]
    subagent: str
    brief: str


Action = Annotated[
    RespondAction | AskUserAction | CallToolAction | DelegateAction,
    Field(discriminator="type"),
]


class Reply(_Frozen):
    state_patch: dict[str, object]
    action: Action


def _list_form(item_ref: str) -> dict[str, Any]:
    array = {"type": "array", "items": {"$ref": f"#/$defs/{item_ref}"}}
    return {
        "anyOf": [
            {"type": "null"},
            *(
                {
                    "type": "object",
                    "properties": {form: array},
                    "required": [form],
                    "additionalProperties": False,
                }
                for form in _LIST_FORMS
            ),
        ]
    }


@cache
def reply_schema() -> dict[str, Any]:
    """JSON schema of a reply with `state_patch` pinned to the patchable fields.

    `Reply.state_patch` stays a free-form dict for runtime validation (see
    `apply_patch`); this schema is what the model is shown and constrained by.
    The same dict is returned on every call: callers must not mutate it.
    """
    schema = Reply.model_json_schema()
    defs = schema.setdefault("$defs", {})
    for name, model in (("Finding", Finding), ("PlanStep", PlanStep)):
        item = model.model_json_schema()
        defs.update(item.pop("$defs", {}))
        defs[name] = item
    schema["properties"]["state_patch"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goal": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "findings": _list_form("Finding"),
            "plan": _list_form("PlanStep"),
        },
    }
    return schema


class ReplyError(ValueError):
    """A reply broke the contract; the message tells the model how to fix it."""


_FENCED = re.compile(r"\A```[A-Za-z]*[ \t]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)


def _unfence(text: str) -> str:
    """Strip ONE code fence that wraps the whole reply; anything else is kept.

    Small local models wrap their JSON in a fence almost every time (seen with
    gemma4:12b), so the whole reply may be one fenced block, nothing else.
    """
    match = _FENCED.match(text.strip())
    return match["body"] if match else text


def parse_reply(text: str) -> Reply:
    """Parse a model reply strictly: one JSON object, two keys.

    One code fence around the whole reply is tolerated; prose is not.
    """
    try:
        data = json.loads(_unfence(text))
    except json.JSONDecodeError as error:
        if error.msg == "Extra data":
            raise ReplyError(
                f"Reply has extra content after the first JSON object at "
                f"character {error.pos}. Check that the braces are balanced: "
                f"`goal`, `findings` and `plan` go INSIDE `state_patch`, and "
                f"`state_patch` and `action` are the only top-level keys."
            ) from error
        raise ReplyError(
            f"Reply is not valid JSON ({error}). Reply with one bare JSON object "
            f'{{"state_patch": {{...}}, "action": {{...}}}}, without a code fence.'
        ) from error
    if not isinstance(data, dict):
        raise ReplyError(f"Reply must be a JSON object, got {type(data).__name__}.")
    keys = set(data)
    if keys != {"state_patch", "action"}:
        missing = sorted({"state_patch", "action"} - keys)
        extra = sorted(keys - {"state_patch", "action"})
        raise ReplyError(
            f"Reply must have exactly the keys `state_patch` and `action`; "
            f"missing: {missing}, unexpected: {extra}."
        )
    action = data["action"]
    if isinstance(action, dict) and action.get("type") not in _ACTION_TYPES:
        raise ReplyError(
            f"Unknown action type {action.get('type')!r}; "
            f"valid types: {', '.join(_ACTION_TYPES)}."
        )
    try:
        return Reply.model_validate(data)
    except ValidationError as error:
        raise ReplyError(f"Invalid reply: {_describe(error)}") from error
