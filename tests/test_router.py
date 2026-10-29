import asyncio
import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from llm_harness.config import ConfigError, HarnessConfig, ModelConfig, ProviderConfig
from llm_harness.router import ModelRouter, SessionCost, make_router_factory
from llm_harness.store import CallRecord, Store
from llm_harness.types import (
    ChatRequest,
    Done,
    ErrorEvent,
    Message,
    ModelSwitch,
    Notice,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
    UsageReport,
)
from tests.fakes import ScriptedProvider, assert_stream_contract

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
OK = [TextDelta(text="ok"), Done(finish_reason="stop")]
REQUEST = ChatRequest(
    model="ignored", messages=[Message(role="user", content="hi")], think=None
)


def failing(kind: str, message: str = "boom") -> list[StreamEvent]:
    return [ErrorEvent(kind=kind, message=message)]  # type: ignore[arg-type]


class PerCallProvider:
    """One script per call (the last one repeats); an Exception is raised."""

    def __init__(self, *scripts: list[StreamEvent] | Exception) -> None:
        self.scripts = list(scripts)
        self.requests: list[ChatRequest] = []
        self.closed = 0
        self.close_error: Exception | None = None

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(req)
        script = self.scripts.pop(0) if len(self.scripts) > 1 else self.scripts[0]
        if isinstance(script, Exception):
            raise script
        for event in script:
            yield event

    async def aclose(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class Factory:
    """Provider factory: the provider id is the host of the base_url."""

    def __init__(self, providers: dict[str, Any], broken: frozenset[str] = frozenset()):
        self.providers = providers
        self.broken = broken
        self.built: list[str] = []

    def __call__(self, config: ProviderConfig) -> Any:
        assert config.base_url is not None
        provider_id = urlsplit(config.base_url).hostname.removesuffix(".test")  # type: ignore[union-attr]
        self.built.append(provider_id)
        if provider_id in self.broken:
            raise ConfigError(f"missing key for {provider_id}")
        return self.providers[provider_id]


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakeTimer:
    """Each reading is `step` later than the previous one."""

    def __init__(self, step: float = 0.5) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        reading = self.value
        self.value += self.step
        return reading


def model_dict(
    model_id: str, priority: float, *tags: str, provider: str | None = None, **extra
) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id.upper(),
        "provider": provider or model_id,
        "model": f"{model_id}-remote",
        "tags": list(tags),
        "priority": priority,
    } | extra


def make_config(*models: dict[str, Any]) -> HarnessConfig:
    provider_ids = {m["provider"] for m in models}
    return HarnessConfig.model_validate(
        {
            "providers": {
                pid: {"type": "ollama", "base_url": f"http://{pid}.test"}
                for pid in provider_ids
            },
            "models": list(models),
        }
    )


def pick(config: HarnessConfig, model_id: str) -> ModelConfig:
    return next(m for m in config.models if m.id == model_id)


class World:
    """A config, scripted providers, a real tmp_path store and fake time."""

    def __init__(
        self,
        tmp_path: Path,
        providers: dict[str, Any],
        *models: dict[str, Any],
        broken: frozenset[str] = frozenset(),
    ) -> None:
        self.config = make_config(*models)
        self.providers = providers
        self.factory = Factory(providers, broken)
        self.db = tmp_path / "data" / "harness.db"
        self.store = Store(self.db)
        self.clock = FakeClock()
        self.timer = FakeTimer()

    def router(
        self,
        first: str,
        *,
        manual: bool = False,
        think_flag: bool | None = None,
        store: Store | None | str = "own",
        **kwargs: Any,
    ) -> ModelRouter:
        return ModelRouter(
            self.config,
            pick(self.config, first),
            is_manual=manual,
            think_flag=think_flag,
            provider_factory=self.factory,
            store=self.store if store == "own" else store,  # type: ignore[arg-type]
            clock=self.clock,
            timer=self.timer,
            **kwargs,
        )

    def rows(self, columns: str = "*") -> list[tuple]:
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(
                f"SELECT {columns} FROM llm_calls ORDER BY id"
            ).fetchall()

    def backoff(self) -> dict[str, tuple[int, str]]:
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT model_id, failures, until FROM model_backoff")
            return {model_id: (n, until) for model_id, n, until in rows}

    def put_in_backoff(self, model_id: str, times: int = 1) -> None:
        """Fail the model `times` times at T0 (manual picks always count)."""
        for _ in range(times):
            self._fail_once(model_id)
        self.store.backoff_entries()

    def _fail_once(self, model_id: str) -> None:
        self.store.record_attempt(
            CallRecord(
                time=T0,
                model_id=model_id,
                provider_id=model_id,
                input_tokens=None,
                output_tokens=None,
                reasoning_tokens=None,
                cost_usd=None,
                seconds=0.0,
                outcome="error",
                error_kind="connection",
                fallback_from=None,
            ),
            "fail",
            attempt_started=T0,
            is_manual=True,
        )


