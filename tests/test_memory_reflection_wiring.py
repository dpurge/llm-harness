"""Memory reflection after an answer: the gate, the events, the failures, the trace.

Offline: a scripted provider (step replies plus one reflection reply per reflecting
turn), a throwaway SQLite file and a fake clock. A reflection request is told apart
from a step request by its `<turn-record>` block.
"""

import asyncio
import json
from pathlib import Path

import pytest

import llm_harness.orchestrator as orchestrator_module
from llm_harness.config import AgentConfig, StorageConfig, VectorConfig
from llm_harness.memory import MemoryStore
from llm_harness.orchestrator import MAX_NOTICE_CHARS, Orchestrator, _notice_line
from llm_harness.storage import Storage, open_storage
from llm_harness.tools import ToolRegistry
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    Notice,
    StatusEvent,
    StreamEvent,
    TextDelta,
    Usage,
    UsageReport,
)
from tests.fake_steps import ScriptedSteps, reply_events
from tests.test_memory_tools import FakeClock, memory_registry
from tests.test_orchestrator import (
    RESPOND,
    SET_GOAL,
    StubTool,
    call,
    make,
    model_cfg,
    run,
)
from tests.test_tool_approval import FakeApprover

GOAL = "say hello"
NEW_GOAL = {"goal": "say goodbye"}
ASK = {"type": "ask_user", "question": "What do you want?"}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path,
        relational_path=tmp_path / "memory.db",
    )


def reflection(
    action: str = "none",
    *,
    id: int | None = None,
    text: str | None = None,
    keywords: list[str] | None = None,
    extra: list[StreamEvent] | None = None,
) -> list[StreamEvent]:
    body = json.dumps({"action": action, "id": id, "text": text, "keywords": keywords})
    return [*(extra or []), TextDelta(text=body), Done()]


def build(
    clock: FakeClock,
    storage: Storage | None,
    scripts: list[list[StreamEvent]],
    *,
    user_id: str | None = "alice",
    **kwargs: object,
) -> tuple[Orchestrator, ScriptedSteps]:
    """An orchestrator with memory on, a stub `search` tool and goal GOAL."""
    tools = memory_registry(clock)
    tools.register(StubTool())
    kwargs.setdefault("goal", GOAL)
    return make(scripts, tools=tools, storage=storage, user_id=user_id, **kwargs)


def reflections(provider: ScriptedSteps) -> list[ChatRequest]:
    return [r for r in provider.requests if "<turn-record>" in r.messages[1].content]


def kinds(orchestrator: Orchestrator) -> list[str]:
    return [span.kind for span in orchestrator.last_trace or ()]


def tool_turn(*, goal_patch: dict | None = None) -> list[list[StreamEvent]]:
    return [
        reply_events(call("search", q="x"), goal_patch or {}),
        reply_events(RESPOND),
    ]


async def store_of(storage: Storage, clock: FakeClock) -> MemoryStore:
    return MemoryStore(storage.relational, clock)


# --- the gate ---------------------------------------------------------------------


async def test_a_chat_turn_without_tool_or_goal_change_makes_no_extra_call(
    storage, clock
) -> None:
    orchestrator, provider = build(clock, storage, [reply_events(RESPOND)])
    events = await run(orchestrator, "hi")
    assert len(provider.requests) == 1
    assert [type(e) for e in events] == [StatusEvent, TextDelta, Done]
    assert "reflection" not in kinds(orchestrator)
    assert kinds(orchestrator) == ["turn", "recall", "step", "llm_call"]


async def test_the_first_turn_that_sets_the_goal_reflects(storage, clock) -> None:
    orchestrator, provider = build(
        clock,
        storage,
        [reply_events(RESPOND, SET_GOAL), reflection()],
        goal="",
    )
    await run(orchestrator, "hi")
    assert len(reflections(provider)) == 1


async def test_a_tool_turn_makes_exactly_one_extra_call(storage, clock) -> None:
    orchestrator, provider = build(clock, storage, [*tool_turn(), reflection()])
    await run(orchestrator, "hi")
    assert len(provider.requests) == 3
    assert len(reflections(provider)) == 1


async def test_a_goal_change_makes_exactly_one_extra_call(storage, clock) -> None:
    orchestrator, provider = build(
        clock, storage, [reply_events(RESPOND, NEW_GOAL), reflection()]
    )
    await run(orchestrator, "hi")
    assert len(provider.requests) == 2
    assert len(reflections(provider)) == 1


