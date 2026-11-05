"""The media-only Content-Security-Policy, and the route guarantees of the A2UI feature."""

from typing import Any

import pytest
from nicegui import app as nicegui_app
from nicegui import ui
from nicegui.testing import User
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from llm_harness.config import GuiConfig
from llm_harness.gui.app import run_gui
from llm_harness.gui.security import MediaCspMiddleware, add_gui_middleware
from tests.test_gui import (
    CONFIG,
    MODEL_1,
    REPLY,
    Factory,
    StepFactory,
    open_chat,
    router_factory_for,
)

PORT = 8080
HEADER = b"content-security-policy"
EMPTY_POLICY = "img-src 'self' data:; media-src 'self' data:"
TWO_HOSTS_POLICY = (
    "img-src 'self' data: https://a.example.com https://b.example.org; "
    "media-src 'self' data: https://a.example.com https://b.example.org"
)


class Inner:
    """A bare ASGI app that answers http with the given headers and ignores the rest."""

    def __init__(self, headers: list[tuple[bytes, bytes]] | None = None) -> None:
        self.headers = headers or [(b"content-type", b"text/plain")]
        self.scopes: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send) -> None:
        self.scopes.append(scope)
        if scope["type"] == "http":
            await send(
                {"type": "http.response.start", "status": 200, "headers": self.headers}
            )
            await send({"type": "http.response.body", "body": b"ok"})


async def no_receive() -> dict[str, Any]:
    raise AssertionError("must not read the request")


async def drive(app, scope: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, no_receive, send)
    return sent