async def wait_until(condition) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached within 5 seconds")


async def run(router: ModelRouter, request: ChatRequest = REQUEST) -> list[StreamEvent]:
    events = [event async for event in router.stream_chat(request)]
    assert_stream_contract(events)
    return events


def two_models(tmp_path: Path, first: Any, second: Any) -> World:
    return World(
        tmp_path,
        {"a": first, "b": second},
        model_dict("a", 9, "chat", "default"),
        model_dict("b", 5, "chat"),
    )


# --- success path ---------------------------------------------------------


async def test_success_relays_deltas_then_one_usage_report_then_done(tmp_path: Path):
    usage = Usage(input_tokens=1_000_000, output_tokens=500_000)
    provider = PerCallProvider(
        [
            ThinkingDelta(text="hm"),
            TextDelta(text="a"),
            UsageEvent(usage=usage),
            Done(finish_reason="stop"),
        ]
    )
    world = World(
        tmp_path,
        {"a": provider},
        model_dict("a", 1, "chat", price={"input_per_mtok": 3, "output_per_mtok": 10}),
    )
    events = await run(world.router("a"))
    assert events == [
        ThinkingDelta(text="hm"),
        TextDelta(text="a"),
        UsageReport(
            model_id="a",
            usage=usage,
            cost_usd=8.0,
            seconds=0.5,
            session_cost_usd=8.0,
            session_cost_incomplete=False,
        ),
        Done(finish_reason="stop"),
    ]
    assert not any(isinstance(e, UsageEvent) for e in events)
    assert [r[9] for r in world.rows()] == ["ok"]


async def test_success_deletes_the_backoff_row(tmp_path: Path):
    world = two_models(tmp_path, PerCallProvider(OK), PerCallProvider(OK))
    world.put_in_backoff("a")
    world.clock.now = T0 + timedelta(hours=2)
    await run(world.router("a"))
    assert world.backoff() == {}


async def test_the_request_gets_the_candidates_own_model_id(tmp_path: Path):
    a, b = PerCallProvider(failing("connection")), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    await run(world.router("a"))
    assert [r.model for r in a.requests] == ["a-remote"]
    assert [r.model for r in b.requests] == ["b-remote"]
    assert b.requests[0].messages == REQUEST.messages


async def test_think_follows_flag_then_model_setting_then_none(tmp_path: Path):
    a, b = PerCallProvider(failing("connection")), PerCallProvider(OK)
    world = World(
        tmp_path,
        {"a": a, "b": b},
        model_dict("a", 9, "chat", thinking=False),
        model_dict("b", 5, "chat", thinking=True),
    )
    await run(world.router("a", store=None))
    assert (a.requests[-1].think, b.requests[-1].think) == (False, True)
    await run(world.router("a", think_flag=True, store=None))
    assert (a.requests[-1].think, b.requests[-1].think) == (True, True)
    c, d = PerCallProvider(failing("connection")), PerCallProvider(OK)
    plain = World(
        tmp_path / "plain",
        {"a": c, "b": d},
        model_dict("a", 9, "chat"),
        model_dict("b", 5, "chat"),
    )
    await run(plain.router("a", store=None))
    assert (c.requests[-1].think, d.requests[-1].think) == (None, None)


async def test_the_session_s_model_and_think_are_ignored(tmp_path: Path):
    a = PerCallProvider(OK)
    world = World(tmp_path, {"a": a}, model_dict("a", 1, "chat", thinking=False))
    await run(world.router("a"), REQUEST.model_copy(update={"think": True}))
    assert (a.requests[0].model, a.requests[0].think) == ("a-remote", False)


async def test_required_tags_select_the_pool(tmp_path: Path):
    a, b, c = (
        PerCallProvider(failing("connection")),
        PerCallProvider(OK),
        PerCallProvider(OK),
    )
    world = World(
        tmp_path,
        {"a": a, "b": b, "c": c},
        model_dict("a", 9, "chat"),
        model_dict("b", 5, "chat"),
        model_dict("c", 1, "code"),
    )
    events = await run(world.router("a", required_tags=frozenset({"code"})))
    assert (
        ModelSwitch(from_model_id="a", reason="connection", to_model_id="c") in events
    )
    assert b.requests == []


