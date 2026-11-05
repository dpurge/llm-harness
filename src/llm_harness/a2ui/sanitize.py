"""Sanitizers for model-supplied HTML and SVG (A2UI custom components).

Both are pure functions: untrusted text in, a string that is safe to inline out,
`ValueError` for input that is refused rather than cleaned.

SVG uses `xml.etree.ElementTree` (not nh3): it parses as XML, so there is no HTML
parser namespace confusion (mutation XSS), element and attribute case is kept, and
the output is rebuilt from an allowlist, never copied. DOCTYPE, entity, CDATA and
processing instructions are refused before parsing, so no entity is ever expanded.
Attribute values that nh3 would leave untouched (`url(...)`) need a value check anyway.
"""

import re
from dataclasses import dataclass
from html import escape
from xml.etree import ElementTree

import nh3

MAX_CHARS = 100_000
MAX_SVG_DEPTH = 20

# HTML. No img, svg, form controls, style or script: no external loading, no CSS.
# Links keep only absolute http(s) URLs; nh3 forces `rel` and `target` is not allowed.
_HTML_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span", "br", "hr", "ul", "ol",
    "li", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "strong",
    "em", "b", "i", "s", "code", "pre", "blockquote", "sup", "sub", "a",
}  # fmt: skip
# "*" must be listed: ammonia otherwise keeps its default generic `title` and `lang`.
_HTML_ATTRIBUTES = {
    "*": set(),
    "a": {"href"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
# Dropped with their content; other disallowed tags only lose the tag, the text stays.
_HTML_DROP_CONTENT = {"script", "style", "noscript", "template", "iframe", "object"}
_HTML_LINK_REL = "noopener noreferrer nofollow"


def _clean_html(html: str) -> str:
    return nh3.clean(
        html,
        tags=_HTML_TAGS,
        clean_content_tags=_HTML_DROP_CONTENT,
        attributes=_HTML_ATTRIBUTES,
        url_schemes={"http", "https"},
        url_relative="deny",
        link_rel=_HTML_LINK_REL,
        strip_comments=True,
    )


def sanitize_html_block(html: str) -> str:
    """Allowlisted structural HTML; idempotent (a second pass reaches the stable form)."""
    if len(html) > MAX_CHARS:
        raise ValueError("A2UI HTML block too large")
    return _clean_html(_clean_html(html))


# SVG.
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
_SVG_TAGS = frozenset({
    "svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line",
    "polyline", "polygon", "text", "tspan", "linearGradient", "radialGradient", "stop",
    "clipPath", "mask", "symbol", "use", "marker",
})  # fmt: skip
_SVG_TEXT_TAGS = frozenset({"title", "desc", "text", "tspan"})
_SVG_HREF_TAGS = frozenset({"use", "linearGradient", "radialGradient"})
_SVG_ATTRIBUTES = frozenset({
    "fill", "stroke", "stroke-width", "d", "points", "x", "y", "cx", "cy", "r", "rx",
    "ry", "x1", "y1", "x2", "y2", "width", "height", "viewBox", "transform", "opacity",
    "fill-opacity", "stroke-opacity", "stroke-linecap", "stroke-linejoin",
    "stroke-dasharray", "font-size", "font-family", "font-weight", "text-anchor",
    "dominant-baseline", "id", "offset", "stop-color", "gradientUnits",
    "gradientTransform", "clip-path", "mask", "marker-start", "marker-mid",
    "marker-end", "fill-rule", "preserveAspectRatio",
})  # fmt: skip
_FRAGMENT = re.compile(r"#[A-Za-z_][A-Za-z0-9_.-]*")
_SAFE_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_URL_CALL = re.compile(r"url\(", re.IGNORECASE)
_LOCAL_URL_CALL = re.compile(r"url\(\s*#[A-Za-z_][A-Za-z0-9_.-]*\s*\)", re.IGNORECASE)
_REFUSED_MARKUP = ("<!doctype", "<!entity", "<?", "<![cdata[")
_SCHEME = re.compile(r"(javascript|vbscript|data):", re.IGNORECASE)
_LOCAL_URL_REFERENCE = re.compile(
    r"url\(\s*#([A-Za-z_][A-Za-z0-9_.-]*)\s*\)", re.IGNORECASE
)
_SCOPE_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_FIT_STYLE = "width:100%;height:auto"


@dataclass(frozen=True)
class _SvgScope:
    """How an SVG is made safe to share a page: ids prefixed, root sized by CSS."""

    prefix: str = ""
    fit_container: bool = False


def _is_safe_value(name: str, value: str) -> bool:
    # CSS escapes (`\75rl(`) and comments could hide `url(`; no attribute needs them.
    if "\\" in value or "/*" in value:
        return False
    if _SCHEME.search(re.sub(r"\s+", "", value)):
        return False
    if len(_URL_CALL.findall(value)) != len(_LOCAL_URL_CALL.findall(value)):
        return False
    if name == "id":
        return _SAFE_ID.fullmatch(value) is not None
    return True


def _local_name(qualified: str, namespace: str) -> str | None:
    """`{ns}name` gives `name` for the wanted namespace; an unqualified name is kept."""
    if qualified.startswith("{"):
        uri, _, name = qualified[1:].partition("}")
        return name if uri == namespace else None
    return qualified


def _check_depth(root: ElementTree.Element) -> None:
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        if depth > MAX_SVG_DEPTH:
            raise ValueError("A2UI SVG nested too deeply")
        stack.extend((child, depth + 1) for child in element)


def _clean_attributes(
    tag: str, element: ElementTree.Element, scope: _SvgScope
) -> list[tuple[str, str]]:
    kept: dict[str, str] = {}
    for qualified, value in element.attrib.items():
        if qualified.startswith("{"):
            name = _local_name(qualified, _XLINK_NAMESPACE)
            if name != "href":
                continue
        else:
            name = qualified
        if name == "href":
            if tag in _SVG_HREF_TAGS and _FRAGMENT.fullmatch(value):
                kept["href"] = f"#{scope.prefix}{value[1:]}"
        elif name in _SVG_ATTRIBUTES and _is_safe_value(name, value):
            kept[name] = _scoped_value(name, value, scope.prefix)
    return list(kept.items())


def _scoped_value(name: str, value: str, prefix: str) -> str:
    """`id` and every `url(#id)` of a checked value get `prefix`; a reference to
    an id that does not exist stays dangling, now inside the prefix too."""
    if name == "id":
        return prefix + value
    return _LOCAL_URL_REFERENCE.sub(lambda m: f"url(#{prefix}{m.group(1)})", value)


def _serialize(
    element: ElementTree.Element, root: bool, scope: _SvgScope
) -> str | None:
    tag = _local_name(element.tag, _SVG_NAMESPACE)
    if tag not in _SVG_TAGS:
        return None
    attributes = _clean_attributes(tag, element, scope)
    if root:
        if scope.fit_container:
            attributes = [a for a in attributes if a[0] not in ("width", "height")]
            attributes.append(("style", _FIT_STYLE))
        attributes.insert(0, ("xmlns", _SVG_NAMESPACE))
    rendered = "".join(
        f' {name}="{escape(value, quote=True)}"' for name, value in attributes
    )
    text = escape(element.text or "") if tag in _SVG_TEXT_TAGS else ""
    parts = [f"<{tag}{rendered}>", text]
    for child in element:
        child_markup = _serialize(child, root=False, scope=scope)
        if child_markup is not None:
            parts.append(child_markup)
        if tag in _SVG_TEXT_TAGS:
            parts.append(escape(child.tail or ""))
    parts.append(f"</{tag}>")
    return "".join(parts)


def sanitize_svg(svg: str) -> str:
    """One allowlisted `<svg>` element, rebuilt from the parsed tree; idempotent.

    Raises `ValueError` for oversize, empty or malformed input, a DOCTYPE, entity,
    CDATA or processing instruction, a root that is not svg, or nesting over 20.
    Unknown elements are dropped with their subtree; unknown attributes are dropped.
    """
    return _render_svg(svg, _SvgScope())


def sanitize_svg_image(svg: str, prefix: str) -> str:
    """`sanitize_svg` for one page-wide image: ids scoped, root fitted to its container.

    `prefix` is put before every `id`, `href="#id"` and `url(#id)`. Two inline
    SVGs on one page share one id namespace; distinct prefixes keep their
    gradients and `use` references apart, and a reference can no longer reach an
    element of the page outside the SVG. A reference to an id that does not
    exist stays dangling, inside the prefix too. `prefix` must match
    `[A-Za-z_][A-Za-z0-9_-]*`, else `ValueError`; the result is not idempotent.

    The root `width` and `height` are dropped (the `viewBox` stays) and the root
    gets `style="width:100%;height:auto"`, added here after sanitizing because
    the sanitizer drops every `style`.
    """
    scope = _SvgScope(prefix=_checked_prefix(prefix), fit_container=True)
    return _render_svg(svg, scope)


def _checked_prefix(prefix: str) -> str:
    if _SCOPE_PREFIX.fullmatch(prefix) is None:
        raise ValueError("A2UI SVG id prefix must match [A-Za-z_][A-Za-z0-9_-]*")
    return prefix


def _render_svg(svg: str, scope: _SvgScope) -> str:
    if len(svg) > MAX_CHARS:
        raise ValueError("A2UI SVG too large")
    lowered = svg.lower()
    for marker in _REFUSED_MARKUP:
        if marker in lowered:
            raise ValueError(f"A2UI SVG may not contain {marker}")
    try:
        root = ElementTree.fromstring(svg.strip())
    except ElementTree.ParseError as error:
        raise ValueError(f"A2UI SVG is not well-formed XML: {error}") from error
    if _local_name(root.tag, _SVG_NAMESPACE) != "svg":
        raise ValueError("A2UI SVG root element must be <svg>")
    _check_depth(root)
    markup = _serialize(root, root=True, scope=scope)
    assert markup is not None  # the root was just checked to be svg
    return markup
