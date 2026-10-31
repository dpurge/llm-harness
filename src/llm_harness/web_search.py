"""Web search for the model: a provider interface and a DuckDuckGo HTML scraper.

`DuckDuckGoHtmlProvider` reads `https://html.duckduckgo.com/html/` through the
same `WebFetcher` as `web_fetch` (same guard, caps and deadline) and parses the
page with the standard library `html.parser`. Scraping is fragile by nature: the
markup can change and a bot check can appear; both have their own error text.
The `SearchProvider` interface keeps the engine replaceable.
"""

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from llm_harness.web_fetcher import FetchError, WebFetcher

SEARCH_URL = "https://html.duckduckgo.com/html/"
MSG_BOT_CHECK = "error: the search engine asked for a bot check; try again later"
MSG_LAYOUT_CHANGED = (
    "error: could not read the search results (the page layout may have changed)"
)
_BOT_CHECK_MARKERS = ("anomaly-modal", "challenge-form", "bots use duckduckgo")
_REDIRECT_HOSTS = frozenset({"duckduckgo.com", "www.duckduckgo.com"})
_REDIRECT_PATH = "/l/"


class SearchError(Exception):
    """A search that failed; `str(exc)` is a short message safe for the model."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str  # "" when the engine gave none


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        """At most `max_results` results, best first; [] when there are none."""
        ...


def _collapse(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _target_url(href: str) -> str | None:
    """The http(s) page a result link leads to, or None when it is not usable.

    DuckDuckGo wraps targets as `//duckduckgo.com/l/?uddg=<urlencoded url>`. A
    direct absolute link is accepted. Other DuckDuckGo links (ads, internal
    pages), relative links and non-http schemes are dropped.
    """
    absolute = urljoin(SEARCH_URL, href.strip())
    parts = urlsplit(absolute)
    if (parts.hostname or "") in _REDIRECT_HOSTS:
        if parts.path != _REDIRECT_PATH:
            return None
        targets = parse_qs(parts.query).get("uddg")
        if not targets:
            return None
        absolute = targets[0]
        parts = urlsplit(absolute)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return absolute


@dataclass
class _Block:
    classes: set[str]
    title: list[str] = field(default_factory=list)
    snippet: list[str] = field(default_factory=list)
    href: str | None = None
    has_snippet_element: bool = False

    @property
    def is_ad(self) -> bool:
        return any(token.startswith("result--ad") for token in self.classes)


class _ResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self.saw_no_results_marker = False
        self._block: _Block | None = None
        self._div_depth = 0
        # (kind, tag, nesting depth of that tag) while collecting a title/snippet
        self._capture: tuple[str, str, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = set(values.get("class", "").split())
        if "no-results" in classes:
            self.saw_no_results_marker = True
        if self._capture is not None and tag == self._capture[1]:
            kind, name, depth = self._capture
            self._capture = (kind, name, depth + 1)
        if tag == "div":
            if self._block is None:
                if "result" in classes:
                    self._block = _Block(classes)
                    self._div_depth = 1
            else:
                self._div_depth += 1
            return
        block = self._block
        if block is None or self._capture is not None:
            return
        if "result__a" in classes and block.href is None:
            block.href = values.get("href", "")
            self._capture = ("title", tag, 1)
        elif "result__snippet" in classes and not block.has_snippet_element:
            block.has_snippet_element = True
            self._capture = ("snippet", tag, 1)

    def handle_endtag(self, tag: str) -> None:
        if self._capture is not None and tag == self._capture[1]:
            kind, name, depth = self._capture
            self._capture = None if depth == 1 else (kind, name, depth - 1)
        if tag == "div" and self._block is not None:
            self._div_depth -= 1
            if self._div_depth == 0:
                self._finish_block()

    def handle_data(self, data: str) -> None:
        if self._block is None or self._capture is None:
            return
        target = (
            self._block.title if self._capture[0] == "title" else self._block.snippet
        )
        target.append(data)

    def close(self) -> None:
        super().close()
        if self._block is not None:  # unbalanced markup: keep what was read
            self._finish_block()

    def _finish_block(self) -> None:
        block, self._block, self._capture = self._block, None, None
        if block is None or block.is_ad or not block.href:
            return
        url = _target_url(block.href)
        title = _collapse(block.title)
        if url is None or not title:
            return
        self.results.append(
            SearchResult(title=title, url=url, snippet=_collapse(block.snippet))
        )


def parse_results(html: str) -> list[SearchResult]:
    """Results of a DuckDuckGo HTML results page; [] only for a page that says so.

    Raises `SearchError` for a bot check page and for a page that has neither
    results nor the "no results" marker (the layout has probably changed).
    """
    parser = _ResultParser()
    parser.feed(html)
    parser.close()
    if parser.results:
        return parser.results
    lowered = html.lower()
    if any(marker in lowered for marker in _BOT_CHECK_MARKERS):
        raise SearchError(MSG_BOT_CHECK)
    if parser.saw_no_results_marker:
        return []
    raise SearchError(MSG_LAYOUT_CHANGED)


class DuckDuckGoHtmlProvider:
    """DuckDuckGo's HTML endpoint, fetched through the guarded `WebFetcher`."""

    name = "duckduckgo"

    def __init__(self, fetcher: WebFetcher) -> None:
        self._fetcher = fetcher

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        url = f"{SEARCH_URL}?{urlencode({'q': query})}"
        try:
            page = await self._fetcher.fetch(url)
        except FetchError as exc:
            raise SearchError(str(exc)) from None
        return parse_results(page.text)[:max_results]
