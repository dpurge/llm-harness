"""Orchestrator step loop over a scripted step model (offline, injected clock)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from llm_harness.config import ModelConfig
from llm_harness.orchestrator import Orchestrator
from llm_harness.state import RunState
from llm_harness.tools import ToolContext, ToolRegistry
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    StatusEvent,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
    UsageReport,
)
from tests.fake_steps import ScriptedSteps, reply_events

CANARY = "CANARY-5c2e08"
START = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

RESPOND = {"type": "respond", "text": "hello"}
ASK = {"type": "ask_user", "question": "What do you want?"}
CALL_TOOL = {"type": "call_tool", "tool": "search", "arguments": {}}
SET_GOAL = {"goal": "say hello"}


class FakeClock:
    """Each call is one minute later than the last."""

    def __init__(self) -> None:
        self._now = START

    def __call__(self) -> datetime:
        self._now += timedelta(minutes=1)
        return self._now


def model_cfg() -> ModelConfig:
    return ModelConfig(
        id="m1", name="M1", provider="p", model="pm", tags=["chat"], priority=1
    )


def make(
    scripts: list[list[StreamEvent]], **kwargs: object
) -> tuple[Orchestrator, ScriptedSteps]:
    provider = ScriptedSteps(scripts, hang_after=kwargs.pop("hang_after", None))  # type: ignore[arg-type]
    orchestrator = Orchestrator(
        provider,
        model_cfg(),
        clock=FakeClock(),
        identity=lambda: ("user", "host"),
        **kwargs,  # type: ignore[arg-type]
    )
    return orchestrator, provider


async def run(orchestrator: Orchestrator, text: str) -> list[StreamEvent]:
    return [event async for event in orchestrator.send(text)]


def observation(request: ChatRequest) -> str:
    return request.messages[1].content.split("## Observation\n\n", 1)[1].strip()


def everything(request: ChatRequest) -> str:
    return "\n".join(message.content for message in request.messages)


def progress(orchestrator: Orchestrator) -> dict:
    """The state minus the counters and environment the harness rewrites per turn."""
    return orchestrator.state.model_dump(exclude={"environment", "turn", "step"})


async def test_respond_with_goal_streams_status_text_done_and_no_raw_json() -> None:
    orchestrator, _ = make([reply_events(RESPOND, SET_GOAL)])
    events = await run(orchestrator, "hi")
    assert events == [
        StatusEvent(step=1, action_type="respond", text="answering"),
        TextDelta(text="hello"),
        Done(),
    ]
    assert orchestrator.state.goal == "say hello"
    assert orchestrator.state.step == 1
    assert orchestrator.state.turn == 1


async def test_initial_state_is_empty_with_environment_at_turn_zero() -> None:
    orchestrator, _ = make([])
    state = orchestrator.state
    assert (state.goal, state.turn, state.step) == ("", 0, 0)
    assert (state.environment.user_name, state.environment.computer_name) == (
        "user",
        "host",
    )
    assert state.environment.now.utcoffset() is not None


async def test_display_events_are_forwarded_and_model_json_is_not() -> None:
    usage = UsageEvent(usage=Usage(input_tokens=1, output_tokens=2))
    report = UsageReport(
        model_id="m1",
        usage=None,
        cost_usd=None,
        seconds=0.1,
        session_cost_usd=0.0,
        session_cost_incomplete=False,
    )
    switch = ModelSwitch(from_model_id="a", reason="backoff", to_model_id="b")
    script = reply_events(
        RESPOND,
        SET_GOAL,
        Notice(text="note"),
        switch,
        ThinkingDelta(text="hmm "),
        ThinkingDelta(text="ok"),
    )
    script.insert(-1, usage)
    script.insert(-1, report)
    orchestrator, _ = make([script])
    events = await run(orchestrator, "hi")
    assert events[:4] == [
        Notice(text="note"),
        switch,
        ThinkingDelta(text="hmm "),
        ThinkingDelta(text="ok"),
    ]
    assert events[4:6] == [usage, report]
    assert [e for e in events if isinstance(e, TextDelta)] == [TextDelta(text="hello")]
    assert events[-1] == Done()


async def test_two_turns_refresh_environment_and_the_model_cannot_patch_it() -> None:
    sneaky = {"environment": {"user_name": "root"}}
    orchestrator, provider = make(
        [
            reply_events(RESPOND, SET_GOAL),
            reply_events(RESPOND, sneaky),
            reply_events(RESPOND, {}),
        ]
    )
    await run(orchestrator, "one")
    first = orchestrator.state
    events = await run(orchestrator, "two")
    second = orchestrator.state
    assert (first.turn, second.turn) == (1, 2)
    assert second.environment.now > first.environment.now
    assert second.environment.user_name == "user"
    assert events[-1] == Done()
    assert len(provider.requests) == 3
    assert "`environment` is protected" in observation(provider.requests[2])


async def test_respond_with_empty_goal_is_rejected_then_retried() -> None:
    orchestrator, provider = make(
        [reply_events(RESPOND), reply_events(RESPOND, SET_GOAL)]
    )
    events = await run(orchestrator, "hi")
    assert events[-1] == Done()
    assert orchestrator.state.goal == "say hello"
    assert observation(provider.requests[1]) == (
        "hi\n\nYour previous reply was rejected: "
        "goal is empty: set `goal` in state_patch, "
        "or use ask_user if you cannot tell what the user wants."
    )


async def test_ask_user_with_empty_goal_is_accepted() -> None:
    orchestrator, _ = make([reply_events(ASK)])
    events = await run(orchestrator, "hmm")
    assert events == [
        StatusEvent(step=1, action_type="ask_user", text="asking the user"),
        TextDelta(text="What do you want?"),
        Done(),
    ]
    assert orchestrator.state.goal == ""
    assert orchestrator.pending_question == "What do you want?"


async def test_ambiguous_message_asks_and_the_answer_is_the_next_observation() -> None:
    orchestrator, provider = make(
        [
            reply_events(ASK),
            reply_events(RESPOND, SET_GOAL),
        ]
    )
    first = await run(orchestrator, "do the thing")
    assert TextDelta(text="What do you want?") in first
    assert observation(provider.requests[0]) == "do the thing"
    second = await run(orchestrator, "I want a greeting")
    assert second[-1] == Done()
    assert observation(provider.requests[1]) == (
        'You asked the user: "What do you want?"\nThe user answered: I want a greeting'
    )
    assert orchestrator.pending_question is None


async def test_invalid_json_three_times_ends_with_error_and_unchanged_state() -> None:
    orchestrator, provider = make(
        [[TextDelta(text="not json"), Done()] for _ in range(3)]
    )
    before = progress(orchestrator)
    events = await run(orchestrator, "hi")
    assert len(provider.requests) == 3
    assert [type(e) for e in events] == [ErrorEvent]
    error = events[0]
    assert isinstance(error, ErrorEvent) and error.kind == "unknown"
    assert "rejected 3 times" in error.message
    assert "not valid JSON" in error.message
    assert progress(orchestrator) == before
    # the error text of attempt N is the observation of attempt N+1
    assert "not valid JSON" in observation(provider.requests[1])
    assert "not valid JSON" in observation(provider.requests[2])
    assert observation(provider.requests[0]) == "hi"


async def test_attempts_are_built_from_the_same_state() -> None:
    orchestrator, provider = make(
        [
            reply_events(RESPOND, {"goal": 5}),
            reply_events(RESPOND, {"findings": [{"text": "x", "source": ""}]}),
            reply_events(RESPOND, SET_GOAL),
        ]
    )
    events = await run(orchestrator, "hi")
    assert events[-1] == Done()
    users = [
        r.messages[1].content.split("## Observation")[0] for r in provider.requests
    ]
    assert users[0] == users[1] == users[2]
    assert "`goal` must be a plain string" in observation(provider.requests[1])
    assert "findings" in observation(provider.requests[2])


async def test_retry_request_still_holds_the_user_message_once_per_rejection() -> None:
    orchestrator, provider = make(
        [
            [TextDelta(text="nope"), Done()],
            [TextDelta(text="{}"), Done()],
            reply_events(RESPOND, SET_GOAL),
        ]
    )
    await run(orchestrator, "please say hello")
    for request in provider.requests:
        assert observation(request).startswith("please say hello")
    assert (
        observation(provider.requests[1]).count("Your previous reply was rejected") == 1
    )
    # The error of attempt 1 is not carried into attempt 3; only the latest is.
    assert (
        observation(provider.requests[2]).count("Your previous reply was rejected") == 1
    )


async def test_valid_reply_on_the_third_attempt_succeeds() -> None:
    orchestrator, provider = make(
        [
            [TextDelta(text="nope"), Done()],
            [TextDelta(text="{}"), Done()],
            reply_events(RESPOND, SET_GOAL),
        ]
    )
    events = await run(orchestrator, "hi")
    assert len(provider.requests) == 3
    assert events[-1] == Done()
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert orchestrator.state.goal == "say hello"


async def test_oversized_patch_is_a_rejected_attempt() -> None:
    big = {"findings": {"$append": [{"text": "x" * 500, "source": "s"}]}}
    orchestrator, provider = make(
        [reply_events(RESPOND, big), reply_events(RESPOND, SET_GOAL)],
        max_state_chars=400,
    )
    await run(orchestrator, "hi")
    assert "over the limit of 400" in observation(provider.requests[1])
    assert orchestrator.state.findings == []


async def test_call_tool_forever_stops_at_the_step_cap() -> None:
    orchestrator, provider = make(
        [reply_events(CALL_TOOL, SET_GOAL) for _ in range(3)], max_steps=3
    )
    events = await run(orchestrator, "hi")
    assert len(provider.requests) == 3
    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert [s.step for s in statuses] == [1, 2, 3]
    assert statuses[0].action_type == "call_tool"
    last = events[-1]
    assert isinstance(last, ErrorEvent) and last.kind == "unknown"
    assert "step cap of 3" in last.message
    assert not any(isinstance(e, Done) for e in events)
    # patches of the valid steps stay committed
    assert orchestrator.state.goal == "say hello"
    assert (
        observation(provider.requests[1])
        == "unknown tool search; available tools: none"
    )


async def test_delegate_gets_a_not_available_observation_then_can_respond() -> None:
    delegate = {"type": "delegate", "subagent": "researcher", "brief": "look"}
    orchestrator, provider = make(
        [reply_events(delegate, SET_GOAL), reply_events(RESPOND)]
    )
    events = await run(orchestrator, "hi")
    assert observation(provider.requests[1]) == "delegate is not available yet"
    assert events[0] == StatusEvent(
        step=1, action_type="delegate", text="delegating to researcher"
    )
    assert events[-1] == Done()
    assert orchestrator.state.step == 2


class StubTool:
    """Test-only tool: returns a fixed text, or raises/hangs when told to."""

    def __init__(
        self, name: str = "search", result: str = "found it", *, error=None, hang=False
    ) -> None:
        self.name = name
        self._result = result
        self._error = error
        self._hang = hang
        self.calls: list[dict[str, object]] = []
        self.contexts: list[ToolContext] = []

    async def run(self, arguments: dict[str, object], ctx: ToolContext) -> str:
        self.calls.append(arguments)
        self.contexts.append(ctx)
        if self._hang:
            await asyncio.Event().wait()
        if self._error is not None:
            raise self._error
        return self._result


def registry(*tools: StubTool) -> ToolRegistry:
    tool_registry = ToolRegistry()
    for tool in tools:
        tool_registry.register(tool)
    return tool_registry


def call(tool: str, **arguments: object) -> dict:
    return {"type": "call_tool", "tool": tool, "arguments": arguments}


async def test_call_tool_result_is_the_next_observation_then_respond() -> None:
    stub = StubTool()
    orchestrator, provider = make(
        [reply_events(call("search", q="x"), SET_GOAL), reply_events(RESPOND)],
        tools=registry(stub),
    )
    events = await run(orchestrator, "hi")
    assert stub.calls == [{"q": "x"}]
    assert observation(provider.requests[1]) == "tool search result: found it"
    assert events[0] == StatusEvent(
        step=1, action_type="call_tool", text="calling tool search"
    )
    assert events[-1] == Done()
    assert orchestrator.state.step == 2


async def test_a_tool_run_gets_the_storage_user_agent_id_and_tags() -> None:
    stub = StubTool()
    storage = object()
    orchestrator, _ = make(
        [reply_events(call("search"), SET_GOAL), reply_events(RESPOND)],
        tools=registry(stub),
        user_id="ann",
        storage=storage,
    )
    await run(orchestrator, "hi")
    (ctx,) = stub.contexts
    assert ctx == ToolContext(
        storage=storage,  # type: ignore[arg-type]
        user_id="ann",
        agent_id="orchestrator",
        tags=frozenset({"orchestrator"}),
    )
    assert ctx.storage is storage


async def test_a_tool_run_without_storage_gets_none() -> None:
    stub = StubTool()
    orchestrator, _ = make(
        [reply_events(call("search"), SET_GOAL), reply_events(RESPOND)],
        tools=registry(stub),
    )
    await run(orchestrator, "hi")
    assert stub.contexts[0].storage is None
    assert stub.contexts[0].user_id is None


async def test_unknown_tool_observation_lists_available_names_sorted() -> None:
    orchestrator, provider = make(
        [reply_events(call("nope"), SET_GOAL), reply_events(RESPOND)],
        tools=registry(StubTool("zeta"), StubTool("alpha")),
    )
    events = await run(orchestrator, "hi")
    assert (
        observation(provider.requests[1])
        == "unknown tool nope; available tools: alpha, zeta"
    )
    assert events[-1] == Done()


async def test_failing_tool_gives_failed_observation_and_the_loop_continues() -> None:
    boom = StubTool(error=ValueError("bad input"))
    orchestrator, provider = make(
        [reply_events(call("search"), SET_GOAL), reply_events(RESPOND)],
        tools=registry(boom),
    )
    events = await run(orchestrator, "hi")
    assert (
        observation(provider.requests[1]) == "tool search failed: ValueError: bad input"
    )
    assert events[-1] == Done()


async def test_failing_tool_message_is_redacted() -> None:
    secret = "sk-abcdefgh12345678"
    boom = StubTool(error=RuntimeError(f"auth failed with {secret}"))
    orchestrator, provider = make(
        [reply_events(call("search"), SET_GOAL), reply_events(RESPOND)],
        tools=registry(boom),
    )
    await run(orchestrator, "hi")
    text = observation(provider.requests[1])
    assert secret not in everything(provider.requests[1])
    assert (
        text == "tool search failed: RuntimeError: auth failed with [REDACTED:api_key]"
    )


async def test_cancellation_inside_a_tool_propagates_and_resets_busy() -> None:
    orchestrator, _ = make(
        [reply_events(call("search"), SET_GOAL), reply_events(RESPOND, SET_GOAL)],
        tools=registry(StubTool(hang=True)),
    )
    task = asyncio.create_task(run(orchestrator, "first"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = await run(orchestrator, "second")
    assert events[-1] == Done()


async def test_call_tool_loop_with_a_working_tool_stops_at_the_step_cap() -> None:
    orchestrator, provider = make(
        [reply_events(call("search"), SET_GOAL) for _ in range(2)],
        max_steps=2,
        tools=registry(StubTool()),
    )
    events = await run(orchestrator, "hi")
    last = events[-1]
    assert isinstance(last, ErrorEvent) and "step cap of 2" in last.message
    assert observation(provider.requests[1]) == "tool search result: found it"


async def test_run_state_round_trips_through_json_and_a_new_orchestrator_resumes() -> (
    None
):
    patch = {
        **SET_GOAL,
        "findings": {"$append": [{"text": "f", "source": "user"}]},
        "plan": {"$append": [{"text": "p", "status": "todo"}]},
    }
    first, _ = make([reply_events(ASK, patch)])
    await run(first, "do the thing")
    snapshot = first.snapshot()
    assert snapshot.pending_question == "What do you want?"
    restored_snapshot = RunState.model_validate_json(snapshot.model_dump_json())
    assert restored_snapshot == snapshot

    second, provider = make([reply_events(RESPOND)])
    second.restore(restored_snapshot)
    assert second.pending_question == "What do you want?"
    events = await run(second, "a greeting")
    assert events[-1] == Done()
    assert observation(provider.requests[0]) == (
        'You asked the user: "What do you want?"\nThe user answered: a greeting'
    )
    assert second.state.turn == 2
    assert second.state.goal == "say hello"
    assert [f.text for f in second.state.findings] == ["f"]
    assert [p.text for p in second.state.plan] == ["p"]
    assert second.pending_question is None


async def test_restore_while_a_turn_is_running_raises() -> None:
    orchestrator, _ = make([reply_events(RESPOND, SET_GOAL)], hang_after=0)
    snapshot = orchestrator.snapshot()
    task = asyncio.create_task(run(orchestrator, "first"))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="running"):
        orchestrator.restore(snapshot)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_respond_text_is_streamed_as_text_delta() -> None:
    answer = {"type": "respond", "text": "line one\nline two"}
    orchestrator, _ = make([reply_events(answer, SET_GOAL)])
    events = await run(orchestrator, "hi")
    assert events[1:] == [TextDelta(text="line one\nline two"), Done()]


async def test_thinking_is_forwarded_but_never_stored_or_resent() -> None:
    orchestrator, provider = make(
        [
            reply_events(CALL_TOOL, SET_GOAL, ThinkingDelta(text=f"secret {CANARY}")),
            reply_events(RESPOND),
            reply_events(RESPOND),
        ]
    )
    events = await run(orchestrator, "hi")
    assert ThinkingDelta(text=f"secret {CANARY}") in events
    await run(orchestrator, "again")
    assert len(provider.requests) == 3
    assert all(CANARY not in everything(r) for r in provider.requests)
    assert CANARY not in orchestrator.state.model_dump_json()


async def test_old_user_message_never_reaches_a_later_request() -> None:
    orchestrator, provider = make([reply_events(RESPOND, SET_GOAL) for _ in range(3)])
    await run(orchestrator, f"remember {CANARY}")
    await run(orchestrator, "second")
    await run(orchestrator, "third")
    assert CANARY in everything(provider.requests[0])
    assert CANARY not in everything(provider.requests[2])


async def test_only_state_carries_earlier_user_messages_over_five_turns() -> None:
    kept = {"findings": {"$append": [{"text": "KEPT-77", "source": "user"}]}}
    scripts = [reply_events(RESPOND, {**SET_GOAL, **kept})]
    scripts += [reply_events(RESPOND) for _ in range(4)]
    orchestrator, provider = make(scripts)
    sent = [f"{CANARY}-turn{n}" for n in range(1, 6)]
    for text in sent:
        await run(orchestrator, text)
    assert len(provider.requests) == 5
    for index, request in enumerate(provider.requests):
        # a request holds its own turn's message and no earlier one
        assert sent[index] in everything(request)
        assert all(old not in everything(request) for old in sent[:index])
        # what the model chose to keep survives from turn 2 on
        assert ("KEPT-77" in everything(request)) is (index > 0)


async def test_request_carries_the_model_and_the_think_flag() -> None:
    orchestrator, provider = make([reply_events(RESPOND, SET_GOAL)], think=True)
    await run(orchestrator, "hi")
    request = provider.requests[0]
    assert request.model == "pm"
    assert request.think is True


async def test_think_flag_defaults_to_the_model_setting_when_not_given() -> None:
    orchestrator, provider = make([reply_events(RESPOND, SET_GOAL)])
    await run(orchestrator, "hi")
    assert provider.requests[0].think is None


async def test_closing_the_send_stream_closes_the_provider_stream() -> None:
    script = reply_events(RESPOND, SET_GOAL, ThinkingDelta(text="hm"))
    orchestrator, provider = make([script], hang_after=1)
    stream = orchestrator.send("hi")
    assert await anext(stream) == ThinkingDelta(text="hm")
    await stream.aclose()
    assert provider.closed


async def test_cancelling_a_turn_closes_the_provider_stream() -> None:
    orchestrator, provider = make([reply_events(RESPOND, SET_GOAL)], hang_after=0)
    task = asyncio.create_task(run(orchestrator, "hi"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed


async def test_canary_in_state_is_resent() -> None:
    kept = {"findings": {"$append": [{"text": CANARY, "source": "user"}]}}
    orchestrator, provider = make(
        [reply_events(RESPOND, {**SET_GOAL, **kept}), reply_events(RESPOND)]
    )
    await run(orchestrator, "one")
    await run(orchestrator, "two")
    assert CANARY in everything(provider.requests[1])


async def test_provider_error_is_forwarded_and_state_untouched() -> None:
    error = ErrorEvent(kind="rate_limit", message="slow down")
    orchestrator, provider = make([[ThinkingDelta(text="t"), error]])
    before = progress(orchestrator)
    events = await run(orchestrator, "hi")
    assert events == [ThinkingDelta(text="t"), error]
    assert len(provider.requests) == 1
    assert progress(orchestrator) == before


async def test_stream_without_terminal_event_is_an_unknown_error() -> None:
    orchestrator, _ = make([[TextDelta(text="{}")]])
    events = await run(orchestrator, "hi")
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ErrorEvent) and error.kind == "unknown"
    assert "without a terminal event" in error.message


async def test_second_send_while_running_raises() -> None:
    orchestrator, _ = make([reply_events(RESPOND, SET_GOAL)], hang_after=0)

    async def consume() -> None:
        await run(orchestrator, "first")

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="already running"):
        await run(orchestrator, "second")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancellation_propagates_and_resets_the_busy_flag() -> None:
    orchestrator, _ = make(
        [reply_events(CALL_TOOL, SET_GOAL), reply_events(RESPOND, SET_GOAL)],
        hang_after=1,
    )
    task = asyncio.create_task(run(orchestrator, "first"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # nothing was committed by the unfinished step
    assert orchestrator.state.goal == ""
    # the flag is reset: a new turn runs
    events = await run(orchestrator, "second")
    assert events[-1] == Done()


async def test_has_completed_turn_is_false_until_a_turn_ends_with_done() -> None:
    orchestrator, _ = make([reply_events(RESPOND, SET_GOAL)])
    assert orchestrator.has_completed_turn is False
    await run(orchestrator, "hi")
    assert orchestrator.has_completed_turn is True


async def test_has_completed_turn_is_true_after_ask_user() -> None:
    orchestrator, _ = make([reply_events(ASK, SET_GOAL)])
    await run(orchestrator, "hi")
    assert orchestrator.has_completed_turn is True


async def test_has_completed_turn_stays_false_after_failed_turns() -> None:
    error = ErrorEvent(kind="rate_limit", message="slow down")
    bad = [TextDelta(text="not json"), Done()]
    orchestrator, _ = make([[error], bad, bad, bad])
    await run(orchestrator, "hi")
    assert orchestrator.has_completed_turn is False
    await run(orchestrator, "again")  # rejected three times
    assert orchestrator.has_completed_turn is False


async def test_has_completed_turn_stays_false_after_a_cancelled_turn() -> None:
    orchestrator, _ = make([reply_events(RESPOND, SET_GOAL)], hang_after=0)
    task = asyncio.create_task(run(orchestrator, "first"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert orchestrator.has_completed_turn is False


async def test_request_after_ask_user_holds_the_question_and_the_answer() -> None:
    orchestrator, provider = make(
        [
            reply_events({"type": "ask_user", "question": "Which city?"}),
            reply_events(RESPOND, SET_GOAL),
        ]
    )
    await run(orchestrator, "weather please")
    await run(orchestrator, "Lisbon")
    text = observation(provider.requests[1])
    assert 'You asked the user: "Which city?"' in text
    assert "The user answered: Lisbon" in text


async def test_long_question_is_capped_in_the_answer_observation() -> None:
    long_question = "q" * 5000
    orchestrator, provider = make(
        [
            reply_events({"type": "ask_user", "question": long_question}),
            reply_events(RESPOND, SET_GOAL),
        ]
    )
    await run(orchestrator, "x")
    await run(orchestrator, "answer")
    text = observation(provider.requests[1])
    assert len(text) < 700
    assert text.endswith("The user answered: answer")


async def test_failed_answer_turn_keeps_the_pending_question_in_the_snapshot() -> None:
    bad = [TextDelta(text="not json"), Done()]
    orchestrator, provider = make(
        [reply_events(ASK), bad, bad, bad, reply_events(RESPOND, SET_GOAL)]
    )
    await run(orchestrator, "hmm")
    events = await run(orchestrator, "first answer")
    assert isinstance(events[-1], ErrorEvent)
    assert orchestrator.pending_question == "What do you want?"
    assert orchestrator.snapshot().pending_question == "What do you want?"
    await run(orchestrator, "second answer")
    assert 'You asked the user: "What do you want?"' in observation(
        provider.requests[4]
    )
    assert orchestrator.pending_question is None


async def test_whitespace_goal_is_rejected_with_an_actionable_retry() -> None:
    orchestrator, provider = make(
        [reply_events(RESPOND, {"goal": "  "}), reply_events(RESPOND, SET_GOAL)]
    )
    events = await run(orchestrator, "hi")
    assert events[-1] == Done()
    assert "`goal` must not be blank; use null to clear it" in observation(
        provider.requests[1]
    )


async def test_respond_with_a_blank_restored_goal_is_rejected_as_empty() -> None:
    first, _ = make([reply_events(ASK, SET_GOAL)])
    await run(first, "hi")
    blank = first.snapshot().model_copy(
        update={"state": first.state.model_copy(update={"goal": "  "})}
    )
    second, provider = make([reply_events(RESPOND), reply_events(RESPOND, SET_GOAL)])
    second.restore(blank)
    events = await run(second, "again")
    assert events[-1] == Done()
    assert "goal is empty" in observation(provider.requests[1])


async def test_long_observation_never_cuts_the_rejection_off() -> None:
    from llm_harness.context import MAX_OBSERVATION_CHARS

    long_text = "a" * (MAX_OBSERVATION_CHARS + 1000)
    orchestrator, provider = make(
        [
            [TextDelta(text="not json"), Done()],
            reply_events(RESPOND, SET_GOAL),
        ]
    )
    await run(orchestrator, long_text)
    retried = observation(provider.requests[1])
    assert "Your previous reply was rejected: Reply is not valid JSON" in retried
    assert retried.startswith("a" * 1000)
    assert len(retried) <= MAX_OBSERVATION_CHARS
    assert "truncated" not in retried.split("Your previous reply")[1]


async def test_huge_rejection_text_is_capped_in_retry_and_final_error() -> None:
    from llm_harness.context import MAX_OBSERVATION_CHARS

    huge_key = "k" * 50_000
    orchestrator, provider = make(
        [reply_events(RESPOND, {huge_key: 1}) for _ in range(3)]
    )
    events = await run(orchestrator, "hi")
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert len(error.message) < 600
    retried = observation(provider.requests[1])
    assert len(retried) <= MAX_OBSERVATION_CHARS
    assert "Your previous reply was rejected: Unknown state key" in retried
    assert len(retried) < 1000


async def test_the_user_id_is_in_the_environment_before_and_after_a_turn() -> None:
    orchestrator, _ = make([reply_events(RESPOND, SET_GOAL)], user_id="ann")
    assert orchestrator.state.environment.user_id == "ann"
    await run(orchestrator, "hi")
    assert orchestrator.state.environment.user_id == "ann"


async def test_an_unset_user_id_is_none() -> None:
    orchestrator, _ = make([reply_events(RESPOND, SET_GOAL)])
    assert orchestrator.state.environment.user_id is None
    await run(orchestrator, "hi")
    assert orchestrator.state.environment.user_id is None


async def test_two_turns_change_now_but_keep_the_user_id() -> None:
    orchestrator, _ = make(
        [reply_events(RESPOND, SET_GOAL), reply_events(RESPOND)], user_id="ann"
    )
    await run(orchestrator, "one")
    first = orchestrator.state.environment
    await run(orchestrator, "two")
    second = orchestrator.state.environment
    assert second.now > first.now
    assert first.user_id == second.user_id == "ann"


async def test_the_next_turn_overwrites_a_restored_user_id() -> None:
    orchestrator, _ = make([reply_events(RESPOND, SET_GOAL)], user_id="ann")
    forged = orchestrator.state.environment.model_copy(update={"user_id": "mallory"})
    orchestrator.restore(
        RunState(state=orchestrator.state.model_copy(update={"environment": forged}))
    )
    assert orchestrator.state.environment.user_id == "mallory"
    await run(orchestrator, "hi")
    assert orchestrator.state.environment.user_id == "ann"
