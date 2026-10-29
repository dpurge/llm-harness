"""Call trace of the last turn: tree shape, statuses, data policy (offline)."""

import asyncio
from contextlib import aclosing

import pytest

from llm_harness.config import ConfigError
from llm_harness.context import build_request
from llm_harness.orchestrator import Orchestrator
from llm_harness.trace import Span, TraceRecorder
from llm_harness.types import (
    Done,
    ErrorEvent,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
    UsageReport,
)
from tests.fake_steps import reply_events
from tests.test_orchestrator import (
    CALL_TOOL,
    CHILD_FINDING,
    CHILD_RESPOND,
    RESEARCHER,
    RESPOND,
    SET_GOAL,
    StubTool,
    call,
    delegate,
    make,
    make_delegating,
    model_cfg,
    registry,
    run,
)

REJECTED = [TextDelta(text="this is not json"), Done()]


class TickClock:
    """A monotonic clock that moves one millisecond per reading."""

    def __init__(self) -> None:
        self._seconds = 0.0

    def __call__(self) -> float:
        self._seconds += 0.001
        return self._seconds


def make_traced(scripts: list[list[StreamEvent]], **kwargs: object):
    return make(scripts, recorder=TraceRecorder(TickClock()), **kwargs)


def trace_of(orchestrator: Orchestrator) -> tuple[Span, ...]:
    trace = orchestrator.last_trace
    assert trace is not None
    return trace


def of_kind(spans: tuple[Span, ...], kind: str) -> list[Span]:
    return [span for span in spans if span.kind == kind]


def test_span_is_frozen_and_rejects_unknown_fields() -> None:
    span = Span(
        id="s1",
        parent_id=None,
        kind="turn",
        name="turn 1",
        start_ms=0,
        duration_ms=1,
        status="ok",
        attributes={},
    )
    with pytest.raises(ValueError):
        span.name = "other"
    with pytest.raises(ValueError):
        Span(**{**span.model_dump(), "extra": 1})


def test_start_and_duration_are_rounded_to_three_decimals() -> None:
    readings = iter([100.0, 100.0123456789, 100.9876543219, 101.0])
    recorder = TraceRecorder(lambda: next(readings))
    recorder.reset()
    with recorder.span("turn", "turn 1"), recorder.span("step", "step 1"):
        pass
    spans = recorder.spans
    assert spans is not None
    step, turn = spans[1], spans[0]
    assert step.start_ms == 12.346
    assert step.duration_ms == 975.309
    assert turn.start_ms == 0.0


async def test_last_trace_is_none_before_the_first_turn() -> None:
    orchestrator, _ = make_traced([])
    assert orchestrator.last_trace is None


async def test_tree_of_a_rejected_reply_then_a_tool_then_a_respond() -> None:
    orchestrator, _ = make_traced(
        [
            REJECTED,
            reply_events(call("search"), SET_GOAL),
            reply_events(RESPOND, SET_GOAL),
        ],
        tools=registry(StubTool()),
    )
    await run(orchestrator, "hi")
    spans = trace_of(orchestrator)
    assert [(s.id, s.parent_id, s.kind, s.status) for s in spans] == [
        ("s1", None, "turn", "ok"),
        ("s2", "s1", "step", "ok"),
        ("s3", "s2", "llm_call", "rejected"),
        ("s4", "s2", "llm_call", "ok"),
        ("s5", "s2", "tool_call", "ok"),
        ("s6", "s1", "step", "ok"),
        ("s7", "s6", "llm_call", "ok"),
    ]
    assert [s.name for s in spans if s.kind in ("turn", "step")] == [
        "turn 1",
        "step 1",
        "step 2",
    ]
    # offsets are from the turn start, durations come from the injected clock
    assert spans[0].start_ms == 0
    assert all(s.duration_ms > 0 for s in spans)
    assert spans[2].start_ms > spans[1].start_ms


