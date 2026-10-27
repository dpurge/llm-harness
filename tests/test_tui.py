"""Terminal UI tests: Textual's Pilot drives the app with a scripted provider."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import Collapsible, Input, Markdown, Select, Static
from textual.widgets.markdown import MarkdownBlock, MarkdownFence

from llm_harness.config import ConfigError, HarnessConfig, ProviderConfig
from llm_harness.redact import REDACTED
from llm_harness.store import Store
from llm_harness.tui.app import ChatApp
from llm_harness.types import (
    Done,
    ErrorEvent,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
)
from tests.fakes import ScriptedProvider, router_factory_for

CONFIG = HarnessConfig.model_validate(
    {
        "providers": {
            "local": {"type": "ollama", "base_url": "http://localhost:11434"},
            "remote": {
                "type": "openai",
                "base_url": "https://api.example/v1",
                "api_key_env": "MISSING_KEY",
            },
        },
        "models": [
            {
                "id": "m1",
                "name": "Model One",
                "provider": "local",
                "model": "provider-model-1",
                "tags": ["default"],
                "priority": 1,
            },
            {
                "id": "m2",
                "name": "Model Two",
                "provider": "local",
                "model": "provider-model-2",
                "tags": [],
                "priority": 1,
            },
            {
                "id": "m3",
                "name": "Model Three",
                "provider": "remote",
                "model": "provider-model-3",
                "tags": [],
                "priority": 1,
            },
        ],
    }
)
MODEL_1, MODEL_2, MODEL_3 = CONFIG.models

REPLY = [TextDelta(text="Hel"), TextDelta(text="lo"), Done(finish_reason="stop")]


class RecordingProvider(ScriptedProvider):
    def __init__(self, events: list[StreamEvent], **kwargs: Any) -> None:
        super().__init__(events, **kwargs)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class Factory:
    """Builds one RecordingProvider per call; `openai` providers fail like a missing key."""

    def __init__(self, events: list[StreamEvent], **provider_kwargs: Any) -> None:
        self._events = events
        self._provider_kwargs = provider_kwargs
        self.providers: list[RecordingProvider] = []

    def __call__(self, config: ProviderConfig) -> RecordingProvider:
        if config.type == "openai":
            raise ConfigError("environment variable MISSING_KEY is not set")
        provider = RecordingProvider(self._events, **self._provider_kwargs)
        self.providers.append(provider)
        return provider


CANARY = "sk-CANARY-123456789"


class BoomFactory(Factory):
    """Like Factory, but `openai` providers fail with a non-ConfigError."""

    def __call__(self, config: ProviderConfig) -> RecordingProvider:
        if config.type == "openai":
            raise ValueError(f"boom {CANARY}")
        return super().__call__(config)


class FlakyProvider(RecordingProvider):
    """The first stream raises after one delta; later streams answer normally."""

    async def stream_chat(self, req):
        if self.requests:
            async for event in super().stream_chat(req):
                yield event
            return
        self.requests.append(req.model_copy(deep=True))
        yield TextDelta(text="par")
        raise RuntimeError(f"kaboom {CANARY}")


class FlakyFactory:
    def __init__(self) -> None:
        self.providers: list[FlakyProvider] = []

    def __call__(self, config: ProviderConfig) -> FlakyProvider:
        provider = FlakyProvider(REPLY)
        self.providers.append(provider)
        return provider


def make_app(
    factory: Callable[[ProviderConfig], Any],
    *,
    model=MODEL_1,
    think_flag=None,
    is_manual=False,
) -> ChatApp:
    return ChatApp(
        CONFIG, model, router_factory_for(CONFIG, factory, think_flag), is_manual
    )


def texts(app: ChatApp, css_class: str) -> list[str]:
    return [w.visual.plain for w in app.query(f"#log Static.{css_class}")]


def reply_texts(app: ChatApp) -> list[str]:
    """The rendered text of each reply: its non-empty markdown blocks, one per line."""
    return [
        "\n".join(b.visual.plain for b in w.query(MarkdownBlock) if b.visual.plain)
        for w in app.query("#log Markdown.reply")
    ]


def prompt_of(app: ChatApp) -> Input:
    return app.query_one("#prompt", Input)


async def wait_for(pilot: Pilot, condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await pilot.pause()
    raise AssertionError("condition not reached")


async def send(pilot: Pilot, app: ChatApp, text: str) -> None:
    prompt_of(app).value = text
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


# Streaming and display


async def test_streamed_reply_appears_in_the_log() -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        assert texts(app, "user") == ["you: hi"]
        assert reply_texts(app) == ["Hello"]
        assert prompt_of(app).value == ""


async def test_blank_input_is_ignored() -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "   ")
        assert factory.providers[0].requests == []
        assert texts(app, "user") == []


async def test_thinking_is_in_a_collapsed_block_not_in_the_reply() -> None:
    factory = Factory([ThinkingDelta(text="pond"), ThinkingDelta(text="ering"), *REPLY])
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        (block,) = app.query(Collapsible)
        assert block.title == "Thinking"
        assert block.collapsed is True
        assert texts(app, "thinking") == ["pondering"]
        assert reply_texts(app) == ["Hello"]


async def test_no_thinking_block_without_thinking_deltas() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        assert list(app.query(Collapsible)) == []


async def test_usage_line_follows_the_reply() -> None:
    usage = UsageEvent(usage=Usage(input_tokens=3, output_tokens=7))
    app = make_app(Factory([TextDelta(text="x"), usage, Done()]))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        (line,) = texts(app, "usage")
        assert line.startswith("[m1 tokens in=3 out=7 $0.00 ")
        assert line.endswith("s | session $0.00]")


async def test_error_event_shows_error_and_restores_the_input() -> None:
    factory = Factory(
        [TextDelta(text="par"), ErrorEvent(kind="auth", message="bad key")]
    )
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "hello")
        assert texts(app, "error") == ["error (auth): bad key"]
        assert prompt_of(app).value == "hello"
        assert prompt_of(app).disabled is False
        assert app._session is not None and app._session.history == []


async def test_input_is_disabled_during_a_turn_and_enabled_after() -> None:
    factory = Factory([TextDelta(text="par"), *REPLY], hang_after=1)
    app = make_app(factory)
    async with app.run_test() as pilot:
        prompt_of(app).value = "hi"
        await pilot.press("enter")
        await wait_for(pilot, lambda: reply_texts(app) == ["par"])
        assert prompt_of(app).disabled is True
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert prompt_of(app).disabled is False


async def test_escape_cancels_the_turn_and_restores_the_input() -> None:
    factory = Factory([TextDelta(text="par"), *REPLY], hang_after=1)
    app = make_app(factory)
    async with app.run_test() as pilot:
        prompt_of(app).value = "hi there"
        await pilot.press("enter")
        await wait_for(pilot, lambda: reply_texts(app) == ["par"])
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert texts(app, "note") == ["cancelled"]
        assert prompt_of(app).value == "hi there"
        assert app._session is not None and app._session.history == []
        assert factory.providers[0].closed is True


async def test_second_request_carries_the_whole_history() -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "one")
        await send(pilot, app, "two")
        messages = factory.providers[0].requests[-1].messages
        assert [(m.role, m.content) for m in messages] == [
            ("user", "one"),
            ("assistant", "Hello"),
            ("user", "two"),
        ]


@pytest.mark.parametrize("flag", [True, False, None])
async def test_think_flag_reaches_the_request(flag: bool | None) -> None:
    factory = Factory(REPLY)
    app = make_app(factory, think_flag=flag)
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        assert factory.providers[0].requests[-1].think is flag


# Security

MARKUP = "[bold red]x[/] [link=http://evil]y[/link]"


async def test_markup_in_model_text_appears_literally() -> None:
    factory = Factory(
        [
            ThinkingDelta(text=MARKUP),
            TextDelta(text=MARKUP),
            Done(),
        ]
    )
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, MARKUP)
        assert reply_texts(app) == [MARKUP]
        assert texts(app, "thinking") == [MARKUP]
        assert texts(app, "user") == [f"you: {MARKUP}"]


async def test_markup_in_an_error_message_appears_literally() -> None:
    factory = Factory([ErrorEvent(kind="bad_request", message=MARKUP)])
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        assert texts(app, "error") == [f"error (bad_request): {MARKUP}"]


async def test_control_characters_are_stripped_from_all_model_text() -> None:
    dirty = "a\x1b[31mb\x07c\x9bd\x7fe\nf"
    clean = "a[31mbcde\nf"
    factory = Factory([ThinkingDelta(text=dirty), TextDelta(text=dirty), Done()])
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        # Markdown joins the two lines of a paragraph with a space.
        assert reply_texts(app) == [clean.replace("\n", " ")]
        assert texts(app, "thinking") == [clean]
    error_app = make_app(Factory([ErrorEvent(kind="bad_request", message=dirty)]))
    async with error_app.run_test() as pilot:
        await send(pilot, error_app, "hi")
        assert texts(error_app, "error") == [f"error (bad_request): {clean}"]


# Model selector


async def test_selector_lists_exactly_the_models_yaml_names() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        select = app.query_one("#model", Select)
        assert [label.plain for label, _ in select._options] == [
            "Model One",
            "Model Two",
            "Model Three",
        ]
        assert [value for _, value in select._options] == ["m1", "m2", "m3"]
        assert select.value == "m1"


async def test_selector_starts_on_the_requested_model() -> None:
    factory = Factory(REPLY)
    app = make_app(factory, model=MODEL_2)
    async with app.run_test() as pilot:
        assert app.query_one("#model", Select).value == "m2"
        await send(pilot, app, "hi")
        assert factory.providers[0].requests[-1].model == "provider-model-2"


async def test_selection_with_empty_history_applies_immediately() -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        app.query_one("#model", Select).value = "m2"
        await pilot.pause()
        await send(pilot, app, "hi")
        assert len(factory.providers) == 2
        assert factory.providers[0].close_calls == 1
        assert factory.providers[1].requests[-1].model == "provider-model-2"


async def test_selection_with_history_waits_for_ctrl_n() -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "first")
        app.query_one("#model", Select).value = "m2"
        await pilot.pause()
        assert texts(app, "note") == ["applies to the next new chat (Ctrl+N)"]
        assert len(factory.providers) == 1

        await send(pilot, app, "second")
        assert factory.providers[0].requests[-1].model == "provider-model-1"

        await pilot.press("ctrl+n")
        await pilot.pause()
        assert len(factory.providers) == 2
        assert factory.providers[0].close_calls == 1
        assert reply_texts(app) == []
        await send(pilot, app, "third")
        request = factory.providers[1].requests[-1]
        assert request.model == "provider-model-2"
        assert [m.content for m in request.messages] == ["third"]


async def test_config_error_on_selection_is_shown_and_session_keeps_working() -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        app.query_one("#model", Select).value = "m3"
        await pilot.pause()
        assert texts(app, "error") == [
            "error: environment variable MISSING_KEY is not set"
        ]
        assert app.query_one("#model", Select).value == "m1"
        await send(pilot, app, "hi")
        assert factory.providers[0].requests[-1].model == "provider-model-1"
        assert factory.providers[0].close_calls == 0


async def test_config_error_on_new_chat_keeps_the_current_chat() -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "first")
        app.query_one("#model", Select).value = "m3"
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert texts(app, "error") == [
            "error: environment variable MISSING_KEY is not set"
        ]
        assert reply_texts(app) == ["Hello"]
        await send(pilot, app, "second")
        assert [m.content for m in factory.providers[0].requests[-1].messages] == [
            "first",
            "Hello",
            "second",
        ]


async def test_failed_initial_model_can_be_replaced_by_a_working_one() -> None:
    factory = Factory(REPLY)
    app = make_app(factory, model=MODEL_3)
    async with app.run_test() as pilot:
        assert len(texts(app, "error")) == 1
        await send(pilot, app, "hi")
        assert texts(app, "user") == []
        assert "no working model" in texts(app, "error")[-1]
        app.query_one("#model", Select).value = "m1"
        await pilot.pause()
        await send(pilot, app, "hi")
        assert reply_texts(app) == ["Hello"]


# Unexpected exceptions


def screen_text(app: ChatApp) -> str:
    return "\n".join(w.visual.plain for w in app.screen.query(Static))


async def test_unexpected_stream_exception_is_one_masked_line_and_the_app_goes_on() -> (
    None
):
    factory = FlakyFactory()
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "hello")
        assert texts(app, "error") == [
            f"error (unexpected): RuntimeError: kaboom {REDACTED}"
        ]
        assert CANARY not in screen_text(app)
        assert "Traceback" not in screen_text(app)
        assert prompt_of(app).value == "hello"
        assert prompt_of(app).disabled is False
        assert app._session is not None and app._session.history == []
        await send(pilot, app, "again")
        assert reply_texts(app) == ["par", "Hello"]
        assert prompt_of(app).value == ""
        assert app.return_code is None


async def test_unexpected_provider_factory_exception_keeps_the_current_session() -> (
    None
):
    factory = BoomFactory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "first")
        app.query_one("#model", Select).value = "m3"
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert texts(app, "error") == [
            f"error (unexpected): ValueError: boom {REDACTED}"
        ]
        assert CANARY not in screen_text(app)
        assert "Traceback" not in screen_text(app)
        assert prompt_of(app).disabled is False
        await send(pilot, app, "second")
        assert [m.content for m in factory.providers[0].requests[-1].messages] == [
            "first",
            "Hello",
            "second",
        ]
        assert factory.providers[0].close_calls == 0


async def test_unexpected_factory_exception_on_the_first_model_is_shown_too() -> None:
    factory = BoomFactory(REPLY)
    app = make_app(factory, model=MODEL_3)
    async with app.run_test():
        assert texts(app, "error") == [
            f"error (unexpected): ValueError: boom {REDACTED}"
        ]
        assert CANARY not in screen_text(app)


# Model names are literal text

NAMES = ["GPT [beta]", "[/]", "[bold]x[/bold]"]


def config_with_names(names: list[str]) -> HarnessConfig:
    return HarnessConfig.model_validate(
        {
            "providers": {"local": {"type": "ollama", "base_url": "http://x"}},
            "models": [
                {
                    "id": f"id{i}",
                    "name": name,
                    "provider": "local",
                    "model": "m",
                    "tags": [],
                    "priority": 1,
                }
                for i, name in enumerate(names)
            ],
        }
    )


@pytest.mark.parametrize("initial", range(len(NAMES)))
async def test_model_names_are_literal_text_not_markup(initial: int) -> None:
    config = config_with_names(NAMES)
    app = ChatApp(
        config,
        config.models[initial],
        router_factory_for(config, Factory(REPLY)),
        False,
    )
    async with app.run_test() as pilot:
        select = app.query_one("#model", Select)
        for index, name in enumerate(NAMES):
            select.value = f"id{index}"
            await pilot.pause()
            assert select.query_one("#label", Static).visual.plain == name
        prompts = [
            option.prompt for option in select.query_one("SelectOverlay").options
        ]
        assert all(isinstance(prompt, Text) for prompt in prompts)
        assert [prompt.plain for prompt in prompts] == NAMES


# Fallback notes, the usage line and the manual flag

FALLBACK_CONFIG = HarnessConfig.model_validate(
    {
        "providers": {
            "dead": {"type": "ollama", "base_url": "http://dead.invalid:1"},
            "local": {"type": "ollama", "base_url": "http://local.invalid:2"},
        },
        "models": [
            {
                "id": "dead-model",
                "name": "Dead",
                "provider": "dead",
                "model": "dead-id",
                "tags": ["default", "chat"],
                "priority": 200,
            },
            {
                "id": "live-model",
                "name": "Live",
                "provider": "local",
                "model": "live-id",
                "tags": ["chat", "default"],
                "priority": 100,
                "price": {"input_per_mtok": 1000, "output_per_mtok": 2000},
            },
        ],
    }
)
DEAD_MODEL = FALLBACK_CONFIG.models[0]


class FallbackFactory:
    """`dead` fails to connect; `local` answers with 3 in / 7 out tokens."""

    def __init__(self) -> None:
        self.dead = RecordingProvider(
            [ErrorEvent(kind="connection", message="refused")]
        )
        self.live = RecordingProvider(
            [
                TextDelta(text="pong"),
                UsageEvent(usage=Usage(input_tokens=3, output_tokens=7)),
                Done(),
            ]
        )

    def __call__(self, config: ProviderConfig) -> RecordingProvider:
        return self.dead if (config.base_url or "").endswith(":1") else self.live


def fallback_app(factory: FallbackFactory) -> ChatApp:
    router_factory = router_factory_for(FALLBACK_CONFIG, factory)
    return ChatApp(FALLBACK_CONFIG, DEAD_MODEL, router_factory, False)


async def test_a_fallback_shows_the_note_the_reply_and_the_usage_line() -> None:
    app = fallback_app(FallbackFactory())
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        assert texts(app, "note") == [
            "dead-model unavailable (connection); using live-model"
        ]
        assert reply_texts(app) == ["pong"]
        (line,) = texts(app, "usage")
        assert line.startswith("[live-model tokens in=3 out=7 $0.02 ")
        assert line.endswith("s | session $0.02]")
        assert texts(app, "error") == []


async def test_a_second_turn_skips_the_model_in_backoff_when_a_store_is_used(
    tmp_path: Path,
) -> None:
    factory = FallbackFactory()
    store = Store(tmp_path / "harness.db")
    router_factory = router_factory_for(FALLBACK_CONFIG, factory, None, store)
    app = ChatApp(FALLBACK_CONFIG, DEAD_MODEL, router_factory, False)
    async with app.run_test() as pilot:
        await send(pilot, app, "one")
        await send(pilot, app, "two")
        assert texts(app, "note") == [
            "dead-model unavailable (connection); using live-model",
            "dead-model is in backoff; using live-model",
        ]
        assert [line.rsplit("| ", 1)[1] for line in texts(app, "usage")] == [
            "session $0.02]",
            "session $0.03]",
        ]
        assert len(factory.dead.requests) == 1


async def test_an_unusable_database_notice_is_shown_once_as_a_note(
    tmp_path: Path,
) -> None:
    (tmp_path / "harness.db").mkdir()  # a directory where the file should be
    router_factory = router_factory_for(
        CONFIG, Factory(REPLY), None, Store(tmp_path / "harness.db")
    )
    app = ChatApp(CONFIG, MODEL_1, router_factory, False)
    async with app.run_test() as pilot:
        await send(pilot, app, "one")
        await send(pilot, app, "two")
        (note,) = texts(app, "note")
        assert note.startswith("usage database unavailable (")
        assert reply_texts(app) == ["Hello", "Hello"]


async def test_quit_closes_every_provider_the_router_built() -> None:
    factory = FallbackFactory()
    app = fallback_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        await pilot.press("ctrl+q")
    assert (factory.dead.close_calls, factory.live.close_calls) == (1, 1)


async def test_the_default_model_is_automatic_and_a_selector_change_is_manual() -> None:
    router_factory = router_factory_for(CONFIG, Factory(REPLY))
    app = ChatApp(CONFIG, MODEL_1, router_factory, False)
    async with app.run_test() as pilot:
        assert router_factory.calls == [("m1", False)]
        await send(pilot, app, "hi")
        await pilot.press("ctrl+n")  # same model: the flag is kept
        await pilot.pause()
        assert router_factory.calls[-1] == ("m1", False)

        app.query_one("#model", Select).value = "m2"  # empty history: applies now
        await pilot.pause()
        assert router_factory.calls[-1] == ("m2", True)

        await send(pilot, app, "hi")
        app.query_one("#model", Select).value = "m1"  # waits for the next new chat
        await pilot.pause()
        assert len(router_factory.calls) == 3
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert router_factory.calls[-1] == ("m1", True)
        await pilot.press("ctrl+n")  # no selector change: the flag is kept
        await pilot.pause()
        assert router_factory.calls[-1] == ("m1", True)


async def test_a_model_given_on_the_command_line_stays_manual_on_new_chat() -> None:
    router_factory = router_factory_for(CONFIG, Factory(REPLY))
    app = ChatApp(CONFIG, MODEL_1, router_factory, True)
    async with app.run_test() as pilot:
        assert router_factory.calls == [("m1", True)]
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert router_factory.calls[-1] == ("m1", True)


# Quit


async def test_quit_closes_the_active_provider() -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+q")
    assert factory.providers[0].close_calls == 1


@pytest.mark.parametrize("command", ["/quit", " /QUIT ", "/exit"])
async def test_quit_command_quits_without_a_model_call(command: str) -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        prompt_of(app).value = command
        await pilot.press("enter")
        await pilot.pause()
    assert app.return_code == 0
    assert factory.providers[0].requests == []
    assert factory.providers[0].close_calls == 1


async def test_quit_command_while_a_turn_runs_cancels_it_and_quits() -> None:
    factory = Factory(REPLY, hang_after=0)
    app = make_app(factory)
    async with app.run_test() as pilot:
        prompt_of(app).value = "hi"
        await pilot.press("enter")
        await wait_for(pilot, lambda: prompt_of(app).disabled)
        # The input is disabled during a turn, so the message is posted directly.
        prompt = prompt_of(app)
        prompt.post_message(Input.Submitted(prompt, "/quit"))
        await pilot.pause()
    assert app.return_code == 0
    assert len(factory.providers[0].requests) == 1
    assert factory.providers[0].closed is True
    assert factory.providers[0].close_calls == 1


async def test_quit_command_is_not_added_to_the_history() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        prompt_of(app).value = "/quit"
        await pilot.press("enter")
        await pilot.pause()
        assert app._session is not None
        assert [m.content for m in app._session.history] == ["hi", "Hello"]
        assert texts(app, "user") == ["you: hi"]


async def test_text_that_merely_contains_quit_is_a_normal_message() -> None:
    factory = Factory(REPLY)
    app = make_app(factory)
    async with app.run_test() as pilot:
        await send(pilot, app, "/quit now")
        assert texts(app, "user") == ["you: /quit now"]
        assert len(factory.providers[0].requests) == 1


async def test_the_placeholder_mentions_the_quit_command() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test():
        assert "/quit" in prompt_of(app).placeholder


async def test_footer_shows_the_key_bindings() -> None:
    app = make_app(Factory(REPLY))
    async with app.run_test() as pilot:
        await pilot.pause()
        shown = {
            active.binding.key
            for active in app.active_bindings.values()
            if active.binding.show
        }
        assert {"escape", "ctrl+n", "ctrl+q"} <= shown


# Markdown in the reply


@dataclass
class Rendered:
    reply: list[str]
    cells: list[str]
    fences: list[tuple[str, str]]
    errors: list[str]


async def render_reply(text: str, *, deltas: list[str] | None = None) -> Rendered:
    """Run one turn whose reply is `text` (or `deltas`) and read what the log shows."""
    events = [TextDelta(text=d) for d in (deltas or [text])] + [Done()]
    app = make_app(Factory(events))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        await pilot.pause()
        return Rendered(
            reply=reply_texts(app),
            cells=[
                w.visual.plain for w in app.query("#log Markdown.reply .cell, .header")
            ],
            fences=[(w.code, w.lexer) for w in app.query(MarkdownFence)],
            errors=texts(app, "error"),
        )


async def test_the_reply_is_a_markdown_widget_and_the_rest_stays_plain_static() -> None:
    events = [
        ThinkingDelta(text="t"),
        TextDelta(text="**hi**"),
        Done(),
    ]
    app = make_app(Factory(events))
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        (reply,) = app.query("#log .reply")
        assert isinstance(reply, Markdown)
        assert reply.source == "**hi**"
        assert reply_texts(app) == ["hi"]
        for css_class in ("user", "thinking"):
            (plain,) = app.query(f"#log .{css_class}")
            assert type(plain) is Static
            assert not isinstance(plain, Markdown)


async def test_a_table_renders_as_cells() -> None:
    r = await render_reply("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert r.cells == ["a", "b", "1", "2"]


async def test_a_code_fence_renders_as_a_fence_block_with_its_language() -> None:
    r = await render_reply("```python\nx = 1\n```\n")
    assert r.fences == [("x = 1", "python")]


async def test_strikethrough_and_autolink_text_render() -> None:
    r = await render_reply("~~gone~~ see https://a.example/x")
    assert r.reply == ["gone see https://a.example/x"]


async def test_rich_markup_and_raw_html_in_the_reply_stay_literal() -> None:
    text = (
        "[bold red]x[/] <b>y</b> <script>alert(1)</script> [link=http://evil]z[/link]"
    )
    r = await render_reply(text)
    assert r.reply == [text]


async def test_raw_html_in_a_code_fence_stays_literal() -> None:
    r = await render_reply("```\n<script>alert(1)</script>\n```\n")
    assert r.fences == [("<script>alert(1)</script>", "")]


async def test_control_characters_are_stripped_before_markdown_is_parsed() -> None:
    r = await render_reply("**a\x1b[31mb\x07**\u202ec")
    assert r.reply == ["a[31mbc"]


async def test_footnotes_and_task_lists_do_not_crash_the_terminal_reply() -> None:
    # Textual's widget cannot show footnotes; the parser must leave them as text.
    text = "a[^1]\n\n[^1]: note\n\n- [x] done\n"
    r = await render_reply(text)
    assert r.errors == []
    assert "[x] done" in r.reply[0]


async def test_an_image_shows_its_alt_text_and_nothing_is_fetched() -> None:
    r = await render_reply("![the alt](http://evil.example/p.png?q=secret)")
    assert r.errors == []
    assert "the alt" in r.reply[0]


async def test_unfinished_fences_tables_and_emphasis_do_not_raise() -> None:
    for text in ("```py\nx = ", "| a | b |\n|---|", "**bold and *it", "[x](http://a"):
        r = await render_reply(text)
        assert r.errors == [], text


async def test_streaming_deltas_end_rendered() -> None:
    r = await render_reply("", deltas=["**bo", "ld**"])
    assert r.reply == ["bold"]


async def test_a_link_in_the_reply_opens_nothing_on_render_or_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(ChatApp, "open_url", lambda self, url, **kw: opened.append(url))
    app = make_app(
        Factory([TextDelta(text="[x](https://e.example) https://a.example"), Done()])
    )
    async with app.run_test() as pilot:
        await send(pilot, app, "hi")
        (reply,) = app.query("#log Markdown.reply")
        assert opened == []
        reply.post_message(Markdown.LinkClicked(reply, "https://e.example"))
        await pilot.pause()
        assert opened == []
