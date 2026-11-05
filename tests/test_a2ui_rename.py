"""A createSurface that reuses a surface id is renamed to the first free `<id>_<n>`."""

import copy

import pytest
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

import llm_harness.gui.app as gui_app
from llm_harness.a2ui.demo import DEMO_MESSAGES
from llm_harness.a2ui.validation import SurfaceValidator
from llm_harness.orchestrator import Orchestrator
from llm_harness.types import Done, Notice, StreamEvent, TextDelta, UiSurface
from tests.a2ui_support import components, create, data_model, text
from tests.fake_steps import reply_events
from tests.test_gui import StepFactory, elements, open_chat, texts, wait_for
from tests.test_orchestrator import RESPOND, SET_GOAL, everything, make

NOTE = 'surface "demo" already exists; the new surface is "{}"'


def delete(surface_id: str) -> dict[str, object]:
    return {"version": "1.0", "deleteSurface": {"surfaceId": surface_id}}


def surface_batch(surface_id: str, label: str) -> list[dict[str, object]]:
    return [create(surface_id), components([text("root", label)], surface_id)]


def label_of(validator: SurfaceValidator, surface_id: str) -> object:
    return validator.components(surface_id)["root"]["text"]


# --- the validator -----------------------------------------------------------


def test_a_new_surface_id_is_kept_and_has_no_note() -> None:
    validator = SurfaceValidator()
    outcome = validator.submit(surface_batch("demo", "one"))
    assert outcome.ok
    assert outcome.notes == ()
    assert validator.surface_ids == ("demo",)


def test_a_reused_id_is_renamed_and_the_original_is_untouched() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    outcome = validator.submit(surface_batch("demo", "two"))
    assert outcome.ok, outcome.errors
    assert outcome.notes == (NOTE.format("demo_2"),)
    assert validator.surface_ids == ("demo", "demo_2")
    assert label_of(validator, "demo") == "one"
    assert label_of(validator, "demo_2") == "two"


def test_the_accepted_messages_carry_the_new_id_and_the_input_is_unchanged() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    batch = surface_batch("demo", "two")
    before = copy.deepcopy(batch)
    outcome = validator.submit(batch)
    assert batch == before
    ids = [
        next(iter(body.values()))["surfaceId"]
        for body in (
            {key: value for key, value in message.items() if key != "version"}
            for message in outcome.messages
        )
    ]
    assert ids == ["demo_2", "demo_2"]


def test_repeated_reuse_gives_demo_2_then_demo_3_then_demo_4() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    notes = [
        validator.submit(surface_batch("demo", label)).notes
        for label in ("two", "three", "four")
    ]
    assert notes == [
        (NOTE.format("demo_2"),),
        (NOTE.format("demo_3"),),
        (NOTE.format("demo_4"),),
    ]
    assert validator.surface_ids == ("demo", "demo_2", "demo_3", "demo_4")
    assert label_of(validator, "demo_4") == "four"


def test_a_free_suffix_that_a_model_already_used_is_skipped() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo_2", "mine")).ok
    assert validator.submit(surface_batch("demo", "one")).ok
    outcome = validator.submit(surface_batch("demo", "two"))
    assert outcome.notes == (NOTE.format("demo_3"),)
    assert label_of(validator, "demo_2") == "mine"


def test_later_messages_of_the_same_batch_are_rewritten() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    outcome = validator.submit(
        [
            create("demo"),
            components([text("root", "two")], "demo"),
            data_model({"name": "x"}, "/", "demo"),
            components(
                [
                    {"id": "root", "component": "Column", "children": ["a", "extra"]},
                    text("a", "a"),
                    text("extra", "e"),
                ],
                "demo",
            ),
        ]
    )
    assert outcome.ok, outcome.errors
    assert outcome.notes == (NOTE.format("demo_2"),)
    assert label_of(validator, "demo") == "one"
    assert validator.data_model("demo") != {"name": "x"}
    assert validator.data_model("demo_2") == {"name": "x"}
    assert "extra" in validator.components("demo_2")
    assert "extra" not in validator.components("demo")


