"""Framework-free text of the State and Trace tabs, shared by the web and terminal UIs.

Pure: no nicegui, no textual. Every model-derived string goes through
`sanitize_text` here, so a front end only has to show the result as plain text.
"""

import json
from dataclasses import dataclass
from typing import Any

from llm_harness.config import ModelConfig
from llm_harness.context import MAX_OBSERVATION_CHARS, answer_observation, build_request
from llm_harness.display import sanitize_text
from llm_harness.orchestrator import Orchestrator
from llm_harness.state import Finding, OrchestratorState, PlanStep
from llm_harness.trace import Span


def preview_observation(orchestrator: Orchestrator, typed: str) -> str:
    """The observation the next send would use: the typed text, empty if none.

    After an `ask_user` the typed text is the answer, wrapped as `send` does.
    """
    if orchestrator.pending_question and typed:
        return answer_observation(orchestrator.pending_question, typed)
    return typed


def counters_text(state: OrchestratorState) -> str:
    return f"turn {state.turn}, step {state.step}"


def goal_text(state: OrchestratorState) -> str:
    goal = sanitize_text(state.goal)
    return f"Goal: {goal}" if goal else "Goal: (none)"


def finding_text(finding: Finding) -> str:
    return f"{sanitize_text(finding.text)} (source: {sanitize_text(finding.source)})"


def plan_step_text(step: PlanStep) -> str:
    return f"[{step.status}] {sanitize_text(step.text)}"


def environment_text(state: OrchestratorState, question: str | None) -> str:
    env = state.environment
    return (
        f"now: {env.now.isoformat()}\n"
        f"user_name: {sanitize_text(env.user_name)}\n"
        f"computer_name: {sanitize_text(env.computer_name)}\n"
        f"user_id: {sanitize_text(env.user_id) if env.user_id else '(not set)'}"
        + (f"\npending question: {sanitize_text(question)}" if question else "")
    )


@dataclass(frozen=True)
class RequestPreview:
    """The request the next send would build, as the text a panel shows."""

    system_count: str
    state_count: str
    observation_count: str
    system_text: str
    user_text: str


def build_preview(
    orchestrator: Orchestrator, model: ModelConfig, typed: str
) -> RequestPreview:
    """Preview of the next request, from the same `build_request` `send` calls."""
    observation = preview_observation(orchestrator, typed)
    request = build_request(orchestrator.state, observation, model)
    system, user = request.messages
    cut = (
        f" (cut to {MAX_OBSERVATION_CHARS} in the request)"
        if len(observation) > MAX_OBSERVATION_CHARS
        else ""
    )
    return RequestPreview(
        system_count=f"system (instructions + schemas): {len(system.content)} characters",
        state_count=f"state JSON: {len(orchestrator.state.model_dump_json())} characters",
        observation_count=f"observation: {len(observation)} characters{cut}",
        system_text=sanitize_text(system.content),
        user_text=sanitize_text(user.content),
    )


# Trace tab

# Attributes that can be long: each is shown in its own collapsed section, whole.
# Every other attribute is one short line of the span's details.
LONG_FIELDS = ("messages", "thinking", "reply", "rejection", "arguments", "result")
OPEN_BY_DEFAULT = ("turn", "step")


def trace_text(value: Any) -> str:
    """A model-derived value as one sanitised string, whole (never cut)."""
    if isinstance(value, str):
        return sanitize_text(value)
    return sanitize_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def span_title(span: Span) -> str:
    """Kind, name, status word and duration: the status is text, not only colour."""
    return (
        f"{span.kind}: {trace_text(span.name)} | {span.status} | "
        f"{span.duration_ms:g} ms"
    )


def span_children(spans: tuple[Span, ...]) -> dict[str | None, list[Span]]:
    """Group spans by parent, in the given order.

    A span whose parent is not in `spans` (it is still open: `Orchestrator.last_trace`
    holds closed spans only) is listed under the root key `None`, not dropped.
    """
    known = {span.id for span in spans}
    grouped: dict[str | None, list[Span]] = {}
    for span in spans:
        parent = span.parent_id if span.parent_id in known else None
        grouped.setdefault(parent, []).append(span)
    return grouped


def span_details_text(span: Span) -> str:
    """The short lines of a span: its fixed fields and every non-long attribute."""
    lines = [
        f"kind: {span.kind}",
        f"name: {trace_text(span.name)}",
        f"start: {span.start_ms:g} ms",
        f"duration: {span.duration_ms:g} ms",
        f"status: {span.status}",
    ]
    if span.parent_id is not None:
        lines.append(f"parent: {span.parent_id}")
    lines += [
        f"{key}: {trace_text(value)}"
        for key, value in span.attributes.items()
        if key not in LONG_FIELDS
    ]
    return "\n".join(lines)


def long_field_keys(span: Span) -> list[str]:
    """The long fields this span has, in the fixed display order."""
    return [key for key in LONG_FIELDS if key in span.attributes]


def long_field_text(span: Span, key: str) -> str:
    """The whole text of one long field; messages get a `[role]` header each."""
    value = span.attributes[key]
    if key == "messages":
        return "\n\n".join(
            f"[{trace_text(message['role'])}]\n{trace_text(message['content'])}"
            for message in value
        )
    return trace_text(value)
