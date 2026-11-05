"""The JSON examples in the orchestrator prompt parse and validate as written."""

from pathlib import Path

import llm_harness
from llm_harness.a2ui.validation import SurfaceValidator
from llm_harness.state import RespondAction, parse_reply

PROMPT = Path(llm_harness.__file__).parent / "prompts" / "orchestrator.md"


def example_lines() -> list[str]:
    text = PROMPT.read_text(encoding="utf-8")
    section = text.split("## Optional UI", 1)[1].split("\n## ", 1)[0]
    return [line for line in section.splitlines() if line.startswith('{"type"')]


def ui_of(line: str) -> list[dict[str, object]]:
    # The prompt shows the `action` object; a reply wraps it with a `state_patch`.
    reply = parse_reply(f'{{"state_patch": {{}}, "action": {line}}}')
    assert isinstance(reply.action, RespondAction)
    assert reply.action.ui is not None
    return reply.action.ui


def test_the_prompt_has_the_six_examples() -> None:
    assert len(example_lines()) == 6


def test_every_example_parses_and_validates_in_order_on_one_validator() -> None:
    validator = SurfaceValidator()
    for line in example_lines():
        outcome = validator.submit(ui_of(line))
        assert outcome.ok, outcome.errors
    assert validator.surface_ids == ("demo", "profile", "form", "note", "chart")


def test_each_creating_example_validates_alone_on_a_fresh_validator() -> None:
    lines = example_lines()
    for line in (lines[0], lines[1], lines[3], lines[4], lines[5]):
        outcome = SurfaceValidator().submit(ui_of(line))
        assert outcome.ok, outcome.errors


def test_the_binding_example_resolves_to_the_text_it_promises() -> None:
    validator = SurfaceValidator()
    for line in example_lines()[:2]:
        assert validator.submit(ui_of(line)).ok
    name = validator.components("profile")["name"]["text"]
    assert validator.resolve("profile", name).value == "Hi Ann"
    assert validator.submit(ui_of(example_lines()[2])).ok
    assert validator.resolve("profile", name).value == "Hi Bob"


def test_the_prompt_names_the_v1_keys_and_not_the_python_field_names() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    assert '"@path"' in text
    assert '"@call"' in text
    assert '{"path": "/' not in text
    assert '{"call": "' not in text


def test_the_form_example_sends_the_data_model_and_binds_the_text_field() -> None:
    validator = SurfaceValidator()
    assert validator.submit(ui_of(example_lines()[3])).ok
    assert validator.sends_data_model("form") is True
    field = validator.components("form")["who"]
    assert field["component"] == "TextField"
    assert field["value"] == {"@path": "/form/name"}
    assert validator.set_value("form", "/form/name", "Ann").ok
    assert validator.data_model("form") == {"form": {"name": "Ann"}}


def test_the_prompt_lists_every_drawn_component_and_not_the_media_ones() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    section = text.split("## Optional UI", 1)[1].split("\n## ", 1)[0]
    for name in (
        "Row",
        "List",
        "Card",
        "Tabs",
        "Modal",
        "Divider",
        "Icon",
        "TextField",
        "CheckBox",
        "ChoicePicker",
        "Slider",
        "DateTimeInput",
    ):
        assert f"`{name}`" in section, name
    assert "not drawn yet" not in section
    for name in ("Image", "Video", "AudioPlayer"):
        assert f"`{name}`" in section, name
    assert "data:image/png;base64" in section
    assert "`demo_2`" in section
    assert "`[ui]` line" in section


def test_the_custom_examples_name_the_custom_catalog_and_validate_alone() -> None:
    lines = example_lines()
    for line, kind in ((lines[4], "HtmlBlock"), (lines[5], "SvgImage")):
        validator = SurfaceValidator()
        assert validator.submit(ui_of(line)).ok
        (surface_id,) = validator.surface_ids
        assert validator.components(surface_id)["root"]["component"] == kind
        assert "llm-harness/custom/v1" in line


def test_the_prompt_names_the_custom_components_and_what_is_sanitized() -> None:
    section = PROMPT.read_text(encoding="utf-8").split("## Optional UI", 1)[1]
    for needle in ("`HtmlBlock`", "`SvgImage`", "llm-harness/custom/v1", "sanitized"):
        assert needle in section, needle
