"""GUI: HtmlBlock and SvgImage are drawn through the sanitizers."""

import re

import pytest
from nicegui import ElementFilter, ui
from nicegui.testing import User

import llm_harness.gui.app as gui_app
from llm_harness.a2ui.catalog import CUSTOM_CATALOG_ID
from llm_harness.a2ui.sanitize import MAX_CHARS
from llm_harness.a2ui.validation import SurfaceValidator
from tests.a2ui_support import FORBIDDEN_CLASS, call, found, open_surface, shown
from tests.test_a2ui_custom import create, html_block, svg_image, update
from tests.test_gui import StepFactory, elements, open_chat, texts, wait_for
from tests.test_gui_a2ui_turn import drawing_reply, send
from tests.test_gui_a2ui_update import click_and_wait, update_reply

HOSTILE_HTML = (
    '<p>safe text</p><script>alert(1)</script><img src=x onerror="alert(2)">'
    '<a href="javascript:alert(3)" onclick="alert(4)">bad link</a>'
    '<a href="https://example.org/x">good link</a><form><input name="q"></form>'
)
HOSTILE_SVG = (
    '<svg width="400" height="200" viewBox="0 0 40 20" onload="alert(1)">'
    "<script>alert(2)</script><foreignObject><p>x</p></foreignObject>"
    '<defs><linearGradient id="g"><stop offset="0" stop-color="red"/></linearGradient>'
    '<g id="shape"><rect width="5" height="5" fill="url(#g)" onclick="alert(3)"/></g></defs>'
    '<use href="https://evil.example/x.svg#a"/><use href="#shape"/>'
    '<text x="1" y="1">svg label</text></svg>'
)
KINDS = ("a2ui-text", "a2ui-html", "a2ui-svg", "a2ui-button")
BAD_PATTERNS = (
    "<script",
    " onerror=",
    " onload=",
    " onclick=",
    "javascript:",
    "evil.example",
)


@pytest.fixture(autouse=True)
def no_leftover_pages() -> None:
    gui_app._open_pages.clear()


def surface(items: list[dict], data: object = None) -> SurfaceValidator:
    validator = SurfaceValidator()
    messages: list[dict] = [create(), update(items)]
    if data is not None:
        messages.insert(
            1,
            {
                "version": "1.0",
                "updateDataModel": {"surfaceId": "s", "path": "/", "value": data},
            },
        )
    outcome = validator.submit(messages)
    assert outcome.ok, outcome.errors
    return validator


def html_contents(user: User) -> list[str]:
    """What every `ui.html` of the page sends to the browser (after the filter)."""
    with user.client:
        return [
            e.props["innerHTML"] for e in ElementFilter(kind=ui.html, local_scope=False)
        ]


def error_lines(user: User) -> list[str]:
    return shown(user, "a2ui-error")


# --- HtmlBlock ---------------------------------------------------------------


async def test_html_block_keeps_safe_text_and_drops_hostile_markup(user: User) -> None:
    await open_surface(user, surface([html_block(HOSTILE_HTML)]))
    (block,) = found(user, "a2ui-html")
    content = block.props["innerHTML"]
    assert "safe text" in content and "good link" in content and "bad link" in content
    assert 'href="https://example.org/x"' in content
    for bad in (*BAD_PATTERNS, "<img", "<form", "<input", "alert"):
        assert bad not in content, bad
    assert error_lines(user) == []


async def test_html_block_is_a_ui_html_with_a_callable_filter_and_full_width(
    user: User,
) -> None:
    await open_surface(user, surface([html_block("<p>x</p>")]))
    (block,) = found(user, "a2ui-html")
    assert isinstance(block, ui.html)
    assert callable(
        block._sanitize
    )  # NiceGUI 3.18 filters on the server only for a callable
    assert block.props["sanitize"] is False  # no client-side mode requested
    assert "w-full" in block.classes
    assert not any(FORBIDDEN_CLASS.search(c) for c in block.classes)
    assert block.style.get("width") is None and block.style.get("height") is None


async def test_html_block_resolves_a_path_and_a_call(user: User) -> None:
    validator = surface(
        [
            {"id": "root", "component": "Column", "children": ["a", "b"]},
            html_block({"@path": "/note"}, "a"),
            html_block(call("formatString", value="<b>${/who}</b>"), "b"),
        ],
        {"note": "<p>from path</p>", "who": "Ann"},
    )
    await open_surface(user, validator)
    contents = [b.props["innerHTML"] for b in found(user, "a2ui-html")]
    assert contents == ["<p>from path</p>", "<b>Ann</b>"]


async def test_a_hostile_bound_value_is_cleaned_too(user: User) -> None:
    validator = surface([html_block({"@path": "/h"})], {"h": HOSTILE_HTML})
    await open_surface(user, validator)
    (block,) = found(user, "a2ui-html")
    assert all(bad not in block.props["innerHTML"] for bad in BAD_PATTERNS)


