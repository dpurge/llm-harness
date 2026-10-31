"""web_extract: main text out of HTML, offline, and the only trafilatura user."""

import ast
import socket
from pathlib import Path

import pytest

from llm_harness import web_extract
from llm_harness.web_extract import extract_text, html_to_text
from llm_harness.web_fetcher import FetchError

ARTICLE = (Path(__file__).parent / "fixtures" / "article.html").read_text("utf-8")
SRC = Path(__file__).parent.parent / "src" / "llm_harness"


def assert_article_text(text: str) -> None:
    assert "How tide tables are built" in text
    assert "harmonic constituents" in text
    assert "[harmonic analysis primer](https://example.org/harmonic-analysis)" in text
    assert "SCRIPT-ONLY-TEXT" not in text
    assert "NAV-ONLY-MENU-LINK" not in text
    assert "FOOTER-ONLY-COPYRIGHT" not in text


def test_html_to_text_keeps_main_content_and_markdown_link() -> None:
    assert_article_text(html_to_text(ARTICLE))


async def test_extract_text_runs_in_a_thread_and_returns_the_same_text() -> None:
    assert_article_text(await extract_text(ARTICLE))


def test_extract_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("extract tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    assert_article_text(html_to_text(ARTICLE))


@pytest.mark.parametrize("html", ["", "   ", "<html><body></body></html>"])
def test_nothing_readable_gives_empty_string(html: str) -> None:
    assert html_to_text(html) == ""


def test_extractor_failure_is_a_short_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trafilatura

    def explode(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(trafilatura, "extract", explode)
    with pytest.raises(FetchError) as caught:
        html_to_text(ARTICLE)
    assert str(caught.value) == "error: could not extract text (RuntimeError)"


def test_extract_call_uses_the_planned_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trafilatura

    seen: dict[str, object] = {}

    def record(html: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "text"

    monkeypatch.setattr(trafilatura, "extract", record)
    assert html_to_text("<p>x</p>") == "text"
    assert seen == {
        "url": None,
        "include_links": True,
        "include_comments": False,
        "output_format": "txt",
    }


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text("utf-8"), filename=str(path))


def _imports_trafilatura(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if any(name.split(".")[0] == "trafilatura" for name in names):
            return True
    return False


def test_only_web_extract_imports_trafilatura() -> None:
    importers = {
        path.name for path in SRC.rglob("*.py") if _imports_trafilatura(_parse(path))
    }
    assert importers == {"web_extract.py"}


def test_trafilatura_is_imported_lazily() -> None:
    tree = _parse(SRC / "web_extract.py")
    top_level = [
        node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    assert not _imports_trafilatura(ast.Module(body=top_level, type_ignores=[]))


# trafilatura's download helpers (urllib3 based); "download" alone is too common a word.
FORBIDDEN_NAMES = {"fetch_url", "fetch_response", "fetch_file", "downloads"}


def test_no_trafilatura_download_helper_is_referenced_in_src() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(_parse(path)):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.alias):
                names.extend(node.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                names.extend((node.module or "").split("."))
            offenders += [
                f"{path.name}:{name}" for name in names if name in FORBIDDEN_NAMES
            ]
    assert offenders == []


def test_only_extract_is_used_from_trafilatura() -> None:
    used = {
        node.attr
        for node in ast.walk(_parse(SRC / "web_extract.py"))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "trafilatura"
    }
    assert used == {"extract"}
    assert hasattr(web_extract, "extract_text")
