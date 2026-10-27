"""Web GUI tests: NiceGUI's `user` simulation drives the page with a scripted provider.

The simulation has no browser, so these tests prove page logic and what the
server sends (element types and text props), not how the page looks.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from nicegui import ElementFilter, app, ui
from nicegui.elements.mixins.content_element import ContentElement
from nicegui.testing import User

import llm_harness.gui.app as gui_app
from llm_harness.config import ConfigError, HarnessConfig, ProviderConfig
from llm_harness.gui.app import register_page, run_gui
from llm_harness.gui.security import LoopbackOnlyMiddleware
from llm_harness.markdown_render import sanitize_html
from llm_harness.redact import REDACTED
from llm_harness.session import ChatSession
from llm_harness.store import Store
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
MISSING_KEY_MESSAGE = "environment variable MISSING_KEY is not set"


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
            raise ConfigError(MISSING_KEY_MESSAGE)
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


@pytest.fixture(autouse=True)
def no_leftover_pages() -> None:
    """Pages of earlier tests are never deleted; start each test with none open."""
    gui_app._open_pages.clear()


@pytest.fixture
def middleware_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict]]:
    """Stand-in for `app.add_middleware`: the shared test app has already started."""
    calls: list[tuple[Any, dict]] = []
    monkeypatch.setattr(
        app, "add_middleware", lambda cls, **kwargs: calls.append((cls, kwargs))
    )
    return calls


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[ChatSession]:
    """Every ChatSession the page builds, in creation order."""
    created: list[ChatSession] = []

    class RecordingSession(ChatSession):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(gui_app, "ChatSession", RecordingSession)
    return created


async def wait_for(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def open_chat(
    user: User,
    factory: Callable[[ProviderConfig], Any],
    *,
    model=MODEL_1,
    think_flag: bool | None = None,
    is_manual: bool = False,
) -> None:
    register_page(
        CONFIG, model, router_factory_for(CONFIG, factory, think_flag), is_manual
    )
    await user.open("/")


def elements(user: User, marker: str) -> list[Any]:
    with user.client:
        return list(ElementFilter(marker=marker, local_scope=False))


def texts(user: User, marker: str) -> list[str]:
    return [element.text for element in elements(user, marker)]


def replies(user: User) -> list[str]:
    """The HTML each reply element sends to the browser (after our sanitizer)."""
    return [element.props["innerHTML"] for element in elements(user, "reply")]


def para(text: str) -> str:
    return f"<p>{text}</p>\n"


def widget(user: User, marker: str) -> Any:
    (element,) = elements(user, marker)
    return element


def log_texts(user: User) -> list[str]:
    return [
        child.props["innerHTML"] if isinstance(child, ui.html) else child.text
        for child in widget(user, "log").default_slot.children
    ]


async def send(user: User, text: str) -> None:
    """Type `text`, press Enter and wait until the turn has finished."""
    echoes = len(texts(user, "user"))
    user.find(marker="prompt").type(text).trigger("keydown.enter")
    await wait_for(lambda: len(texts(user, "user")) == echoes + 1)
    await wait_for(lambda: widget(user, "prompt").enabled)


def choose(user: User, model_id: str) -> None:
    with user.client:
        widget(user, "model").value = model_id


# Streaming and display


async def test_streamed_reply_appears(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    await send(user, "hi")
    assert texts(user, "user") == ["you: hi"]
    assert replies(user) == [para("Hello")]
    assert widget(user, "prompt").value == ""


async def test_page_has_a_title_and_the_controls(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    await user.should_see("llm-harness")
    for marker in ("model", "log", "prompt", "send", "stop", "new-chat"):
        assert len(elements(user, marker)) == 1


async def test_layout_uses_the_full_width_and_the_message_area_fills_the_height(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    page, log = widget(user, "page"), widget(user, "log")
    assert "w-full" in page.classes
    assert not [c for c in page.classes if c.startswith("max-w")]
    assert "mx-auto" not in page.classes
    assert "grow" in log.classes and "w-full" in log.classes
    assert not [c for c in log.classes if c.startswith(("h-", "max-h"))]


async def test_input_row_is_below_the_message_area_and_full_width(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    page = widget(user, "page")
    with user.client:
        order = [e for e in page.descendants()]
    log, prompt = widget(user, "log"), widget(user, "prompt")
    assert order.index(log) < order.index(prompt)
    assert "w-full" in prompt.parent_slot.parent.classes
    assert "w-full" in widget(user, "model").classes


async def test_blank_input_is_ignored(user: User) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    user.find(marker="prompt").type("   ").trigger("keydown.enter")
    await asyncio.sleep(0.1)
    assert factory.providers[0].requests == []
    assert texts(user, "user") == []


async def test_send_button_sends_the_input(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    user.find(marker="prompt").type("hi")
    user.find(marker="send").click()
    await wait_for(lambda: replies(user) == [para("Hello")])


async def test_thinking_is_in_a_collapsed_expansion_not_in_the_reply(
    user: User,
) -> None:
    factory = Factory([ThinkingDelta(text="pond"), ThinkingDelta(text="ering"), *REPLY])
    await open_chat(user, factory)
    await send(user, "hi")
    with user.client:
        (expansion,) = ElementFilter(kind=ui.expansion, local_scope=False)
    assert expansion.props["label"] == "Thinking"
    assert expansion.value is False
    (thinking,) = elements(user, "thinking")
    assert thinking.parent_slot.parent is expansion
    assert thinking.text == "pondering"
    assert replies(user) == [para("Hello")]
    assert "pondering" not in log_texts(user)[1:]


async def test_no_expansion_without_thinking_deltas(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    await send(user, "hi")
    await user.should_not_see(kind=ui.expansion)


async def test_usage_line_follows_the_reply(user: User) -> None:
    usage = UsageEvent(usage=Usage(input_tokens=3, output_tokens=7))
    await open_chat(user, Factory([*REPLY[:-1], usage, REPLY[-1]]))
    await send(user, "hi")
    echo, reply, usage_line = log_texts(user)
    assert (echo, reply) == ("you: hi", para("Hello"))
    assert usage_line.startswith("[m1 tokens in=3 out=7 $0.00 ")
    assert usage_line.endswith("s | session $0.00]")


async def test_error_event_shows_an_error_block_and_restores_the_input(
    user: User, sessions: list[ChatSession]
) -> None:
    factory = Factory(
        [TextDelta(text="par"), ErrorEvent(kind="connection", message="boom")]
    )
    await open_chat(user, factory)
    await send(user, "hi")
    assert texts(user, "error") == ["error (connection): boom"]
    assert widget(user, "prompt").value == "hi"
    assert widget(user, "prompt").enabled
    assert sessions[0].history == []


async def test_stop_cancels_the_turn_and_restores_the_input(
    user: User, sessions: list[ChatSession]
) -> None:
    factory = Factory([TextDelta(text="par"), *REPLY], hang_after=1)
    await open_chat(user, factory)
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: replies(user) == [para("par")])
    user.find(marker="stop").click()
    await wait_for(lambda: widget(user, "prompt").enabled)
    assert texts(user, "note") == ["cancelled"]
    assert widget(user, "prompt").value == "hi"
    assert not widget(user, "stop").enabled
    assert sessions[0].history == []
    assert factory.providers[0].closed


async def test_controls_are_disabled_during_a_turn_and_enabled_after(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY, hang_after=0))
    assert not widget(user, "stop").enabled
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: not widget(user, "prompt").enabled)
    assert not widget(user, "send").enabled
    assert not widget(user, "new-chat").enabled
    assert widget(user, "stop").enabled
    user.find(marker="stop").click()
    await wait_for(lambda: widget(user, "prompt").enabled)
    assert widget(user, "send").enabled
    assert not widget(user, "stop").enabled


async def test_input_is_enabled_again_after_a_normal_turn(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    await send(user, "hi")
    assert widget(user, "prompt").enabled
    assert widget(user, "send").enabled
    assert not widget(user, "stop").enabled


# History and the think flag


async def test_turn_two_carries_the_whole_history(user: User) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    await send(user, "one")
    await send(user, "two")
    messages = factory.providers[0].requests[-1].messages
    assert [(m.role, m.content) for m in messages] == [
        ("user", "one"),
        ("assistant", "Hello"),
        ("user", "two"),
    ]


async def test_thinking_is_not_added_to_history(
    user: User, sessions: list[ChatSession]
) -> None:
    await open_chat(user, Factory([ThinkingDelta(text="secret"), *REPLY]))
    await send(user, "hi")
    assert [m.content for m in sessions[0].history] == ["hi", "Hello"]


@pytest.mark.parametrize("flag", [None, True, False])
async def test_the_think_flag_reaches_the_request(
    user: User, flag: bool | None
) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory, think_flag=flag)
    await send(user, "hi")
    assert factory.providers[0].requests[-1].think is flag


async def test_the_model_thinking_setting_applies_without_a_flag(user: User) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory, model=MODEL_1.model_copy(update={"thinking": True}))
    await send(user, "hi")
    assert factory.providers[0].requests[-1].think is True


# Model selector


async def test_selector_lists_exactly_the_models_yaml_names(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    select = widget(user, "model")
    assert select.options == {m.id: m.name for m in CONFIG.models}
    assert list(select.options.values()) == ["Model One", "Model Two", "Model Three"]
    assert select.value == "m1"


async def test_selector_starts_on_the_initial_model(user: User) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory, model=MODEL_2)
    assert widget(user, "model").value == "m2"
    await send(user, "hi")
    assert factory.providers[0].requests[-1].model == "provider-model-2"


async def test_selector_with_empty_history_swaps_the_session_immediately(
    user: User,
) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    choose(user, "m2")
    await wait_for(lambda: len(factory.providers) == 2)
    assert factory.providers[0].close_calls == 1
    assert factory.providers[1].close_calls == 0
    await send(user, "hi")
    assert factory.providers[0].requests == []
    assert factory.providers[1].requests[-1].model == "provider-model-2"
    assert user.notify.messages == []


async def test_selector_with_history_notifies_and_keeps_the_session_until_new_chat(
    user: User,
) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    await send(user, "one")
    choose(user, "m2")
    await wait_for(lambda: "applies to the next new chat" in user.notify.messages)
    assert len(factory.providers) == 1
    await send(user, "two")
    assert factory.providers[0].requests[-1].model == "provider-model-1"
    assert len(factory.providers[0].requests[-1].messages) == 3

    user.find(marker="new-chat").click()
    await wait_for(lambda: len(factory.providers) == 2)
    assert factory.providers[0].close_calls == 1
    assert texts(user, "user") == [] and replies(user) == []
    await send(user, "three")
    request = factory.providers[1].requests[-1]
    assert request.model == "provider-model-2"
    assert [m.content for m in request.messages] == ["three"]


async def test_new_chat_with_the_same_model_starts_a_fresh_history(user: User) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    await send(user, "one")
    user.find(marker="new-chat").click()
    await wait_for(lambda: len(factory.providers) == 2)
    await send(user, "two")
    assert [m.content for m in factory.providers[1].requests[-1].messages] == ["two"]


async def test_config_error_on_selector_change_is_shown_and_reverted(
    user: User,
) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    choose(user, "m3")
    await wait_for(lambda: texts(user, "error") == [f"error: {MISSING_KEY_MESSAGE}"])
    await wait_for(lambda: widget(user, "model").value == "m1")
    assert len(factory.providers) == 1
    assert factory.providers[0].close_calls == 0
    await send(user, "hi")
    assert factory.providers[0].requests[-1].model == "provider-model-1"


async def test_config_error_on_new_chat_keeps_the_session_and_the_log(
    user: User,
) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    await send(user, "one")
    choose(user, "m3")
    await wait_for(lambda: "applies to the next new chat" in user.notify.messages)
    user.find(marker="new-chat").click()
    await wait_for(lambda: texts(user, "error") == [f"error: {MISSING_KEY_MESSAGE}"])
    assert texts(user, "user") == ["you: one"]
    assert factory.providers[0].close_calls == 0
    await send(user, "two")
    assert len(factory.providers[0].requests[-1].messages) == 3


async def test_a_failing_initial_model_shows_an_error_and_recovers(user: User) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory, model=MODEL_3)
    assert texts(user, "error") == [f"error: {MISSING_KEY_MESSAGE}"]
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: len(texts(user, "error")) == 2)
    assert texts(user, "error")[1].startswith("no working model")
    choose(user, "m1")
    await wait_for(lambda: len(factory.providers) == 1)
    await send(user, "hi")
    assert replies(user) == [para("Hello")]


# Unexpected exceptions


async def test_unexpected_stream_exception_is_one_masked_line_and_the_page_goes_on(
    user: User, sessions: list[ChatSession]
) -> None:
    factory = FlakyFactory()
    await open_chat(user, factory)
    await send(user, "hello")
    assert texts(user, "error") == [
        f"error (unexpected): RuntimeError: kaboom {REDACTED}"
    ]
    assert all(CANARY not in text for text in log_texts(user))
    assert "Traceback" not in " ".join(log_texts(user))
    assert widget(user, "prompt").value == "hello"
    assert widget(user, "prompt").enabled
    assert widget(user, "send").enabled
    assert sessions[0].history == []
    await send(user, "again")
    assert replies(user) == [para("par"), para("Hello")]
    assert widget(user, "prompt").value == ""


async def test_unexpected_provider_factory_exception_keeps_the_current_session(
    user: User,
) -> None:
    factory = BoomFactory(REPLY)
    await open_chat(user, factory)
    await send(user, "first")
    choose(user, "m3")
    await wait_for(lambda: "applies to the next new chat" in user.notify.messages)
    user.find(marker="new-chat").click()
    await wait_for(
        lambda: (
            texts(user, "error") == [f"error (unexpected): ValueError: boom {REDACTED}"]
        )
    )
    assert all(CANARY not in text for text in log_texts(user))
    assert texts(user, "user") == ["you: first"]
    assert widget(user, "prompt").enabled
    assert factory.providers[0].close_calls == 0
    await send(user, "second")
    assert len(factory.providers[0].requests[-1].messages) == 3


async def test_unexpected_factory_exception_on_the_first_model_is_shown_too(
    user: User,
) -> None:
    await open_chat(user, BoomFactory(REPLY), model=MODEL_3)
    assert texts(user, "error") == [f"error (unexpected): ValueError: boom {REDACTED}"]


# Model names are plain text in the selector

HTML_NAMES = ["<b>x</b>", "<img src=x onerror=alert(1)>"]


async def test_model_names_are_passed_to_the_select_as_plain_text(user: User) -> None:
    # Quasar's QSelect sets innerHTML only for an option with `html: true` or
    # with the `options-html`/`display-value-html` props (quasar.umd.js, QSelect).
    config = HarnessConfig.model_validate(
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
                for i, name in enumerate(HTML_NAMES)
            ],
        }
    )
    register_page(
        config, config.models[0], router_factory_for(config, Factory(REPLY)), False
    )
    await user.open("/")
    select = widget(user, "model")
    assert select.props["options"] == [
        {"value": index, "label": name} for index, name in enumerate(HTML_NAMES)
    ]
    assert not {"options-html", "display-value-html"} & set(select.props)


# Escaping and sanitizing


SCRIPT = "<script>alert(1)</script>"
BOLD = "<b>x</b>"


async def test_only_the_reply_uses_html_and_it_has_our_own_sanitizer(
    user: User,
) -> None:
    factory = Factory(
        [
            ThinkingDelta(text=BOLD),
            TextDelta(text=SCRIPT),
            TextDelta(text=BOLD),
            Done(finish_reason="stop"),
        ]
    )
    await open_chat(user, factory)
    await send(user, SCRIPT)
    assert texts(user, "user") == [f"you: {SCRIPT}"]
    assert texts(user, "thinking") == [BOLD]
    with user.client:
        content_elements = [
            e for e in user.client.elements.values() if isinstance(e, ContentElement)
        ]
        (reply,) = elements(user, "reply")
        assert content_elements == [reply]
        assert isinstance(reply, ui.html)
        assert not isinstance(reply, ui.markdown)
        # Not True (browser-side DOMPurify) and not False (no sanitizing).
        assert reply._sanitize is sanitize_html
        assert reply.props["sanitize"] is False
        assert all(
            isinstance(e, ui.label) and "innerHTML" not in e.props
            for e in (*elements(user, "user"), *elements(user, "thinking"))
        )
    await user.should_not_see(kind=ui.markdown)


async def test_usage_line_stays_a_plain_label(user: User) -> None:
    usage = UsageEvent(usage=Usage(input_tokens=3, output_tokens=7))
    events = [TextDelta(text="**x**"), usage, Done(finish_reason="stop")]
    await open_chat(user, Factory(events))
    await send(user, "hi")
    (line,) = elements(user, "usage")
    assert isinstance(line, ui.label)


async def test_error_line_stays_a_plain_label(user: User) -> None:
    await open_chat(user, Factory([ErrorEvent(kind="bad_request", message="**e**")]))
    await send(user, "hi")
    (line,) = elements(user, "error")
    assert isinstance(line, ui.label)
    assert line.text == "error (bad_request): **e**"


async def test_raw_html_in_the_reply_is_shown_as_literal_text(user: User) -> None:
    factory = Factory(
        [TextDelta(text=SCRIPT), TextDelta(text=BOLD), Done(finish_reason="stop")]
    )
    await open_chat(user, factory)
    await send(user, "hi")
    (html,) = replies(user)
    assert html == para("&lt;script&gt;alert(1)&lt;/script&gt;&lt;b&gt;x&lt;/b&gt;")
    assert "<script" not in html and "<b>" not in html


async def test_markdown_in_the_reply_is_rendered(user: User) -> None:
    text = "**bold** and a table\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    await open_chat(user, Factory([TextDelta(text=text), Done(finish_reason="stop")]))
    await send(user, "hi")
    (html,) = replies(user)
    assert "<strong>bold</strong>" in html
    assert "<table>" in html and "<td>2</td>" in html


async def test_the_reply_container_is_full_width_and_marked_for_the_markdown_css(
    user: User,
) -> None:
    await open_chat(user, Factory(REPLY))
    await send(user, "hi")
    reply = widget(user, "reply")
    assert "w-full" in reply.classes and "llm-md" in reply.classes
    assert not [c for c in reply.classes if c.startswith(("max-w", "h-", "max-h"))]


async def test_the_markdown_css_scrolls_wide_content_inside_its_box() -> None:
    css = gui_app._MARKDOWN_CSS
    assert "overflow-x: auto" in css
    assert ".llm-md pre" in css and ".llm-md table" in css
    assert ".llm-md .k {" in css or ".llm-md .k{" in css  # Pygments token colours
    assert "max-height" not in css and "max-width: 4" not in css


async def test_two_replies_in_a_row_keep_separate_elements(user: User) -> None:
    await open_chat(user, Factory([TextDelta(text="**one**"), Done()]))
    await send(user, "a")
    await send(user, "b")
    assert replies(user) == ["<p><strong>one</strong></p>\n"] * 2
    assert len(set(map(id, elements(user, "reply")))) == 2


async def test_streamed_partial_markdown_ends_rendered(user: User) -> None:
    events = [TextDelta(text="**bo"), TextDelta(text="ld**"), Done()]
    await open_chat(user, Factory(events))
    await send(user, "hi")
    assert replies(user) == ["<p><strong>bold</strong></p>\n"]


async def test_unfinished_markdown_in_the_reply_does_not_break_the_turn(
    user: User,
) -> None:
    text = "```py\nx = \n\n| a | b |\n|---|\n**bold *it"
    await open_chat(user, Factory([TextDelta(text=text), Done()]))
    await send(user, "hi")
    assert len(replies(user)) == 1
    assert texts(user, "error") == []
    assert widget(user, "prompt").enabled


async def test_an_image_in_the_reply_degrades_to_its_alt_text(user: User) -> None:
    text = "![the alt](http://evil.example/p.png?q=secret)"
    await open_chat(user, Factory([TextDelta(text=text), Done()]))
    await send(user, "hi")
    (html,) = replies(user)
    assert html == para("the alt")


async def test_error_text_is_shown_literally(user: User) -> None:
    factory = Factory([ErrorEvent(kind="bad_request", message=f"{SCRIPT}{BOLD}")])
    await open_chat(user, factory)
    await send(user, "hi")
    assert texts(user, "error") == [f"error (bad_request): {SCRIPT}{BOLD}"]
    with user.client:
        assert [
            e for e in user.client.elements.values() if isinstance(e, ContentElement)
        ] == []


async def test_control_characters_are_stripped_everywhere(user: User) -> None:
    dirty = "a\x1b[31mb\x07c\x7fd\x9be"
    clean = "a[31mbcde"
    factory = Factory(
        [ThinkingDelta(text=dirty), TextDelta(text=dirty), Done(finish_reason="stop")]
    )
    await open_chat(user, factory)
    await send(user, dirty)
    assert texts(user, "user") == [f"you: {clean}"]
    assert replies(user) == [para(clean)]
    assert texts(user, "thinking") == [clean]


async def test_control_characters_are_stripped_from_error_messages(user: User) -> None:
    dirty = "a\x1b[31mb\x07c"
    await open_chat(user, Factory([ErrorEvent(kind="bad_request", message=dirty)]))
    await send(user, "hi")
    assert texts(user, "error") == ["error (bad_request): a[31mbc"]


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


async def test_a_fallback_shows_the_note_the_reply_and_the_usage_line(
    user: User,
) -> None:
    factory = FallbackFactory()
    router_factory = router_factory_for(FALLBACK_CONFIG, factory)
    register_page(FALLBACK_CONFIG, DEAD_MODEL, router_factory, False)
    await user.open("/")
    await send(user, "hi")
    assert texts(user, "note") == [
        "dead-model unavailable (connection); using live-model"
    ]
    assert replies(user) == [para("pong")]
    (line,) = texts(user, "usage")
    assert line.startswith("[live-model tokens in=3 out=7 $0.02 ")
    assert line.endswith("s | session $0.02]")
    assert texts(user, "error") == []
    for marker in ("note", "usage"):
        (label,) = elements(user, marker)
        assert isinstance(label, ui.label)
        assert "llm-secondary" in label.classes


async def test_an_unusable_database_notice_is_shown_once_as_a_note(
    user: User, tmp_path: Path
) -> None:
    (tmp_path / "harness.db").mkdir()  # a directory where the file should be
    router_factory = router_factory_for(
        CONFIG, Factory(REPLY), None, Store(tmp_path / "harness.db")
    )
    register_page(CONFIG, MODEL_1, router_factory, False)
    await user.open("/")
    await send(user, "one")
    await send(user, "two")
    (note,) = texts(user, "note")
    assert note.startswith("usage database unavailable (")
    assert replies(user) == [para("Hello"), para("Hello")]


async def test_the_default_model_is_automatic_and_a_selector_change_is_manual(
    user: User,
) -> None:
    factory = Factory(REPLY)
    router_factory = router_factory_for(CONFIG, factory)
    register_page(CONFIG, MODEL_1, router_factory, False)
    await user.open("/")
    assert router_factory.calls == [("m1", False)]
    await send(user, "hi")
    user.find(marker="new-chat").click()  # same model: the flag is kept
    await wait_for(lambda: len(router_factory.calls) == 2)
    assert router_factory.calls[-1] == ("m1", False)

    choose(user, "m2")  # empty history: applies now
    await wait_for(lambda: len(router_factory.calls) == 3)
    assert router_factory.calls[-1] == ("m2", True)

    await send(user, "hi")
    choose(user, "m1")  # waits for the next new chat
    await wait_for(lambda: "applies to the next new chat" in user.notify.messages)
    assert len(router_factory.calls) == 3
    user.find(marker="new-chat").click()
    await wait_for(lambda: len(router_factory.calls) == 4)
    assert router_factory.calls[-1] == ("m1", True)
    user.find(marker="new-chat").click()  # no selector change: the flag is kept
    await wait_for(lambda: len(router_factory.calls) == 5)
    assert router_factory.calls[-1] == ("m1", True)


async def test_a_model_given_on_the_command_line_stays_manual_on_new_chat(
    user: User,
) -> None:
    router_factory = router_factory_for(CONFIG, Factory(REPLY))
    register_page(CONFIG, MODEL_1, router_factory, True)
    await user.open("/")
    assert router_factory.calls == [("m1", True)]
    user.find(marker="new-chat").click()
    await wait_for(lambda: len(router_factory.calls) == 2)
    assert router_factory.calls[-1] == ("m1", True)


async def test_closing_the_page_closes_every_provider_the_router_built(
    user: User,
) -> None:
    factory = FallbackFactory()
    register_page(
        FALLBACK_CONFIG,
        DEAD_MODEL,
        router_factory_for(FALLBACK_CONFIG, factory),
        False,
    )
    await user.open("/")
    await send(user, "hi")
    user.client.delete()
    await wait_for(lambda: factory.dead.close_calls == 1)
    assert factory.live.close_calls == 1


# One session per client


async def test_two_clients_have_separate_sessions_and_histories(
    user: User, create_user: Callable[[], User], sessions: list[ChatSession]
) -> None:
    factory = Factory(REPLY)
    register_page(CONFIG, MODEL_1, router_factory_for(CONFIG, factory), False)
    second = create_user()
    await user.open("/")
    await second.open("/")
    assert len(factory.providers) == 2
    assert len(sessions) == 2 and sessions[0] is not sessions[1]

    await send(user, "from first")
    assert [m.content for m in sessions[0].history] == ["from first", "Hello"]
    assert sessions[1].history == []
    assert texts(second, "user") == []

    await send(second, "from second")
    assert [m.content for m in sessions[1].history] == ["from second", "Hello"]
    assert [m.content for m in sessions[0].history] == ["from first", "Hello"]
    request = factory.providers[1].requests[-1]
    assert [m.content for m in request.messages] == ["from second"]
    assert texts(user, "user") == ["you: from first"]


# Cleanup


async def test_deleting_the_client_closes_its_provider_only(
    user: User, create_user: Callable[[], User]
) -> None:
    factory = Factory(REPLY)
    register_page(CONFIG, MODEL_1, router_factory_for(CONFIG, factory), False)
    second = create_user()
    await user.open("/")
    await second.open("/")
    user.client.delete()
    await wait_for(lambda: factory.providers[0].close_calls == 1)
    assert factory.providers[1].close_calls == 0


async def test_deleting_the_client_during_a_turn_cancels_it_and_closes_the_provider(
    user: User, sessions: list[ChatSession]
) -> None:
    factory = Factory(REPLY, hang_after=0)
    await open_chat(user, factory)
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: not widget(user, "prompt").enabled)
    user.client.delete()
    await wait_for(lambda: factory.providers[0].close_calls == 1)
    assert factory.providers[0].closed
    assert sessions[0].history == []


# /quit


@pytest.fixture
def shutdowns(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Stand-in for `app.shutdown`, so the test process survives."""
    calls: list[bool] = []
    monkeypatch.setattr(app, "shutdown", lambda: calls.append(True))
    return calls


