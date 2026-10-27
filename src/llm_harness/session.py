"""Chat session: owns the history and drives one provider turn at a time."""

from collections.abc import AsyncIterator
from contextlib import aclosing

from llm_harness.providers.base import ChatProvider
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    Message,
    StreamEvent,
    TextDelta,
)

_NO_TERMINAL = "the provider ended the stream without a terminal event"


class ChatSession:
    def __init__(
        self, provider: ChatProvider, model: str, think: bool | None = None
    ) -> None:
        self._provider = provider
        self._model = model
        self._think = think
        self._history: list[Message] = []
        self._busy = False

    @property
    def history(self) -> list[Message]:
        return list(self._history)

    async def send(self, text: str) -> AsyncIterator[StreamEvent]:
        """Send a user message and stream the events of the reply.

        The user message stays in history only if the turn ends with Done;
        thinking is never stored.
        """
        if self._busy:
            raise RuntimeError("a turn is already running")
        self._busy = True
        turn_start = len(self._history)
        committed = False
        try:
            self._history.append(Message(role="user", content=text))
            request = ChatRequest(
                model=self._model, messages=self.history, think=self._think
            )
            reply: list[str] = []
            async with aclosing(self._provider.stream_chat(request)) as stream:
                async for event in stream:
                    if isinstance(event, TextDelta):
                        reply.append(event.text)
                    elif isinstance(event, Done):
                        self._history.append(
                            Message(role="assistant", content="".join(reply))
                        )
                        committed = True
                        yield event
                        return
                    elif isinstance(event, ErrorEvent):
                        # Roll back before yielding so the consumer sees clean history.
                        del self._history[turn_start:]
                        yield event
                        return
                    yield event
            del self._history[turn_start:]
            yield ErrorEvent(kind="unknown", message=_NO_TERMINAL)
        finally:
            if not committed:
                del self._history[turn_start:]
            self._busy = False
