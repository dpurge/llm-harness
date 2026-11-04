"""`Orchestrator.propose_memories`: the gate, the call, the failures, the trace.

Offline: a scripted provider, a throwaway SQLite file and a fake clock. An add request
is told apart from a step request by its `<pasted-text>` block.
"""

import asyncio
import json
from pathlib import Path

import pytest

import llm_harness.orchestrator as orchestrator_module
from llm_harness.config import StorageConfig, VectorConfig
from llm_harness.memory import MemoryStore
from llm_harness.memory_add import ProposedNote, add_schema
from llm_harness.orchestrator import Orchestrator
from llm_harness.storage import Storage, open_storage
from llm_harness.tools import ToolRegistry
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    StreamEvent,
    TextDelta,
    Usage,
    UsageReport,
)
from tests.fake_steps import ScriptedSteps, reply_events
from tests.test_memory_tools import FakeClock, memory_registry
from tests.test_orchestrator import RESPOND, StubTool, make, model_cfg, run

GOAL = "say hello"


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


def proposals(notes: list[str], keywords: list[str]) -> list[StreamEvent]:
    return [TextDelta(text=json.dumps({"notes": notes, "keywords": keywords})), Done()]


def build(
    clock: FakeClock,
    storage: Storage | None,
    scripts: list[list[StreamEvent]],
    *,
    user_id: str | None = "alice",
    **kwargs: object,
) -> tuple[Orchestrator, ScriptedSteps]:
    tools = memory_registry(clock)
    tools.register(StubTool())
    kwargs.setdefault("goal", GOAL)
    return make(scripts, tools=tools, storage=storage, user_id=user_id, **kwargs)


def adds(provider: ScriptedSteps) -> list[ChatRequest]:
    return [r for r in provider.requests if "<pasted-text>" in r.messages[1].content]


def span_of(orchestrator: Orchestrator, kind: str):
    return next(s for s in orchestrator.last_trace or () if s.kind == kind)


# --- the gate (criterion 7) -----------------------------------------------------------


async def test_memory_off_makes_no_model_call_and_returns_the_off_result(
    storage, clock
) -> None:
    no_storage, p1 = build(clock, None, [])
    no_user, p2 = build(clock, storage, [], user_id=None)
    no_search, p3 = make([], storage=storage, user_id="alice")  # no memory tools
    for orchestrator, provider in ((no_storage, p1), (no_user, p2), (no_search, p3)):
        result = await orchestrator.propose_memories("I like tea", False)
        assert result.off is True and result.notes == () and result.error is None
        assert result.report is None
        assert provider.requests == []


async def test_a_registry_without_memory_search_is_off(storage, clock) -> None:
    tools = ToolRegistry()
    tools.register(StubTool())
    orchestrator, provider = make([], tools=tools, storage=storage, user_id="alice")
    assert (await orchestrator.propose_memories("x")).off is True
    assert provider.requests == []


# --- success and the usage report (criterion 8) ---------------------------------------


async def test_a_scripted_reply_gives_the_notes_the_usage_report_and_a_clean_request(
    storage, clock
) -> None:
    report = UsageReport(
        model_id="m1",
        usage=Usage(input_tokens=10, output_tokens=5),
        cost_usd=None,
        seconds=0.5,
        session_cost_usd=0.0,
        session_cost_incomplete=False,
    )
    script = [report, *proposals(["The user likes tea."], ["tea, drink"])]
    orchestrator, provider = build(clock, storage, [script])
    store = MemoryStore(storage.relational, clock)
    await store.save("alice", "Alice likes green tea", ["tea"])
    await store.save("bob", "Bob private tea secret-ish", ["tea"])
    await store.save("bob", "Team rule: tea is free", ["tea"], shared=True)

    result = await orchestrator.propose_memories("I like tea a lot", False)

    assert result.error is None and result.off is False
    assert result.notes == (ProposedNote("The user likes tea.", ("tea", "drink")),)
    assert result.report == report
    (request,) = provider.requests
    assert request.model == model_cfg().model
    assert request.response_schema == add_schema()
    assert [m.role for m in request.messages] == ["system", "user"]
    user = request.messages[1].content
    assert "<pasted-text>\nI like tea a lot\n</pasted-text>" in user
    assert "Alice likes green tea" in user  # related notes: own and shared
    assert "[shared]" in user and "Team rule: tea is free" in user
    assert "Bob private" not in user
    assert await store.list("alice") != []  # nothing was saved by the call
    assert len(await store.list("alice")) == 2


async def test_nothing_new_is_an_empty_result_not_an_error(storage, clock) -> None:
    orchestrator, _ = build(clock, storage, [proposals([], [])])
    result = await orchestrator.propose_memories("already known")
    assert result.notes == () and result.error is None and result.off is False