async def test_llm_call_holds_request_thinking_reply_rejection_usage_and_model() -> (
    None
):
    usage = UsageEvent(
        usage=Usage(input_tokens=11, output_tokens=7, reasoning_tokens=3)
    )
    report = UsageReport(
        model_id="answering-model",
        usage=None,
        cost_usd=None,
        seconds=0.1,
        session_cost_usd=0.0,
        session_cost_incomplete=False,
    )
    rejected = [ThinkingDelta(text="think A "), *REJECTED]
    valid = reply_events(
        RESPOND, SET_GOAL, ThinkingDelta(text="think "), ThinkingDelta(text="B")
    )
    valid.insert(-1, usage)
    valid.insert(-1, report)
    orchestrator, provider = make_traced([rejected, valid])
    await run(orchestrator, "hi")
    first, second = of_kind(trace_of(orchestrator), "llm_call")
    assert first.status == "rejected"
    assert first.attributes["rejection"]
    assert first.attributes["reply"] == "this is not json"
    assert first.attributes["thinking"] == "think A "
    assert "usage" not in first.attributes
    assert second.status == "ok"
    assert second.attributes["thinking"] == "think B"
    assert second.attributes["usage"] == {
        "input_tokens": 11,
        "output_tokens": 7,
        "reasoning_tokens": 3,
    }
    assert second.attributes["model"] == provider.requests[1].model
    assert second.attributes["reported_model"] == "answering-model"
    assert second.attributes["reply"] == next(
        e.text for e in valid if isinstance(e, TextDelta)
    )
    assert second.attributes["messages"] == [
        {"role": m.role, "content": m.content} for m in provider.requests[1].messages
    ]
    assert [m["role"] for m in second.attributes["messages"]] == ["system", "user"]


async def test_usage_report_alone_puts_usage_cost_and_seconds_in_the_llm_call() -> None:
    report = UsageReport(
        model_id="routed-model",
        usage=Usage(input_tokens=21, output_tokens=5, reasoning_tokens=None),
        cost_usd=0.0042,
        seconds=1.5,
        session_cost_usd=0.0042,
        session_cost_incomplete=False,
    )
    valid = reply_events(RESPOND, SET_GOAL)
    valid.insert(-1, report)
    orchestrator, _ = make_traced([valid])
    await run(orchestrator, "hi")
    (llm_call,) = of_kind(trace_of(orchestrator), "llm_call")
    assert llm_call.attributes["usage"] == {
        "input_tokens": 21,
        "output_tokens": 5,
        "reasoning_tokens": None,
    }
    assert llm_call.attributes["cost_usd"] == 0.0042
    assert llm_call.attributes["seconds"] == 1.5
    assert llm_call.attributes["reported_model"] == "routed-model"


async def test_tool_call_holds_name_arguments_and_result_and_errors_set_status() -> (
    None
):
    boom = StubTool(name="boom", error=RuntimeError("it broke"))
    orchestrator, _ = make_traced(
        [
            reply_events(call("search", q="x"), SET_GOAL),
            reply_events(call("missing"), SET_GOAL),
            reply_events(call("boom"), SET_GOAL),
            reply_events(RESPOND, SET_GOAL),
        ],
        tools=registry(StubTool(), boom),
    )
    await run(orchestrator, "hi")
    ok, unknown, failing = of_kind(trace_of(orchestrator), "tool_call")
    assert (ok.name, ok.status) == ("search", "ok")
    assert ok.attributes == {
        "tool": "search",
        "arguments": {"q": "x"},
        "result": "tool search result: found it",
    }
    assert unknown.status == "error"
    assert unknown.attributes["result"].startswith("tool missing is not available;")
    assert failing.status == "error"
    assert failing.attributes["result"] == "tool boom failed: RuntimeError: it broke"


async def test_a_new_turn_replaces_the_trace() -> None:
    orchestrator, _ = make_traced(
        [reply_events(RESPOND, SET_GOAL), reply_events(RESPOND, SET_GOAL)]
    )
    await run(orchestrator, "one")
    await run(orchestrator, "two")
    spans = trace_of(orchestrator)
    assert [s.kind for s in spans] == ["turn", "step", "llm_call"]
    assert spans[0].attributes == {"turn": 2}
    assert spans[0].id == "s1"


def statuses(orchestrator: Orchestrator) -> dict[str, str]:
    return {s.kind: s.status for s in trace_of(orchestrator)}


