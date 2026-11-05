"""Sanitizers for custom A2UI HTML and SVG components: hostile tables, offline."""

import re
from html.parser import HTMLParser

import pytest

from llm_harness.a2ui.sanitize import (
    MAX_CHARS,
    MAX_SVG_DEPTH,
    sanitize_html_block,
    sanitize_svg,
)

# HTML

# (name, input, regexes that must NOT match the output, text that must survive)
HTML_HOSTILE = [
    ("script", "<p>ok</p><script>alert(1)</script>", [r"(?i)script", r"alert"], "<p>ok</p>"),
    ("img-onerror", "<p>ok</p><img src=x onerror=alert(1)>", [r"(?i)<img", r"onerror"], "ok"),
    ("iframe", "<p>ok</p><iframe src='http://x'>inner</iframe>", [r"(?i)iframe", r"inner", r"http"], "ok"),
    ("object", "<p>ok</p><object data='http://x'>in</object>", [r"(?i)object", r"http"], "ok"),
    ("embed", "<p>ok</p><embed src='http://x'>", [r"(?i)embed", r"http"], "ok"),
    ("form", "<p>ok</p><form action='http://x'><input name=a><button>b</button></form>",
     [r"(?i)<form", r"(?i)<input", r"(?i)<button", r"action"], "ok"),
    ("style", "<p>ok</p><style>p{background:url(http://x)}</style>", [r"(?i)style", r"http"], "ok"),
    ("link", "<p>ok</p><link rel=stylesheet href='http://x'>", [r"(?i)<link", r"http"], "ok"),
    ("meta-refresh", "<p>ok</p><meta http-equiv=refresh content='0;url=http://x'>",
     [r"(?i)<meta", r"refresh", r"http"], "ok"),
    ("base", "<p>ok</p><base href='http://x/'>", [r"(?i)<base", r"http"], "ok"),
    ("svg-onload", "<p>ok</p><svg onload=alert(1)><circle r=1></circle></svg>",
     [r"(?i)<svg", r"onload", r"alert"], "ok"),
    ("href-javascript", "<a href=\"javascript:alert(1)\">x</a>", [r"(?i)javascript", r"href"], "x"),
    ("href-entity-tab", "<a href=\"jav&#x09;ascript:alert(1)\">x</a>", [r"(?i)script", r"href"], "x"),
    ("href-mixed-case-space", "<a href=\" JaVaScRiPt:alert(1)\">x</a>", [r"(?i)script", r"href"], "x"),
    ("href-newline", "<a href=\"java\nscript:alert(1)\">x</a>", [r"(?i)script:", r"href"], "x"),
    ("href-vbscript", "<a href='vbscript:x'>x</a>", [r"(?i)vbscript", r"href"], "x"),
    ("href-data-html", "<a href='data:text/html,<script>alert(1)</script>'>x</a>",
     [r"(?i)data:", r"href", r"(?i)script"], "x"),
    ("href-relative", "<a href='/internal'>x</a>", [r"href"], "x"),
    ("href-fragment", "<a href='#x'>x</a>", [r"href"], "x"),
    ("href-protocol-relative", "<a href='//evil.example/x'>x</a>", [r"href"], "x"),
    ("style-attr", "<p style='background:url(http://x)'>ok</p>", [r"style", r"http", r"url"], "ok"),
    ("class-attr", "<p class='fixed inset-0'>ok</p>", [r"class", r"fixed"], "ok"),
    ("srcset", "<img srcset='http://x 1x' src='http://x'><p>ok</p>", [r"srcset", r"http", r"(?i)<img"], "ok"),
    ("noscript-mxss", "<noscript><p title=\"</noscript><img src=x onerror=alert(1)>\">",
     [r"(?i)<img", r"onerror=", r"(?i)<noscript"], ""),
    ("math-mglyph-style", "<math><mtext><table><mglyph><style><!--</style><img title=\"--&gt;&lt;img src=1 onerror=alert(1)&gt;\">",
     [r"(?i)<img", r"(?i)<style", r"(?i)<mglyph", r"onerror=", r"(?i)<math"], ""),
    ("unclosed", "<div><p>ok<script>alert(1)", [r"(?i)script", r"alert"], "ok"),
    ("nested-forms", "<form><form><p>ok</p></form></form>", [r"(?i)<form"], "<p>ok</p>"),
    ("onclick-upper", "<p ONCLICK=alert(1)>ok</p>", [r"(?i)onclick", r"alert"], "<p>ok</p>"),
    ("onmouseover-a", "<a href='https://ok.example/' onmouseover='x()'>l</a>", [r"(?i)onmouseover"], "l"),
    ("target-blank", "<a href='https://ok.example/' target='_blank'>l</a>", [r"target"], "l"),
    ("comment", "<p>ok</p><!-- <script>alert(1)</script> -->", [r"<!--", r"script"], "ok"),
    ("template", "<template><img src=x onerror=alert(1)></template><p>ok</p>", [r"(?i)template", r"onerror"], "ok"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("html", "absent", "kept"),
    [pytest.param(h, a, k, id=name) for name, h, a, k in HTML_HOSTILE],
)
def test_html_hostile_input_is_neutralised(
    html: str, absent: list[str], kept: str
) -> None:
    out = sanitize_html_block(html)
    for pattern in absent:
        assert not re.search(pattern, out), f"{pattern!r} survived in {out!r}"
    assert kept in out


def test_html_safe_content_survives() -> None:
    html = (
        "<h1>T</h1><p>a <strong>b</strong> <em>c</em> <code>d</code></p>"
        '<ul><li>x</li></ul><table><thead><tr><th colspan="2">h</th></tr></thead>'
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table><pre>p</pre><hr><br>"
    )
    assert sanitize_html_block(html) == html


def test_html_https_link_is_kept_with_forced_rel_and_no_target() -> None:
    out = sanitize_html_block(
        "<a href='https://ok.example/p?q=1' target='_blank' rel='opener'>l</a>"
    )
    assert 'href="https://ok.example/p?q=1"' in out
    assert 'rel="noopener noreferrer nofollow"' in out
    assert "target" not in out and 'opener"' not in out


def test_html_output_contains_only_allowlisted_tags_and_attributes() -> None:
    class Collect(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[tuple[str, list[str]]] = []

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            self.seen.append((tag, [name for name, _ in attrs]))

    collector = Collect()
    collector.feed(sanitize_html_block("".join(h for _, h, _, _ in HTML_HOSTILE)))
    for tag, attributes in collector.seen:
        assert tag in {
            "h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span", "br", "hr", "ul", "ol",
            "li", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "strong",
            "em", "b", "i", "s", "code", "pre", "blockquote", "sup", "sub", "a",
        }  # fmt: skip
        assert set(attributes) <= {"href", "rel", "colspan", "rowspan"}


def test_html_oversize_is_rejected_before_parsing() -> None:
    with pytest.raises(ValueError, match="A2UI HTML block too large"):
        sanitize_html_block("a" * (MAX_CHARS + 1))
    assert sanitize_html_block("a" * MAX_CHARS) == "a" * MAX_CHARS


def test_html_empty_and_plain_text() -> None:
    assert sanitize_html_block("") == ""
    assert sanitize_html_block("1 < 2 & 3") == "1 &lt; 2 &amp; 3"


# SVG

SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'
SVG_OPEN = f'<svg {SVG_NS} viewBox="0 0 10 10">'

# (name, input, regexes that must NOT match the output, text that must survive)
SVG_HOSTILE = [
    ("script", f"{SVG_OPEN}<rect width=\"1\"/><script>alert(1)</script></svg>", [r"script", r"alert"], 'width="1"'),
    ("foreign-object", f"{SVG_OPEN}<circle r=\"1\"/><foreignObject><div xmlns=\"http://www.w3.org/1999/xhtml\">hi<script>x</script></div></foreignObject></svg>",
     [r"(?i)foreignObject", r"<div", r"hi", r"script"], 'r="1"'),
    ("onload-root", f"<svg {SVG_NS} onload=\"alert(1)\"><rect/></svg>", [r"onload", r"alert"], "<rect>"),
    ("onclick-rect", f"{SVG_OPEN}<rect onclick=\"alert(1)\" width=\"2\"/></svg>", [r"onclick", r"alert"], 'width="2"'),
    ("onclick-upper", f"{SVG_OPEN}<rect ONCLICK=\"alert(1)\" width=\"2\"/></svg>", [r"(?i)onclick", r"alert"], 'width="2"'),
    ("use-external", f"{SVG_OPEN}<use href=\"http://evil/x.svg#a\"/></svg>", [r"evil", r"href"], "<use>"),
    ("use-xlink-data", f"<svg {SVG_NS} xmlns:xlink=\"http://www.w3.org/1999/xlink\"><use xlink:href=\"data:image/svg+xml,x\"/></svg>",
     [r"data:", r"href"], "<use>"),
    ("use-javascript", f"{SVG_OPEN}<use href=\"javascript:alert(1)\"/></svg>", [r"javascript", r"href"], "<use>"),
    ("use-relative", f"{SVG_OPEN}<use href=\"other.svg#a\"/></svg>", [r"other", r"href"], "<use>"),
    ("image", f"{SVG_OPEN}<rect width=\"1\"/><image href=\"http://evil/i.png\"/></svg>", [r"image", r"evil"], 'width="1"'),
    ("anchor", f"<svg {SVG_NS} xmlns:xlink=\"http://www.w3.org/1999/xlink\"><a xlink:href=\"javascript:alert(1)\"><rect width=\"3\"/></a></svg>",
     [r"<a", r"javascript", r"href", r"width"], "<svg"),
    ("set-href", f"{SVG_OPEN}<a><set attributeName=\"href\" to=\"javascript:alert(1)\"/></a><rect/></svg>",
     [r"<set", r"javascript", r"attributeName"], "<rect>"),
    ("animate", f"{SVG_OPEN}<rect><animate attributeName=\"x\" to=\"5\"/></rect></svg>", [r"animate", r"attributeName"], "<rect>"),
    ("animate-transform", f"{SVG_OPEN}<rect><animateTransform attributeName=\"transform\"/></rect></svg>", [r"(?i)animate"], "<rect>"),
    ("style-element", f"{SVG_OPEN}<style>@import url(http://x);</style><rect/></style></svg>".replace("</style></svg>", "</svg>"),
     [r"style", r"import", r"http://x"], "<rect>"),
    ("style-attr", f"{SVG_OPEN}<rect style=\"fill:url(http://x)\" width=\"1\"/></svg>", [r"style", r"http://x"], 'width="1"'),
    ("fill-external-url", f"{SVG_OPEN}<rect fill=\"url(http://x/y.svg#g)\" width=\"1\"/></svg>", [r"http://x", r"url", r"fill"], 'width="1"'),
    ("stroke-external-quoted", f"{SVG_OPEN}<rect stroke=\"url('http://x/y.svg#g')\"/></svg>", [r"http://x", r"stroke"], "<rect>"),
    ("fill-mixed-urls", f"{SVG_OPEN}<rect fill=\"url(#g) url(http://x)\"/></svg>", [r"http://x", r"fill"], "<rect>"),
    ("fill-css-escape", f"{SVG_OPEN}<rect fill=\"\\75rl(http://x)\"/></svg>", [r"http://x", r"fill"], "<rect>"),
    ("clip-path-external", f"{SVG_OPEN}<rect clip-path=\"url(//x/y#c)\"/></svg>", [r"clip-path", r"//x/y"], "<rect>"),
    ("filter-attr", f"{SVG_OPEN}<rect filter=\"url(#f)\"/></svg>", [r"filter"], "<rect>"),
    ("data-in-value", f"{SVG_OPEN}<text font-family=\"data:x\">t</text></svg>", [r"data:", r"font-family"], ">t<"),
    ("class-attr", f"{SVG_OPEN}<rect class=\"x\"/></svg>", [r"class"], "<rect>"),
    ("bad-id", f"{SVG_OPEN}<rect id=\"a b&quot;onload=x\"/></svg>", [r"id=", r"onload"], "<rect>"),
    ("svg-prefixed-script", "<svg:svg xmlns:svg=\"http://www.w3.org/2000/svg\"><svg:rect width=\"4\"/><svg:script>alert(1)</svg:script></svg:svg>",
     [r"script", r"alert"], 'width="4"'),
    ("xhtml-script", f"{SVG_OPEN}<x:script xmlns:x=\"http://www.w3.org/1999/xhtml\">alert(1)</x:script><rect/></svg>", [r"script", r"alert", r"xhtml"], "<rect>"),
    ("script-other-namespace", f"{SVG_OPEN}<script xmlns=\"http://www.w3.org/1999/xhtml\">alert(1)</script></svg>", [r"script", r"alert"], "<svg"),
    ("text-with-script-child", f"{SVG_OPEN}<text>a<script>alert(1)</script>b</text></svg>", [r"script", r"alert"], ">ab<"),
    ("switch", f"{SVG_OPEN}<switch><foreignObject/><rect/></switch></svg>", [r"switch", r"foreign"], "<svg"),
    ("text-escaped", f"{SVG_OPEN}<text>&lt;script&gt;alert(1)&lt;/script&gt;</text></svg>", [r"<script"], "&lt;script&gt;"),
    ("xml-space-attr", f"{SVG_OPEN}<rect xml:space=\"preserve\" width=\"1\"/></svg>", [r"xml:space", r"space"], 'width="1"'),
]  # fmt: skip


@pytest.mark.parametrize(
    ("svg", "absent", "kept"),
    [pytest.param(s, a, k, id=name) for name, s, a, k in SVG_HOSTILE],
)
def test_svg_hostile_input_is_neutralised(
    svg: str, absent: list[str], kept: str
) -> None:
    out = sanitize_svg(svg)
    for pattern in absent:
        assert not re.search(pattern, out), f"{pattern!r} survived in {out!r}"
    assert kept in out


SVG_REFUSED = [
    ("doctype-entity", '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg"><text>&x;</text></svg>'),
    ("doctype-only", '<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("doctype-lowercase", '<!doctype svg><svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("entity-only", '<!ENTITY a "b"><svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("billion-laughs", '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;"><!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]><svg xmlns="http://www.w3.org/2000/svg"><text>&lol3;</text></svg>'),
    ("xml-stylesheet", '<?xml-stylesheet href="http://x/s.css"?><svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("xml-declaration", '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'),
    ("pi-inside", '<svg xmlns="http://www.w3.org/2000/svg"><?php echo 1 ?></svg>'),
    ("cdata", '<svg xmlns="http://www.w3.org/2000/svg"><text><![CDATA[<script>alert(1)</script>]]></text></svg>'),
    ("cdata-lowercase", '<svg xmlns="http://www.w3.org/2000/svg"><text><![cdata[x]]></text></svg>'),
    ("undefined-entity", '<svg xmlns="http://www.w3.org/2000/svg"><text>&x;</text></svg>'),
    ("unbound-prefix", '<svg xmlns="http://www.w3.org/2000/svg"><x:script>a</x:script></svg>'),
    ("not-svg-root", "<div>hi</div>"),
    ("html-root", "<html><body><svg/></body></html>"),
    ("foreign-namespace-root", '<svg xmlns="http://evil.example/ns"><rect/></svg>'),
    ("prefixed-foreign-root", '<s:svg xmlns:s="http://evil.example/ns"/>'),
    ("not-xml", "not xml at all"),
    ("malformed", '<svg xmlns="http://www.w3.org/2000/svg"><rect></svg>'),
    ("empty", ""),
    ("whitespace", "   \n"),
]  # fmt: skip


@pytest.mark.parametrize("svg", [pytest.param(s, id=name) for name, s in SVG_REFUSED])
def test_svg_refused_input_raises(svg: str) -> None:
    with pytest.raises(ValueError):
        sanitize_svg(svg)


def test_svg_depth_limit() -> None:
    def nested(levels: int) -> str:
        return (
            f"<svg {SVG_NS}>" + "<g>" * (levels - 1) + "</g>" * (levels - 1) + "</svg>"
        )

    assert sanitize_svg(nested(MAX_SVG_DEPTH)).count("<g>") == MAX_SVG_DEPTH - 1
    with pytest.raises(ValueError, match="nested too deeply"):
        sanitize_svg(nested(MAX_SVG_DEPTH + 1))


def test_svg_depth_limit_counts_dropped_elements() -> None:
    svg = (
        f"<svg {SVG_NS}>" + "<foreignObject>" * 30 + "</foreignObject>" * 30 + "</svg>"
    )
    with pytest.raises(ValueError, match="nested too deeply"):
        sanitize_svg(svg)


def test_svg_oversize_is_rejected() -> None:
    with pytest.raises(ValueError, match="too large"):
        sanitize_svg(f"<svg {SVG_NS}>" + " " * MAX_CHARS + "</svg>")


def test_svg_external_entity_is_never_resolved(tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    svg = f'<!DOCTYPE svg [<!ENTITY x SYSTEM "{secret.as_uri()}">]><svg {SVG_NS}><text>&x;</text></svg>'
    with pytest.raises(ValueError):
        sanitize_svg(svg)


GOOD_SVG = (
    f'<svg {SVG_NS} viewBox="0 0 100 50" width="200" height="100">'
    "<title>Chart</title><desc>d</desc>"
    '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="0" gradientUnits="objectBoundingBox">'
    '<stop offset="0" stop-color="#f00"></stop><stop offset="1" stop-color="#00f"></stop>'
    '</linearGradient><clipPath id="c"><rect x="0" y="0" width="50" height="50"></rect></clipPath></defs>'
    '<rect x="1" y="2" width="40" height="30" rx="2" ry="2" fill="url(#g)" stroke="#000" '
    'stroke-width="2" opacity="0.5" fill-opacity="0.9" stroke-opacity="1"></rect>'
    '<circle cx="60" cy="25" r="10" fill="red" clip-path="url(#c)"></circle>'
    '<path d="M0 0 L10 10 Z" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="2 2" '
    'transform="translate(1 1)"></path>'
    '<polyline points="0,0 1,1"></polyline><polygon points="0,0 1,1 2,0"></polygon>'
    '<ellipse cx="1" cy="1" rx="1" ry="2"></ellipse><line x1="0" y1="0" x2="5" y2="5"></line>'
    '<text x="5" y="9" font-size="4" font-family="sans-serif" font-weight="bold" '
    'text-anchor="middle" dominant-baseline="middle">a &amp; b<tspan>c</tspan></text>'
    '<symbol id="s"><rect width="1" height="1"></rect></symbol><use href="#s" x="3"></use>'
    "</svg>"
)


def test_svg_valid_document_round_trips_intact() -> None:
    assert sanitize_svg(GOOD_SVG) == GOOD_SVG


def test_svg_fragment_href_is_kept_and_xlink_is_normalised() -> None:
    xlink = f'<svg {SVG_NS} xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="#ok"/></svg>'
    assert 'href="#ok"' in sanitize_svg(f'{SVG_OPEN}<use href="#ok"/></svg>')
    assert 'href="#ok"' in sanitize_svg(xlink)


def test_svg_local_url_is_kept_and_external_url_removed_in_one_document() -> None:
    out = sanitize_svg(
        f'{SVG_OPEN}<rect fill="url(#g)" stroke="url(http://x/y.svg#g)"/></svg>'
    )
    assert 'fill="url(#g)"' in out
    assert "stroke" not in out and "http://x" not in out


def test_svg_output_root_has_namespace_even_if_input_has_none() -> None:
    assert sanitize_svg("<svg><rect/></svg>").startswith(f"<svg {SVG_NS}>")


def test_svg_attribute_values_are_escaped() -> None:
    out = sanitize_svg(f'{SVG_OPEN}<text font-family="a&quot;&gt;&lt;b">x</text></svg>')
    assert 'font-family="a&quot;&gt;&lt;b"' in out
    assert "<b" not in out


# Both


@pytest.mark.parametrize("html", [h for _, h, _, _ in HTML_HOSTILE])
def test_html_is_idempotent(html: str) -> None:
    once = sanitize_html_block(html)
    assert sanitize_html_block(once) == once


@pytest.mark.parametrize("svg", [s for _, s, _, _ in SVG_HOSTILE] + [GOOD_SVG])
def test_svg_is_idempotent(svg: str) -> None:
    once = sanitize_svg(svg)
    assert sanitize_svg(once) == once
