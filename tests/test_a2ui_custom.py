"""The custom catalog (HtmlBlock, SvgImage), its validation and the SVG id scoping."""

import time

import pytest

from llm_harness.a2ui.catalog import (
    CUSTOM_CATALOG_ID,
    GuardedBasicCatalog,
    custom_catalog,
)
from llm_harness.a2ui.sanitize import sanitize_svg, sanitize_svg_image
from llm_harness.a2ui.validation import SurfaceValidator
from tests.a2ui_support import CATALOG_ID, call, data_model, text

GRADIENT_SVG = (
    '<svg viewBox="0 0 10 10"><defs><linearGradient id="g">'
    '<stop offset="0" stop-color="red"/></linearGradient><g id="shape">'
    '<rect width="5" height="5" fill="url(#g)"/></g></defs><use href="#shape"/></svg>'
)


def create(catalog_id: str = CUSTOM_CATALOG_ID, surface_id: str = "s") -> dict:
    return {
        "version": "1.0",
        "createSurface": {"surfaceId": surface_id, "catalogId": catalog_id},
    }


def update(items: list[dict], surface_id: str = "s") -> dict:
    return {
        "version": "1.0",
        "updateComponents": {"surfaceId": surface_id, "components": items},
    }


def html_block(value: object, component_id: str = "root") -> dict:
    return {"id": component_id, "component": "HtmlBlock", "html": value}


def svg_image(value: object, component_id: str = "root") -> dict:
    return {"id": component_id, "component": "SvgImage", "svg": value}


def custom(items: list[dict], data: object = None) -> SurfaceValidator:
    validator = SurfaceValidator()
    messages = [create(), update(items)]
    if data is not None:
        messages.insert(1, data_model(data))
    outcome = validator.submit(messages)
    assert outcome.ok, outcome.errors
    return validator


# --- catalog -----------------------------------------------------------------


def test_the_custom_catalog_id_is_the_confirmed_default() -> None:
    assert CUSTOM_CATALOG_ID == "llm-harness/custom/v1"
    assert custom_catalog(GuardedBasicCatalog()).catalog_id == CUSTOM_CATALOG_ID


def test_the_custom_catalog_keeps_every_basic_component_and_function() -> None:
    basic = GuardedBasicCatalog()
    derived = custom_catalog(basic)
    assert set(basic.components) | {"HtmlBlock", "SvgImage"} == set(derived.components)
    assert set(basic.functions) == set(derived.functions)
    assert "HtmlBlock" not in basic.components


def test_the_custom_catalog_instructions_are_three_lines_about_sanitizing() -> None:
    lines = custom_catalog(GuardedBasicCatalog()).to_json()["instructions"].split("\n")
    assert len(lines) == 3
    assert "HtmlBlock" in lines[0] and "SvgImage" in lines[0]
    assert "sanitized" in lines[2] and "no scripts" in lines[2]


def test_the_new_components_take_a_required_dynamic_string_and_a_weight() -> None:
    document = custom_catalog(GuardedBasicCatalog()).to_json()["components"]
    for name, prop in (("HtmlBlock", "html"), ("SvgImage", "svg")):
        schema = document[name]
        assert schema["required"] == ["component", prop]
        assert schema["properties"][prop]["$ref"].endswith("/DynamicString")
        weight_ref = document["Divider"]["properties"]["weight"]["$ref"]
        assert schema["properties"]["weight"]["$ref"] == weight_ref


def test_the_custom_catalog_keeps_the_time_bounded_regex() -> None:
    validator = custom(
        [text("root", call("regex", value="a" * 60 + "!", pattern="(a|aa)+$"))]
    )
    started = time.monotonic()
    result = validator.resolve("s", validator.components("s")["root"]["text"])
    assert not result.ok and "too slow" in result.error
    assert time.monotonic() - started < 5


# --- validation --------------------------------------------------------------


def test_a_custom_surface_with_both_components_and_basic_ones_is_accepted() -> None:
    validator = custom(
        [
            {"id": "root", "component": "Column", "children": ["t", "h", "v", "b"]},
            text("t", "Title"),
            html_block("<p>x</p>", "h"),
            svg_image('<svg viewBox="0 0 1 1"/>', "v"),
            {
                "id": "b",
                "component": "Button",
                "child": "t",
                "action": {"event": {"name": "go", "context": {}}},
            },
        ]
    )
    kinds = {c["component"] for c in validator.components("s").values()}
    assert kinds == {"Column", "Text", "HtmlBlock", "SvgImage", "Button"}


def test_both_catalogs_work_in_one_validator() -> None:
    validator = SurfaceValidator()
    messages = [
        create(CATALOG_ID, "basic"),
        update([text("root", "hi")], "basic"),
        create(CUSTOM_CATALOG_ID, "rich"),
        update([html_block("<p>x</p>")], "rich"),
    ]
    assert validator.submit(messages).ok
    assert validator.surface_ids == ("basic", "rich")


@pytest.mark.parametrize(
    "component", [html_block("<p>x</p>"), svg_image("<svg/>")], ids=["html", "svg"]
)
def test_the_new_components_are_unknown_in_a_basic_surface(component: dict) -> None:
    validator = SurfaceValidator()
    outcome = validator.submit([create(CATALOG_ID), update([component])])
    assert not outcome.ok
    assert "Unrecognized component type" in outcome.errors[0]
    assert validator.surface_ids == ()