async def test_cancel_mid_stream_closes_every_open_span_as_cancelled() -> None:
    orchestrator, _ = make_traced(
        [reply_events(RESPOND, SET_GOAL, ThinkingDelta(text="partial"))],
        hang_after=1,
    )
    task = asyncio.create_task(run(orchestrator, "first"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert statuses(orchestrator) == {
        "turn": "cancelled",
        "step": "cancelled",
        "llm_call": "cancelled",
    }
    (llm_call,) = of_kind(trace_of(orchestrator), "llm_call")
    assert llm_call.attributes["thinking"] == "partial"
    assert llm_call.duration_ms > 0


async def test_cancel_inside_a_tool_marks_the_tool_call_cancelled() -> None:
    orchestrator, _ = make_traced(
        [reply_events(call("search"), SET_GOAL)],
        tools=registry(StubTool(hang=True)),
    )
    task = asyncio.create_task(run(orchestrator, "first"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert statuses(orchestrator) == {
        "turn": "cancelled",
        "step": "cancelled",
        "llm_call": "ok",
        "tool_call": "cancelled",
    }


async def test_error_event_marks_llm_call_step_and_turn_as_error() -> None:
    orchestrator, _ = make_traced([[ErrorEvent(kind="connection", message="no route")]])
    events = await run(orchestrator, "hi")
    assert events == [ErrorEvent(kind="connection", message="no route")]
    assert statuses(orchestrator) == {
        "turn": "error",
        "step": "error",
        "llm_call": "error",
    }
    (llm_call,) = of_kind(trace_of(orchestrator), "llm_call")
    assert llm_call.attributes["error"] == "no route"


async def test_step_cap_and_exhausted_retries_end_in_error() -> None:
    capped, _ = make_traced(
        [reply_events(CALL_TOOL, SET_GOAL) for _ in range(2)], max_steps=2
    )
    await run(capped, "hi")
    spans = trace_of(capped)
    assert [s.status for s in of_kind(spans, "step")] == ["ok", "ok"]
    assert spans[0].status == "error"

    rejected, _ = make_traced([REJECTED] * 3)
    await run(rejected, "hi")
    spans = trace_of(rejected)
    assert [s.status for s in of_kind(spans, "llm_call")] == ["rejected"] * 3
    assert statuses(rejected)["step"] == "error"
    assert spans[0].status == "error"


async def test_closing_the_generator_early_keeps_a_cancelled_partial_trace() -> None:
    orchestrator, provider = make_traced(
        [reply_events(call("search"), SET_GOAL), reply_events(RESPOND, SET_GOAL)],
        tools=registry(StubTool()),
    )
    async with aclosing(orchestrator.send("hi")) as events:
        first = await anext(events)
    assert first.step == 1  # the StatusEvent of the tool step
    assert provider.closed
    assert statuses(orchestrator) == {
        "turn": "cancelled",
        "step": "cancelled",
        "llm_call": "ok",
    }
    # the generator was closed in a clean state: a new turn runs
    assert (await run(orchestrator, "again"))[-1] == Done()


async def test_thinking_stays_in_the_trace_and_out_of_state_and_requests() -> None:
    secret_thought = "THOUGHT-7d41"
    orchestrator, provider = make_traced(
        [
            reply_events(call("search"), SET_GOAL, ThinkingDelta(text=secret_thought)),
            reply_events(RESPOND, SET_GOAL),
        ],
        tools=registry(StubTool()),
    )
    await run(orchestrator, "hi")
    (first_call, _) = of_kind(trace_of(orchestrator), "llm_call")
    assert first_call.attributes["thinking"] == secret_thought
    for request in provider.requests:
        assert secret_thought not in "\n".join(m.content for m in request.messages)
    follow_up = build_request(orchestrator.state, "next", model_cfg())
    assert secret_thought not in "\n".join(m.content for m in follow_up.messages)
    assert secret_thought not in orchestrator.state.model_dump_json()
    assert secret_thought not in orchestrator.snapshot().model_dump_json()


async def test_a_20000_character_reply_is_kept_whole() -> None:
    text = "abcdefghij" * 2000
    orchestrator, _ = make_traced(
        [reply_events({"type": "respond", "text": text}, SET_GOAL)]
    )
    await run(orchestrator, "hi")
    (llm_call,) = of_kind(trace_of(orchestrator), "llm_call")
    assert len(llm_call.attributes["reply"]) > 20000
    assert text in llm_call.attributes["reply"]


async def test_an_api_key_in_a_tool_result_is_masked_in_the_trace() -> None:
    key = "sk-abcdef1234567890"
    orchestrator, _ = make_traced(
        [reply_events(call("search"), SET_GOAL), reply_events(RESPOND, SET_GOAL)],
        tools=registry(StubTool(result=f"the key is {key}")),
    )
    await run(orchestrator, "hi")
    (tool_call,) = of_kind(trace_of(orchestrator), "tool_call")
    assert "[REDACTED:api_key]" in tool_call.attributes["result"]
    dumped = "".join(span.model_dump_json() for span in trace_of(orchestrator))
    assert key not in dumped
    # the second request carried the key as a raw observation; its trace copy is masked
    second_request = of_kind(trace_of(orchestrator), "llm_call")[1]
    assert key not in str(second_request.attributes["messages"])


async def test_the_trace_keeps_the_request_as_sent_without_a_second_cut() -> None:
    long_result = "r" * 6000
    orchestrator, provider = make_traced(
        [reply_events(call("search"), SET_GOAL), reply_events(RESPOND, SET_GOAL)],
        tools=registry(StubTool(result=long_result)),
    )
    await run(orchestrator, "hi")
    sent = provider.requests[1].messages[1].content
    traced = of_kind(trace_of(orchestrator), "llm_call")[1].attributes["messages"][1]
    assert traced["content"] == sent
    assert len(sent) < len(long_result)  # build_request's own cap applied
    (tool_call,) = of_kind(trace_of(orchestrator), "tool_call")
    assert long_result in tool_call.attributes["result"]  # the tool text is whole


async def test_events_are_identical_with_and_without_a_recorder() -> None:
    def scripts() -> list[list[StreamEvent]]:
        return [
            REJECTED,
            reply_events(call("search"), SET_GOAL, ThinkingDelta(text="t")),
            reply_events(RESPOND, SET_GOAL),
        ]

    recorded, _ = make_traced(scripts(), tools=registry(StubTool()))
    silent, _ = make(
        scripts(), recorder=TraceRecorder(enabled=False), tools=registry(StubTool())
    )
    assert await run(recorded, "hi") == await run(silent, "hi")
    assert trace_of(recorded)
    assert silent.last_trace is None


async def test_subagent_span_parents_its_child_steps_and_calls() -> None:
    # Nothing in the orchestrator opens a subagent span yet (a later step does);
    # this drives the recorder directly.
    recorder = TraceRecorder(TickClock())
    recorder.reset()
    with (
        recorder.span("turn", "turn 1") as turn,
        recorder.span("step", "step 1", turn) as step,
        recorder.span("subagent", "researcher", step) as sub,
    ):
        sub.attributes["state"] = {"goal": "find it"}
        with (
            recorder.span("step", "step 1", sub) as child,
            recorder.span("llm_call", "model", child),
        ):
            pass
    spans = recorder.spans
    assert spans is not None
    assert [(s.id, s.parent_id, s.kind) for s in spans] == [
        ("s1", None, "turn"),
        ("s2", "s1", "step"),
        ("s3", "s2", "subagent"),
        ("s4", "s3", "step"),
        ("s5", "s4", "llm_call"),
    ]


def test_a_large_state_attribute_is_kept_whole() -> None:
    recorder = TraceRecorder(TickClock())
    recorder.reset()
    state = {"notes": ["line " * 200] * 10, "goal": "g"}
    with (
        recorder.span("turn", "turn 1") as turn,
        recorder.span("subagent", "researcher", turn) as sub,
    ):
        sub.attributes["state"] = state
    spans = recorder.spans
    assert spans is not None
    assert spans[1].attributes["state"] == state


def _adoptable_child() -> tuple[Span, ...]:
    child = TraceRecorder(TickClock())
    child.reset()
    with (
        child.span("turn", "turn 1") as turn,
        child.span("step", "step 1", turn) as step,
        child.span("llm_call", "model", step) as llm,
    ):
        llm.attributes["model"] = "m"
        step.status = "rejected"
    spans = child.spans
    assert spans is not None
    return spans


def test_adopt_remaps_ids_links_the_root_to_the_parent_and_shifts_offsets() -> None:
    child = _adoptable_child()
    recorder = TraceRecorder(TickClock())
    recorder.reset()
    with (
        recorder.span("turn", "turn 1") as turn,
        recorder.span("step", "step 1", turn) as step,
        recorder.span("subagent", "researcher", step) as sub,
    ):
        recorder.adopt(child, sub, 10.0)
    spans = recorder.spans
    assert spans is not None
    assert [(s.id, s.parent_id, s.kind) for s in spans] == [
        ("s1", None, "turn"),
        ("s2", "s1", "step"),
        ("s3", "s2", "subagent"),
        ("s4", "s3", "turn"),
        ("s5", "s4", "step"),
        ("s6", "s5", "llm_call"),
    ]
    adopted = spans[3:]
    assert [s.start_ms for s in adopted] == [round(c.start_ms + 10.0, 3) for c in child]
    assert [(s.name, s.status, s.duration_ms) for s in adopted] == [
        (c.name, c.status, c.duration_ms) for c in child
    ]
    assert adopted[2].attributes == {"model": "m"}
    assert adopted[1].status == "rejected"


def test_adopted_ids_never_collide_with_spans_opened_afterwards() -> None:
    recorder = TraceRecorder(TickClock())
    recorder.reset()
    with recorder.span("turn", "turn 1") as turn:
        with recorder.span("subagent", "researcher", turn) as sub:
            recorder.adopt(_adoptable_child(), sub, 0.0)
        with recorder.span("step", "step 2", turn):
            pass
    spans = recorder.spans
    assert spans is not None
    ids = [s.id for s in spans]
    assert len(ids) == len(set(ids)) == 6


def test_adopt_keeps_the_child_spans_in_opening_order_after_the_parent_span() -> None:
    recorder = TraceRecorder(TickClock())
    recorder.reset()
    with recorder.span("turn", "turn 1") as turn:
        with recorder.span("subagent", "researcher", turn) as sub:
            recorder.adopt(_adoptable_child(), sub, 0.0)
        with recorder.span("step", "step 2", turn):
            pass
    spans = recorder.spans
    assert spans is not None
    assert [s.kind for s in spans] == [
        "turn",
        "subagent",
        "turn",
        "step",
        "llm_call",
        "step",
    ]


def test_a_disabled_recorder_adopts_nothing() -> None:
    recorder = TraceRecorder(TickClock(), enabled=False)
    recorder.reset()
    with recorder.span("turn", "turn 1") as turn:
        recorder.adopt(_adoptable_child(), turn, 5.0)
    assert recorder.spans is None


def test_span_handle_start_ms_is_the_spans_offset() -> None:
    recorder = TraceRecorder(TickClock())
    recorder.reset()
    with (
        recorder.span("turn", "turn 1") as turn,
        recorder.span("step", "step 1", turn) as step,
    ):
        started = step.start_ms
    spans = recorder.spans
    assert spans is not None
    assert started == spans[1].start_ms > 0


async def test_delegation_nests_the_childs_trace_under_a_subagent_span() -> None:
    orchestrator, _, _, _ = make_delegating(
        [reply_events(delegate(brief="find it"), SET_GOAL), reply_events(RESPOND)],
        [[reply_events(CHILD_RESPOND, CHILD_FINDING)]],
        recorder=TraceRecorder(TickClock()),
    )
    await run(orchestrator, "go")
    spans = trace_of(orchestrator)
    ids = [s.id for s in spans]
    assert len(ids) == len(set(ids))
    by_id = {s.id: s for s in spans}
    (sub,) = of_kind(spans, "subagent")
    assert sub.name == "researcher"
    assert by_id[sub.parent_id].kind == "step"
    assert sub.attributes["subagent"] == "researcher"
    assert sub.attributes["outcome"] == "responded"
    children = [s for s in spans if s.parent_id == sub.id]
    assert [c.kind for c in children] == ["turn"]
    child_steps = [s for s in spans if s.parent_id == children[0].id]
    assert [c.kind for c in child_steps] == ["step"]
    child_calls = [s for s in spans if s.parent_id == child_steps[0].id]
    assert [c.kind for c in child_calls] == ["llm_call"]
    # Parent and child spans are all attached to one tree rooted at the turn.
    roots = [s for s in spans if s.parent_id is None]
    assert len(roots) == 1
    descendants = spans[spans.index(sub) + 1 :]
    assert all(span.start_ms >= sub.start_ms for span in descendants)
    state = sub.attributes["state"]
    assert state["goal"] == "find it"
    assert state["findings"] == [{"text": "Paris is the capital", "source": "atlas"}]
    assert state["turn"] == 1 and state["step"] == 1


async def test_a_factory_that_fails_leaves_an_error_subagent_span_without_children() -> (
    None
):
    def factory(child):
        raise ConfigError("nope")

    orchestrator, _ = make_traced(
        [reply_events(delegate(), SET_GOAL), reply_events(RESPOND)],
        subagents=(RESEARCHER,),
        subagent_factory=factory,
    )
    await run(orchestrator, "go")
    spans = trace_of(orchestrator)
    (sub,) = of_kind(spans, "subagent")
    assert sub.status == "error"
    assert [s for s in spans if s.parent_id == sub.id] == []
