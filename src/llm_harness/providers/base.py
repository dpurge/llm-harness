"""The provider protocol that every adapter implements."""

from collections.abc import AsyncIterator
from typing import Protocol

from llm_harness.types import ChatRequest, StreamEvent


class ChatProvider(Protocol):
    """Streams one chat turn as neutral events.

    Contract: zero or more ThinkingDelta/TextDelta, at most one UsageEvent,
    then exactly one terminal Done or ErrorEvent. Cancellation closes the SDK
    stream and lets CancelledError propagate. Adapters map SDK exceptions to
    an ErrorEvent with a redacted, truncated message; programming errors still
    raise.
    """

    def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]: ...

    async def aclose(self) -> None: ...
