"""Draw an accepted A2UI v1.0 surface with NiceGUI elements.

Draws Text, Image, Video, AudioPlayer, Icon, Divider, Row, Column, List, Card, Tabs, Modal, Button, the
input components of `gui.a2ui_inputs` and the custom catalog's HtmlBlock and
SvgImage (sanitized, see `gui.a2ui_draw`). Values may be literals, `{"@path": ...}`
data bindings and `{"@call": ...}` function calls; they are evaluated by
`SurfaceValidator.resolve` (a2ui-core's DataContext) on the CURRENT data model
every time the surface is drawn. A value that cannot be evaluated shows a visible
`[A2UI value error: ...]` line. Everything else shows a visible `[unsupported ...]`
line; nothing is dropped silently and model text is never rendered as HTML except
through the sanitizers of HtmlBlock and SvgImage (everything else is a label).
Image, Video and AudioPlayer are drawn only for URLs the media policy allows
(`a2ui.media`); a refused URL shows `[A2UI media refused: <reason>]`.
"""

import math
import re
from collections.abc import Callable

from nicegui import ui

from llm_harness.a2ui.validation import OPEN_URL, SurfaceValidator
from llm_harness.display import sanitize_text
from llm_harness.gui.a2ui_draw import (
    DIM,
    PRE_WRAP,
    ActionHandler,
    Draw,
    error_line,
    note_line,
    render_audio_player,
    render_html_block,
    render_image,
    render_svg_image,
    render_video,
    resolved,
    text_value,
    unsupported_component,
    unsupported_value,
    value_error,
    value_error_text,
)
from llm_harness.gui.a2ui_inputs import (
    render_check_box,
    render_choice_picker,
    render_date_time_input,
    render_slider,
    render_text_field,
)

ROOT_ID = "root"
MAX_DEPTH = 32
MAX_TEMPLATE_ITEMS = 100
MAX_WEIGHT = 1000
# Only a web link may be opened, and only when nothing in it can be read as
# whitespace or a control character by a browser.
_WEB_URL = re.compile(r"https?://[^/\s\x00-\x1f\x7f][^\s\x00-\x1f\x7f]*", re.IGNORECASE)
_OPEN_URL_REFUSED = "[A2UI openUrl refused: scheme]"
# Quasar treats a prefix such as `img:` or `https:` as an image URL, so only
# plain Material icon names (lowercase, digits, underscore) reach `ui.icon`.
_ICON_CATALOG_NAME = re.compile(r"[a-z0-9][A-Za-z0-9_]{0,63}")
_ICON_MATERIAL_NAME = re.compile(r"[a-z0-9_]{1,64}")
_JUSTIFY = {
    "start": "justify-start",
    "center": "justify-center",
    "end": "justify-end",
    "spaceBetween": "justify-between",
    "spaceAround": "justify-around",
    "spaceEvenly": "justify-evenly",
    "stretch": "justify-stretch",
}
_ALIGN = ("start", "center", "end", "stretch")
_INPUTS: dict[str, Callable[[Draw, dict[str, object]], None]] = {
    "TextField": render_text_field,
    "CheckBox": render_check_box,
    "ChoicePicker": render_choice_picker,
    "Slider": render_slider,
    "DateTimeInput": render_date_time_input,
}


def render_surface(
    validator: SurfaceValidator,
    surface_id: str,
    on_action: ActionHandler,
    on_edit: Callable[[], None] | None = None,
    view: dict[str, str] | None = None,
    media_hosts: tuple[str, ...] = (),
) -> None:
    """Build the elements of a validated surface inside the current slot.

    `on_edit` is called after a user edit was stored in the data model (the
    caller draws the surface again); `view` keeps the user's choices (selected
    tab) between those redraws; `media_hosts` are the extra hosts Image, Video
    and AudioPlayer may load from (see `a2ui.media`).
    """
    components = validator.components(surface_id)
    if ROOT_ID not in components:
        error_line(f"[A2UI error: surface {sanitize_text(surface_id)!r} has no root]")
        return
    draw = Draw(
        validator,
        surface_id,
        on_action,
        components,
        on_edit,
        view,
        media_hosts=media_hosts,
    )
    _render(draw, ROOT_ID, ())


def _render(draw: Draw, component_id: str, path: tuple[str, ...]) -> None:
    component = draw.components.get(component_id)
    if component is None:
        error_line(f"[A2UI error: missing component {sanitize_text(component_id)!r}]")
        return
    if component_id in path:
        error_line(f"[A2UI error: cycle at component {sanitize_text(component_id)!r}]")
        return
    if len(path) >= MAX_DEPTH:
        error_line(f"[A2UI error: nesting deeper than {MAX_DEPTH} levels]")
        return
    here = (*path, component_id)
    weight = _weight(component.get("weight"))
    if weight is None:
        _render_kind(draw, component, here)
        return
    # A weight is the flex-grow of the component inside its Row or Column.
    with (
        ui.column()
        .classes("w-full grow gap-0")
        .style(f"flex-grow: {weight:g}")
        .mark("a2ui-weighted")
    ):
        _render_kind(draw, component, here)


