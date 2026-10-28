"""Call trace of one orchestrator turn: a tree of frozen spans.

The tree is `turn -> step -> llm_call | tool_call`, stored flat and linked by
`parent_id`. Spans are display and debug data only: nothing here is read by
`build_request` or stored in the orchestrator state. Text is kept whole (no
cut); secrets are masked with `redact` when a span closes.
"""

import asyncio
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

from llm_harness.redact import redact
from llm_harness.types import _Frozen

SpanKind = Literal["turn", "step", "llm_call", "tool_call"]
SpanStatus = Literal["ok", "error", "rejected", "cancelled"]

# `redact` cuts at `limit` and compares with `len(text) > limit`, so None is not
# accepted; a huge integer means "never cut".
UNCUT = sys.maxsize

# Offsets and durations are shown to people: a float like 45535.52383299939 is noise.
_MS_DECIMALS = 3


class Span(_Frozen):
    id: str
    parent_id: str | None
    kind: SpanKind
    name: str
    start_ms: float  # offset from the start of the turn
    duration_ms: float
    status: SpanStatus
    attributes: dict[str, Any]


class SpanHandle:
    """An open span: the code inside the `with` block fills it in.

    `status` stays None until the outcome is known; closing then picks `ok`, or
    `cancelled`/`error` when the block was left by an exception.
    """

    def __init__(self, span_id: str, parent_id: str | None) -> None:
        self.id = span_id
        self.parent_id = parent_id
        self.status: SpanStatus | None = None
        # Raw values; masked as a whole when the span closes, so a secret split
        # across streamed chunks is still found.
        self.attributes: dict[str, Any] = {}


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value, limit=UNCUT)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


class TraceRecorder:
    """Opens and closes spans for one turn at a time; `reset` starts a new turn.

    `clock` returns seconds from a monotonic source; tests inject a fake one.
    A disabled recorder takes the same calls and keeps nothing.
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, *, enabled: bool = True
    ) -> None:
        self._clock = clock
        self._enabled = enabled
        self._origin: float | None = None
        self._count = 0
        # One slot per opened span, in opening order; filled when it closes.
        self._slots: list[Span | None] | None = None

    def reset(self) -> None:
        """Forget the previous turn; the next span is the start of a new one."""
        self._origin = None
        self._count = 0
        if self._enabled:
            self._slots = []

    @property
    def spans(self) -> tuple[Span, ...] | None:
        """Closed spans in opening order; None before the first turn."""
        if self._slots is None:
            return None
        return tuple(span for span in self._slots if span is not None)

    def _now_ms(self) -> float:
        now = self._clock()
        if self._origin is None:  # the first span is the turn: offset 0
            self._origin = now
        return (now - self._origin) * 1000

    @contextmanager
    def span(
        self, kind: SpanKind, name: str, parent: SpanHandle | None = None
    ) -> Iterator[SpanHandle]:
        self._count += 1
        handle = SpanHandle(f"s{self._count}", parent.id if parent else None)
        start = self._now_ms()
        slots = self._slots
        index = -1
        if slots is not None:
            index = len(slots)
            slots.append(None)
        outcome: SpanStatus | None = None
        try:
            yield handle
        except (asyncio.CancelledError, GeneratorExit):
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            if slots is not None:
                slots[index] = Span(
                    id=handle.id,
                    parent_id=handle.parent_id,
                    kind=kind,
                    name=_scrub(name),
                    start_ms=round(start, _MS_DECIMALS),
                    duration_ms=round(self._now_ms() - start, _MS_DECIMALS),
                    status=handle.status or outcome or "ok",
                    attributes=_scrub(handle.attributes),
                )