# --- fallback triggers -----------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["connection", "rate_limit", "not_found", "unknown", "auth"]
)
async def test_each_trigger_kind_falls_back_before_output(tmp_path: Path, kind: str):
    a, b = PerCallProvider(failing(kind)), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    events = await run(world.router("a"))
    assert events[0] == ModelSwitch(from_model_id="a", reason=kind, to_model_id="b")  # type: ignore[arg-type]
    assert events[1] == TextDelta(text="ok")
    assert isinstance(events[-2], UsageReport) and events[-2].model_id == "b"
    assert events[-1] == Done(finish_reason="stop")
    rows = world.rows("model_id, outcome, error_kind, fallback_from")
    assert rows == [("a", "error", kind, None), ("b", "ok", None, "a")]
    assert world.backoff()["a"][0] == 1


async def test_bad_request_is_shown_and_stops_the_chain(tmp_path: Path):
    a, b = PerCallProvider(failing("bad_request", "too long")), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    world.put_in_backoff("a")
    world.clock.now = T0 + timedelta(hours=1)
    events = await run(world.router("a", manual=True))
    assert events == failing("bad_request", "too long")
    assert b.requests == [] and world.factory.built == ["a"]
    assert world.rows("model_id, outcome, error_kind") == [
        ("a", "error", "connection"),  # the seeded failure
        ("a", "error", "bad_request"),
    ]
    assert world.backoff()["a"][0] == 1  # unchanged by bad_request


async def test_stream_without_terminal_event_counts_as_unknown(tmp_path: Path):
    a, b = PerCallProvider([]), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    events = await run(world.router("a"))
    assert events[0] == ModelSwitch(
        from_model_id="a", reason="unknown", to_model_id="b"
    )
    assert world.rows("model_id, outcome, error_kind") == [
        ("a", "error", "unknown"),
        ("b", "ok", None),
    ]


async def test_stream_that_ends_after_output_without_terminal_is_passed_through(
    tmp_path: Path,
):
    a, b = PerCallProvider([TextDelta(text="x")]), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    events = await run(world.router("a"))
    assert events[0] == TextDelta(text="x")
    assert isinstance(events[-1], ErrorEvent) and events[-1].kind == "unknown"
    assert b.requests == []


@pytest.mark.parametrize("started", [TextDelta(text="x"), ThinkingDelta(text="t")])
async def test_error_after_output_passes_through_without_fallback(
    tmp_path: Path, started: StreamEvent
):
    a = PerCallProvider([started, *failing("connection", "dropped")])
    b = PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    events = await run(world.router("a"))
    assert events == [started, *failing("connection", "dropped")]
    assert b.requests == [] and world.factory.built == ["a"]
    assert world.rows("model_id, outcome, error_kind") == [("a", "error", "connection")]
    assert world.backoff()["a"][0] == 1


async def test_bad_request_after_output_does_not_count_toward_backoff(tmp_path: Path):
    a = PerCallProvider([TextDelta(text="x"), *failing("bad_request")])
    world = two_models(tmp_path, a, PerCallProvider(OK))
    await run(world.router("a"))
    assert world.backoff() == {}


# --- cancellation and programming errors -----------------------------------


async def test_cancellation_writes_a_cancelled_row_and_reraises(tmp_path: Path):
    hanging = ScriptedProvider(OK, hang_after=1)
    world = two_models(tmp_path, hanging, PerCallProvider(OK))
    world.put_in_backoff("a")
    before = world.backoff()
    got: list[StreamEvent] = []

    async def consume() -> None:
        async for event in world.router("a", manual=True).stream_chat(REQUEST):
            got.append(event)

    task = asyncio.create_task(consume())
    await wait_until(lambda: bool(got))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled = world.rows("model_id, outcome, error_kind, input_tokens, cost_usd")
    assert cancelled[-1] == ("a", "cancelled", None, None, None)
    assert len(cancelled) == 2  # the seeded failure and the cancelled attempt
    assert world.backoff() == before
    assert "b" not in world.factory.built


async def test_second_cancel_during_the_cancelled_row_write_still_writes_it(
    tmp_path: Path,
):
    release, writing = threading.Event(), threading.Event()
    hanging = ScriptedProvider(OK, hang_after=0)
    world = two_models(tmp_path, hanging, PerCallProvider(OK))
    real = world.store.record_attempt

    def slow_record(*args: Any, **kwargs: Any) -> None:
        writing.set()
        assert release.wait(timeout=10)
        real(*args, **kwargs)

    world.store.record_attempt = slow_record  # type: ignore[method-assign]

    async def consume() -> None:
        async for _ in world.router("a").stream_chat(REQUEST):
            pass

    task = asyncio.create_task(consume())
    await wait_until(lambda: bool(hanging.requests))
    task.cancel()
    await wait_until(writing.is_set)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_until(lambda: bool(world.rows("outcome")))
    assert world.rows("outcome") == [("cancelled",)]