@pytest.mark.parametrize("command", ["/quit", " /QUIT "])
async def test_quit_shuts_down_without_a_model_call(
    user: User, shutdowns: list[bool], sessions: list[ChatSession], command: str
) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    user.find(marker="prompt").type(command).trigger("keydown.enter")
    await wait_for(lambda: shutdowns == [True])
    assert "Shutting down" in user.notify.messages
    assert factory.providers[0].requests == []
    assert sessions[0].history == []
    assert texts(user, "user") == []
    assert widget(user, "prompt").value == ""


async def test_quit_while_a_turn_hangs_cancels_it_then_shuts_down(
    user: User, shutdowns: list[bool], sessions: list[ChatSession]
) -> None:
    factory = Factory(REPLY, hang_after=0)
    await open_chat(user, factory)
    user.find(marker="prompt").type("hi").trigger("keydown.enter")
    await wait_for(lambda: not widget(user, "prompt").enabled)
    (page,) = gui_app._open_pages
    # The input is disabled during a turn, so the handler is called directly.
    with user.client:
        widget(user, "prompt").value = "/quit"
        await page._on_send()
    assert factory.providers[0].closed
    assert shutdowns == [True]
    assert sessions[0].history == []
    assert widget(user, "prompt").value == ""


@pytest.mark.parametrize("text", ["/quit now", "please /quit"])
async def test_text_that_merely_contains_quit_is_a_normal_message(
    user: User, shutdowns: list[bool], text: str
) -> None:
    factory = Factory(REPLY)
    await open_chat(user, factory)
    await send(user, text)
    assert texts(user, "user") == [f"you: {text}"]
    assert len(factory.providers[0].requests) == 1
    assert shutdowns == []