async def test_a_delegation_counts_as_a_tool_turn(storage, clock) -> None:
    child = AgentConfig(
        id="helper",
        name="Helper",
        description="Helps.",
        tags=["memory"],
        model_tags=["chat"],
        examples=["help"],
    )
    child_provider = ScriptedSteps(
        [reply_events({"type": "respond", "text": "child-canary"}, SET_GOAL)]
    )
    delegate = {"type": "delegate", "subagent": "helper", "brief": "do it"}
    orchestrator, provider = build(
        clock,
        storage,
        [reply_events(delegate), reply_events(RESPOND), reflection()],
        subagents=[child],
        subagent_factory=lambda agent: (child_provider, model_cfg()),
    )
    await run(orchestrator, "hi")
    # The child answers once and never reflects, though it set its own goal.
    assert len(child_provider.requests) == 1
    assert len(reflections(provider)) == 1
    tool_results = (
        reflections(provider)[0].messages[1].content.split("## Tool results")[1]
    )
    assert "child-canary" in tool_results


async def test_memory_off_makes_no_extra_call(storage, clock) -> None:
    cases = {
        "no memory tools": {"tools": ToolRegistry(), "storage": storage},
        "no storage": {"tools": memory_registry(clock), "storage": None},
        "no user id": {
            "tools": memory_registry(clock),
            "storage": storage,
            "user_id": None,
        },
    }
    for name, kwargs in cases.items():
        orchestrator, provider = make(
            [reply_events(RESPOND, NEW_GOAL)],
            **{"user_id": "alice", "goal": GOAL, **kwargs},
        )
        events = await run(orchestrator, "hi")
        assert len(provider.requests) == 1, name
        assert [type(e) for e in events] == [StatusEvent, TextDelta, Done], name
        assert "reflection" not in kinds(orchestrator), name


async def test_a_subagent_never_reflects(storage, clock) -> None:
    helper = AgentConfig(
        id="helper",
        name="Helper",
        description="Helps.",
        tags=["memory"],
        model_tags=["chat"],
    )
    orchestrator, provider = build(
        clock,
        storage,
        [reply_events(RESPOND, NEW_GOAL)],
        agent=helper,
    )
    await run(orchestrator, "hi")
    assert len(provider.requests) == 1
    assert "reflection" not in kinds(orchestrator)


async def test_no_reflection_after_ask_user_or_a_failed_turn(storage, clock) -> None:
    asking, provider = build(clock, storage, [reply_events(ASK, NEW_GOAL)])
    await run(asking, "hi")
    assert len(provider.requests) == 1

    failing, provider = build(
        clock,
        storage,
        [[TextDelta(text="par"), ErrorEvent(kind="connection", message="boom")]],
    )
    events = await run(failing, "hi")
    assert isinstance(events[-1], ErrorEvent)
    assert len(provider.requests) == 1

    looping, provider = build(
        clock,
        storage,
        [reply_events(call("search"), NEW_GOAL)] * 2,
        max_steps=2,
    )
    events = await run(looping, "hi")
    assert isinstance(events[-1], ErrorEvent)
    assert len(provider.requests) == 2


# --- the effect -------------------------------------------------------------------


async def test_save_is_applied_and_the_notice_sits_between_answer_and_done(
    storage, clock
) -> None:
    orchestrator, _ = build(
        clock,
        storage,
        [
            *tool_turn(),
            reflection(
                "save", text="Prefers metric units.", keywords=["metric", "units"]
            ),
        ],
    )
    events = await run(orchestrator, "hi")
    assert [type(e) for e in events] == [
        StatusEvent,
        StatusEvent,
        TextDelta,
        Notice,
        Done,
    ]
    assert events[3] == Notice(text="memory saved #1: Prefers metric units.")
    (saved,) = await (await store_of(storage, clock)).list("alice")
    assert saved.text == "Prefers metric units."
    assert saved.keywords == ("metric", "units")


async def test_update_and_forget_are_applied(storage, clock) -> None:
    store = await store_of(storage, clock)
    await store.save("alice", "Likes tea.", ["tea"])
    await store.save("alice", "Lives in Lisbon.", ["lisbon"])
    orchestrator, _ = build(
        clock,
        storage,
        [
            *tool_turn(),
            reflection("update", id=1, text="Likes green tea.", keywords=["tea"]),
            *tool_turn(),
            reflection("forget", id=2),
        ],
    )
    first = await run(orchestrator, "tea")
    assert Notice(text="memory updated #1: Likes green tea.") in first
    assert (await store.get("alice", 1)).text == "Likes green tea."
    second = await run(orchestrator, "lisbon")
    assert Notice(text="memory forgot #2") in second
    assert await store.get("alice", 2) is None