async def test_closing_the_stream_early_records_a_cancelled_row(tmp_path: Path):
    world = two_models(tmp_path, PerCallProvider(OK), PerCallProvider(OK))
    stream = world.router("a").stream_chat(REQUEST)
    assert await anext(stream) == TextDelta(text="ok")
    await stream.aclose()  # type: ignore[attr-defined]
    assert world.rows("outcome") == [("cancelled",)]
    assert world.backoff() == {}


async def test_programming_exception_is_recorded_without_backoff_and_reraised(
    tmp_path: Path,
):
    a = PerCallProvider(RuntimeError("bug"))
    world = two_models(tmp_path, a, PerCallProvider(OK))
    with pytest.raises(RuntimeError, match="bug"):
        await run(world.router("a"))
    assert world.rows("model_id, outcome, error_kind") == [("a", "error", "unknown")]
    assert world.backoff() == {}
    assert "b" not in world.factory.built


# --- backoff ---------------------------------------------------------------


async def test_manual_pick_in_backoff_is_tried_and_success_resets(tmp_path: Path):
    a = PerCallProvider(OK)
    world = two_models(tmp_path, a, PerCallProvider(OK))
    world.put_in_backoff("a")
    events = await run(world.router("a", manual=True))
    assert not any(isinstance(e, ModelSwitch) for e in events)
    assert len(a.requests) == 1
    assert world.backoff() == {}


async def test_manual_pick_in_backoff_failing_extends_and_continues(tmp_path: Path):
    a, b = PerCallProvider(failing("connection")), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    world.put_in_backoff("a")
    failures, until = world.backoff()["a"]
    events = await run(world.router("a", manual=True))
    assert events[0] == ModelSwitch(
        from_model_id="a", reason="connection", to_model_id="b"
    )
    new_failures, new_until = world.backoff()["a"]
    assert new_failures == failures + 1
    assert new_until > until


async def test_automatic_pick_in_backoff_is_skipped_with_a_backoff_note(
    tmp_path: Path,
):
    a, b = PerCallProvider(OK), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    world.put_in_backoff("a")
    events = await run(world.router("a"))
    assert events[0] == ModelSwitch(
        from_model_id="a", reason="backoff", to_model_id="b"
    )
    assert a.requests == [] and len(b.requests) == 1
    assert world.rows("model_id") == [("a",), ("b",)]  # the seed row, then b


async def test_other_automatic_candidates_in_backoff_are_skipped_silently(
    tmp_path: Path,
):
    a, b, c = (
        PerCallProvider(failing("connection")),
        PerCallProvider(OK),
        PerCallProvider(OK),
    )
    world = World(
        tmp_path,
        {"a": a, "b": b, "c": c},
        model_dict("a", 9, "chat"),
        model_dict("b", 5, "chat"),
        model_dict("c", 1, "chat"),
    )
    world.put_in_backoff("b")
    events = await run(world.router("a"))
    switches = [e for e in events if isinstance(e, ModelSwitch)]
    assert switches == [
        ModelSwitch(from_model_id="a", reason="connection", to_model_id="c")
    ]
    assert b.requests == []


async def test_backoff_survives_a_new_router_and_store(tmp_path: Path):
    a, b = PerCallProvider(failing("connection")), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    await run(world.router("a"))
    world.store = Store(world.db)  # a new process-like setup
    events = await run(world.router("a"))
    assert events[0] == ModelSwitch(
        from_model_id="a", reason="backoff", to_model_id="b"
    )
    assert len(a.requests) == 1


async def test_backoff_expires_with_the_clock(tmp_path: Path):
    a, b = PerCallProvider(failing("connection"), OK), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    await run(world.router("a"))
    world.clock.now = T0 + timedelta(minutes=2)
    events = await run(world.router("a"))
    assert not any(isinstance(e, ModelSwitch) for e in events)
    assert world.backoff() == {}


async def test_all_candidates_in_backoff_gives_the_final_error_without_a_call(
    tmp_path: Path,
):
    a, b = PerCallProvider(OK), PerCallProvider(OK)
    world = two_models(tmp_path, a, b)
    world.put_in_backoff("a", times=3)
    world.put_in_backoff("b", times=4)
    events = await run(world.router("a"))
    assert events == [
        ErrorEvent(
            kind="unknown",
            message="no model available: a backoff 3m; b backoff 5m; "
            "pick a model to try it now",
        )
    ]
    assert a.requests == [] and b.requests == []


