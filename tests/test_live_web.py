"""Live web checks; deselected by default, run with `pytest -m live`.

They call the real DuckDuckGo and example.com through `WebFetcher`. Behind a
TLS-inspecting proxy, point `SSL_CERT_FILE` at a CA bundle that includes the
proxy's CA, and set `LLM_HARNESS_LIVE_RELAX_TLS_STRICT=1` if the proxy CA breaks
Python's strict X.509 profile (chain and host name stay verified).
"""

import os

import pytest

from llm_harness.web_fetcher import WebFetcher
from llm_harness.web_search import DuckDuckGoHtmlProvider
from llm_harness.web_tools import (
    WebFetchInput,
    WebFetchTool,
    WebSearchInput,
    WebSearchTool,
)
from tests.test_builtin_tools import ctx_for

pytestmark = pytest.mark.live


def live_fetcher() -> WebFetcher:
    relax = os.environ.get("LLM_HARNESS_LIVE_RELAX_TLS_STRICT") == "1"
    return WebFetcher(relax_tls_strict=relax)


async def test_live_web_search() -> None:
    provider = DuckDuckGoHtmlProvider(live_fetcher())
    results = await provider.search("python httpx", 5)
    assert len(results) >= 1
    assert all(r.url.startswith(("http://", "https://")) for r in results)
    tool = WebSearchTool(provider)
    text = await tool.run(WebSearchInput(query="python httpx"), ctx_for("web_search"))
    assert text.startswith('<untrusted-web-content source="duckduckgo"'), text


async def test_live_web_fetch() -> None:
    tool = WebFetchTool(live_fetcher())
    text = await tool.run(
        WebFetchInput(url="https://example.com/"), ctx_for("web_fetch")
    )
    assert "Example Domain" in text, text
