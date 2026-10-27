import asyncio

import pytest

from llm_harness.session import ChatSession
from llm_harness.types import (
    Done,
    ErrorEvent,
    Message,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
)
from tests.fakes import ScriptedProvider, assert_stream_contract

NO_TERMINAL = "the provider ended the stream without a terminal event"


def good_reply(text: str = "ok") -> list[StreamEvent]:
    return [TextDelta(text=text), Done(finish_reason="stop")]


async def collect(session: ChatSession, text: str) -> list[StreamEvent]:
    return [event async for event in session.send(text)]


async def test_events_are_re_yielded_unchanged():
    events = [
        ThinkingDelta(text="hm"),
        TextDelta(text="a"),
        UsageEvent(usage=Usage(input_tokens=1)),
        Done(),
    ]
    session = ChatSession(ScriptedProvider(events), "m")
    assert await collect(session, "hi") == events


async def test_turn_one_sends_new_user_message_with_model_and_think():
    provider = ScriptedProvider(good_reply())
    session = ChatSession(provider, "m", think=True)
    await collect(session, "hi")
    request = provider.requests[-1]
    assert request.model == "m"
    assert request.think is True
    assert request.messages == [Message(role="user", content="hi")]


async def test_turn_n_sends_whole_history():
    provider = ScriptedProvider(good_reply("ok"))
    session = ChatSession(provider, "m")
    await collect(session, "one")
    await collect(session, "two")
    await collect(session, "three")
    assert provider.requests[-1].messages == [
        Message(role="user", content="one"),
        Message(role="assistant", content="ok"),
        Message(role="user", content="two"),
        Message(role="assistant", content="ok"),
        Message(role="user", content="three"),
    ]


async def test_assistant_message_is_joined_text_and_excludes_thinking():
    events = [
        ThinkingDelta(text="secret reasoning"),
        TextDelta(text="Hel"),
        ThinkingDelta(text="more"),
        TextDelta(text="lo"),
        Done(),
    ]
    session = ChatSession(ScriptedProvider(events), "m")
    await collect(session, "hi")
    assert session.history == [
        Message(role="user", content="hi"),
        Message(role="assistant", content="Hello"),
    ]
    assert "reasoning" not in str(session.history)


async def test_error_event_rolls_back_user_message():
    error = ErrorEvent(kind="auth", message="bad key")
    session = ChatSession(ScriptedProvider([TextDelta(text="x"), error]), "m")
    events = await collect(session, "hi")
    assert events[-1] == error
    assert session.history == []


async def test_history_is_clean_when_error_is_yielded():
    error = ErrorEvent(kind="auth", message="bad key")
    session = ChatSession(ScriptedProvider([error]), "m")
    stream = session.send("hi")
    assert await anext(stream) == error
    assert session.history == []
    await stream.aclose()


async def test_cancel_rolls_back_and_closes_provider_stream():
    provider = ScriptedProvider([TextDelta(text="a")], hang_after=1)
    session = ChatSession(provider, "m")
    got_first = asyncio.Event()

    async def consume() -> None:
        async for _ in session.send("hi"):
            got_first.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(got_first.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.history == []
    assert provider.closed


async def test_aclose_of_send_rolls_back_and_closes_provider_stream():
    provider = ScriptedProvider([TextDelta(text="a")], hang_after=1)
    session = ChatSession(provider, "m")
    stream = session.send("hi")
    await anext(stream)
    await stream.aclose()
    assert session.history == []
    assert provider.closed


async def test_missing_terminal_event_yields_one_error_and_rolls_back():
    provider = ScriptedProvider([TextDelta(text="a")])
    session = ChatSession(provider, "m")
    events = await collect(session, "hi")
    assert events == [
        TextDelta(text="a"),
        ErrorEvent(kind="unknown", message=NO_TERMINAL),
    ]
    assert_stream_contract(events)
    assert session.history == []
    assert provider.closed


async def test_second_concurrent_send_raises():
    provider = ScriptedProvider([TextDelta(text="a")], hang_after=1)
    session = ChatSession(provider, "m")
    first = session.send("one")
    await anext(first)
    with pytest.raises(RuntimeError, match="a turn is already running"):
        await anext(session.send("two"))
    await first.aclose()
    assert session.history == []


async def test_session_is_reusable_after_a_failed_turn():
    session = ChatSession(ScriptedProvider([TextDelta(text="a")]), "m")
    await collect(session, "one")
    assert len(await collect(session, "two")) == 2


async def test_history_property_returns_a_copy():
    session = ChatSession(ScriptedProvider(good_reply()), "m")
    await collect(session, "hi")
    session.history.clear()
    assert len(session.history) == 2


@pytest.mark.parametrize(
    "events",
    [
        [Done()],
        [
            ThinkingDelta(text="t"),
            TextDelta(text="a"),
            UsageEvent(usage=Usage()),
            Done(),
        ],
        [TextDelta(text="a"), ErrorEvent(kind="unknown", message="x")],
    ],
)
def test_contract_accepts_good_streams(events):
    assert_stream_contract(events)


@pytest.mark.parametrize(
    "events",
    [
        [Done(), Done()],
        [Done(), ErrorEvent(kind="unknown", message="x")],
        [UsageEvent(usage=Usage()), UsageEvent(usage=Usage()), Done()],
        [TextDelta(text="a")],
        [],
        [Done(), TextDelta(text="late")],
    ],
    ids=[
        "two-done",
        "done-and-error",
        "usage-twice",
        "no-terminal",
        "empty",
        "delta-after",
    ],
)
def test_contract_rejects_bad_streams(events):
    with pytest.raises(AssertionError):
        assert_stream_contract(events)