def test_a_delete_in_the_same_batch_deletes_the_renamed_surface() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    outcome = validator.submit([*surface_batch("demo", "two"), delete("demo")])
    assert outcome.ok, outcome.errors
    assert validator.surface_ids == ("demo",)
    assert label_of(validator, "demo") == "one"


def test_a_later_batch_uses_the_id_it_was_told() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    assert validator.submit(surface_batch("demo", "two")).ok
    assert validator.submit([components([text("root", "told")], "demo_2")]).ok
    assert label_of(validator, "demo_2") == "told"
    assert label_of(validator, "demo") == "one"
    assert validator.submit([delete("demo_2")]).ok
    assert validator.surface_ids == ("demo",)


def test_a_surface_deleted_earlier_may_be_created_again_under_its_own_id() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    assert validator.submit([delete("demo")]).ok
    outcome = validator.submit(surface_batch("demo", "again"))
    assert outcome.ok
    assert outcome.notes == ()
    assert validator.surface_ids == ("demo",)


def test_delete_then_create_in_one_batch_is_not_a_reuse() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    outcome = validator.submit([delete("demo"), *surface_batch("demo", "again")])
    assert outcome.ok, outcome.errors
    assert outcome.notes == ()
    assert label_of(validator, "demo") == "again"


def test_two_creates_of_one_id_in_one_batch_rename_the_second() -> None:
    """The library alone rejects it ("Surface demo already exists."); now it is renamed."""
    validator = SurfaceValidator()
    outcome = validator.submit(
        [
            create("demo"),
            components([text("root", "a")], "demo"),
            create("demo"),
            components([text("root", "b")], "demo"),
        ]
    )
    assert outcome.ok, outcome.errors
    assert outcome.notes == (NOTE.format("demo_2"),)
    assert label_of(validator, "demo") == "a"
    assert label_of(validator, "demo_2") == "b"


def test_a_rejected_batch_changes_nothing_and_has_no_note() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    outcome = validator.submit(
        [create("demo"), {"version": "1.0", "updateComponents": {"surfaceId": "demo"}}]
    )
    assert not outcome.ok
    assert outcome.notes == ()
    assert validator.surface_ids == ("demo",)
    assert validator.submit(surface_batch("demo", "ok")).notes == (
        NOTE.format("demo_2"),
    )


def test_the_note_never_carries_control_characters() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("a\x1b[31mb", "one")).ok
    outcome = validator.submit(surface_batch("a\x1b[31mb", "two"))
    assert outcome.ok
    assert "\x1b" not in outcome.notes[0]


def test_the_replay_keeps_the_rename_across_later_submits() -> None:
    validator = SurfaceValidator()
    assert validator.submit(surface_batch("demo", "one")).ok
    assert validator.submit(surface_batch("demo", "two")).ok
    assert validator.submit(surface_batch("other", "x")).ok
    assert validator.surface_ids == ("demo", "demo_2", "other")
    assert validator.set_value("demo_2", "/v", "kept").ok
    assert validator.submit(surface_batch("third", "x")).ok
    assert validator.data_model("demo_2") == {"v": "kept"}


# --- the orchestrator --------------------------------------------------------

UI = copy.deepcopy(DEMO_MESSAGES)
CLICKED = {"type": "respond", "text": "clicked"}
LINE = "[ui] " + NOTE.format("demo_2")


async def turn(orchestrator: Orchestrator, text: str = "hi") -> list[StreamEvent]:
    return [event async for event in orchestrator.send(text)]


def drawing(ui: list[dict[str, object]]) -> list[StreamEvent]:
    return reply_events({**RESPOND, "ui": ui}, SET_GOAL)


async def test_the_next_request_carries_the_note_once_and_the_user_sees_a_notice() -> (
    None
):
    orchestrator, provider = make(
        [
            drawing(UI),
            drawing(UI),
            reply_events({**RESPOND, "text": "third"}, SET_GOAL),
            reply_events({**RESPOND, "text": "fourth"}, SET_GOAL),
        ]
    )
    await turn(orchestrator, "one")
    events = await turn(orchestrator, "two")
    assert Notice(text=LINE) in events
    assert LINE not in everything(provider.requests[1])  # told AFTER the turn
    await turn(orchestrator, "three")
    seen = everything(provider.requests[2])
    assert seen.count(LINE) == 1
    assert seen.index(LINE) < seen.index("three")
    await turn(orchestrator, "four")
    assert LINE not in everything(provider.requests[3])


