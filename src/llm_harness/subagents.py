"""One child agent run: a second Orchestrator with its own state, tools and cap.

The parent's `delegate` branch (and the wiring that builds the child's provider)
call this module; it knows nothing about the parent's loop. `SubagentRun.events()`
forwards what the user should see and `result` says how the child ended.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal

from llm_harness.config import (
    AgentConfig,
    HarnessConfig,
    ModelConfig,
    select_agent_model,
)
from llm_harness.context import subagent_instructions
from llm_harness.orchestrator import Orchestrator
from llm_harness.providers import ProviderFactory
from llm_harness.providers.base import ChatProvider
from llm_harness.redact import redact
from llm_harness.router import ModelRouter, SessionCost, make_router_factory
from llm_harness.state import Environment, Finding, OrchestratorState
from llm_harness.storage import Storage
from llm_harness.store import Store
from llm_harness.tools import ToolRegistry
from llm_harness.trace import Span
from llm_harness.types import (
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    UsageEvent,
    UsageReport,
)

SUBAGENT_MAX_STEPS = 5
SUBAGENT_MAX_RETRIES = 2

Outcome = Literal["responded", "asked", "step_limit", "error"]

# What the user sees from the child; its Done and its JSON/answer TextDelta are the
# parent's business, not the user's.
_FORWARDED = (ThinkingDelta, Notice, ModelSwitch, UsageEvent, UsageReport)


@dataclass(frozen=True)
class SubagentResult:
    outcome: Outcome
    text: str
    findings: tuple[Finding, ...]
    state: OrchestratorState
    spans: tuple[Span, ...]


def result_observation(agent_id: str, result: SubagentResult) -> str:
    """The one observation the parent receives for a finished child."""
    finding_lines = [f"- {f.text} (source: {f.source})" for f in result.findings]
    if result.outcome == "asked":
        return f"needs: {result.text}"
    if result.outcome == "responded":
        return "\n".join([result.text, *finding_lines])
    if result.outcome == "step_limit":
        head = (
            f"subagent {agent_id} reached its step limit of {SUBAGENT_MAX_STEPS} "
            f"without an answer"
        )
        if not finding_lines:
            return head
        return "\n".join([f"{head}; findings so far:", *finding_lines])
    return f"subagent {agent_id} failed: {redact(result.text)}"


class SubagentRun:
    """Runs one child to its end. Iterate `events()` fully, then read `result`."""

    def __init__(
        self,
        child: AgentConfig,
        environment: Environment,
        brief: str,
        provider: ChatProvider,
        model_cfg: ModelConfig,
        tools: ToolRegistry,
        *,
        think: bool | None = None,
        storage: Storage | None = None,
    ) -> None:
        self._child = child
        self._brief = brief
        self._provider = provider
        # The child's environment must equal the parent's: send() refreshes it from
        # these callables every turn, so they return the parent's values.
        self._orchestrator = Orchestrator(
            provider,
            model_cfg,
            think=think,
            clock=lambda: environment.now,
            identity=lambda: (environment.user_name, environment.computer_name),
            user_id=environment.user_id,
            max_steps=SUBAGENT_MAX_STEPS,
            max_retries=SUBAGENT_MAX_RETRIES,
            tools=tools,
            storage=storage,
            agent=child,
            instructions=subagent_instructions(child.id, child.name, child.description),
            can_delegate=False,
            goal=brief,
        )
        self._result: SubagentResult | None = None

    @property
    def result(self) -> SubagentResult:
        if self._result is None:
            raise RuntimeError("the subagent run has not finished")
        return self._result

    async def events(self) -> AsyncIterator[StreamEvent]:
        """Forward the child's display events; set `result` when it ends.

        A child failure never raises here; cancellation does propagate.
        """
        outcome: Outcome = "error"
        text = "the subagent ended without a result"
        try:
            async with aclosing(self._orchestrator.send(self._brief)) as stream:
                async for event in stream:
                    if isinstance(event, _FORWARDED):
                        yield event
                    elif isinstance(event, ErrorEvent):
                        capped = self._orchestrator.state.step >= SUBAGENT_MAX_STEPS
                        outcome = "step_limit" if capped else "error"
                        text = event.message
                    elif isinstance(event, TextDelta):
                        # The respond text or the ask_user question: it arrives
                        # right before Done and is the child's answer.
                        text = event.text
                    elif isinstance(event, Done):
                        asked = self._orchestrator.pending_question is not None
                        outcome = "asked" if asked else "responded"
        except Exception as error:  # noqa: BLE001 - becomes a result; Cancelled passes
            outcome = "error"
            text = f"{type(error).__name__}: {error}"
        finally:
            await self._provider.aclose()
        state = self._orchestrator.state
        self._result = SubagentResult(
            outcome=outcome,
            text=text,
            findings=tuple(state.findings),
            state=state,
            spans=self._orchestrator.last_trace or (),
        )


SubagentFactory = Callable[[AgentConfig], tuple[ChatProvider, ModelConfig]]


def make_subagent_factory(
    config: HarnessConfig,
    provider_factory: ProviderFactory,
    think_flag: bool | None,
    store: Store | None,
    costs: SessionCost,
) -> SubagentFactory:
    """Builds the child's provider and first model.

    `SubagentRun` closes the provider it is given, so every call returns a new
    router; all of them share `store` and `costs` with the parent's router.
    """

    def make(agent: AgentConfig) -> tuple[ChatProvider, ModelConfig]:
        first = select_agent_model(config, agent)  # ConfigError: reported to the parent
        router_factory = make_router_factory(
            config,
            provider_factory,
            think_flag,
            store,
            required_tags=frozenset(agent.model_tags),
            costs=costs,
        )
        return router_factory(first, False), first

    return make


# Maps a session's router (the parent's) to the factory of that session's children.
SubagentFactoryMaker = Callable[[ChatProvider], SubagentFactory]


def make_subagent_factory_maker(
    config: HarnessConfig,
    provider_factory: ProviderFactory,
    think_flag: bool | None,
    store: Store | None,
) -> SubagentFactoryMaker:
    """Builds, per session, the subagent factory bound to that session's router.

    The child routers feed the parent router's own `SessionCost`, so one session
    shows one total, and the next session (a new chat, another GUI client)
    starts again at zero.
    """

    def for_session(parent: ChatProvider) -> SubagentFactory:
        if not isinstance(parent, ModelRouter):
            raise TypeError(
                f"subagents need a ModelRouter as the parent, got {type(parent).__name__}"
            )
        return make_subagent_factory(
            config, provider_factory, think_flag, store, parent.costs
        )

    return for_session