async def test_all_failed_gives_the_final_error_with_every_attempt(tmp_path: Path):
    a = PerCallProvider(failing("connection"))
    b = PerCallProvider(failing("rate_limit", "slow down"))
    world = two_models(tmp_path, a, b)
    events = await run(world.router("a"))
    assert events[-1] == ErrorEvent(
        kind="rate_limit",
        message="no model available: a connection; b rate_limit; "
        "last error: slow down; pick a model to try it now",
    )
    assert world.rows("model_id, outcome, error_kind, fallback_from") == [
        ("a", "error", "connection", None),
        ("b", "error", "rate_limit", "a"),
    ]


async def test_final_error_redacts_and_limits_the_last_message(tmp_path: Path):
    secret = "sk-CANARYCANARYCANARY"
    a = PerCallProvider(failing("auth", f"bad key {secret} " + "x" * 1000))
    world = World(tmp_path, {"a": a}, model_dict("a", 1, "chat"))
    events = await run(world.router("a"))
    message = events[-1].message  # type: ignore[union-attr]
    assert secret not in message
    assert "[REDACTED:api_key]" in message
    assert len(message) <= 301
    assert message.startswith("no model available: a auth; last error: bad key")


async def test_config_error_on_a_fallback_provider_skips_with_reason_config(
    tmp_path: Path,
):
    a, c, d = (
        PerCallProvider(failing("connection")),
        PerCallProvider(OK),
        PerCallProvider(OK),
    )
    world = World(
        tmp_path,
        {"a": a, "c": c, "d": d},
        model_dict("a", 9, "chat"),
        model_dict("c", 5, "chat"),
        model_dict("d", 1, "chat"),
        broken=frozenset({"c"}),
    )
    events = await run(world.router("a"))
    assert [e for e in events if isinstance(e, ModelSwitch)] == [
        ModelSwitch(from_model_id="a", reason="connection", to_model_id="c"),
        ModelSwitch(from_model_id="c", reason="config", to_model_id="d"),
    ]
    assert c.requests == []
    assert world.rows("model_id") == [("a",), ("d",)]
    assert "c" not in world.backoff()


async def test_config_error_everywhere_is_part_of_the_final_error(tmp_path: Path):
    a, c = PerCallProvider(failing("auth", "no")), PerCallProvider(OK)
    world = World(
        tmp_path,
        {"a": a, "c": c},
        model_dict("a", 9, "chat"),
        model_dict("c", 5, "chat"),
        broken=frozenset({"c"}),
    )
    events = await run(world.router("a"))
    assert events[-1] == ErrorEvent(
        kind="auth",
        message="no model available: a auth; c config; last error: no; "
        "pick a model to try it now",
    )


# --- providers -------------------------------------------------------------


async def test_fallback_providers_are_built_lazily(tmp_path: Path):
    world = two_models(tmp_path, PerCallProvider(OK), PerCallProvider(OK))
    router = world.router("a")
    assert world.factory.built == ["a"]  # the first model's, in the constructor
    await run(router)
    assert world.factory.built == ["a"]


async def test_fallback_provider_is_built_on_first_use_and_cached(tmp_path: Path):
    a = PerCallProvider(failing("connection"))
    world = two_models(tmp_path, a, PerCallProvider(OK))
    router = world.router("a", store=None)
    await run(router)
    await run(router)
    assert world.factory.built == ["a", "b"]


async def test_models_of_one_provider_share_one_instance(tmp_path: Path):
    shared = PerCallProvider(failing("not_found"), OK)
    world = World(
        tmp_path,
        {"p": shared},
        model_dict("a", 9, "chat", provider="p"),
        model_dict("b", 5, "chat", provider="p"),
    )
    await run(world.router("a"))
    assert world.factory.built == ["p"]
    assert [r.model for r in shared.requests] == ["a-remote", "b-remote"]


def test_config_error_of_the_first_provider_propagates_from_the_constructor(
    tmp_path: Path,
):
    world = two_models(tmp_path, PerCallProvider(OK), PerCallProvider(OK))
    world.factory.broken = frozenset({"a"})
    with pytest.raises(ConfigError, match="missing key for a"):
        world.router("a")


async def test_aclose_closes_every_built_provider_and_is_idempotent(tmp_path: Path):
    a, b, c = (
        PerCallProvider(failing("connection")),
        PerCallProvider(OK),
        PerCallProvider(OK),
    )
    world = World(
        tmp_path,
        {"a": a, "b": b, "c": c},
        model_dict("a", 9, "chat"),
        model_dict("b", 5, "chat"),
        model_dict("c", 1, "chat"),
    )
    router = world.router("a")
    await run(router)
    await router.aclose()
    await router.aclose()
    assert (a.closed, b.closed, c.closed) == (1, 1, 0)  # c was never built


