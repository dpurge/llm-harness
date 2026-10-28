"""ModelRouter: a ChatProvider that walks the fallback chain for each turn."""

import asyncio
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from llm_harness.config import (
    ConfigError,
    HarnessConfig,
    ModelConfig,
    resolve_think,
)
from llm_harness.providers import ProviderFactory
from llm_harness.providers.base import ChatProvider
from llm_harness.redact import redact
from llm_harness.routing import (
    FALLBACK_KINDS,
    BackoffEntry,
    compute_cost,
    fallback_chain,
    is_backing_off,
)
from llm_harness.store import BackoffAction, CallRecord, Store
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    StreamEvent,
    SwitchReason,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
    UsageReport,
)

# (model, is_manual) -> provider for that model's chat turns
RouterFactory = Callable[[ModelConfig, bool], ChatProvider]

_NO_TERMINAL = "the provider ended the stream without a terminal event"
_LAST_ERROR_LIMIT = 120
_FINAL_MESSAGE_LIMIT = 300


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Attempt:
    """What one candidate did in this turn."""

    model: ModelConfig
    started: datetime
    started_at: float  # timer reading
    fallback_from: str | None
    is_manual_pick: bool
    seconds: float = 0.0
    usage: Usage | None = None
    output_started: bool = False
    terminal: Done | ErrorEvent | None = None


