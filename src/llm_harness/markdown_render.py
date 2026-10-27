"""Markdown for the chat front ends: one parser setup, one HTML allowlist.

Model text is untrusted. The parser never interprets raw HTML (`html=False`),
images never reach the output (a prompt-injected image URL would leak the chat
to another server), and the GUI runs the HTML through `sanitize_html` as well.

Extensions live in `_EXTENSIONS`; adding one is a one-line change. Textual's
Markdown widget cannot show some of them, see `_TEXTUAL_UNSUPPORTED`.
"""

import re
from collections.abc import Callable
from functools import partial
from html import escape

import nh3
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

# Each entry takes the parser and configures it. CommonMark itself is the base.
_EXTENSIONS: tuple[Callable[[MarkdownIt], object], ...] = (
    partial(MarkdownIt.enable, names=["table", "strikethrough", "linkify"]),
    tasklists_plugin,
    footnote_plugin,
)

# Textual's Markdown widget crashes on footnote tokens (an unmatched `_close`
# token pops an empty stack) and drops the checkbox that the task list plugin
# emits as raw inline HTML, so the terminal parser leaves these two out.
_TEXTUAL_UNSUPPORTED: tuple[Callable[[MarkdownIt], object], ...] = (
    tasklists_plugin,
    footnote_plugin,
)

# Pygments lexers are regex based and slow on huge input; show such code plain.
_MAX_HIGHLIGHT_CHARS = 20_000
_FORMATTER = HtmlFormatter(nowrap=True)


def _highlight_code(code: str, language: str, attrs: str) -> str:
    """Highlighted, HTML-escaped code; "" lets markdown-it escape it as plain."""
    if not language or len(code) > _MAX_HIGHLIGHT_CHARS:
        return ""
    try:
        lexer = get_lexer_by_name(language, stripnl=False)
    except ClassNotFound:
        return ""
    return highlight(code, lexer, _FORMATTER)


def _render_image_as_alt_text(
    self: RendererHTML,
    tokens: list[Token],
    idx: int,
    options: OptionsDict,
    env: EnvType,
) -> str:
    return escape(self.renderInlineAsText(tokens[idx].children, options, env))


def _build_parser(extensions: tuple[Callable[[MarkdownIt], object], ...]) -> MarkdownIt:
    parser = MarkdownIt(
        "commonmark",
        {"html": False, "linkify": True, "highlight": _highlight_code},
    )
    for extension in extensions:
        extension(parser)
    return parser


_HTML_PARSER = _build_parser(_EXTENSIONS)
_HTML_PARSER.add_render_rule("image", _render_image_as_alt_text)


def parser_factory() -> MarkdownIt:
    """A fresh parser for Textual's Markdown widget (see `_TEXTUAL_UNSUPPORTED`)."""
    supported = tuple(e for e in _EXTENSIONS if e not in _TEXTUAL_UNSUPPORTED)
    return _build_parser(supported)


def render_html(text: str) -> str:
    """Markdown to HTML; raw HTML in `text` is escaped, images become alt text."""
    return _HTML_PARSER.render(text)


# Sanitizer. Whatever the parser emits, only this allowlist reaches the browser.
_ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "s",
    "del", "code", "pre", "blockquote", "ul", "ol", "li", "input", "a", "table",
    "thead", "tbody", "tr", "th", "td", "sup", "section", "div", "span",
}  # fmt: skip
# "*" must be listed: ammonia otherwise keeps its default generic `title` and `lang`.
_ALLOWED_ATTRIBUTES = {
    "*": set(),
    "code": {"class"},
    "pre": {"class"},
    "span": {"class"},
    "div": {"class"},
    "sup": {"class"},
    "section": {"class"},
    "li": {"class", "id"},
    "a": {"href", "id"},
    "input": {"type", "checked"},
}
_SAFE_CLASS = re.compile(r"[A-Za-z0-9_-]+( [A-Za-z0-9_-]+)*")
# Footnote anchors only (fn1, fnref1, fnref1:1); never an id that could shadow a page global.
_SAFE_ID = re.compile(r"fn(ref)?[0-9]+(:[0-9]+)?")
_SAFE_HREF = re.compile(r"(https?:|mailto:|#)", re.IGNORECASE)


def _filter_attribute(tag: str, attribute: str, value: str) -> str | None:
    if attribute == "class":
        return value if _SAFE_CLASS.fullmatch(value) else None
    if attribute == "id":
        return value if _SAFE_ID.fullmatch(value) else None
    if attribute == "href":
        return value if _SAFE_HREF.match(value) else None
    if tag == "input" and attribute == "type":
        return value if value == "checkbox" else None
    return value


def _keep_fragment_only(url: str) -> str | None:
    """Relative URLs are dropped (also `//host/x`) except `#fragment`."""
    return url if url.startswith("#") else None


def sanitize_html(html: str) -> str:
    """Strip everything but the allowlisted tags, attributes and URL schemes.

    Idempotent. ammonia orders `rel` before `target` on a link whose `href` it
    dropped, and the other way round when it reads that output again; a second
    pass reaches the stable form.
    """
    return _drop_target_on_fragment_links(_clean(_clean(html)))


# One opening tag, skipping `>` inside quoted attribute values.
_ANCHOR_TAG = re.compile(r'<a\b(?:[^>"]|"[^"]*")*>')
_FRAGMENT_HREF = re.compile(r'\shref="#')


def _drop_target_on_fragment_links(html: str) -> str:
    """A footnote link must scroll this page: a new tab would open a second chat session.

    ammonia can only set `target` on every link, so the fragment ones are fixed here.
    """

    def fix(match: re.Match[str]) -> str:
        tag = match.group(0)
        if _FRAGMENT_HREF.search(tag):
            return tag.replace(' target="_blank"', "")
        return tag

    return _ANCHOR_TAG.sub(fix, html)


def _clean(html: str) -> str:
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        clean_content_tags={"script", "style"},
        attributes=_ALLOWED_ATTRIBUTES,
        attribute_filter=_filter_attribute,
        url_schemes={"http", "https", "mailto"},
        url_relative=_keep_fragment_only,
        link_rel="noopener noreferrer",
        set_tag_attribute_values={
            "a": {"target": "_blank"},
            "input": {"disabled": ""},
        },
        strip_comments=True,
    )


def pygments_css(container_class: str) -> str:
    """Token colours for highlighted code inside `.container_class`."""
    return HtmlFormatter(style="default").get_style_defs(f".{container_class}")