async def test_aclose_continues_past_a_failing_provider(tmp_path: Path):
    a, b = PerCallProvider(failing("connection")), PerCallProvider(OK)
    a.close_error = RuntimeError("close failed")
    world = two_models(tmp_path, a, b)
    router = world.router("a")
    await run(router)
    with pytest.raises(RuntimeError, match="close failed"):
        await router.aclose()
    assert (a.closed, b.closed) == (1, 1)
    await router.aclose()
    assert (a.closed, b.closed) == (1, 1)


# --- usage report ----------------------------------------------------------


def priced(model_id: str, priority: float, *tags: str) -> dict[str, Any]:
    return model_dict(
        model_id, priority, *tags, price={"input_per_mtok": 1, "output_per_mtok": 2}
    )


def reply(input_tokens: int | None, output_tokens: int | None) -> list[StreamEvent]:
    usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens)
    return [TextDelta(text="r"), UsageEvent(usage=usage), Done()]


async def test_session_total_accumulates_and_incomplete_after_unknown_usage(
    tmp_path: Path,
):
    a = PerCallProvider(
        reply(1_000_000, 500_000), reply(2_000_000, 0), OK, reply(1_000_000, 0)
    )
    world = World(tmp_path, {"a": a}, priced("a", 1, "chat"))
    router = world.router("a")

    def report(events: list[StreamEvent]) -> UsageReport:
        return next(e for e in events if isinstance(e, UsageReport))

    first = report(await run(router))
    assert (first.cost_usd, first.session_cost_usd) == (2.0, 2.0)
    assert first.session_cost_incomplete is False
    second = report(await run(router))
    assert (second.cost_usd, second.session_cost_usd) == (2.0, 4.0)
    third = report(await run(router))
    assert third.usage is None and third.cost_usd is None
    assert (third.session_cost_usd, third.session_cost_incomplete) == (4.0, True)
    fourth = report(await run(router))
    assert (fourth.session_cost_usd, fourth.session_cost_incomplete) == (5.0, True)
    assert world.rows("cost_usd, input_tokens") == [
        (2.0, 1_000_000),
        (2.0, 2_000_000),
        (None, None),
        (1.0, 1_000_000),
    ]


async def test_failed_attempt_that_reported_usage_counts_in_the_session_total(
    tmp_path: Path,
):
    a = PerCallProvider(
        [
            UsageEvent(usage=Usage(input_tokens=1_000_000, output_tokens=0)),
            *failing("connection"),
        ]
    )
    b = PerCallProvider(reply(1_000_000, 0))
    world = World(
        tmp_path, {"a": a, "b": b}, priced("a", 9, "chat"), priced("b", 5, "chat")
    )
    events = await run(world.router("a"))
    report = next(e for e in events if isinstance(e, UsageReport))
    assert report.session_cost_usd == 2.0
    assert report.session_cost_incomplete is False
    assert world.rows("model_id, outcome, cost_usd") == [
        ("a", "error", 1.0),
        ("b", "ok", 1.0),
    ]


def usage_report(events: list[StreamEvent]) -> UsageReport:
    return next(e for e in events if isinstance(e, UsageReport))


async def test_routers_sharing_a_session_cost_never_show_a_decreasing_total(
    tmp_path: Path,
):
    # Parent (a), child (b), parent again: the child's total includes the parent's.
    a = PerCallProvider(reply(1_000_000, 0), reply(1_000_000, 0))
    b = PerCallProvider(reply(2_000_000, 0))
    world = World(
        tmp_path, {"a": a, "b": b}, priced("a", 9, "chat"), priced("b", 5, "chat")
    )
    costs = SessionCost()
    parent, child = world.router("a", costs=costs), world.router("b", costs=costs)

    totals = [
        usage_report(await run(parent)).session_cost_usd,
        usage_report(await run(child)).session_cost_usd,
        usage_report(await run(parent)).session_cost_usd,
    ]
    assert totals == [1.0, 3.0, 4.0]
    assert totals == sorted(totals)
    assert costs.total == 4.0


async def test_routers_without_session_cost_each_start_at_zero(tmp_path: Path):
    a = PerCallProvider(reply(1_000_000, 0))
    world = World(tmp_path, {"a": a}, priced("a", 1, "chat"))
    first, second = world.router("a"), world.router("a")
    assert usage_report(await run(first)).session_cost_usd == 1.0
    assert usage_report(await run(second)).session_cost_usd == 1.0