async def test_none_is_silent(storage, clock) -> None:
    orchestrator, provider = build(clock, storage, [*tool_turn(), reflection()])
    events = await run(orchestrator, "hi")
    assert len(reflections(provider)) == 1
    assert [type(e) for e in events] == [StatusEvent, StatusEvent, TextDelta, Done]
    span = next(s for s in orchestrator.last_trace if s.kind == "reflection")
    assert span.attributes["action"] == "none"
    assert span.attributes["outcome"] == "none"
    assert span.status == "ok"


async def test_a_reflection_cannot_change_another_users_memory(storage, clock) -> None:
    store = await store_of(storage, clock)
    await store.save("bob", "Bob likes tea.", ["tea"])
    orchestrator, _ = build(
        clock,
        storage,
        [*tool_turn(), reflection("update", id=1, text="Hacked.", keywords=None)],
    )
    events = await run(orchestrator, "hi")
    assert Notice(text="memory: no memory #1") in events
    assert (await store.get("bob", 1)).text == "Bob likes tea."
    assert await store.list("alice") == []


async def test_forget_of_an_unknown_id_says_so(storage, clock) -> None:
    orchestrator, _ = build(clock, storage, [*tool_turn(), reflection("forget", id=9)])
    events = await run(orchestrator, "hi")
    assert Notice(text="memory: no memory #9") in events


async def test_a_separator_in_the_stored_text_stays_one_line(storage, clock) -> None:
    text = "Line one\u2028line two\u2029line three."
    orchestrator, _ = build(
        clock, storage, [*tool_turn(), reflection("save", text=text, keywords=["l"])]
    )
    events = await run(orchestrator, "hi")
    (notice,) = [e for e in events if isinstance(e, Notice)]
    assert notice.text == "memory saved #1: Line one line two line three."
    assert len(notice.text.splitlines()) == 1


def test_a_notice_is_clipped_to_300_characters_with_an_ellipsis() -> None:
    clipped = _notice_line("x" * 1000)
    assert len(clipped) == MAX_NOTICE_CHARS == 300
    assert clipped.endswith("…") and clipped.startswith("x" * 299)
    exact = "y" * 300
    assert _notice_line(exact) == exact


def test_a_notice_line_has_no_line_or_paragraph_separator() -> None:
    line = _notice_line("a\nb\r\nc\u2028d\u2029e\x0bf\x85g")
    assert line == "a b c d e f g"


def test_a_notice_line_masks_a_secret_before_clipping() -> None:
    secret = "sk-" + "a1b2c3d4" * 4
    line = _notice_line("x" * 290 + secret)
    assert "sk-" not in line and "a1b2" not in line
    assert len(line) == 300 and line.endswith("…")


# --- failures ---------------------------------------------------------------------


class HangingReflection:
    """Serves the step replies, then hangs on the reflection call."""

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self._steps = ScriptedSteps(scripts)
        self.requests = self._steps.requests
        self.reflecting = asyncio.Event()
        self.closed = False

    async def stream_chat(self, req: ChatRequest):
        if "<turn-record>" in req.messages[1].content:
            self.requests.append(req)
            self.reflecting.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.closed = True
            return
        async for event in self._steps.stream_chat(req):
            yield event

    async def aclose(self) -> None:
        return None


def assert_answered_then_failed(events: list[StreamEvent], reason: str) -> Notice:
    assert not [e for e in events if isinstance(e, ErrorEvent)]
    assert isinstance(events[-1], Done)
    assert any(e == TextDelta(text="hello") for e in events)
    (notice,) = [e for e in events if isinstance(e, Notice)]
    assert notice.text.startswith("memory reflection failed: ")
    assert reason in notice.text
    assert events.index(notice) == len(events) - 2
    return notice


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ([TextDelta(text="not json"), Done()], "not valid JSON"),
        (
            [TextDelta(text='{"action": "save", "text": "t"}'), Done()],
            "`save` needs `keywords`",
        ),
        (
            [ErrorEvent(kind="rate_limit", message="slow down")],
            "provider error (rate_limit): slow down",
        ),
        ([TextDelta(text="{}")], "the provider ended the stream"),
        (
            reflection("save", text="key sk-" + "a1b2c3d4" * 4, keywords=["k"]),
            "secret",
        ),
    ],
    ids=["invalid-json", "missing-keywords", "provider-error", "no-terminal", "secret"],
)
async def test_a_bad_reflection_gives_the_failure_notice_and_still_done(
    storage, clock, script, reason
) -> None:
    orchestrator, _ = build(clock, storage, [*tool_turn(), script])
    events = await run(orchestrator, "hi")
    notice = assert_answered_then_failed(events, reason)
    assert "sk-a1b2" not in notice.text
    assert await (await store_of(storage, clock)).list("alice") == []
    span = next(s for s in orchestrator.last_trace if s.kind == "reflection")
    assert span.status == "error" and span.attributes["outcome"] == "failed"
    assert span.attributes["error"] in notice.text
    assert orchestrator.has_completed_turn
    assert not orchestrator._busy