async def test_a_too_large_html_block_shows_the_error_line(user: User) -> None:
    await open_surface(user, surface([html_block("<p>" + "x" * MAX_CHARS + "</p>")]))
    assert found(user, "a2ui-html") == []
    (line,) = error_lines(user)
    assert line == "[A2UI value error: A2UI HTML block too large]"


async def test_a_missing_path_in_an_html_block_shows_the_error_line(user: User) -> None:
    await open_surface(user, surface([html_block({"@path": "/nowhere"})], {"a": 1}))
    (line,) = error_lines(user)
    assert line.startswith("[A2UI value error: ") and "/nowhere" in line


async def test_a_non_string_html_value_is_an_unsupported_value(user: User) -> None:
    validator = surface([html_block({"@path": "/n"})], {"n": 5})
    await open_surface(user, validator)
    assert found(user, "a2ui-html") == []
    assert shown(user, "a2ui-unsupported") == ["[unsupported A2UI value]"]


# --- SvgImage ----------------------------------------------------------------


async def test_svg_image_drops_hostile_parts_and_keeps_safe_ones(user: User) -> None:
    await open_surface(user, surface([svg_image(HOSTILE_SVG)]))
    (svg,) = found(user, "a2ui-svg")
    content = svg.props["innerHTML"]
    for bad in (*BAD_PATTERNS, "foreignObject", "alert"):
        assert bad not in content, bad
    assert "svg label" in content and "<linearGradient" in content
    assert error_lines(user) == []


async def test_svg_internal_references_survive_with_the_prefix_on_both_sides(
    user: User,
) -> None:
    await open_surface(user, surface([svg_image(HOSTILE_SVG)]))
    content = found(user, "a2ui-svg")[0].props["innerHTML"]
    (gradient,) = re.findall(r'<linearGradient id="([^"]+)"', content)
    (shape,) = re.findall(r'<g id="([^"]+)"', content)
    assert gradient.startswith("a2ui-") and gradient.endswith("-g")
    assert shape.endswith("-shape") and shape.startswith("a2ui-")
    assert f'fill="url(#{gradient})"' in content
    assert f'href="#{shape}"' in content
    assert 'id="g"' not in content and "(#g)" not in content
    assert content.count(gradient) == 2  # applied once, not twice by the filter


async def test_two_svg_images_with_the_same_ids_get_different_prefixes(
    user: User,
) -> None:
    validator = surface(
        [
            {"id": "root", "component": "Column", "children": ["one", "two"]},
            svg_image(HOSTILE_SVG, "one"),
            svg_image(HOSTILE_SVG, "two"),
        ]
    )
    await open_surface(user, validator)
    contents = [s.props["innerHTML"] for s in found(user, "a2ui-svg")]
    assert len(contents) == 2
    ids = [re.findall(r'\bid="([^"]+)"', c) for c in contents]
    assert len(ids[0]) == 2 and not set(ids[0]) & set(ids[1])
    for index, content in enumerate(contents):
        assert all(f"#{other}" not in content for other in ids[1 - index])


async def test_svg_has_no_fixed_size_and_fits_its_container(user: User) -> None:
    await open_surface(user, surface([svg_image(HOSTILE_SVG)]))
    (svg,) = found(user, "a2ui-svg")
    root = svg.props["innerHTML"].split(">", 1)[0]
    assert 'viewBox="0 0 40 20"' in root
    assert "width=" not in root and "height=" not in root
    assert 'style="width:100%;height:auto"' in root
    assert "px" not in svg.props["innerHTML"]
    assert "w-full" in svg.classes
    assert not any(FORBIDDEN_CLASS.search(c) for c in svg.classes)
    assert "height" not in svg.style and "width" not in svg.style


async def test_svg_resolves_a_path_and_a_call(user: User) -> None:
    validator = surface(
        [
            {"id": "root", "component": "Row", "children": ["a", "b"]},
            svg_image({"@path": "/s"}, "a"),
            svg_image(
                call(
                    "formatString",
                    value='<svg viewBox="0 0 1 1"><text>${/who}</text></svg>',
                ),
                "b",
            ),
        ],
        {"s": '<svg viewBox="0 0 1 1"><text>bound</text></svg>', "who": "Ann"},
    )
    await open_surface(user, validator)
    contents = [s.props["innerHTML"] for s in found(user, "a2ui-svg")]
    assert ">bound</text>" in contents[0] and ">Ann</text>" in contents[1]


@pytest.mark.parametrize(
    ("markup", "reason"),
    [
        ("<svg><rect></svg>", "not well-formed"),
        ('<!DOCTYPE svg><svg viewBox="0 0 1 1"/>', "<!doctype"),
        ("<p>not svg</p>", "root element must be <svg>"),
        ("<svg>" + "x" * MAX_CHARS + "</svg>", "too large"),
        ("<svg>" + "<g>" * 25 + "</g>" * 25 + "</svg>", "nested too deeply"),
    ],
    ids=["malformed", "doctype", "not-svg", "too-large", "too-deep"],
)
async def test_a_refused_svg_shows_the_error_line(
    user: User, markup: str, reason: str
) -> None:
    await open_surface(user, surface([svg_image(markup)]))
    assert found(user, "a2ui-svg") == []
    (line,) = error_lines(user)
    assert line.startswith("[A2UI value error: A2UI SVG")
    assert reason in line
    assert len(line) < 300


