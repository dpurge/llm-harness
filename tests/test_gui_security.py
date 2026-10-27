"""Host/Origin middleware tests: the ASGI app is driven directly with hand-built scopes."""

from typing import Any

import pytest

from llm_harness.gui.security import LoopbackOnlyMiddleware

PORT = 8080


class SpyApp:
    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send) -> None:
        self.scopes.append(scope)


async def never_called_receive() -> dict[str, Any]:
    raise AssertionError("the middleware must not read the request")


def make_scope(
    kind: str = "http",
    *,
    host: str | None = f"127.0.0.1:{PORT}",
    origin: str | None = None,
) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return {"type": kind, "headers": headers}


async def call(scope: dict[str, Any]) -> tuple[SpyApp, list[dict[str, Any]]]:
    inner = SpyApp()
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await LoopbackOnlyMiddleware(inner, PORT)(scope, never_called_receive, send)
    return inner, sent


def assert_http_forbidden(inner: SpyApp, sent: list[dict[str, Any]]) -> None:
    assert inner.scopes == []
    start, body = sent
    assert start["type"] == "http.response.start"
    assert start["status"] == 403
    assert (b"content-type", b"text/plain; charset=utf-8") in start["headers"]
    assert body == {"type": "http.response.body", "body": b"forbidden\n"}


def assert_websocket_closed_before_accept(
    inner: SpyApp, sent: list[dict[str, Any]]
) -> None:
    assert inner.scopes == []
    assert sent == [{"type": "websocket.close", "code": 1008}]


GOOD_HOSTS = [
    f"127.0.0.1:{PORT}",
    f"localhost:{PORT}",
    f"LOCALHOST:{PORT}",
]
BAD_HOSTS = [
    "evil.example",
    "evil.example:8080",
    "127.0.0.1.evil.example:8080",
    "localhost.evil.example:8080",
    "localhost:8081",
    "127.0.0.1",
    "localhost",
    "",
    "[::1]:8080",
    f"127.0.0.1:{PORT} ",
]
GOOD_ORIGINS = [
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
    f"HTTP://LocalHost:{PORT}",
]
BAD_ORIGINS = [
    "http://evil.example",
    "null",
    "http://127.0.0.1:9999",
    f"https://localhost:{PORT}",
    f"http://localhost:{PORT}.evil.example",
    f"http://127.0.0.1:{PORT}/",
    "",
]


@pytest.mark.parametrize("kind", ["http", "websocket"])
@pytest.mark.parametrize("host", GOOD_HOSTS)
async def test_allowed_hosts_pass_through(kind: str, host: str) -> None:
    scope = make_scope(kind, host=host)
    inner, sent = await call(scope)
    assert inner.scopes == [scope]
    assert sent == []


@pytest.mark.parametrize("host", BAD_HOSTS)
async def test_bad_hosts_get_a_plain_403_and_never_reach_the_app(host: str) -> None:
    assert_http_forbidden(*await call(make_scope(host=host)))


async def test_a_missing_host_header_is_rejected() -> None:
    assert_http_forbidden(*await call(make_scope(host=None)))


async def test_the_403_does_not_echo_the_host() -> None:
    _, sent = await call(make_scope(host="evil.example"))
    assert b"evil.example" not in repr(sent).encode()


async def test_two_host_headers_must_both_be_allowed() -> None:
    scope = make_scope()
    scope["headers"].append((b"host", b"evil.example"))
    assert_http_forbidden(*await call(scope))


@pytest.mark.parametrize("kind", ["http", "websocket"])
@pytest.mark.parametrize("origin", GOOD_ORIGINS)
async def test_allowed_origins_pass_through(kind: str, origin: str) -> None:
    scope = make_scope(kind, origin=origin)
    inner, sent = await call(scope)
    assert inner.scopes == [scope]
    assert sent == []


@pytest.mark.parametrize("origin", BAD_ORIGINS)
async def test_bad_origins_are_rejected_even_with_a_good_host(origin: str) -> None:
    assert_http_forbidden(*await call(make_scope(origin=origin)))


@pytest.mark.parametrize("host", BAD_HOSTS)
async def test_websocket_with_a_bad_host_is_closed_before_accept(host: str) -> None:
    assert_websocket_closed_before_accept(
        *await call(make_scope("websocket", host=host))
    )


@pytest.mark.parametrize("origin", BAD_ORIGINS)
async def test_websocket_with_a_bad_origin_is_closed_before_accept(
    origin: str,
) -> None:
    assert_websocket_closed_before_accept(
        *await call(make_scope("websocket", origin=origin))
    )


async def test_a_missing_origin_is_allowed() -> None:
    scope = make_scope(origin=None)
    inner, _ = await call(scope)
    assert inner.scopes == [scope]


async def test_lifespan_passes_through_untouched() -> None:
    scope = {"type": "lifespan"}
    inner, sent = await call(scope)
    assert inner.scopes == [scope]
    assert sent == []


def test_the_port_given_is_the_port_enforced() -> None:
    middleware = LoopbackOnlyMiddleware(SpyApp(), 9123)
    assert middleware._hosts == {"127.0.0.1:9123", "localhost:9123"}
