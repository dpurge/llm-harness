"""Turn a fetched HTML page into readable text.

The only module that imports `trafilatura`, and only its `extract` function,
which works on a string already in memory. The download helpers of
trafilatura (urllib3 based) are never used: they would bypass the URL guard in
`web_fetcher`. The import is lazy because it costs time that a session without
web tools should not pay.
"""

import asyncio

from llm_harness.web_fetcher import FetchError


def html_to_text(html: str) -> str:
    """Main text of `html` with links as markdown; "" when nothing is readable."""
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
            url=None,
            include_links=True,
            include_comments=False,
            output_format="txt",
        )
    except Exception as exc:  # noqa: BLE001 - third-party extractor; becomes FetchError
        raise FetchError(
            f"error: could not extract text ({type(exc).__name__})"
        ) from None
    return text.strip() if text else ""


async def extract_text(html: str) -> str:
    """`html_to_text` in a worker thread, so the event loop stays free."""
    return await asyncio.to_thread(html_to_text, html)