async def test_failed_attempt_cost_is_added_to_the_shared_session_cost(
    tmp_path: Path,
):
    failed_with_usage = [
        UsageEvent(usage=Usage(input_tokens=1_000_000, output_tokens=0)),
        *failing("connection"),
    ]
    a, b = PerCallProvider(failed_with_usage), PerCallProvider(reply(1_000_000, 0))
    world = World(
        tmp_path, {"a": a, "b": b}, priced("a", 9, "chat"), priced("b", 5, "chat")
    )
    costs = SessionCost()
    await run(world.router("a", costs=costs))
    assert costs.total == 2.0  # the failed attempt (1.0) and the retry (1.0)
    other = PerCallProvider(reply(1_000_000, 0))
    world.providers["a"] = other
    report = usage_report(await run(world.router("a", costs=costs)))
    assert report.session_cost_usd == 3.0


async def test_unknown_cost_marks_the_shared_session_cost_incomplete(tmp_path: Path):
    a = PerCallProvider(OK)  # no usage event: the cost is unknown
    b = PerCallProvider(reply(1_000_000, 0))
    world = World(
        tmp_path, {"a": a, "b": b}, priced("a", 9, "chat"), priced("b", 5, "chat")
    )
    costs = SessionCost()
    await run(world.router("a", costs=costs))
    assert costs.incomplete is True
    report = usage_report(await run(world.router("b", costs=costs)))
    assert report.session_cost_incomplete is True
    assert report.session_cost_usd == 1.0


async def test_router_factory_shares_the_given_session_cost(tmp_path: Path):
    a = PerCallProvider(reply(1_000_000, 0))
    world = World(tmp_path, {"a": a}, priced("a", 1, "chat"))
    model = pick(world.config, "a")
    costs = SessionCost()
    make = make_router_factory(
        world.config,
        world.factory,
        None,
        world.store,
        clock=world.clock,
        timer=world.timer,
        costs=costs,
    )
    first, second = make(model, False), make(model, False)
    assert usage_report(await run(first)).session_cost_usd == 1.0  # type: ignore[arg-type]
    assert usage_report(await run(second)).session_cost_usd == 2.0  # type: ignore[arg-type]
    assert costs.total == 2.0


async def test_router_factory_without_session_cost_keeps_routers_private(
    tmp_path: Path,
):
    a = PerCallProvider(reply(1_000_000, 0))
    world = World(tmp_path, {"a": a}, priced("a", 1, "chat"))
    model = pick(world.config, "a")
    make = make_router_factory(
        world.config,
        world.factory,
        None,
        world.store,
        clock=world.clock,
        timer=world.timer,
    )
    first, second = make(model, False), make(model, False)
    assert usage_report(await run(first)).session_cost_usd == 1.0  # type: ignore[arg-type]
    assert usage_report(await run(second)).session_cost_usd == 1.0  # type: ignore[arg-type]


async def test_failed_attempt_without_usage_stores_null_cost(tmp_path: Path):
    world = two_models(tmp_path, PerCallProvider(failing("auth")), PerCallProvider(OK))
    await run(world.router("a"))
    assert world.rows("input_tokens, output_tokens, cost_usd")[0] == (None, None, None)


async def test_seconds_come_from_the_injected_timer(tmp_path: Path):
    world = two_models(tmp_path, PerCallProvider(failing("auth")), PerCallProvider(OK))
    world.timer.step = 2.5
    events = await run(world.router("a"))
    report = next(e for e in events if isinstance(e, UsageReport))
    assert report.seconds == 2.5
    assert world.rows("seconds") == [(2.5,), (2.5,)]


# --- one turn through the router ------------------------------------------


async def test_every_candidate_failing_ends_the_turn_with_one_error(tmp_path: Path):
    world = two_models(
        tmp_path,
        PerCallProvider(failing("connection")),
        PerCallProvider(failing("auth")),
    )
    events = await run(world.router("a"))
    assert isinstance(events[-1], ErrorEvent)
    assert sum(isinstance(e, (Done, ErrorEvent)) for e in events) == 1


async def test_a_fallback_ending_in_done_streams_switch_report_and_the_reply(
    tmp_path: Path,
):
    world = two_models(
        tmp_path, PerCallProvider(failing("connection")), PerCallProvider(OK)
    )
    events = await run(world.router("a"))
    assert any(isinstance(e, ModelSwitch) for e in events)
    assert any(isinstance(e, UsageReport) for e in events)
    assert isinstance(events[-1], Done)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "ok"


