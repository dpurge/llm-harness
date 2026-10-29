"""The child run: outcomes, observation text, refusals, forwarding (offline)."""

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict

from llm_harness.config import AgentConfig, ConfigError, HarnessConfig, ModelConfig
from llm_harness.router import ModelRouter, SessionCost, make_router_factory
from llm_harness.state import Environment, OrchestratorState
from llm_harness.subagents import (
    SubagentResult,
    SubagentRun,
    make_subagent_factory,
    make_subagent_factory_maker,
    result_observation,
)
from llm_harness.tools import ToolContext, ToolRegistry
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
    UsageReport,
)
from tests.fake_steps import ScriptedSteps, reply_events

PARENT_ENV = Environment(
    now=datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC),
    user_name="parent-user",
    computer_name="parent-host",
    user_id="u-1",
)
BRIEF = "find the capital of France"
RESPOND = {"type": "respond", "text": "Paris"}
ASK = {"type": "ask_user", "question": "Which France?"}
DELEGATE = {"type": "delegate", "subagent": "other", "brief": "x"}
FINDING = {
    "findings": {"$append": [{"text": "Paris is the capital", "source": "atlas"}]}
}


class AnyArguments(BaseModel):
    model_config = ConfigDict(extra="allow")


class StubTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"Stub tool {name}."
        self.tags: list[str] = []
        self.input_model = AnyArguments
        self.calls = 0

    async def run(self, arguments: BaseModel, ctx: ToolContext) -> str:
        self.calls += 1
        return "found it"


class ClosingSteps(ScriptedSteps):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.provider_closed = False

    async def aclose(self) -> None:
        self.provider_closed = True


CHILD = AgentConfig(
    id="researcher",
    name="Researcher",
    description="Looks things up.",
    tags=["research"],
    model_tags=["chat"],
    tools=["search"],
)


def model_cfg() -> ModelConfig:
    return ModelConfig(
        id="m1", name="M1", provider="p", model="pm", tags=["chat"], priority=1
    )


def registry() -> tuple[ToolRegistry, StubTool, StubTool]:
    allowed, other = StubTool("search"), StubTool("other")
    tools = ToolRegistry()
    tools.register(allowed)
    tools.register(other)
    return tools, allowed, other


def make(
    scripts: list[list[StreamEvent]], **kwargs: object
) -> tuple[SubagentRun, ClosingSteps]:
    provider = ClosingSteps(scripts, hang_after=kwargs.pop("hang_after", None))  # type: ignore[arg-type]
    tools = kwargs.pop("tools", None) or registry()[0]
    run = SubagentRun(CHILD, PARENT_ENV, BRIEF, provider, model_cfg(), tools, **kwargs)  # type: ignore[arg-type]
    return run, provider


async def drain(run: SubagentRun) -> list[StreamEvent]:
    return [event async for event in run.events()]


def observation(request: ChatRequest) -> str:
    return request.messages[1].content.split("## Observation\n\n", 1)[1].strip()


async def test_respond_gives_responded_with_text_and_finding_lines() -> None:
    run, _ = make([reply_events(RESPOND, FINDING)])
    await drain(run)
    result = run.result
    assert (result.outcome, result.text) == ("responded", "Paris")
    assert result_observation("researcher", result) == (
        "Paris\n- Paris is the capital (source: atlas)"
    )


async def test_responded_without_findings_is_just_the_text() -> None:
    run, _ = make([reply_events(RESPOND)])
    await drain(run)
    assert result_observation("researcher", run.result) == "Paris"


async def test_ask_user_ends_the_run_as_asked_with_needs_observation() -> None:
    run, provider = make([reply_events(ASK)])
    await drain(run)
    assert run.result.outcome == "asked"
    assert result_observation("researcher", run.result) == "needs: Which France?"
    assert len(provider.requests) == 1


async def test_a_child_delegate_is_refused_and_the_child_keeps_running() -> None:
    run, provider = make([reply_events(DELEGATE), reply_events(RESPOND)])
    await drain(run)
    assert observation(provider.requests[1]) == (
        "delegate is not available to subagents"
    )
    assert (run.result.outcome, run.result.text) == ("responded", "Paris")
    assert len(provider.requests) == 2  # no grandchild shares this provider


async def test_a_child_that_never_responds_stops_after_five_calls() -> None:
    step = reply_events({"type": "call_tool", "tool": "search", "arguments": {}})
    run, provider = make([step] * 5 + [reply_events(RESPOND)] * 3, tools=registry()[0])
    await drain(run)
    assert len(provider.requests) == 5
    assert run.result.outcome == "step_limit"
    assert run.result.state.step == 5


async def test_step_limit_observation_names_the_agent_and_keeps_findings() -> None:
    step = reply_events(
        {"type": "call_tool", "tool": "search", "arguments": {}}, FINDING
    )
    run, _ = make(
        [step]
        + [reply_events({"type": "call_tool", "tool": "search", "arguments": {}})] * 4
    )
    await drain(run)
    text = result_observation("researcher", run.result)
    assert text.startswith("subagent researcher reached its step limit of 5")
    assert "- Paris is the capital (source: atlas)" in text