class ModelRouter:
    """Sends each turn to the first model and falls back along the chain.

    The router ignores the `model` and `think` of the request it receives (the
    session sends the first model's values): for every candidate it sends that
    candidate's own provider model id and its own `thinking` (flag, then model
    setting). It emits ModelSwitch, UsageReport and Notice events, swallows the
    adapters' UsageEvent, and records every attempt in the store.
    """

    def __init__(
        self,
        config: HarnessConfig,
        first: ModelConfig,
        *,
        is_manual: bool,
        think_flag: bool | None,
        provider_factory: ProviderFactory,
        store: Store | None,
        required_tags: frozenset[str] = frozenset({"chat"}),
        clock: Callable[[], datetime] = _utc_now,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._is_manual = is_manual
        self._think_flag = think_flag
        self._provider_factory = provider_factory
        self._store = store
        self._clock = clock
        self._timer = timer
        self._chain = fallback_chain(config.models, first, required_tags)
        self._providers: dict[str, ChatProvider] = {}
        self._session_cost = 0.0
        self._cost_incomplete = False
        # Eager, so a ConfigError of the first model reaches the caller as before.
        self._provider_for(first)

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        entries: dict[str, BackoffEntry] = {}
        if self._store is not None:
            entries = await asyncio.to_thread(self._store.backoff_entries)
            notice = self._store.pop_notice()
            if notice is not None:
                yield Notice(text=notice)

        now = self._clock()
        skipped: list[str] = []  # "<id> <kind | backoff Nm | config>" per candidate
        left: tuple[str, SwitchReason] | None = None
        failed_id: str | None = None
        last_error: ErrorEvent | None = None

        for index, model in enumerate(self._chain):
            is_manual_pick = index == 0 and self._is_manual
            entry = entries.get(model.id)
            if is_backing_off(entry, now) and not is_manual_pick:
                assert entry is not None
                minutes = math.ceil((entry.until - now).total_seconds() / 60)
                skipped.append(f"{model.id} backoff {minutes}m")
                if index == 0:
                    left = (model.id, "backoff")
                continue
            if left is not None:
                yield ModelSwitch(
                    from_model_id=left[0], reason=left[1], to_model_id=model.id
                )
                left = None
            try:
                provider = self._provider_for(model)
            except ConfigError:
                skipped.append(f"{model.id} config")
                left = (model.id, "config")
                continue

            attempt = _Attempt(
                model,
                self._clock(),
                self._timer(),
                fallback_from=failed_id,
                is_manual_pick=is_manual_pick,
            )
            request = req.model_copy(
                update={
                    "model": model.model,
                    "think": resolve_think(self._think_flag, model),
                }
            )
            async with aclosing(
                self._run_attempt(provider, request, attempt)
            ) as events:
                async for event in events:
                    yield event

            terminal = attempt.terminal or ErrorEvent(
                kind="unknown", message=_NO_TERMINAL
            )
            if isinstance(terminal, Done):
                await self._record(attempt, "ok", None, "reset")
                yield self._usage_report(attempt)
                yield terminal
                return

            kind = terminal.kind
            counts = kind in FALLBACK_KINDS
            await self._record(attempt, "error", kind, "fail" if counts else "keep")
            self._add_failed_cost(attempt)
            if kind == "bad_request" or attempt.output_started:
                yield terminal
                return
            skipped.append(f"{model.id} {kind}")
            left = (model.id, kind)
            failed_id = model.id
            last_error = terminal

        yield self._no_model_event(skipped, last_error)

    async def aclose(self) -> None:
        providers, self._providers = list(self._providers.values()), {}
        errors: list[Exception] = []
        for provider in providers:
            try:
                await provider.aclose()
            except Exception as exc:  # noqa: BLE001 - keep closing the others
                errors.append(exc)
        if errors:
            raise errors[0]

    def _provider_for(self, model: ModelConfig) -> ChatProvider:
        provider = self._providers.get(model.provider)
        if provider is None:
            provider = self._provider_factory(self._config.providers[model.provider])
            self._providers[model.provider] = provider
        return provider

    async def _run_attempt(
        self, provider: ChatProvider, request: ChatRequest, attempt: _Attempt
    ) -> AsyncIterator[ThinkingDelta | TextDelta]:
        """Relay deltas at once; keep usage and the terminal event for the caller."""
        try:
            async with aclosing(provider.stream_chat(request)) as stream:
                async for event in stream:
                    if isinstance(event, ThinkingDelta | TextDelta):
                        attempt.output_started = True
                        yield event
                    elif isinstance(event, UsageEvent):
                        attempt.usage = event.usage
                    elif isinstance(event, Done | ErrorEvent):
                        attempt.terminal = event
                        break
        except (asyncio.CancelledError, GeneratorExit):
            # GeneratorExit: the consumer was cancelled while we were suspended.
            self._finish(attempt)
            await self._record(attempt, "cancelled", None, "keep", shielded=True)
            raise
        except Exception:
            self._finish(attempt)
            await self._record(attempt, "error", "unknown", "keep")
            raise
        self._finish(attempt)

    def _finish(self, attempt: _Attempt) -> None:
        attempt.seconds = self._timer() - attempt.started_at

    async def _record(
        self,
        attempt: _Attempt,
        outcome: Literal["ok", "error", "cancelled"],
        error_kind: str | None,
        backoff: BackoffAction,
        *,
        shielded: bool = False,
    ) -> None:
        if self._store is None:
            return
        # A cancelled attempt reports no tokens.
        usage = None if outcome == "cancelled" else attempt.usage
        call = CallRecord(
            time=attempt.started,
            model_id=attempt.model.id,
            provider_id=attempt.model.provider,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            reasoning_tokens=usage.reasoning_tokens if usage else None,
            cost_usd=compute_cost(usage, attempt.model.price),
            seconds=attempt.seconds,
            outcome=outcome,
            error_kind=error_kind,
            fallback_from=attempt.fallback_from,
        )
        write = asyncio.to_thread(
            self._store.record_attempt,
            call,
            backoff,
            attempt_started=attempt.started,
            is_manual=attempt.is_manual_pick,
        )
        # The row must be written even when the task is cancelled again.
        if shielded:
            await asyncio.shield(write)
        else:
            await write

    def _add_failed_cost(self, attempt: _Attempt) -> None:
        cost = compute_cost(attempt.usage, attempt.model.price)
        if cost is not None:
            self._session_cost += cost

    def _usage_report(self, attempt: _Attempt) -> UsageReport:
        cost = compute_cost(attempt.usage, attempt.model.price)
        if cost is None:
            self._cost_incomplete = True
        else:
            self._session_cost += cost
        return UsageReport(
            model_id=attempt.model.id,
            usage=attempt.usage,
            cost_usd=cost,
            seconds=attempt.seconds,
            session_cost_usd=self._session_cost,
            session_cost_incomplete=self._cost_incomplete,
        )

    @staticmethod
    def _no_model_event(
        skipped: list[str], last_error: ErrorEvent | None
    ) -> ErrorEvent:
        parts = ["no model available: " + "; ".join(skipped)]
        if last_error is not None:
            parts.append(
                "last error: " + redact(last_error.message, limit=_LAST_ERROR_LIMIT)
            )
        parts.append("pick a model to try it now")
        return ErrorEvent(
            kind=last_error.kind if last_error is not None else "unknown",
            message=redact("; ".join(parts), limit=_FINAL_MESSAGE_LIMIT),
        )


def make_router_factory(
    config: HarnessConfig,
    provider_factory: ProviderFactory,
    think_flag: bool | None,
    store: Store | None,
    *,
    required_tags: frozenset[str] = frozenset({"chat"}),
    clock: Callable[[], datetime] = _utc_now,
    timer: Callable[[], float] = time.monotonic,
) -> RouterFactory:
    def make(model: ModelConfig, is_manual: bool) -> ChatProvider:
        return ModelRouter(
            config,
            model,
            is_manual=is_manual,
            think_flag=think_flag,
            provider_factory=provider_factory,
            store=store,
            required_tags=required_tags,
            clock=clock,
            timer=timer,
        )

    return make