# --- store problems and privacy -------------------------------------------


async def test_unusable_store_does_not_stop_the_turn_and_notices_once(tmp_path: Path):
    world = two_models(tmp_path, PerCallProvider(OK), PerCallProvider(OK))
    world.db.parent.mkdir()
    world.db.write_bytes(bytes(range(256)) * 16)
    router = world.router("a")
    first = await run(router)
    notices = [e for e in first if isinstance(e, Notice)]
    assert len(notices) == 1
    assert notices[0].text.startswith("usage database unavailable (DatabaseError): ")
    assert isinstance(first[-1], Done)
    second = await run(router)
    assert not any(isinstance(e, Notice) for e in second)
    assert isinstance(second[-1], Done)


async def test_router_without_a_store_still_falls_back(tmp_path: Path):
    world = two_models(tmp_path, PerCallProvider(failing("auth")), PerCallProvider(OK))
    events = await run(world.router("a", store=None))
    assert isinstance(events[0], ModelSwitch)
    assert not world.db.exists()


async def test_ledger_stores_no_message_text(tmp_path: Path):
    prompt, text, error = (
        "CANARY-PROMPT",
        "CANARY-REPLY",
        "CANARY-ERROR sk-CANARYCANARYCANARY",
    )
    a = PerCallProvider(failing("connection", error))
    b = PerCallProvider([TextDelta(text=text), Done()])
    world = two_models(tmp_path, a, b)
    request = REQUEST.model_copy(
        update={"messages": [Message(role="user", content=prompt)]}
    )
    await run(world.router("a"), request)
    values = [v for row in world.rows() for v in row]
    assert not any(isinstance(v, str) and "CANARY" in v for v in values)
    assert not any(
        isinstance(v, str) and "CANARY" in v
        for entry in world.backoff().items()
        for v in entry
    )
    data = world.db.read_bytes()
    assert not any(
        c.encode() in data for c in (prompt, text, "CANARY-ERROR", "sk-CANARY")
    )
    assert [r[0] for r in world.rows("id")] == [1, 2]
    with closing(sqlite3.connect(world.db)) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(llm_calls)")]
    assert len(world.rows()[0]) == len(columns) == 12


# --- factory and events ----------------------------------------------------


async def test_router_factory_builds_routers_for_a_model_and_manual_flag(
    tmp_path: Path,
):
    a = PerCallProvider(OK)
    world = two_models(tmp_path, a, PerCallProvider(OK))
    world.put_in_backoff("a")
    factory = make_router_factory(
        world.config,
        world.factory,
        True,
        world.store,
        clock=world.clock,
        timer=world.timer,
    )
    router = factory(pick(world.config, "a"), True)
    assert isinstance(router, ModelRouter)
    await run(router)  # type: ignore[arg-type]
    assert a.requests[0].think is True
    automatic = factory(pick(world.config, "a"), False)
    assert isinstance(automatic, ModelRouter)


@pytest.mark.parametrize(
    ("tags_kwargs", "expected_model"),
    [({}, "b"), ({"required_tags": frozenset({"orchestrator"})}, "c")],
)
async def test_router_factory_passes_required_tags_to_the_fallback_chain(
    tmp_path: Path, tags_kwargs: dict[str, Any], expected_model: str
):
    world = World(
        tmp_path,
        {
            "a": PerCallProvider(failing("connection")),
            "b": PerCallProvider(OK),
            "c": PerCallProvider(OK),
        },
        model_dict("a", 9, "chat", "default"),
        model_dict("b", 5, "chat"),
        model_dict("c", 1, "orchestrator"),
    )
    factory = make_router_factory(
        world.config,
        world.factory,
        None,
        world.store,
        clock=world.clock,
        timer=world.timer,
        **tags_kwargs,
    )
    events = await run(factory(pick(world.config, "a"), False))  # type: ignore[arg-type]
    switches = [e for e in events if isinstance(e, ModelSwitch)]
    assert [s.to_model_id for s in switches] == [expected_model]


def test_new_events_are_frozen_and_reject_extra_fields():
    switch = ModelSwitch(from_model_id="a", reason="config", to_model_id="b")
    with pytest.raises(ValidationError):
        switch.reason = "backoff"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ModelSwitch(from_model_id="a", reason="nope", to_model_id="b")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Notice(text="x", extra=1)  # type: ignore[call-arg]
    report = UsageReport(
        model_id="a",
        usage=None,
        cost_usd=None,
        seconds=1.0,
        session_cost_usd=0.0,
        session_cost_incomplete=False,
    )
    with pytest.raises(ValidationError):
        report.seconds = 2.0  # type: ignore[misc]