async def test_a_provider_error_is_an_error_result_not_an_exception() -> None:
    run, _ = make([[ErrorEvent(kind="unknown", message="boom Bearer abc123token")]])
    events = await drain(run)
    assert run.result.outcome == "error"
    assert events == []  # the error becomes the result, not a forwarded event
    text = result_observation("researcher", run.result)
    assert text.startswith("subagent researcher failed: boom")
    assert "abc123token" not in text


async def test_an_unexpected_exception_is_an_error_result() -> None:
    class Broken(ClosingSteps):
        async def stream_chat(self, req: ChatRequest):  # type: ignore[override]
            raise RuntimeError("kaput")
            yield  # pragma: no cover

    provider = Broken([])
    run = SubagentRun(CHILD, PARENT_ENV, BRIEF, provider, model_cfg(), registry()[0])
    await drain(run)
    assert run.result.outcome == "error"
    assert "RuntimeError: kaput" in run.result.text
    assert provider.provider_closed


async def test_a_tool_outside_the_agents_set_is_refused_and_not_listed() -> None:
    tools, allowed, other = registry()
    refused = {"type": "call_tool", "tool": "other", "arguments": {}}
    run, provider = make([reply_events(refused), reply_events(RESPOND)], tools=tools)
    await drain(run)
    assert other.calls == 0 and allowed.calls == 0
    assert observation(provider.requests[1]) == (
        "tool other is not available; available tools: search"
    )
    for request in provider.requests:
        user = request.messages[1].content
        assert "## Tools\n\nsearch: Stub tool search.\n\n## Observation" in user
        assert "other:" not in user


async def test_the_child_state_starts_with_the_brief_as_goal() -> None:
    run, provider = make([reply_events(RESPOND)])
    await drain(run)
    assert run.result.state.goal == BRIEF
    assert [(c.old, c.new) for c in run.result.state.goal_history] == [("", BRIEF)]
    assert f'"goal":"{BRIEF}"' in provider.requests[0].messages[1].content
    assert run.result.outcome == "responded"  # not rejected by the empty-goal rule
    assert len(provider.requests) == 1


async def test_the_child_environment_equals_the_parents() -> None:
    run, provider = make([reply_events(RESPOND)])
    await drain(run)
    assert run.result.state.environment == PARENT_ENV
    assert '"user_id":"u-1"' in provider.requests[0].messages[1].content


async def test_the_child_uses_the_subagent_prompt() -> None:
    run, provider = make([reply_events(RESPOND)])
    await drain(run)
    system = provider.requests[0].messages[0].content
    assert "researcher" in system and "Looks things up." in system


async def test_display_events_are_forwarded_and_done_and_text_are_dropped() -> None:
    report = UsageReport(
        model_id="m1",
        usage=None,
        cost_usd=None,
        seconds=0.1,
        session_cost_usd=0.0,
        session_cost_incomplete=False,
    )
    notice = Notice(text="careful")
    thinking = ThinkingDelta(text="hm")
    switch = ModelSwitch(from_model_id="a", reason="backoff", to_model_id="b")
    run, _ = make([reply_events(RESPOND, None, thinking, notice, switch, report)])
    events = await drain(run)
    assert events == [thinking, notice, switch, report]
    assert not any(isinstance(e, (Done, TextDelta)) for e in events)


async def test_cancellation_propagates_and_closes_the_stream() -> None:
    run, provider = make([reply_events(RESPOND)], hang_after=0)
    task = asyncio.create_task(drain(run))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed
    assert provider.provider_closed
    with pytest.raises(RuntimeError, match="has not finished"):
        run.result  # noqa: B018


async def test_streams_and_provider_are_closed_after_a_normal_run() -> None:
    run, provider = make([reply_events(RESPOND)])
    await drain(run)
    assert provider.closed and provider.provider_closed


def test_error_observation_is_capped_by_redact() -> None:
    result = SubagentResult(
        outcome="error",
        text="x" * 1000,
        findings=(),
        state=OrchestratorState(environment=PARENT_ENV),
        spans=(),
    )
    assert len(result_observation("researcher", result)) < 400


# --- make_subagent_factory ---------------------------------------------------


def factory_config() -> HarnessConfig:
    def model(model_id: str, priority: float, *tags: str) -> dict[str, object]:
        return {
            "id": model_id,
            "name": model_id,
            "provider": "p",
            "model": f"{model_id}-remote",
            "tags": list(tags),
            "priority": priority,
            "price": {"input_per_mtok": 1, "output_per_mtok": 0},
        }

    return HarnessConfig.model_validate(
        {
            "providers": {"p": {"type": "ollama", "base_url": "http://p.test"}},
            "models": [
                model("plain", 9, "chat"),
                model("low", 1, "chat", "research"),
                model("high", 5, "chat", "research"),
            ],
        }
    )


def researcher(*model_tags: str) -> AgentConfig:
    return CHILD.model_copy(update={"model_tags": list(model_tags)})