# --- mixed surfaces ----------------------------------------------------------


def mixed() -> SurfaceValidator:
    return surface(
        [
            {
                "id": "root",
                "component": "Column",
                "children": ["t", "h", "v", "go", "bad"],
            },
            {"id": "t", "component": "Text", "text": "Title"},
            html_block("<p>rich</p>", "h"),
            svg_image('<svg viewBox="0 0 1 1"><text>pic</text></svg>', "v"),
            {
                "id": "go",
                "component": "Button",
                "child": "go_label",
                "action": {"event": {"name": "pressed", "context": {}}},
            },
            {"id": "go_label", "component": "Text", "text": "Press"},
            {
                "id": "bad",
                "component": "Text",
                "text": "<script>alert(1)</script><b>x</b>",
            },
        ]
    )


async def test_a_mixed_surface_draws_in_order_and_the_button_works(user: User) -> None:
    actions = await open_surface(user, mixed())
    with user.client:
        (column,) = ElementFilter(marker="a2ui-column", local_scope=False)
        order = [
            next((m for m in KINDS if m in child._markers), "?")
            for child in column.default_slot.children
        ]
    assert order == ["a2ui-text", "a2ui-html", "a2ui-svg", "a2ui-button", "a2ui-text"]
    user.find(marker="a2ui-button").click()
    assert actions == [("s", "pressed", {})]


async def test_hostile_text_next_to_the_new_components_is_still_a_label(
    user: User,
) -> None:
    await open_surface(user, mixed())
    assert "<script>alert(1)</script><b>x</b>" in shown(user)
    assert len(html_contents(user)) == 2  # only the two sanitized blocks are html


async def test_no_html_content_of_a_custom_surface_holds_active_markup(
    user: User,
) -> None:
    validator = surface(
        [
            {"id": "root", "component": "Column", "children": ["h", "v"]},
            html_block(HOSTILE_HTML, "h"),
            svg_image(HOSTILE_SVG, "v"),
        ]
    )
    await open_surface(user, validator)
    contents = html_contents(user)
    assert len(contents) == 2
    for content in contents:
        for bad in BAD_PATTERNS:
            assert bad not in content.lower(), bad


async def test_the_page_source_has_no_fixed_layout_classes(user: User) -> None:
    await open_surface(user, mixed())
    with user.client:
        classes = [c for e in user.client.elements.values() for c in e.classes]
    assert not [c for c in classes if FORBIDDEN_CLASS.search(c)]


# --- redraw through a model turn ---------------------------------------------


def board_ui(note: str) -> list[dict]:
    items = [
        {"id": "root", "component": "Column", "children": ["note", "go", "pic"]},
        html_block({"@path": "/note"}, "note"),
        svg_image({"@path": "/pic"}, "pic"),
        {
            "id": "go",
            "component": "Button",
            "child": "go_label",
            "action": {"event": {"name": "refresh", "context": {}}},
        },
        {"id": "go_label", "component": "Text", "text": "Refresh"},
    ]
    data = {"note": note, "pic": '<svg viewBox="0 0 1 1"><text>one</text></svg>'}
    return [
        create(CUSTOM_CATALOG_ID, "board"),
        {
            "version": "1.0",
            "updateDataModel": {"surfaceId": "board", "path": "/", "value": data},
        },
        update(items, "board"),
    ]


async def test_update_data_model_redraws_the_bound_blocks(user: User) -> None:
    changes = [
        {
            "version": "1.0",
            "updateDataModel": {
                "surfaceId": "board",
                "path": "/note",
                "value": "<p>second <script>x()</script>note</p>",
            },
        },
        {
            "version": "1.0",
            "updateDataModel": {
                "surfaceId": "board",
                "path": "/pic",
                "value": '<svg viewBox="0 0 1 1"><text onload="x()">two</text></svg>',
            },
        },
    ]
    factory = StepFactory(
        [drawing_reply(board_ui("<p>one</p>")), update_reply(changes)]
    )
    await open_chat(user, factory)
    await send(user, "show me")
    await wait_for(lambda: len(elements(user, "a2ui-html")) == 1)
    await wait_for(lambda: elements(user, "prompt")[0].enabled)
    assert elements(user, "a2ui-html")[0].props["innerHTML"] == "<p>one</p>"
    await click_and_wait(user, factory, 2)
    await wait_for(
        lambda: (
            "second"
            in "".join(e.props["innerHTML"] for e in elements(user, "a2ui-html"))
        )
    )
    assert elements(user, "a2ui-html")[0].props["innerHTML"] == "<p>second note</p>"
    assert ">two</text>" in elements(user, "a2ui-svg")[0].props["innerHTML"]
    assert "onload" not in elements(user, "a2ui-svg")[0].props["innerHTML"]
    assert len(elements(user, "a2ui-surface")) == 1
    assert texts(user, "a2ui-button") == ["Refresh"]
