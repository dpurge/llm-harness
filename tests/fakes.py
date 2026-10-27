"""Test doubles shared by session and adapter tests."""

import asyncio
from collections.abc import AsyncIterator

from llm_harness.config import HarnessConfig, ModelConfig
from llm_harness.providers import ProviderFactory
from llm_harness.providers.base import ChatProvider
from llm_harness.router import RouterFactory, make_router_factory
from llm_harness.store import Store
from llm_harness.types import (
    ChatRequest,
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


class ScriptedProvider:
    """Yields scripted events; optionally hangs after `hang_after` of them."""

    def __init__(
        self, events: list[StreamEvent], *, hang_after: int | None = None
    ) -> None:
        self._events = events
        self._hang_after = hang_after
        self.requests: list[ChatRequest] = []
        self.closed = False

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(req.model_copy(deep=True))
        try:
            for index, event in enumerate(self._events):
                if index == self._hang_after:
                    await asyncio.Event().wait()
                yield event
            if self._hang_after == len(self._events):
                await asyncio.Event().wait()
        finally:
            self.closed = True

    async def aclose(self) -> None:
        return None


class RecordingRouterFactory:
    """A RouterFactory over make_router_factory that remembers (model id, is_manual)."""

    def __init__(self, inner: RouterFactory) -> None:
        self._inner = inner
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, model: ModelConfig, is_manual: bool) -> ChatProvider:
        self.calls.append((model.id, is_manual))
        return self._inner(model, is_manual)


def router_factory_for(
    config: HarnessConfig,
    provider_factory: ProviderFactory,
    think_flag: bool | None = None,
    store: Store | None = None,
) -> RecordingRouterFactory:
    """The router factory the front ends get, over a scripted provider factory."""
    return RecordingRouterFactory(
        make_router_factory(config, provider_factory, think_flag, store)
    )


def assert_stream_contract(events: list[StreamEvent]) -> None:
    """Assert the adapter contract, plus the router's extra events.

    Deltas, then at most one usage, then exactly one terminal event last.
    ModelSwitch and Notice come before the first delta; at most one UsageReport
    comes after every delta and just before the terminal event.
    """
    terminals = [i for i, e in enumerate(events) if isinstance(e, (Done, ErrorEvent))]
    assert len(terminals) == 1, f"expected exactly one terminal event, got {terminals}"
    assert terminals[0] == len(events) - 1, "terminal event must be last"
    body = events[:-1]
    deltas = [
        i for i, e in enumerate(body) if isinstance(e, (ThinkingDelta, TextDelta))
    ]
    notes = [i for i, e in enumerate(body) if isinstance(e, (ModelSwitch, Notice))]
    assert all(not deltas or n < deltas[0] for n in notes), (
        "ModelSwitch and Notice must come before the first delta"
    )
    usages = [i for i, e in enumerate(body) if isinstance(e, UsageEvent)]
    reports = [i for i, e in enumerate(body) if isinstance(e, UsageReport)]
    assert len(usages) <= 1, "at most one usage event"
    assert len(reports) <= 1, "at most one usage report"
    assert len(body) == len(deltas) + len(notes) + len(usages) + len(reports)
    after_deltas = usages + reports
    assert all(not deltas or i > deltas[-1] for i in after_deltas), (
        "usage must come after every delta"
    )
    if reports:
        assert reports[0] == len(body) - 1, "usage report must be just before terminal"
    if usages:
        assert usages[0] == len(body) - 1 - len(reports), "usage must come last"