def million_tokens_reply() -> list[StreamEvent]:
    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    return [TextDelta(text="x"), UsageEvent(usage=usage), Done()]


class SharedProvider:
    """Serves every router of the test; records the model id of each request."""

    def __init__(self) -> None:
        self.models: list[str] = []

    async def stream_chat(self, req: ChatRequest):
        self.models.append(req.model)
        for event in million_tokens_reply():
            yield event

    async def aclose(self) -> None:
        return None


async def run_router(router: object) -> list[StreamEvent]:
    request = ChatRequest(model="ignored", messages=[], think=None)
    return [event async for event in router.stream_chat(request)]  # type: ignore[attr-defined]


def test_the_factory_picks_the_top_priority_model_with_all_model_tags() -> None:
    make = make_subagent_factory(
        factory_config(), lambda _p: SharedProvider(), None, None, SessionCost()
    )
    _, first = make(researcher("chat", "research"))
    assert first.id == "high"  # "plain" has the top priority but lacks "research"


async def test_the_child_router_chain_is_built_from_the_agents_model_tags() -> None:
    provider = SharedProvider()
    make = make_subagent_factory(
        factory_config(), lambda _p: provider, None, None, SessionCost()
    )
    router, _ = make(researcher("chat", "research"))
    await run_router(router)
    assert provider.models == ["high-remote"]
    # The chain is `high`, then the other research models; "plain" never joins it.
    # Prove it by making the first call fail.

    class Failing(SharedProvider):
        async def stream_chat(self, req: ChatRequest):
            self.models.append(req.model)
            if len(self.models) == 1:
                yield ErrorEvent(kind="connection", message="down")
                return
            async for event in super().stream_chat(req):
                yield event

    failing = Failing()
    make = make_subagent_factory(
        factory_config(), lambda _p: failing, None, None, SessionCost()
    )
    router, _ = make(researcher("chat", "research"))
    await run_router(router)
    assert failing.models[:2] == ["high-remote", "low-remote"]


def test_the_factory_returns_a_new_router_on_every_call() -> None:
    make = make_subagent_factory(
        factory_config(), lambda _p: SharedProvider(), None, None, SessionCost()
    )
    first, _ = make(researcher("chat"))
    second, _ = make(researcher("chat"))
    assert first is not second


async def test_routers_from_the_factory_feed_one_session_cost() -> None:
    costs = SessionCost()
    make = make_subagent_factory(
        factory_config(), lambda _p: SharedProvider(), None, None, costs
    )
    totals = []
    for _ in range(2):
        router, _ = make(researcher("chat"))
        events = await run_router(router)
        totals.append(next(e for e in events if isinstance(e, UsageReport)))
    assert [t.session_cost_usd for t in totals] == [1.0, 2.0]
    assert costs.total == 2.0


def test_the_factory_raises_config_error_when_no_model_has_the_tags() -> None:
    make = make_subagent_factory(
        factory_config(), lambda _p: SharedProvider(), None, None, SessionCost()
    )
    with pytest.raises(ConfigError, match="researcher"):
        make(researcher("chat", "nonexistent"))


# --- make_subagent_factory_maker ---------------------------------------------


def session_parent(config: HarnessConfig) -> ModelRouter:
    """A parent router as a UI builds it: no shared cost holder, its own total."""
    router = make_router_factory(config, lambda _p: SharedProvider(), None, None)(
        config.models[0], False
    )
    assert isinstance(router, ModelRouter)
    return router


async def total_of(router: object) -> float:
    events = await run_router(router)
    return next(e for e in events if isinstance(e, UsageReport)).session_cost_usd


async def test_a_childs_usage_adds_to_its_parents_session_total() -> None:
    config = factory_config()
    maker = make_subagent_factory_maker(config, lambda _p: SharedProvider(), None, None)
    parent = session_parent(config)
    make = maker(parent)
    child_a, _ = make(researcher("chat"))
    child_b, _ = make(researcher("chat"))
    totals = [
        await total_of(parent),
        await total_of(child_a),
        await total_of(parent),
        await total_of(child_b),
    ]
    assert totals == [1.0, 2.0, 3.0, 4.0]  # never decreasing, one running total


async def test_the_next_sessions_children_start_at_zero_again() -> None:
    config = factory_config()
    maker = make_subagent_factory_maker(config, lambda _p: SharedProvider(), None, None)
    first, second = session_parent(config), session_parent(config)
    first_child, _ = maker(first)(researcher("chat"))
    second_child, _ = maker(second)(researcher("chat"))
    assert await total_of(first) == 1.0
    assert await total_of(first_child) == 2.0
    # The second session shares nothing with the first one.
    assert await total_of(second_child) == 1.0
    assert await total_of(second) == 2.0
    assert await total_of(first) == 3.0


def test_the_maker_refuses_a_parent_that_is_not_a_model_router() -> None:
    maker = make_subagent_factory_maker(
        factory_config(), lambda _p: SharedProvider(), None, None
    )
    with pytest.raises(TypeError, match="ModelRouter"):
        maker(SharedProvider())  # type: ignore[arg-type]