async def test_the_placeholder_mentions_the_quit_command(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    assert widget(user, "prompt").props["placeholder"] == (
        "Message, Enter to send, /quit to exit"
    )


# run_gui


async def test_run_gui_binds_loopback_without_reload(
    user: User, monkeypatch: pytest.MonkeyPatch, middleware_calls: list
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ui, "run", lambda *args, **kwargs: calls.append(kwargs))
    assert (
        run_gui(CONFIG, MODEL_1, router_factory_for(CONFIG, Factory(REPLY)), False) == 0
    )
    (kwargs,) = calls
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8080
    assert kwargs["reload"] is False
    assert kwargs["show"] is True
    assert kwargs["title"] == "llm-harness"
    await user.open("/")
    await user.should_see("llm-harness")


async def test_run_gui_passes_the_given_host_port_and_show(
    user: User, monkeypatch: pytest.MonkeyPatch, middleware_calls: list
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ui, "run", lambda *args, **kwargs: calls.append(kwargs))
    run_gui(
        CONFIG,
        MODEL_1,
        router_factory_for(CONFIG, Factory(REPLY)),
        False,
        host="127.0.0.1",
        port=9123,
        show=False,
    )
    (kwargs,) = calls
    assert (kwargs["host"], kwargs["port"], kwargs["show"]) == (
        "127.0.0.1",
        9123,
        False,
    )


async def test_run_gui_registers_the_loopback_middleware_with_its_port_first(
    user: User, monkeypatch: pytest.MonkeyPatch, middleware_calls: list
) -> None:
    order: list[str] = []
    monkeypatch.setattr(ui, "run", lambda *args, **kwargs: order.append("run"))
    monkeypatch.setattr(
        app,
        "add_middleware",
        lambda cls, **kwargs: (
            order.append("middleware"),
            middleware_calls.append((cls, kwargs)),
        ),
    )
    run_gui(
        CONFIG,
        MODEL_1,
        router_factory_for(CONFIG, Factory(REPLY)),
        False,
        port=9123,
        show=False,
    )
    assert middleware_calls == [(LoopbackOnlyMiddleware, {"port": 9123})]
    assert order == ["middleware", "run"]


async def test_run_gui_returns_130_on_ctrl_c_without_raising(
    user: User, monkeypatch: pytest.MonkeyPatch, middleware_calls: list
) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ui, "run", interrupted)
    assert (
        run_gui(CONFIG, MODEL_1, router_factory_for(CONFIG, Factory(REPLY)), False)
        == 130
    )


async def test_run_gui_registers_the_shutdown_handler(
    user: User, monkeypatch: pytest.MonkeyPatch, middleware_calls: list
) -> None:
    handlers: list[Any] = []
    monkeypatch.setattr(ui, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "on_shutdown", handlers.append)
    run_gui(CONFIG, MODEL_1, router_factory_for(CONFIG, Factory(REPLY)), False)
    assert handlers == [gui_app._close_open_pages]


async def test_the_shutdown_handler_closes_the_providers_of_open_pages(
    user: User, create_user: Callable[[], User]
) -> None:
    factory = Factory(REPLY)
    register_page(CONFIG, MODEL_1, router_factory_for(CONFIG, factory), False)
    second = create_user()
    await user.open("/")
    await second.open("/")
    await gui_app._close_open_pages()
    assert [p.close_calls for p in factory.providers] == [1, 1]
    assert gui_app._open_pages == set()


async def test_a_deleted_client_is_no_longer_tracked(user: User) -> None:
    await open_chat(user, Factory(REPLY))
    assert len(gui_app._open_pages) == 1
    user.client.delete()
    await wait_for(lambda: not gui_app._open_pages)