async def test_the_call_is_traced_as_memory_add_with_a_nested_llm_call(
    storage, clock
) -> None:
    orchestrator, _ = build(
        clock, storage, [reply_events(RESPOND), proposals(["n"], ["k"])]
    )
    await run(orchestrator, "hi")
    await orchestrator.propose_memories("I like tea", True)
    span = span_of(orchestrator, "memory_add")
    assert span.status == "ok"
    assert span.attributes["shared"] is True and span.attributes["notes"] == 1
    (llm_call,) = [
        s
        for s in orchestrator.last_trace
        if s.parent_id == span.id and s.kind == "llm_call"
    ]
    assert llm_call.status == "ok"
    assert json.loads(llm_call.attributes["reply"]) == {
        "notes": ["n"],
        "keywords": ["k"],
    }


async def test_a_reply_that_breaks_the_contract_is_an_error_result(
    storage, clock
) -> None:
    script = proposals(["a", "b"], ["k"])
    orchestrator, provider = build(clock, storage, [reply_events(RESPOND), script])
    await run(orchestrator, "hi")
    result = await orchestrator.propose_memories("x")
    assert result.notes == () and result.off is False
    assert result.error is not None and "same length" in result.error
    assert span_of(orchestrator, "memory_add").status == "error"
    assert len(adds(provider)) == 1


async def test_a_secret_in_the_reply_never_reaches_the_error(storage, clock) -> None:
    orchestrator, _ = build(
        clock, storage, [[TextDelta(text="sk-" + "a1b2c3d4" * 4 + " nope"), Done()]]
    )
    result = await orchestrator.propose_memories("x")
    assert result.error is not None and "sk-a1b2" not in result.error


# --- timeout and provider error (criterion 9) -----------------------------------------


class HangingAdd:
    """Serves the step replies, then hangs on the add call."""

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self._steps = ScriptedSteps(scripts)
        self.requests = self._steps.requests
        self.adding = asyncio.Event()
        self.closed = False

    async def stream_chat(self, req: ChatRequest):
        if "<pasted-text>" in req.messages[1].content:
            self.requests.append(req)
            self.adding.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.closed = True
            return
        async for event in self._steps.stream_chat(req):
            yield event

    async def aclose(self) -> None:
        return None


def hanging(clock: FakeClock, storage: Storage) -> tuple[Orchestrator, HangingAdd]:
    provider = HangingAdd([reply_events(RESPOND)])
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
    return orchestrator, provider


async def test_a_provider_that_never_answers_times_out_after_one_attempt(
    storage, clock, monkeypatch
) -> None:
    monkeypatch.setattr(orchestrator_module, "REFLECTION_TIMEOUT_SECONDS", 0.05)
    orchestrator, provider = hanging(clock, storage)
    await run(orchestrator, "hi")
    result = await orchestrator.propose_memories("I like tea")
    assert result.error == "no answer within 0.05 seconds"
    assert result.notes == () and result.off is False
    assert provider.closed
    assert len(provider.requests) == 2  # the step, then ONE add request: no retry
    span = span_of(orchestrator, "memory_add")
    assert span.status == "error"
    assert span.attributes["error"] == "no answer within 0.05 seconds"
    (llm_call,) = [s for s in orchestrator.last_trace if s.parent_id == span.id]
    assert llm_call.status == "error"


async def test_the_default_timeout_is_sixty_seconds() -> None:
    assert orchestrator_module.REFLECTION_TIMEOUT_SECONDS == 60


async def test_a_provider_error_gives_the_reason_and_one_request(
    storage, clock
) -> None:
    error = [ErrorEvent(kind="rate_limit", message="slow down")]
    orchestrator, provider = build(clock, storage, [reply_events(RESPOND), error])
    await run(orchestrator, "hi")
    result = await orchestrator.propose_memories("I like tea")
    assert result.error == "provider error (rate_limit): slow down"
    assert len(provider.requests) == 2
    assert span_of(orchestrator, "memory_add").status == "error"


async def test_a_stream_without_a_terminal_event_is_an_error_result(
    storage, clock
) -> None:
    orchestrator, _ = build(clock, storage, [[TextDelta(text="{}")]])
    result = await orchestrator.propose_memories("x")
    assert result.error is not None and "ended the stream" in result.error


async def test_a_store_that_raises_is_an_error_result(
    storage, clock, monkeypatch
) -> None:
    async def broken(self, *args, **kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(MemoryStore, "search", broken)
    orchestrator, provider = build(clock, storage, [])
    result = await orchestrator.propose_memories("I like tea")
    assert result.error == "RuntimeError: disk gone"
    assert provider.requests == []


# --- cancel (criterion 10) ------------------------------------------------------------


async def test_cancelling_the_awaiting_task_propagates_and_closes_the_stream(
    storage, clock
) -> None:
    orchestrator, provider = hanging(clock, storage)
    await run(orchestrator, "hi")
    task = asyncio.create_task(orchestrator.propose_memories("I like tea"))
    await asyncio.wait_for(provider.adding.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed
    assert span_of(orchestrator, "memory_add").status == "cancelled"