def _weight(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 < value <= MAX_WEIGHT else None


def _render_kind(
    draw: Draw, component: dict[str, object], here: tuple[str, ...]
) -> None:
    kind = component.get("component")
    if kind == "Text":
        _render_text(draw, component)
    elif kind == "Icon":
        _render_icon(draw, component)
    elif kind == "Divider":
        _render_divider(component)
    elif kind in ("Column", "Row", "List"):
        _render_flow(draw, component, here, str(kind))
    elif kind == "Card":
        _render_card(draw, component, here)
    elif kind == "Tabs":
        _render_tabs(draw, component, here)
    elif kind == "Modal":
        _render_modal(draw, component, here)
    elif kind == "Button":
        _render_button(draw, component, here)
    elif kind == "HtmlBlock":
        render_html_block(draw, component)
    elif kind == "SvgImage":
        render_svg_image(draw, component)
    elif kind == "Image":
        render_image(draw, component)
    elif kind == "Video":
        render_video(draw, component)
    elif kind == "AudioPlayer":
        render_audio_player(draw, component)
    elif isinstance(kind, str) and kind in _INPUTS:
        _INPUTS[kind](draw, component)
    else:
        unsupported_component(kind)


def _render_text(draw: Draw, component: dict[str, object]) -> None:
    text = text_value(draw, component.get("text"))
    if text is None:
        return
    classes = f"w-full {PRE_WRAP}"
    if component.get("variant") == "caption":
        classes += f" {DIM}"
    ui.label(text).classes(classes).mark("a2ui-text")


def _render_icon(draw: Draw, component: dict[str, object]) -> None:
    result = resolved(draw, component.get("name"))
    if not result.ok:
        value_error(result.error)
        return
    name = _material_icon_name(result.value)
    if name is None:
        unsupported_value()
        return
    ui.icon(name).mark("a2ui-icon")


def _material_icon_name(value: object) -> str | None:
    """The Material icon name for a catalog icon name (`accountCircle` -> `account_circle`)."""
    if not isinstance(value, str) or _ICON_CATALOG_NAME.fullmatch(value) is None:
        return None
    name = re.sub(r"(?<=[a-z0-9])([A-Z])", lambda m: "_" + m.group(1).lower(), value)
    return name if _ICON_MATERIAL_NAME.fullmatch(name) else None


def _render_divider(component: dict[str, object]) -> None:
    axis = component.get("axis", "horizontal")
    if axis not in ("horizontal", "vertical"):
        unsupported_value()
        return
    divider = ui.separator().mark("a2ui-divider")
    if axis == "vertical":
        divider.props("vertical").classes("self-stretch")


def _render_flow(
    draw: Draw, component: dict[str, object], path: tuple[str, ...], kind: str
) -> None:
    children = _child_draws(draw, component.get("children"))
    if children is None:
        return
    align = component.get("align")
    if align is not None and align not in _ALIGN:
        unsupported_value()
        return
    justify = component.get("justify")
    if justify is not None and justify not in _JUSTIFY:
        unsupported_value()
        return
    horizontal = kind == "Row" or component.get("direction") == "horizontal"
    if kind == "List" and component.get("direction", "vertical") not in (
        "vertical",
        "horizontal",
    ):
        unsupported_value()
        return
    container = (
        ui.row(align_items=align) if horizontal else ui.column(align_items=align)
    )
    container.classes("w-full gap-2").mark(f"a2ui-{kind.lower()}")
    if justify is not None:
        container.classes(_JUSTIFY[justify])
    with container:
        for child_id, child_draw in children:
            _render(child_draw, child_id, path)


def _child_draws(draw: Draw, children: object) -> list[tuple[str, Draw]] | None:
    """(child id, draw scope) for each child; None after a visible line."""
    if isinstance(children, list) and all(isinstance(c, str) for c in children):
        return [(child_id, draw) for child_id in children]
    if not isinstance(children, dict):
        unsupported_value()
        return None
    component_id, list_path = children.get("componentId"), children.get("path")
    if not isinstance(component_id, str) or not isinstance(list_path, str):
        note_line("[unsupported A2UI children template]")
        return None
    absolute = draw.validator.absolute_path(draw.surface_id, draw.scope, list_path)
    result = resolved(draw, {"@path": absolute})
    if not result.ok:
        value_error(result.error)
        return None
    if not isinstance(result.value, list):
        note_line("[unsupported A2UI children template]")
        return None
    if len(result.value) > MAX_TEMPLATE_ITEMS:
        note_line(f"[A2UI list cut at {MAX_TEMPLATE_ITEMS} items]")
    count = min(len(result.value), MAX_TEMPLATE_ITEMS)
    return [(component_id, draw.at(f"{absolute}/{index}")) for index in range(count)]


def _render_card(
    draw: Draw, component: dict[str, object], path: tuple[str, ...]
) -> None:
    child = component.get("child")
    if not isinstance(child, str):
        unsupported_value()
        return
    with ui.card().classes("w-full").mark("a2ui-card"):
        _render(draw, child, path)


def _render_tabs(
    draw: Draw, component: dict[str, object], path: tuple[str, ...]
) -> None:
    tabs = component.get("tabs")
    if (
        not isinstance(tabs, list)
        or not tabs
        or not all(
            isinstance(tab, dict) and isinstance(tab.get("child"), str) for tab in tabs
        )
    ):
        unsupported_value()
        return
    names = [f"tab{index}" for index in range(len(tabs))]
    key = f"tabs:{draw.scope}:{path[-1]}"
    chosen = (draw.view or {}).get(key)
    with ui.column().classes("w-full gap-2").mark("a2ui-tabs-box"):
        with ui.tabs().classes("w-full").mark("a2ui-tabs") as tab_bar:
            for name, tab in zip(names, tabs, strict=True):
                title = text_value(draw, tab.get("title"))
                ui.tab(name, label=title if title is not None else "?").mark("a2ui-tab")
        panels = (
            ui.tab_panels(tab_bar, value=chosen if chosen in names else names[0])
            .classes("w-full")
            .mark("a2ui-tab-panels")
        )
        view = draw.view
        if view is not None:
            panels.on_value_change(lambda event: view.__setitem__(key, event.value))
        with panels:
            for name, tab in zip(names, tabs, strict=True):
                with ui.tab_panel(name).classes("w-full").mark("a2ui-tab-panel"):
                    _render(draw, tab["child"], path)


def _render_modal(
    draw: Draw, component: dict[str, object], path: tuple[str, ...]
) -> None:
    trigger, content = component.get("trigger"), component.get("content")
    if not isinstance(trigger, str) or not isinstance(content, str):
        unsupported_value()
        return
    # "maximized" lifts Quasar's 560px width limit of a dialog; `llm-page` gives the
    # dialog (which Quasar draws outside the page) the page's font sizes.
    with (
        ui.dialog().props("maximized").mark("a2ui-modal") as dialog,
        ui.card().classes("w-full llm-page").mark("a2ui-modal-card"),
    ):
        ui.button("Close", on_click=dialog.close).mark("a2ui-modal-close")
        _render(draw.inside_modal(), content, path)
    # A click on the trigger (a button, say) bubbles up to this box and opens the dialog.
    with ui.row().classes("w-full").mark("a2ui-modal-trigger") as box:
        box.on("click", dialog.open)
        _render(draw, trigger, path)


def _render_button(
    draw: Draw, component: dict[str, object], path: tuple[str, ...]
) -> None:
    action = component.get("action")
    child = _button_child(draw, component.get("child"), path)
    if child is None or _action_kind(action) is None:
        unsupported_value()
        return
    label = text_value(draw, child.get("text"))
    if label is None:
        return
    messages: list[ui.label] = []

    def on_click() -> None:
        for old in messages:
            old.delete()
        messages.clear()
        problem = _perform_action(draw, action)
        if problem is not None:
            messages.append(
                ui.label(problem)
                .classes(f"w-full {PRE_WRAP} text-negative")
                .mark("a2ui-error")
            )

    ui.button(label, on_click=on_click).mark("a2ui-button")


def _action_kind(action: object) -> str | None:
    """ "event" for a named event, "openUrl" for a web-link function call, else None."""
    if not isinstance(action, dict):
        return None
    event = action.get("event")
    if isinstance(event, dict):
        name = event.get("name")
        context = event.get("context", {})
        return "event" if isinstance(name, str) and isinstance(context, dict) else None
    call = action.get("functionCall")
    if isinstance(call, dict) and call.get("@call") == OPEN_URL:
        return OPEN_URL if isinstance(call.get("args"), dict) else None
    return None


def _perform_action(draw: Draw, action: object) -> str | None:
    """Run a click; the visible problem line when it cannot run, else None."""
    assert isinstance(action, dict)  # narrowed by _action_kind at render time
    event = action.get("event")
    if isinstance(event, dict):
        return _send_event(draw, event)
    call = action["functionCall"]
    return _open_url(draw, call["args"].get("url"))


def _send_event(draw: Draw, event: dict[str, object]) -> str | None:
    result = resolved(draw, event.get("context", {}))
    if not result.ok or not isinstance(result.value, dict):
        return value_error_text(result.error or "context is not an object")
    name = sanitize_text(str(event["name"]))
    draw.on_action(draw.surface_id, name, result.value)
    return None


def _open_url(draw: Draw, url_value: object) -> str | None:
    result = resolved(draw, url_value)
    if not result.ok:
        return value_error_text(result.error)
    url = result.value
    if not isinstance(url, str) or _WEB_URL.fullmatch(url) is None:
        return _OPEN_URL_REFUSED
    ui.navigate.to(url, new_tab=True)
    return None


def _button_child(
    draw: Draw, child_id: object, path: tuple[str, ...]
) -> dict[str, object] | None:
    """The button's Text child, or None when it has none."""
    child = draw.components.get(child_id) if isinstance(child_id, str) else None
    if child is None or child.get("component") != "Text" or child_id in path:
        return None
    return child