async def test_a_provider_that_never_answers_times_out(
    storage, clock, monkeypatch
) -> None:
    monkeypatch.setattr(orchestrator_module, "REFLECTION_TIMEOUT_SECONDS", 0.05)
    provider = HangingReflection(tool_turn())
    tools = memory_registry(clock)
    tools.register(StubTool())
    orchestrator = Orchestrator(
        provider,
        model_cfg(),
        tools=tools,
        storage=storage,
        user_id="alice",
        goal=GOAL,
    )
    events = await run(orchestrator, "hi")
    assert_answered_then_failed(events, "no answer within 0.05 seconds")
    assert provider.closed
    span = next(s for s in orchestrator.last_trace if s.kind == "reflection")
    assert span.status == "error"
    (llm_call,) = [s for s in orchestrator.last_trace if s.parent_id == span.id]
    assert llm_call.status == "error"
    assert llm_call.attributes["error"] == "no answer within 0.05 seconds"


async def test_a_store_that_raises_gives_the_failure_notice(
    storage, clock, monkeypatch
) -> None:
    async def broken(self, user_id, text, keywords):
        raise RuntimeError("disk full")

    monkeypatch.setattr(MemoryStore, "save", broken)
    orchestrator, _ = build(
        clock,
        storage,
        [*tool_turn(), reflection("save", text="Likes tea.", keywords=["tea"])],
    )
    events = await run(orchestrator, "hi")
    assert_answered_then_failed(events, "RuntimeError: disk full")


async def test_a_search_that_raises_gives_the_failure_notice(
    storage, clock, monkeypatch
) -> None:
    orchestrator, _ = build(clock, storage, [*tool_turn(), reflection()])
    original = MemoryStore.search
    calls = []

    async def second_search_fails(self, user_id, query, k=5):
        calls.append(query)
        if len(calls) > 1:  # the first search is the automatic recall
            raise RuntimeError("database is locked")
        return await original(self, user_id, query, k)

    monkeypatch.setattr(MemoryStore, "search", second_search_fails)
    events = await run(orchestrator, "hi")
    assert_answered_then_failed(events, "RuntimeError: database is locked")


