"""Orchestrator: drives the step loop over a typed state, never a chat history.

Each step sends one bounded request (instructions, state, observation), parses
the JSON reply, commits its patch and runs its action. State is replaced only by
a whole valid step, so a rejected reply, an error or a cancellation leaves what
earlier valid steps committed and nothing else.
"""

import getpass
import socket
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from llm_harness.config import ORCHESTRATOR_TAG, AgentConfig, ModelConfig
from llm_harness.context import (
    answer_observation,
    build_request,
    delegation_targets,
    retry_observation,
)
from llm_harness.providers.base import ChatProvider
from llm_harness.redact import redact
from llm_harness.state import (
    DEFAULT_MAX_CHARS,
    AskUserAction,
    CallToolAction,
    DelegateAction,
    Environment,
    GoalChange,
    OrchestratorState,
    PatchError,
    Reply,
    ReplyError,
    RespondAction,
    RunState,
    apply_patch,
    parse_reply,
)
from llm_harness.storage import Storage
from llm_harness.tools import Tool, ToolContext, ToolRegistry
from llm_harness.trace import Span, SpanHandle, TraceRecorder
from llm_harness.types import (
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    StatusEvent,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    UsageEvent,
    UsageReport,
)

_NO_TERMINAL = "the provider ended the stream without a terminal event"
_EMPTY_GOAL = (
    "goal is empty: set `goal` in state_patch, "
    "or use ask_user if you cannot tell what the user wants."
)
_NO_DELEGATION = "delegate is not available to subagents"
_STATUS_CHARS = 80

# Shown to the user as they arrive; the model's TextDelta is JSON and is never shown.
_FORWARDED = (ThinkingDelta, Notice, ModelSwitch, UsageEvent, UsageReport)


def _default_clock() -> datetime:
    return datetime.now().astimezone()


def _default_identity() -> tuple[str, str]:
    return getpass.getuser(), socket.gethostname()


@dataclass
class _Delegation:
    """The observation a delegation produced, filled in while its events are forwarded."""

    observation: str = ""


@dataclass
class _Attempt:
    """What one provider stream produced, filled in while its events are forwarded."""

    text: str = ""
    done: bool = False
    failed: bool = False


def _note_in_span(call: SpanHandle, event: StreamEvent) -> None:
    """Copy the display events that belong in the `llm_call` span."""
    if isinstance(event, ThinkingDelta):
        call.attributes["thinking"] = call.attributes.get("thinking", "") + event.text
    elif isinstance(event, UsageEvent):
        call.attributes["usage"] = event.usage.model_dump()
    elif isinstance(event, UsageReport):
        call.attributes["reported_model"] = event.model_id
        # The router emits this instead of UsageEvent; a None usage must not
        # clobber tokens an adapter's UsageEvent already stored.
        if event.usage is not None:
            call.attributes["usage"] = event.usage.model_dump()
        call.attributes["cost_usd"] = event.cost_usd
        call.attributes["seconds"] = event.seconds


def _status_text(reply: Reply) -> str:
    action = reply.action
    if isinstance(action, RespondAction):
        text = "answering"
    elif isinstance(action, AskUserAction):
        text = "asking the user"
    elif isinstance(action, CallToolAction):
        text = f"calling tool {action.tool}"
    else:
        text = f"delegating to {action.subagent}"
    return text[:_STATUS_CHARS]


def _argument_problems(error: ValidationError) -> str:
    # Only location and message: pydantic's str() echoes the offending input.
    return "; ".join(
        f"{'.'.join(str(item) for item in problem['loc']) or '(arguments)'}: "
        f"{problem['msg']}"
        for problem in error.errors(include_input=False, include_url=False)
    )


