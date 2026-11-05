"""Shared state and visible-line helpers of the A2UI NiceGUI renderer.

Every line here is a plain `ui.label`: model text is never rendered as HTML.
The one exception is `render_html_block` / `render_svg_image` (the custom
catalog's HtmlBlock and SvgImage): their text is sanitized first and shown in a
`ui.html` whose `sanitize` is a callable, so NiceGUI filters it on the server.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from nicegui import ui

from llm_harness.a2ui.media import MediaKind, media_url_allowed, media_url_refusal
from llm_harness.a2ui.sanitize import sanitize_html_block, sanitize_svg_image
from llm_harness.a2ui.validation import ResolvedValue, SurfaceValidator
from llm_harness.display import sanitize_text

PRE_WRAP = "whitespace-pre-wrap"
DIM = "text-grey llm-secondary"

# (surface_id, action name, resolved literal context of the action event)
ActionHandler = Callable[[str, str, dict[str, object]], None]
Components = dict[str, dict[str, object]]


@dataclass(frozen=True)
class Draw:
    """What one render pass of a surface knows; `at` and `inside_modal` derive copies."""

    validator: SurfaceValidator
    surface_id: str
    on_action: ActionHandler
    components: Components
    # Called after a user edit was stored, so the surface is drawn again.
    on_edit: Callable[[], None] | None = None
    # Choices of the user that must survive a redraw (selected tab).
    view: dict[str, str] | None = None
    # Data path of the template item being drawn ("/" outside templates).
    scope: str = "/"
    in_modal: bool = False
    # Host names media may load from besides this server (config `a2ui_media_hosts`).
    media_hosts: tuple[str, ...] = ()

    def at(self, scope: str) -> "Draw":
        return replace(self, scope=scope)

    def inside_modal(self) -> "Draw":
        return replace(self, in_modal=True)


def resolved(draw: Draw, value: object) -> ResolvedValue:
    return draw.validator.resolve(draw.surface_id, value, draw.scope)


def text_value(draw: Draw, value: object) -> str | None:
    """The sanitized display text of a dynamic value, or None after a visible error line."""
    result = resolved(draw, value)
    if not result.ok:
        value_error(result.error)
        return None
    shown = as_text(result.value)
    if shown is None:
        unsupported_value()
        return None
    return sanitize_text(shown)


def as_text(value: object) -> str | None:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def unsupported_component(kind: object) -> None:
    name = sanitize_text(kind) if isinstance(kind, str) else "?"
    note_line(f"[unsupported A2UI component: {name}]")


def unsupported_value() -> None:
    note_line("[unsupported A2UI value]")


def note_line(text: str) -> None:
    ui.label(text).classes(f"w-full {PRE_WRAP} {DIM}").mark("a2ui-unsupported")


def value_error_text(reason: str) -> str:
    return f"[A2UI value error: {sanitize_text(reason)}]"


def value_error(reason: str) -> None:
    error_line(value_error_text(reason))


def error_line(text: str) -> None:
    ui.label(text).classes(f"w-full {PRE_WRAP} text-negative").mark("a2ui-error")


_IMAGE_FITS = {
    "contain": "contain",
    "cover": "cover",
    "fill": "fill",
    "none": "none",
    "scaleDown": "scale-down",
}
_MAX_REASON_CHARS = 200
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def render_html_block(draw: Draw, component: dict[str, object]) -> None:
    shown = _markup_text(draw, component.get("html"))
    if shown is None:
        return
    try:
        clean = sanitize_html_block(shown)
    except ValueError as error:
        value_error(str(error)[:_MAX_REASON_CHARS])
        return
    ui.html(clean, sanitize=sanitize_html_block).classes("w-full").mark("a2ui-html")


def render_svg_image(draw: Draw, component: dict[str, object]) -> None:
    shown = _markup_text(draw, component.get("svg"))
    if shown is None:
        return
    prefix = svg_id_prefix(draw, str(component.get("id", "")))
    try:
        ready = sanitize_svg_image(shown, prefix)
    except ValueError as error:
        value_error(str(error)[:_MAX_REASON_CHARS])
        return

    def keep_or_sanitize(content: str) -> str:
        # The prefix is not idempotent: NiceGUI hands `ready` back to this filter,
        # which must not prefix it twice; anything else is sanitized from scratch.
        return content if content == ready else sanitize_svg_image(content, prefix)

    ui.html(ready, sanitize=keep_or_sanitize).classes("w-full").mark("a2ui-svg")


def svg_id_prefix(draw: Draw, component_id: str) -> str:
    """An id prefix unique per surface, component and template item on the page.

    The readable part is cut and sanitized; the digest of the exact triple keeps
    two surfaces or template items that sanitize alike apart.
    """
    triple = f"{draw.surface_id}\x00{component_id}\x00{draw.scope}"
    digest = hashlib.sha256(triple.encode()).hexdigest()[:8]
    readable = _UNSAFE_ID_CHARS.sub("_", f"{draw.surface_id}-{component_id}")[:40]
    return f"a2ui-{readable}-{digest}-"


def _markup_text(draw: Draw, value: object) -> str | None:
    """The resolved string of an `html` or `svg` property, or None after a visible line."""
    result = resolved(draw, value)
    if not result.ok:
        value_error(result.error)
        return None
    if not isinstance(result.value, str):
        unsupported_value()
        return None
    return result.value


def render_image(draw: Draw, component: dict[str, object]) -> None:
    fit = component.get("fit")
    if fit is not None and fit not in _IMAGE_FITS:
        unsupported_value()
        return
    url = media_url(draw, component.get("url"), "image")
    if url is None:
        return
    # Natural aspect: width only, never a height. `variant` is a size hint and is not used.
    image = _media_element(ui.image(""), url).classes("w-full").mark("a2ui-image")
    if fit is not None:
        image.props["fit"] = _IMAGE_FITS[str(fit)]
    description = _optional_text(draw, component.get("description"))
    if description:
        image.props["alt"] = description


def render_video(draw: Draw, component: dict[str, object]) -> None:
    url = media_url(draw, component.get("url"), "video")
    if url is None:
        return
    poster = None
    if component.get("posterUrl") is not None:
        poster = media_url(draw, component.get("posterUrl"), "image")
    video = _media_element(ui.video(""), url).classes("w-full").mark("a2ui-video")
    if poster is not None:
        video.props["poster"] = poster


def render_audio_player(draw: Draw, component: dict[str, object]) -> None:
    url = media_url(draw, component.get("url"), "audio")
    if url is None:
        return
    description = _optional_text(draw, component.get("description"))
    if description:
        ui.label(description).classes(f"w-full {PRE_WRAP} {DIM}").mark(
            "a2ui-audio-text"
        )
    _media_element(ui.audio(""), url).classes("w-full").mark("a2ui-audio")


def media_url(draw: Draw, value: object, kind: MediaKind) -> str | None:
    """The resolved URL when the media policy allows it, else None after a visible line."""
    result = resolved(draw, value)
    if not result.ok:
        value_error(result.error)
        return None
    if not media_url_allowed(result.value, draw.media_hosts, kind):
        reason = media_url_refusal(result.value, draw.media_hosts, kind)
        ui.label(f"[A2UI media refused: {reason}]").classes(
            f"w-full {PRE_WRAP} text-negative"
        ).mark("a2ui-media-refused")
        return None
    assert isinstance(result.value, str)  # an allowed url is text
    return result.value


def _media_element[E: ui.element](element: E, url: str) -> E:
    """Point a media element at `url` through its props.

    NiceGUI's `ui.image(source)` serves a server file when `source` names an
    existing one, so a model-chosen `/etc/hosts` must never be passed as `source`.
    """
    element.props["src"] = url
    return element


def _optional_text(draw: Draw, value: object) -> str | None:
    if value is None:
        return None
    return text_value(draw, value)