def http_scope(
    host: str | None = f"127.0.0.1:{PORT}", path: str = "/", origin: str | None = None
) -> dict[str, Any]:
    headers = []
    if host is not None:
        headers.append((b"host", host.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "scheme": "http",
        "headers": headers,
    }


def policies(message: dict[str, Any]) -> list[str]:
    return [v.decode() for k, v in message["headers"] if k == HEADER]


@pytest.mark.parametrize(
    ("hosts", "expected"),
    [((), EMPTY_POLICY), (("a.example.com", "b.example.org"), TWO_HOSTS_POLICY)],
)
async def test_the_header_has_the_exact_value(
    hosts: tuple[str, ...], expected: str
) -> None:
    sent = await drive(MediaCspMiddleware(Inner(), hosts), http_scope())
    start, body = sent
    assert start["status"] == 200
    assert policies(start) == [expected]
    assert body["body"] == b"ok"


async def test_only_image_and_media_fetch_directives_are_set() -> None:
    (start, _) = await drive(
        MediaCspMiddleware(Inner(), ("a.example.com",)), http_scope()
    )
    (policy,) = policies(start)
    directives = [part.split()[0] for part in policy.split(";")]
    assert directives == ["img-src", "media-src"]  # no script-src, default-src, ...


async def test_existing_headers_are_kept_in_order() -> None:
    inner = Inner([(b"content-type", b"text/html"), (b"x-other", b"1")])
    (start, _) = await drive(MediaCspMiddleware(inner, ()), http_scope())
    assert start["headers"][:2] == [(b"content-type", b"text/html"), (b"x-other", b"1")]
    assert policies(start) == [EMPTY_POLICY]


async def test_a_response_without_headers_still_gets_the_header() -> None:
    inner = Inner([])
    inner.headers = []
    (start, _) = await drive(MediaCspMiddleware(inner, ()), http_scope())
    assert policies(start) == [EMPTY_POLICY]


async def test_the_body_message_is_not_touched() -> None:
    (_, body) = await drive(MediaCspMiddleware(Inner(), ()), http_scope())
    assert body == {"type": "http.response.body", "body": b"ok"}


@pytest.mark.parametrize("kind", ["websocket", "lifespan"])
async def test_other_scopes_pass_through_unchanged(kind: str) -> None:
    inner = Inner()
    scope = {"type": kind, "headers": []}
    sent = await drive(MediaCspMiddleware(inner, ("a.example.com",)), scope)
    assert inner.scopes == [scope]
    assert inner.scopes[0] is scope
    assert sent == []


async def test_a_forbidden_answer_of_the_loopback_check_carries_the_header_too() -> (
    None
):
    app = Starlette(routes=[Route("/", lambda request: PlainTextResponse("ok"))])
    add_gui_middleware(app, PORT, ("a.example.com",))
    sent = await drive(app, http_scope(host="evil.example"))
    assert sent[0]["status"] == 403
    expected = (
        "img-src 'self' data: https://a.example.com; "
        "media-src 'self' data: https://a.example.com"
    )
    assert policies(sent[0]) == [expected]


# --- routes (criterion 16) ---------------------------------------------------

SOCKET_PATH = "/_nicegui_ws/socket.io/"


async def echo_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.close()


def guarded_app() -> Starlette:
    """A bare app with the middleware registered exactly as `run_gui` does."""
    app = Starlette(
        routes=[
            Route(SOCKET_PATH, lambda request: PlainTextResponse("polling")),
            WebSocketRoute(SOCKET_PATH, echo_socket),
        ]
    )
    add_gui_middleware(app, PORT, ())
    return app


def socket_scope(kind: str, host: str, origin: str | None = None) -> dict[str, Any]:
    scope = http_scope(host=host, path=SOCKET_PATH, origin=origin)
    if kind == "websocket":
        scope = {**scope, "type": "websocket", "scheme": "ws", "subprotocols": []}
        scope.pop("method")
    return scope


async def test_the_socket_path_answers_a_good_host_through_the_stack() -> None:
    sent = await drive(guarded_app(), socket_scope("http", f"localhost:{PORT}"))
    assert sent[0]["status"] == 200
    assert policies(sent[0]) == [EMPTY_POLICY]


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("evil.example", None),
        ("evil.example:8080", "http://127.0.0.1:8080"),
        (f"127.0.0.1:{PORT}", "http://evil.example"),
    ],
)
async def test_a_foreign_host_or_origin_gets_403_on_the_socket_path(
    host: str, origin: str | None
) -> None:
    sent = await drive(guarded_app(), socket_scope("http", host, origin))
    assert sent[0]["status"] == 403
    assert sent[1]["body"] == b"forbidden\n"


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("evil.example", None),
        (f"127.0.0.1:{PORT}", "http://evil.example"),
    ],
)
async def test_a_foreign_host_or_origin_websocket_is_closed_with_1008(
    host: str, origin: str | None
) -> None:
    sent = await drive(guarded_app(), socket_scope("websocket", host, origin))
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_a_good_websocket_reaches_the_app() -> None:
    queue = [{"type": "websocket.connect"}]

    async def receive() -> dict[str, Any]:
        return queue.pop(0) if queue else {"type": "websocket.disconnect", "code": 1000}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = socket_scope("websocket", f"127.0.0.1:{PORT}", f"http://127.0.0.1:{PORT}")
    await guarded_app()(scope, receive, send)
    assert [m["type"] for m in sent] == ["websocket.accept", "websocket.close"]


async def test_the_gui_has_no_a2ui_route(user: User) -> None:
    """Widgets are server-side NiceGUI elements: no widget bundle, no action channel."""
    await open_chat(user, StepFactory([]))
    paths = [str(getattr(route, "path", "")) for route in nicegui_app.routes]
    assert "/" in paths  # the chat page is registered, so the list is not empty
    assert any(path.startswith("/_nicegui_ws") for path in paths)
    assert [path for path in paths if "a2ui" in path.lower()] == []


async def test_run_gui_passes_the_configured_media_hosts_to_the_policy(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[type, dict[str, Any]]] = []
    monkeypatch.setattr(ui, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        nicegui_app, "add_middleware", lambda cls, **kw: calls.append((cls, kw))
    )
    config = CONFIG.model_copy(
        update={"gui": GuiConfig(a2ui_media_hosts=["a.example.com"])}
    )
    run_gui(config, MODEL_1, router_factory_for(config, Factory(REPLY)), False)
    assert calls[-1] == (MediaCspMiddleware, {"media_hosts": ("a.example.com",)})