async def test_the_surface_event_names_the_renamed_surface() -> None:
    orchestrator, _ = make([drawing(UI), drawing(UI)])
    await turn(orchestrator)
    events = await turn(orchestrator)
    surfaces = [e for e in events if isinstance(e, UiSurface)]
    assert surfaces == [UiSurface(surface_ids=("demo_2",))]
    assert orchestrator.ui_validator.surface_ids == ("demo", "demo_2")


async def test_a_batch_without_a_reuse_adds_no_note_and_no_notice() -> None:
    orchestrator, provider = make(
        [drawing(UI), reply_events({**RESPOND, "text": "again"}, SET_GOAL)]
    )
    events = await turn(orchestrator)
    assert not [e for e in events if isinstance(e, Notice) and "[ui]" in e.text]
    await turn(orchestrator, "next")
    assert "already exists; the new surface" not in everything(provider.requests[1])


async def test_a_click_accepts_the_new_id_and_the_original_id_still_works() -> None:
    orchestrator, provider = make(
        [drawing(UI), drawing(UI), reply_events(CLICKED), reply_events(CLICKED)]
    )
    await turn(orchestrator, "one")
    await turn(orchestrator, "two")
    new = [e async for e in orchestrator.send_ui_action("demo_2", "demo_clicked", {})]
    assert TextDelta(text="clicked") in new
    assert "ui_action: surface=demo_2 action=demo_clicked" in everything(
        provider.requests[2]
    )
    assert LINE in everything(provider.requests[2])
    old = [e async for e in orchestrator.send_ui_action("demo", "demo_clicked", {})]
    assert TextDelta(text="clicked") in old
    assert "ui_action: surface=demo action=demo_clicked" in everything(
        provider.requests[3]
    )


async def test_a_click_on_an_id_that_was_never_drawn_is_dropped() -> None:
    orchestrator, provider = make([drawing(UI), drawing(UI)])
    await turn(orchestrator)
    await turn(orchestrator)
    events = [e async for e in orchestrator.send_ui_action("demo_3", "x", {})]
    assert events == []
    assert len(provider.requests) == 2


async def test_the_done_event_still_ends_the_turn() -> None:
    orchestrator, _ = make([drawing(UI), drawing(UI)])
    await turn(orchestrator)
    assert isinstance((await turn(orchestrator))[-1], Done)


# --- the GUI -----------------------------------------------------------------


@pytest.fixture
def no_leftover_pages() -> None:
    gui_app._open_pages.clear()


async def test_the_gui_draws_both_surfaces_and_each_button_clicks_its_own_id(
    user: User, no_leftover_pages: None
) -> None:
    factory = StepFactory(
        [drawing(UI), drawing(UI), reply_events(CLICKED), reply_events(CLICKED)]
    )
    await open_chat(user, factory)

    async def say(text: str, buttons: int) -> None:
        user.find(marker="prompt").type(text).trigger("keydown.enter")
        await wait_for(lambda: len(elements(user, "a2ui-button")) == buttons)
        await wait_for(lambda: elements(user, "prompt")[0].enabled)

    await say("one", 1)
    await say("two", 2)
    assert LINE in texts(user, "note")
    old, new = elements(user, "a2ui-button")
    UserInteraction(user, {new}, "a2ui-button").click()
    await wait_for(lambda: len(factory.provider.requests) == 3)
    assert "ui_action: surface=demo_2 action=demo_clicked" in everything(
        factory.provider.requests[2]
    )
    await wait_for(lambda: elements(user, "prompt")[0].enabled)
    UserInteraction(user, {old}, "a2ui-button").click()
    await wait_for(lambda: len(factory.provider.requests) == 4)
    assert "ui_action: surface=demo action=demo_clicked" in everything(
        factory.provider.requests[3]
    )