def test_an_unknown_catalog_id_still_fails_with_the_library_error() -> None:
    outcome = SurfaceValidator().submit([create("llm-harness/custom/v2")])
    assert not outcome.ok
    assert "Catalog not found" in outcome.errors[0]


@pytest.mark.parametrize(
    "component",
    [
        {"id": "root", "component": "HtmlBlock"},
        {"id": "root", "component": "SvgImage"},
        html_block(5),
        svg_image(["x"]),
        {**html_block("x"), "extra": 1},
    ],
    ids=["no-html", "no-svg", "number", "list", "unknown-property"],
)
def test_the_new_components_reject_a_bad_shape(component: dict) -> None:
    outcome = SurfaceValidator().submit([create(), update([component])])
    assert not outcome.ok


def test_a_weight_is_accepted_on_the_new_components() -> None:
    validator = custom([{**html_block("x"), "weight": 2}])
    assert validator.components("s")["root"]["weight"] == 2


# --- values resolve ----------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "wrap", "markup"),
    [
        ("HtmlBlock", html_block, "<p>Ann</p>"),
        ("SvgImage", svg_image, '<svg viewBox="0 0 1 1"><text>Ann</text></svg>'),
    ],
)
def test_both_components_resolve_a_literal_a_path_and_a_call(
    kind: str, wrap: object, markup: str
) -> None:
    prop = "html" if kind == "HtmlBlock" else "svg"
    opener, closer = markup.split("Ann")
    literal = custom([wrap(markup)])  # type: ignore[operator]
    assert literal.resolve("s", literal.components("s")["root"][prop]).value == markup
    bound = custom([wrap({"@path": "/m"})], {"m": markup})  # type: ignore[operator]
    assert bound.resolve("s", bound.components("s")["root"][prop]).value == markup
    formatted = call("formatString", value=f"{opener}${{/n}}{closer}")
    called = custom([wrap(formatted)], {"n": "Ann"})  # type: ignore[operator]
    assert called.resolve("s", called.components("s")["root"][prop]).value == markup


# --- SVG id scoping ----------------------------------------------------------


def test_sanitize_svg_image_prefixes_ids_and_every_reference() -> None:
    out = sanitize_svg_image(GRADIENT_SVG, "p1-")
    assert 'id="p1-g"' in out and 'id="p1-shape"' in out
    assert 'fill="url(#p1-g)"' in out
    assert 'href="#p1-shape"' in out
    assert 'id="g"' not in out and "(#g)" not in out and 'href="#shape"' not in out


def test_two_prefixes_give_two_disjoint_id_sets() -> None:
    first = sanitize_svg_image(GRADIENT_SVG, "a-")
    second = sanitize_svg_image(GRADIENT_SVG, "b-")
    assert 'id="a-g"' in first and 'id="b-g"' in second
    assert 'id="a-g"' not in second


def test_a_reference_to_a_missing_id_stays_dangling_inside_the_prefix() -> None:
    svg = '<svg><rect fill="url(#nowhere)"/><use href="#nowhere"/></svg>'
    out = sanitize_svg_image(svg, "p-")
    assert "url(#p-nowhere)" in out and 'href="#p-nowhere"' in out
    assert 'id="p-nowhere"' not in out


def test_external_references_stay_refused_with_a_prefix() -> None:
    svg = (
        '<svg><use href="https://evil.example/x.svg#a"/><rect fill="url(https://e/x)"/>'
        '<rect id="ok"/></svg>'
    )
    out = sanitize_svg_image(svg, "p-")
    assert "evil" not in out and "url(https" not in out
    assert 'id="p-ok"' in out


def test_text_content_is_not_rewritten_by_the_prefix() -> None:
    out = sanitize_svg_image('<svg><text id="t">url(#g) #g</text></svg>', "p-")
    assert ">url(#g) #g</text>" in out and 'id="p-t"' in out


@pytest.mark.parametrize("prefix", ["", "1a", "a b", 'a"', "a#", "a/"])
def test_a_bad_prefix_is_refused(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        sanitize_svg_image("<svg/>", prefix)


def test_sanitize_svg_image_still_sanitizes() -> None:
    out = sanitize_svg_image(
        '<svg onload="x()"><script>x()</script><foreignObject/><rect id="r"/></svg>',
        "p-",
    )
    assert "onload" not in out and "script" not in out and "foreign" not in out.lower()


def test_sanitize_svg_itself_does_not_prefix() -> None:
    assert 'id="g"' in sanitize_svg(GRADIENT_SVG)


def test_sanitize_svg_image_drops_the_root_size_and_keeps_the_view_box() -> None:
    out = sanitize_svg_image(
        '<svg width="300" height="200" viewBox="0 0 30 20"><rect width="5" height="4"/></svg>',
        "p-",
    )
    root = out.split(">", 1)[0]
    assert "viewBox" in root and "width=" not in root and "height=" not in root
    assert 'style="width:100%;height:auto"' in root
    assert '<rect width="5" height="4">' in out  # inner sizes are kept
    assert "px" not in out


def test_the_root_style_comes_from_the_sanitizer_not_from_the_model() -> None:
    out = sanitize_svg_image('<svg style="width:900px;position:fixed"/>', "p-")
    assert out.count("style=") == 1
    assert "900" not in out and "fixed" not in out