class Orchestrator:
    def __init__(
        self,
        provider: ChatProvider,
        model_cfg: ModelConfig,
        *,
        think: bool | None = None,
        clock: Callable[[], datetime] = _default_clock,
        identity: Callable[[], tuple[str, str]] = _default_identity,
        user_id: str | None = None,
        max_steps: int = 8,
        max_retries: int = 2,
        max_state_chars: int = DEFAULT_MAX_CHARS,
        tools: ToolRegistry | None = None,
        recorder: TraceRecorder | None = None,
        storage: Storage | None = None,
        agent: AgentConfig | None = None,
        instructions: str | None = None,
        can_delegate: bool = True,
        goal: str = "",
        subagents: Sequence[AgentConfig] = (),
        subagent_factory: Callable[[AgentConfig], tuple[ChatProvider, ModelConfig]]
        | None = None,
    ) -> None:
        self._subagents = tuple(subagents)
        self._subagent_factory = subagent_factory
        self._storage = storage
        # Without an agent (tests) the orchestrator keeps its built-in identity.
        self._agent = agent
        self._agent_id = agent.id if agent else ORCHESTRATOR_TAG
        self._agent_tags = frozenset(agent.tags if agent else {ORCHESTRATOR_TAG})
        self._tools = tools if tools is not None else ToolRegistry()
        self._recorder = recorder if recorder is not None else TraceRecorder()
        self._provider = provider
        self._model_cfg = model_cfg
        self._think = think
        self._clock = clock
        self._identity = identity
        self._user_id = user_id
        self._max_steps = max_steps
        self._max_retries = max_retries
        self._max_state_chars = max_state_chars
        self._instructions = instructions
        self._can_delegate = can_delegate
        self._state = self._initial_state(goal)
        self._busy = False
        self._has_completed_turn = False
        # The last ask_user question; part of snapshot() so a run can resume from it.
        self.pending_question: str | None = None

    @property
    def state(self) -> OrchestratorState:
        return self._state

    @property
    def subagents(self) -> tuple[AgentConfig, ...]:
        """The agents this orchestrator may delegate to (read-only, for the preview)."""
        return self._subagents

    @property
    def has_completed_turn(self) -> bool:
        """True once a turn ended with Done; failed or cancelled turns do not count.

        Front ends lock the model selector on it, so a failed first turn leaves
        the user free to pick another model.
        """
        return self._has_completed_turn

    @property
    def last_trace(self) -> tuple[Span, ...] | None:
        """Closed spans of the latest turn in opening order; None before the first.

        Display data only: `build_request` never reads it.
        """
        return self._recorder.spans

    def _initial_state(self, goal: str) -> OrchestratorState:
        """A fresh state; a preset goal is recorded as the model's own would be.

        The first `send` is turn 1, so the history entry carries turn 1.
        """
        history = [GoalChange(turn=1, old="", new=goal)] if goal.strip() else []
        return OrchestratorState(
            goal=goal if goal.strip() else "",
            goal_history=history,
            environment=self._environment(),
        )

    def _environment(self) -> Environment:
        user_name, computer_name = self._identity()
        return Environment(
            now=self._clock(),
            user_name=user_name,
            computer_name=computer_name,
            user_id=self._user_id,
        )

    async def send(self, text: str) -> AsyncIterator[StreamEvent]:
        """Run one user turn: step loop until respond, ask_user, error or step cap."""
        if self._busy:
            raise RuntimeError("a turn is already running")
        self._busy = True
        try:
            # The harness writes the counters and the environment, never a patch.
            self._state = self._state.model_copy(
                update={
                    "turn": self._state.turn + 1,
                    "step": 0,
                    "environment": self._environment(),
                }
            )
            self._recorder.reset()
            # Every span closes in the `with` exit, so cancel, error, step cap and
            # an early close of this generator all keep the partial trace.
            with self._recorder.span("turn", f"turn {self._state.turn}") as turn:
                turn.attributes["turn"] = self._state.turn
                # pending_question stays set until a turn ends with Done, so a failed
                # answer turn can be retried with the question still known.
                observation = (
                    answer_observation(self.pending_question, text)
                    if self.pending_question
                    else text
                )
                for step_number in range(1, self._max_steps + 1):
                    with self._recorder.span(
                        "step", f"step {step_number}", turn
                    ) as step:
                        reply: Reply | None = None
                        last_error = ""
                        step_observation = observation
                        for _ in range(self._max_retries + 1):
                            attempt = _Attempt()
                            request = build_request(
                                self._state,
                                observation,
                                self._model_cfg,
                                self._think,
                                self.available_tools(),
                                instructions=self._instructions,
                                subagents=self._subagents,
                            )
                            with self._recorder.span(
                                "llm_call", request.model, step
                            ) as call:
                                call.attributes["model"] = request.model
                                call.attributes["messages"] = [
                                    message.model_dump() for message in request.messages
                                ]
                                # aclosing: closing send() must close the provider
                                # stream now, not whenever the inner generator is
                                # garbage collected.
                                async with aclosing(
                                    self._stream_attempt(request, attempt, call)
                                ) as events:
                                    async for event in events:
                                        yield event
                                call.attributes["reply"] = attempt.text
                                if attempt.failed:
                                    step.status = turn.status = "error"
                                    return
                                if not attempt.done:
                                    step.status = turn.status = call.status = "error"
                                    yield ErrorEvent(
                                        kind="unknown", message=_NO_TERMINAL
                                    )
                                    return
                                try:
                                    reply, new_state = self._accept(
                                        attempt.text, step_number
                                    )
                                except (ReplyError, PatchError) as error:
                                    last_error = str(error)
                                    call.status = "rejected"
                                    call.attributes["rejection"] = last_error
                                    # Keep the original observation: on turn 1 it is
                                    # the only place the user's request appears, so a
                                    # retry needs it.
                                    observation = retry_observation(
                                        step_observation, last_error
                                    )
                                    continue
                            break
                        if reply is None:
                            step.status = turn.status = "error"
                            yield ErrorEvent(
                                kind="unknown",
                                message=(
                                    f"the orchestrator reply was rejected "
                                    f"{self._max_retries + 1} times; last error: {redact(last_error)}"
                                ),
                            )
                            return

                        self._state = new_state
                        yield StatusEvent(
                            step=step_number,
                            action_type=reply.action.type,
                            text=_status_text(reply),
                        )
                        action = reply.action
                        if isinstance(action, RespondAction):
                            self.pending_question = None
                            turn.status = "ok"
                            yield TextDelta(text=action.text)
                            self._has_completed_turn = True
                            yield Done()
                            return
                        if isinstance(action, AskUserAction):
                            self.pending_question = action.question
                            turn.status = "ok"
                            yield TextDelta(text=action.question)
                            self._has_completed_turn = True
                            yield Done()
                            return
                        if isinstance(action, CallToolAction):
                            observation = await self._run_tool(action, step)
                        elif isinstance(action, DelegateAction):
                            if not self._can_delegate:
                                observation = _NO_DELEGATION
                            else:
                                delegation = _Delegation()
                                async with aclosing(
                                    self._delegate(action, step, delegation)
                                ) as child_events:
                                    async for event in child_events:
                                        yield event
                                observation = delegation.observation
                        else:
                            observation = f"{action.type} is not available yet"

                # Patches committed by earlier valid steps are kept on purpose: they
                # are real progress the next turn can build on.
                turn.status = "error"
                yield ErrorEvent(
                    kind="unknown",
                    message=(
                        f"the orchestrator reached the step cap of {self._max_steps} "
                        f"steps without a respond or ask_user action"
                    ),
                )
        finally:
            self._busy = False

    async def _delegate(
        self, action: DelegateAction, step: SpanHandle, delegation: _Delegation
    ) -> AsyncIterator[StreamEvent]:
        """Run one child agent; forward its display events, set the observation.

        Every child failure becomes an observation, never an exception here;
        cancellation propagates.
        """
        # Imported here: `subagents` imports this module for the child Orchestrator.
        from llm_harness.subagents import SubagentRun, result_observation

        if self._subagent_factory is None:
            delegation.observation = f"{action.type} is not available yet"
            return
        targets = {agent.id: agent for agent in delegation_targets(self._subagents)}
        child = targets.get(action.subagent)
        if child is None:
            available = ", ".join(sorted(targets)) or "none"
            delegation.observation = (
                f"unknown subagent {action.subagent}; available subagents: {available}"
            )
            return
        with self._recorder.span("subagent", child.id, step) as span:
            span.attributes["subagent"] = child.id
            try:
                provider, child_model_cfg = self._subagent_factory(child)
            except Exception as error:  # noqa: BLE001 - becomes an observation
                span.status = "error"
                delegation.observation = (
                    f"subagent {child.id} could not start: {redact(str(error))}"
                )
                return
            try:
                run = SubagentRun(
                    child,
                    self._state.environment,
                    action.brief,
                    provider,
                    child_model_cfg,
                    self._tools,
                    think=self._think,
                    storage=self._storage,
                )
            except BaseException:
                # SubagentRun closes the provider only once it runs.
                await provider.aclose()
                raise
            async with aclosing(run.events()) as events:
                async for event in events:
                    yield event
            result = run.result
            span.attributes["outcome"] = result.outcome
            span.attributes["state"] = result.state.model_dump(mode="json")
            self._recorder.adopt(result.spans, span, span.start_ms)
            delegation.observation = result_observation(child.id, result)

    async def _run_tool(self, action: CallToolAction, step: SpanHandle) -> str:
        """Run a tool; every failure becomes an observation, never an exception."""
        with self._recorder.span("tool_call", action.tool, step) as call:
            call.attributes["tool"] = action.tool
            call.attributes["arguments"] = action.arguments
            result = await self._call_tool(action, call)
            call.attributes["result"] = result
            return result

    def available_tools(self) -> list[Tool]:
        """The tools this agent may use, sorted by name."""
        # Without an agent (tests) every registered tool is allowed.
        if self._agent is None:
            return self._tools.all()
        return self._tools.tools_for(self._agent)

    def _allowed_tool_names(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self.available_tools())

    async def _call_tool(self, action: CallToolAction, call: SpanHandle) -> str:
        allowed = self._allowed_tool_names()
        tool = self._tools.get(action.tool) if action.tool in allowed else None
        if tool is None:
            call.status = "error"
            available = ", ".join(sorted(allowed)) or "none"
            return f"tool {action.tool} is not available; available tools: {available}"
        try:
            arguments = tool.input_model.model_validate(action.arguments)
        except ValidationError as error:
            call.status = "error"
            return (
                f"invalid arguments for tool {action.tool}: {_argument_problems(error)}"
            )
        ctx = ToolContext(
            storage=self._storage,
            user_id=self._user_id,
            agent_id=self._agent_id,
            tags=self._agent_tags,
            allowed_tools=allowed,
        )
        try:
            result = await tool.run(arguments, ctx)
        except Exception as error:  # noqa: BLE001 - becomes an observation; Cancelled passes
            call.status = "error"
            return (
                f"tool {action.tool} failed: "
                f"{type(error).__name__}: {redact(str(error))}"
            )
        return f"tool {action.tool} result: {result}"

    def snapshot(self) -> RunState:
        """The resumable run state; plain data, safe to serialise as JSON."""
        return RunState(state=self._state, pending_question=self.pending_question)

    def restore(self, run_state: RunState) -> None:
        if self._busy:
            raise RuntimeError("cannot restore while a turn is running")
        self._state = run_state.state
        self.pending_question = run_state.pending_question

    def _accept(self, text: str, step: int) -> tuple[Reply, OrchestratorState]:
        """Validate a reply against the current state; raise instead of committing."""
        reply = parse_reply(text)
        new_state = apply_patch(
            self._state, reply.state_patch, max_chars=self._max_state_chars
        )
        if isinstance(reply.action, RespondAction) and not new_state.goal.strip():
            raise ReplyError(_EMPTY_GOAL)
        return reply, new_state.model_copy(update={"step": step})

    async def _stream_attempt(
        self, request, attempt: _Attempt, call: SpanHandle
    ) -> AsyncIterator[StreamEvent]:
        """Forward display events; collect the JSON reply text into `attempt`.

        Thinking, usage and the answering model also go to the `llm_call` span.
        """
        chunks: list[str] = []
        async with aclosing(self._provider.stream_chat(request)) as stream:
            async for event in stream:
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                elif isinstance(event, Done):
                    attempt.text = "".join(chunks)
                    attempt.done = True
                    return
                elif isinstance(event, ErrorEvent):
                    attempt.failed = True
                    call.status = "error"
                    call.attributes["error"] = event.message
                    yield event
                    return
                elif isinstance(event, _FORWARDED):
                    _note_in_span(call, event)
                    yield event
