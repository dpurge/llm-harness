"""Host and Origin checks for the unauthenticated, loopback-only web GUI."""

from starlette.types import ASGIApp, Receive, Scope, Send

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
