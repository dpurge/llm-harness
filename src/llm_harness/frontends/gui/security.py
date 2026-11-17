"""Host and Origin checks and a media-only Content-Security-Policy for the web GUI."""

from collections.abc import Sequence

from starlette.applications import Starlette
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_FORBIDDEN_BODY = b"forbidden\n"


class LoopbackOnlyMiddleware:
    """Serve only requests addressed to this server's own loopback name and port.

    The GUI has no login, so a hostile web page must not be able to reach it:
    a DNS-rebound name arrives with a foreign `Host`, and a cross-origin
    request or websocket arrives with a foreign `Origin`. A missing `Origin`
    is allowed because top-level navigations do not send one.
    """

    def __init__(self, app: ASGIApp, port: int) -> None:
        self._app = app
        self._hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
        self._origins = frozenset(f"http://{host}" for host in self._hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not self._is_allowed(scope):
            await self._reject(scope, send)
            return
        await self._app(scope, receive, send)

    def _is_allowed(self, scope: Scope) -> bool:
        hosts = _header_values(scope, b"host")
        origins = _header_values(scope, b"origin")
        return (
            bool(hosts)
            and all(host in self._hosts for host in hosts)
            and all(origin in self._origins for origin in origins)
        )

    @staticmethod
    async def _reject(scope: Scope, send: Send) -> None:
        if scope["type"] == "websocket":
            # Closing before accept makes the ASGI server answer HTTP 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(_FORBIDDEN_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _FORBIDDEN_BODY})


def _header_values(scope: Scope, name: bytes) -> list[str]:
    """All values of a request header, lowercased (names, hosts and schemes are)."""
    return [
        value.decode("latin-1").lower()
        for key, value in scope.get("headers", [])
        if key.lower() == name
    ]


class MediaCspMiddleware:
    """Add a Content-Security-Policy that limits only where images and media load from.

    The header is `img-src 'self' data: <hosts>; media-src 'self' data: <hosts>`
    with each configured host as `https://<host>`. No other directive is set:
    `script-src` or `default-src` would break NiceGUI's own inline scripts. Only
    `https` is listed, so a browser blocks an `http://` media URL even though
    `a2ui.media` accepts one for an allowed host. Existing response headers are
    kept; websocket and lifespan scopes pass through untouched.
    """

    def __init__(self, app: ASGIApp, media_hosts: Sequence[str] = ()) -> None:
        self._app = app
        sources = " ".join(["'self'", "data:", *(f"https://{h}" for h in media_hosts)])
        policy = f"img-src {sources}; media-src {sources}"
        self._header = (b"content-security-policy", policy.encode("ascii"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [*message.get("headers", []), self._header],
                }
            await send(message)

        await self._app(scope, receive, send_with_policy)


def add_gui_middleware(app: Starlette, port: int, media_hosts: Sequence[str]) -> None:
    """Register the GUI's middleware; call it before the app starts.

    Starlette makes the LAST one added the outermost, so the policy header is also
    on the 403 answers of the Host/Origin check.
    """
    app.add_middleware(LoopbackOnlyMiddleware, port=port)
    app.add_middleware(MediaCspMiddleware, media_hosts=tuple(media_hosts))
