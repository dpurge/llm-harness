"""The chat logic every front end shares: sessions, turns and `/memory` answers.

No framework is imported here. A front end draws through a `TurnView` and keeps
what depends on its toolkit: widgets, key bindings, the approval prompt and the
scheduling of a turn (so that cancelling it works the way the toolkit does it).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from llm_harness.config.settings import (
    ConfigError,
    HarnessConfig,
    ModelConfig,
    find_orchestrator_agent,
)
from llm_harness.core.approval import Approver
from llm_harness.core.display import format_switch, format_usage_report, sanitize_text
from llm_harness.core.orchestrator import Orchestrator
from llm_harness.core.redact import redact
from llm_harness.core.subagents import SubagentFactoryMaker
from llm_harness.core.types import (
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    StatusEvent,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    UiSurface,
    UsageReport,
)
from llm_harness.memory.add import PendingProposals, ProposeResult
from llm_harness.memory.command import NO_MODEL, handle_memory_text
from llm_harness.models.providers.base import ChatProvider
from llm_harness.models.router import RouterFactory
from llm_harness.storage import Storage
from llm_harness.tools.registry import ToolRegistry

_APPROVE_ANSWERS = frozenset({"y", "yes"})


def unexpected_error_line(exc: Exception) -> str:
    """One safe line for an unexpected exception: never a traceback or locals."""
    return (
        f"error (unexpected): {type(exc).__name__}: {redact(sanitize_text(str(exc)))}"
    )


def error_event_line(event: ErrorEvent) -> str:
    return f"error ({event.kind}): {sanitize_text(event.message)}"


def status_text(event: StatusEvent) -> str:
    return f"step {event.step}: {sanitize_text(event.text)}"


def is_approval_yes(text: str) -> bool:
    """Only y or yes approves; anything else denies (fail closed)."""
    return text.strip().lower() in _APPROVE_ANSWERS


def build_orchestrator(
    config: HarnessConfig,
    router: ChatProvider,
    model: ModelConfig,
    *,
    storage: Storage | None,
    tools: ToolRegistry | None,
    approver: Approver | None,
    subagent_factory_maker: SubagentFactoryMaker | None,
) -> Orchestrator:
    return Orchestrator(
        router,
        model,
        user_id=config.user_id,
        storage=storage,
        agent=find_orchestrator_agent(config),
        tools=tools,
        approver=approver,
        subagents=config.agents,
        subagent_factory=(
            subagent_factory_maker(router)
            if subagent_factory_maker is not None
            else None
        ),
    )


class MemoryCommands:
    """The `/memory` commands of one chat: the pending preview and the answers."""

    def __init__(
        self,
        tools: ToolRegistry | None,
        storage: Storage | None,
        config: HarnessConfig,
        *,
        no_model: str = NO_MODEL,
    ) -> None:
        self._tools = tools
        self._storage = storage
        self._config = config
        self._no_model = no_model
        # The `/memory add` preview that waits for `confirm`; this chat only.
        self.pending = PendingProposals()

    async def answer(
        self,
        text: str,
        orchestrator: Orchestrator | None = None,
        announce: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None:
        """None when `text` is not a `/memory` command; else the answer to show."""
        return await handle_memory_text(
            text,
            self._tools,
            self._storage,
            self._config.user_id,
            memory_admins=self._config.tools.memory_admins,
            propose=(
                None if orchestrator is None else self.proposer(orchestrator, announce)
            ),
            pending=self.pending,
            no_model=self._no_model,
        )

    def proposer(
        self,
        orchestrator: Orchestrator,
        announce: Callable[[], Awaitable[None]] | None = None,
    ) -> Callable[[str, bool], Awaitable[ProposeResult]]:
        async def propose(paste: str, shared: bool) -> ProposeResult:
            if announce is not None:
                await announce()
            return await orchestrator.propose_memories(paste, shared)

        return propose


class TurnView(Protocol):
    """What a front end draws for a turn; every method is called from the turn."""

    async def thinking(self, text: str, is_first: bool) -> None: ...

    async def reply(self, text: str, is_first: bool) -> None: ...

    async def note(self, text: str) -> None: ...

    async def status(self, text: str) -> None: ...

    async def surface(self, event: UiSurface) -> None: ...

    async def usage(self, text: str) -> None: ...

    async def error(self, text: str) -> None: ...

    def cancelled(self) -> None: ...

    def end_turn(self, restore_text: str | None) -> None: ...

    def refresh_inspector(self) -> None: ...

    async def announce_proposing(self) -> None: ...

    async def memory_answer(self, text: str) -> None: ...

    def session_reset(self) -> None: ...


class ChatSession:
    """One chat: the router, the orchestrator, the memory commands and the turn."""

    def __init__(
        self,
        config: HarnessConfig,
        router_factory: RouterFactory,
        model: ModelConfig,
        is_manual: bool,
        *,
        storage: Storage | None,
        tools: ToolRegistry | None,
        subagent_factory_maker: SubagentFactoryMaker | None,
        approver: Approver | None,
        view: TurnView,
        no_model: str,
    ) -> None:
        self._config = config
        self._router_factory = router_factory
        self._storage = storage
        self._tools = tools
        self._subagent_factory_maker = subagent_factory_maker
        self._approver = approver
        self._view = view
        self._model = model
        # Whether the current model was picked by the user (--model or selector).
        self._is_manual = is_manual
        self._router: ChatProvider | None = None
        self._orchestrator: Orchestrator | None = None
        self._memory = MemoryCommands(tools, storage, config, no_model=no_model)

    @property
    def orchestrator(self) -> Orchestrator | None:
        return self._orchestrator

    @property
    def router(self) -> ChatProvider | None:
        return self._router

    @property
    def model(self) -> ModelConfig:
        return self._model

    @property
    def is_manual(self) -> bool:
        return self._is_manual

    @property
    def memory(self) -> MemoryCommands:
        return self._memory

    async def start(self, model: ModelConfig, is_manual: bool) -> bool:
        """Swap in a session for `model`; on any error keep the current one."""
        try:
            router = self._router_factory(model, is_manual)
        except ConfigError as exc:
            await self._view.error(f"error: {sanitize_text(str(exc))}")
            return False
        except Exception as exc:  # noqa: BLE001 - shown as one safe line, never a traceback
            await self._view.error(unexpected_error_line(exc))
            return False
        old_router = self._router
        self._router = router
        self._model = model
        self._is_manual = is_manual
        # A new chat or another model starts without a preview.
        self._memory.pending.clear()
        self._view.session_reset()
        self._orchestrator = build_orchestrator(
            self._config,
            router,
            model,
            storage=self._storage,
            tools=self._tools,
            approver=self._approver,
            subagent_factory_maker=self._subagent_factory_maker,
        )
        if old_router is not None:
            await old_router.aclose()
        self._view.refresh_inspector()
        return True

    def is_selector_unlocked(self) -> bool:
        # A failed or cancelled first turn leaves the selector free: only a
        # completed turn locks the model for this chat.
        return self._orchestrator is None or not self._orchestrator.has_completed_turn

    def model_by_id(self, model_id: str) -> ModelConfig:
        return next(m for m in self._config.models if m.id == model_id)

    def new_chat_is_manual(self, selected: ModelConfig) -> bool:
        # A changed selector is a pick by the user; otherwise keep the old flag.
        return self._is_manual or selected.id != self._model.id

    async def run_turn(
        self,
        text: str,
        events: Callable[[], AsyncIterator[StreamEvent]] | None = None,
    ) -> None:
        """Stream one reply into the view; the turn always ends in `end_turn`.

        `events` replaces `orchestrator.send(text)` (a click on a drawn surface);
        such a turn has no typed text to give back, so it restores nothing.
        """
        orchestrator = self._orchestrator
        if orchestrator is None:
            raise RuntimeError("run_turn needs a started session")
        view = self._view
        report: UsageReport | None = None
        thinking_text = ""
        reply_text = ""
        is_first_thinking = True
        is_first_reply = True
        restore_text: str | None = None
        retry_text = text if events is None else None
        try:
            async for event in (events or (lambda: orchestrator.send(text)))():
                if isinstance(event, ThinkingDelta):
                    thinking_text += sanitize_text(event.text)
                    await view.thinking(thinking_text, is_first_thinking)
                    is_first_thinking = False
                elif isinstance(event, TextDelta):
                    reply_text += sanitize_text(event.text)
                    await view.reply(reply_text, is_first_reply)
                    is_first_reply = False
                elif isinstance(event, ModelSwitch):
                    await view.note(format_switch(event))
                elif isinstance(event, StatusEvent):
                    view.refresh_inspector()
                    await view.status(status_text(event))
                elif isinstance(event, Notice):
                    await view.note(sanitize_text(event.text))
                elif isinstance(event, UiSurface):
                    await view.surface(event)
                elif isinstance(event, UsageReport):
                    report = event
                elif isinstance(event, Done):
                    if report is not None:
                        await view.usage(format_usage_report(report))
                elif isinstance(event, ErrorEvent):
                    await view.error(error_event_line(event))
                    restore_text = retry_text
        except asyncio.CancelledError:
            # Only synchronous work here: the task is already being cancelled.
            view.cancelled()
            restore_text = retry_text
            raise
        except Exception as exc:  # noqa: BLE001 - shown as one safe line, never a traceback
            # Unhandled, the worker would crash the app and Textual's crash
            # report prints frame locals (prompt text, history, the bearer token).
            await view.error(unexpected_error_line(exc))
            restore_text = retry_text
        finally:
            view.end_turn(restore_text)

    async def run_memory_add(
        self, text: str, orchestrator: Orchestrator | None
    ) -> None:
        """`/memory add` calls the model, so it runs like a turn and can be cancelled."""
        view = self._view
        restore_text: str | None = None
        try:
            answer = await self._memory.answer(
                text, orchestrator, view.announce_proposing
            )
            if answer is not None:
                await view.memory_answer(answer)
        except asyncio.CancelledError:
            view.cancelled()
            restore_text = text
            raise
        except Exception as exc:  # noqa: BLE001 - shown as one safe line, never a traceback
            await view.error(unexpected_error_line(exc))
            restore_text = text
        finally:
            view.end_turn(restore_text)

    async def aclose(self) -> None:
        """Release what the chat holds; the storage belongs to the caller."""
        self._memory.pending.clear()
        if self._router is not None:
            await self._router.aclose()
            self._router = None
