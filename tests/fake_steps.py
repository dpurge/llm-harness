"""A scripted step model: one prepared event list per provider call."""

import asyncio
import json
from collections.abc import AsyncIterator

from llm_harness.types import ChatRequest, Done, StreamEvent, TextDelta


def reply_events(
    action: dict, state_patch: dict | None = None, *extra: StreamEvent
) -> list[StreamEvent]:
    """The events of one valid orchestrator reply: extras, the JSON text, Done."""
    body = json.dumps({"state_patch": state_patch or {}, "action": action})
    return [*extra, TextDelta(text=body), Done()]


class ScriptedSteps:
    """Serves `scripts` in order, one per stream_chat call.

    Records a deep copy of every request; `closed` is set when a stream is closed. A call beyond the script fails the
    test loudly. `hang_after` makes the first call stall after that many events,
    for cancellation tests.
    """

    def __init__(
        self, scripts: list[list[StreamEvent]], *, hang_after: int | None = None
    ) -> None:
        self._scripts = scripts
        self._hang_after = hang_after
        self.requests: list[ChatRequest] = []
        self.closed = False

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(req.model_copy(deep=True))
        index = len(self.requests) - 1
        if index >= len(self._scripts):
            raise AssertionError(
                f"stream_chat called {len(self.requests)} times, "
                f"only {len(self._scripts)} scripted"
            )
        try:
            for position, event in enumerate(self._scripts[index]):
                if index == 0 and position == self._hang_after:
                    await asyncio.Event().wait()
                yield event
        finally:
            self.closed = True

    async def aclose(self) -> None:
        return None


def as_step_reply(events: list[StreamEvent]) -> list[StreamEvent]:
    """Turn a scripted plain-text reply into a valid orchestrator step reply.

    The text deltas are joined into one `respond` action; thinking, usage and
    other events pass through; an error-terminated script is left as it is,
    because an orchestrator discards the text of a failed attempt anyway.
    """
    done = next((e for e in events if isinstance(e, Done)), None)
    if done is None:
        return list(events)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    body = json.dumps(
        {"state_patch": {"goal": "g"}, "action": {"type": "respond", "text": text}}
    )
    others = [e for e in events if not isinstance(e, (TextDelta, Done))]
    return [*others, TextDelta(text=body), done]