async def test_cancellation_during_the_reflection_propagates_and_resets_busy(
    storage, clock
) -> None:
    provider = HangingReflection(tool_turn())
    tools = memory_registry(clock)
    tools.register(StubTool())
    orchestrator = Orchestrator(
        provider,
        model_cfg(),
        tools=tools,
        storage=storage,
        user_id="alice",
        goal=GOAL,
    )
    task = asyncio.create_task(run(orchestrator, "hi"))
    await asyncio.wait_for(provider.reflecting.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not orchestrator._busy
    assert provider.closed
    reflection_span = next(s for s in orchestrator.last_trace if s.kind == "reflection")
    assert reflection_span.status == "cancelled"
    # The orchestrator is usable again.
    assert orchestrator.state.turn == 1


async def test_closing_the_stream_during_the_reflection_keeps_the_committed_state(
    storage, clock
) -> None:
    orchestrator, _ = build(
        clock,
        storage,
        [
            reply_events(RESPOND, NEW_GOAL),
            reflection("save", text="Likes tea.", keywords=["tea"]),
        ],
    )
    stream = orchestrator.send("hi")
    seen = []
    async for event in stream:
        seen.append(event)
        if isinstance(event, Notice):
            break
    await stream.aclose()
    assert orchestrator.state.goal == "say goodbye"
    assert not orchestrator._busy
    assert not any(isinstance(e, Done) for e in seen)
    assert len(await (await store_of(storage, clock)).list("alice")) == 1


# --- what is sent, approved, traced and billed ---------------------------------------


async def test_the_request_has_the_turn_and_the_notes_and_no_step_machinery(
    storage, clock
) -> None:
    store = await store_of(storage, clock)
    await store.save("alice", "Prefers metric units.", ["metric"])
    stub = StubTool(result="tool-result-canary")
    tools = memory_registry(clock)
    tools.register(stub)
    orchestrator, provider = make(
        [
            reply_events(call("search", q="x")),
            reply_events({"type": "respond", "text": "answer-canary"}),
            reflection(),
        ],
        tools=tools,
        storage=storage,
        user_id="alice",
        goal=GOAL,
    )
    await run(orchestrator, "convert to metric please")
    (request,) = reflections(provider)
    text = "\n".join(m.content for m in request.messages)
    assert "tool-result-canary" in text
    assert "convert to metric please" in text
    assert "answer-canary" in text
    assert "## Goal\n\nsay hello" in text
    assert "#1 [metric]" in text and "Prefers metric units." in text
    assert "state_patch" not in text
    assert "## Tools" not in text and "## State" not in text
    assert request.response_schema is not None
    assert request.model == "pm"


async def test_the_reflection_never_calls_the_approver(storage, clock) -> None:
    approver = FakeApprover(True)
    orchestrator, provider = build(
        clock,
        storage,
        [
            *tool_turn(),
            reflection("save", text="Likes tea.", keywords=["tea"]),
        ],
        approver=approver,
    )
    events = await run(orchestrator, "hi")
    assert len(reflections(provider)) == 1
    assert approver.requests == []
    assert any(isinstance(e, Notice) for e in events)


async def test_the_span_tree_has_a_reflection_with_an_llm_call_child(
    storage, clock
) -> None:
    orchestrator, _ = build(
        clock,
        storage,
        [
            *tool_turn(),
            reflection("save", text="Likes tea.", keywords=["tea"]),
        ],
    )
    await run(orchestrator, "hi")
    spans = orchestrator.last_trace
    turn = next(s for s in spans if s.kind == "turn")
    span = next(s for s in spans if s.kind == "reflection")
    assert span.parent_id == turn.id and span.status == "ok"
    assert span.attributes["action"] == "save"
    assert span.attributes["id"] is None
    assert span.attributes["outcome"] == "applied"
    (llm_call,) = [s for s in spans if s.parent_id == span.id]
    assert llm_call.kind == "llm_call" and llm_call.status == "ok"
    assert llm_call.attributes["model"] == "pm"
    assert [m["role"] for m in llm_call.attributes["messages"]] == ["system", "user"]
    assert "<turn-record>" in llm_call.attributes["messages"][1]["content"]
    assert json.loads(llm_call.attributes["reply"])["action"] == "save"
    # Opened after the last step, so the answer's steps keep their order.
    assert [s.kind for s in spans].index("reflection") > [s.kind for s in spans].index(
        "step"
    )


async def test_the_usage_report_is_forwarded_and_noted_in_the_span(
    storage, clock
) -> None:
    report = UsageReport(
        model_id="m1",
        usage=Usage(input_tokens=7, output_tokens=3),
        cost_usd=0.01,
        seconds=0.5,
        session_cost_usd=0.02,
        session_cost_incomplete=False,
    )
    orchestrator, _ = build(
        clock,
        storage,
        [
            *tool_turn(),
            reflection("save", text="Likes tea.", keywords=["tea"], extra=[report]),
        ],
    )
    events = await run(orchestrator, "hi")
    assert [type(e) for e in events[-3:]] == [UsageReport, Notice, Done]
    llm_call = next(
        s
        for s in orchestrator.last_trace
        if s.kind == "llm_call" and "reported_model" in s.attributes
    )
    assert llm_call.attributes["usage"]["input_tokens"] == 7
    assert llm_call.attributes["cost_usd"] == 0.01


async def test_other_provider_events_are_not_shown(storage, clock) -> None:
    from llm_harness.types import ThinkingDelta

    orchestrator, _ = build(
        clock,
        storage,
        [
            *tool_turn(),
            reflection(
                "save",
                text="Likes tea.",
                keywords=["tea"],
                extra=[ThinkingDelta(text="hmm")],
            ),
        ],
    )
    events = await run(orchestrator, "hi")
    assert not [e for e in events if isinstance(e, ThinkingDelta)]
    assert not [e for e in events if isinstance(e, TextDelta) and "action" in e.text]
    llm_call = next(
        s
        for s in orchestrator.last_trace
        if s.kind == "llm_call" and "thinking" in s.attributes
    )
    assert llm_call.attributes["thinking"] == "hmm"
