"""Fetch one web page for the model, without reaching private networks.

Every hop of a request is checked twice: `check_url` on its text, and
`check_addresses` on what its host resolves to. The guarded transport checks
again at connect time and connects to the validated IP literal, so a DNS answer
that changes between the check and the connection cannot redirect the request
(DNS rebinding). TLS still uses the origin host name for SNI and certificate
verification. All failures are `FetchError` with a short, safe message: no
response body, no resolved IP address.
"""

import asyncio
import codecs
import contextlib
import functools
import ipaddress
import os
import re
import socket
import ssl
import typing
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from importlib import metadata
from urllib.parse import urljoin

import httpcore
import httpx

from llm_harness.redact import redact
from llm_harness.url_guard import (
    MSG_NON_PUBLIC_LITERAL,
    MSG_REDIRECT_DOWNGRADE,
    MSG_REDIRECT_NON_PUBLIC,
    SafeUrl,
    UrlBlocked,
    check_addresses,
    check_url,
)

MAX_BYTES = 1_000_000
DEADLINE_SECONDS = 20.0
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
TIMEOUTS = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
URL_ERROR_LIMIT = 2100
ACCEPT = "text/html,application/xhtml+xml,text/plain;q=0.9"
ACCEPT_ENCODING = "gzip, deflate"
TEXT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "application/json",
    }
)
SUPPORTED_TYPES_TEXT = "text/html, text/plain, text/markdown, application/json"

_MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,60}/[a-z0-9][a-z0-9.+-]{0,60}")

Resolver = Callable[[str, int], Awaitable[list[str]]]
_SocketOption = tuple[int, int, int] | tuple[int, int, bytes | bytearray]


class FetchError(Exception):
    """A fetch that failed; `str(exc)` is a short message safe for the model."""


@dataclass(frozen=True)
class FetchResult:
    """A fetched page: the final response, decoded, within the size cap."""

    url: str  # final URL after redirects
    status: int
    content_type: str  # media type without parameters, lowercase
    text: str
    cut: bool  # True when the body was longer than the size cap
    hops: tuple[str, ...]  # every URL requested, in order; the last is `url`

    @property
    def redirected(self) -> bool:
        return len(self.hops) > 1


def user_agent() -> str:
    try:
        version = metadata.version("llm-harness")
    except metadata.PackageNotFoundError:
        version = "0.1.0"
    return f"llm-harness/{version}"


async def system_resolver(host: str, port: int) -> list[str]:
    """Every A and AAAA address of `host`, without duplicates, in answer order."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


async def _resolve(resolver: Resolver, host: str, port: int) -> list[str]:
    if _is_ip_literal(host):
        return [host]
    return await resolver(host, port)


def _shown_host(host: str) -> str:
    """The host as it may appear in a message: never an IP address."""
    return "the requested address" if _is_ip_literal(host) else host


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connects only to validated public IP literals.

    `connect_tcp` resolves with the injected resolver, validates every address
    (one private address refuses the host) and connects the inner backend to an
    IP literal from that same answer. Unix sockets are not supported.
    """

    def __init__(
        self,
        resolver: Resolver = system_resolver,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._inner = inner if inner is not None else httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[_SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await _resolve(self._resolver, host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"could not resolve {host}") from exc
        check_addresses(host, addresses)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None  # check_addresses refuses an empty list
        raise last_error

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


@functools.cache
def _build_ssl_context(
    cert_file: str, cert_dir: str, relax_strict: bool
) -> ssl.SSLContext:
    del cert_file, cert_dir  # only the cache key: httpx reads them from os.environ
    context = httpx.create_ssl_context(verify=True, trust_env=True)
    if relax_strict:
        # Only the strict X.509 profile (Python 3.13 default) is relaxed: some
        # corporate CAs lack an Authority Key Identifier. The chain and the host
        # name are still verified.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def _verifying_ssl_context(relax_tls_strict: bool = False) -> ssl.SSLContext:
    """Verifying context: certifi's CAs, or SSL_CERT_FILE / SSL_CERT_DIR if set.

    These two variables are the only environment the fetcher reads. They add
    trust anchors (needed behind a corporate TLS proxy) and never switch
    verification off; proxy variables and .netrc stay ignored.
    `relax_tls_strict` clears only `ssl.VERIFY_X509_STRICT`.
    """
    return _build_ssl_context(
        os.environ.get("SSL_CERT_FILE", ""),
        os.environ.get("SSL_CERT_DIR", ""),
        relax_tls_strict,
    )


class _PoolStream(httpx.AsyncByteStream):
    """httpx's own wrapper (httpx._transports.default.AsyncResponseStream) is
    private, so this is the same few lines on the public httpcore types."""

    def __init__(self, stream: typing.AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_httpcore_exceptions():
            async for part in self._stream:
                yield part

    async def aclose(self) -> None:
        close = getattr(self._stream, "aclose", None)
        if close is not None:
            await close()


# Most specific classes are matched by `issubclass` below, like httpx does.
_EXCEPTION_MAP: dict[type[Exception], type[httpx.HTTPError]] = {
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
}


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    """Re-raise httpcore errors as the httpx ones, like httpx's own transport."""
    try:
        yield
    except Exception as exc:  # not BaseException: CancelledError passes through
        mapped: type[httpx.HTTPError] | None = None
        for source, target in _EXCEPTION_MAP.items():
            if isinstance(exc, source) and (
                mapped is None or issubclass(target, mapped)
            ):
                mapped = target
        if mapped is None:
            raise  # e.g. UrlBlocked: the fetcher handles it itself
        raise mapped(str(exc)) from exc


class GuardedTransport(httpx.AsyncBaseTransport):
    """An httpx transport over a connection pool with the guarded backend.

    No proxy, HTTP/1.1 only, certificate verification on. The pool connects to
    the validated IP but is told the origin host name, so SNI, the certificate
    check and the Host header all use the name, not the IP.
    """

    def __init__(
        self,
        resolver: Resolver = system_resolver,
        *,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        relax_tls_strict: bool = False,
    ) -> None:
        backend = GuardedNetworkBackend(resolver, inner=network_backend)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=_verifying_ssl_context(relax_tls_strict),
            http1=True,
            http2=False,
            network_backend=backend,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            core_response = await self._pool.handle_async_request(core_request)
        assert isinstance(core_response.stream, typing.AsyncIterable)
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_PoolStream(core_response.stream),
            extensions=core_response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def _is_non_public_block(exc: UrlBlocked) -> bool:
    text = str(exc)
    return text == MSG_NON_PUBLIC_LITERAL or text.endswith(
        "resolves to a non-public address"
    )


def _redirect_label(exc: UrlBlocked) -> str:
    return MSG_REDIRECT_NON_PUBLIC if _is_non_public_block(exc) else str(exc)


def _timeout_message(seconds: float) -> str:
    return f"error: timed out after {seconds:g} s"


_PHASE_SECONDS = {
    httpx.ConnectTimeout: TIMEOUTS.connect,
    httpx.ReadTimeout: TIMEOUTS.read,
    httpx.WriteTimeout: TIMEOUTS.write,
    httpx.PoolTimeout: TIMEOUTS.pool,
}


def _phase_timeout_message(exc: httpx.TimeoutException) -> str:
    for kind, seconds in _PHASE_SECONDS.items():
        if isinstance(exc, kind) and seconds is not None:
            return _timeout_message(seconds)
    return _timeout_message(TIMEOUTS.read or DEADLINE_SECONDS)


def _media_type(header: str | None) -> str:
    return (header or "").split(";", 1)[0].strip().lower()


def _shown_media_type(media_type: str) -> str:
    if not media_type:
        return "none"
    return media_type if _MEDIA_TYPE.fullmatch(media_type) else "invalid"


def _decoder(response: httpx.Response) -> codecs.IncrementalDecoder:
    """Header charset when it names a known codec, else UTF-8 with replacement."""
    name = response.charset_encoding
    if name:
        try:
            return codecs.getincrementaldecoder(codecs.lookup(name).name)(
                errors="replace"
            )
        except (LookupError, TypeError):
            pass
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


class WebFetcher:
    """GET a public web page: guarded, redirect-aware, size- and time-capped.

    `resolver` and `transport` can be injected (tests). Without a transport,
    each `fetch` uses a fresh `GuardedTransport` and closes it afterwards.
    An injected transport is closed after each fetch as well (httpx closes the
    transport of the client that owns it).
    """

    def __init__(
        self,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        deadline: float = DEADLINE_SECONDS,
        max_bytes: int = MAX_BYTES,
        relax_tls_strict: bool = False,
    ) -> None:
        self._relax_tls_strict = relax_tls_strict
        self._resolver: Resolver = resolver if resolver is not None else system_resolver
        self._transport = transport
        self._deadline = deadline
        self._max_bytes = max_bytes

    async def fetch(self, url: str) -> FetchResult:
        try:
            async with asyncio.timeout(self._deadline):
                return await self._fetch(url)
        except TimeoutError:
            raise FetchError(_timeout_message(self._deadline)) from None

    async def _fetch(self, url: str) -> FetchResult:
        transport = (
            self._transport
            if self._transport is not None
            else GuardedTransport(
                self._resolver, relax_tls_strict=self._relax_tls_strict
            )
        )
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=TIMEOUTS,
            headers={
                "User-Agent": user_agent(),
                "Accept": ACCEPT,
                "Accept-Encoding": ACCEPT_ENCODING,
            },
        ) as client:
            return await self._follow(client, url)

    async def _follow(self, client: httpx.AsyncClient, url: str) -> FetchResult:
        hops: list[str] = []
        current = await self._check(url, is_redirect=False)
        for redirects_so_far in range(MAX_REDIRECTS + 1):
            hops.append(current.url)
            client.cookies.clear()  # nothing from an earlier hop is ever sent
            outcome = await self._request_hop(client, current, hops)
            if isinstance(outcome, FetchResult):
                return outcome
            if redirects_so_far == MAX_REDIRECTS:
                break
            next_url = urljoin(current.url, outcome)
            if current.scheme == "https" and next_url[:5].lower() == "http:":
                raise FetchError(MSG_REDIRECT_DOWNGRADE)
            current = await self._check(next_url, is_redirect=True)
        raise FetchError(f"error: stopped after {MAX_REDIRECTS} redirects")

    async def _check(self, url: str, *, is_redirect: bool) -> SafeUrl:
        """`check_url`, then resolve and `check_addresses`, before any request."""
        try:
            safe = check_url(url)
            try:
                addresses = await _resolve(self._resolver, safe.host, safe.port)
            except OSError:
                raise FetchError(
                    f"error: could not resolve {_shown_host(safe.host)}"
                ) from None
            check_addresses(safe.host, addresses)
        except UrlBlocked as exc:
            raise FetchError(
                _redirect_label(exc) if is_redirect else str(exc)
            ) from None
        return safe

    async def _request_hop(
        self,
        client: httpx.AsyncClient,
        current: SafeUrl,
        hops: list[str],
    ) -> FetchResult | str:
        """One request: the final result, or the Location of a redirect.

        A redirect without a Location is treated like any other non-2xx answer.
        """
        try:
            async with client.stream("GET", current.url) as response:
                location = response.headers.get("location", "").strip()
                if response.status_code in REDIRECT_STATUSES and location:
                    return location
                return await self._read_final(response, current, hops)
        except FetchError:
            raise
        except UrlBlocked as exc:  # raised by the guarded backend at connect time
            raise FetchError(str(exc)) from None
        except httpx.TimeoutException as exc:
            raise FetchError(_phase_timeout_message(exc)) from None
        except (httpx.HTTPError, OSError) as exc:
            raise FetchError(
                f"error: could not connect to {_shown_host(current.host)}"
                f" ({type(exc).__name__})"
            ) from None

    async def _read_final(
        self, response: httpx.Response, current: SafeUrl, hops: list[str]
    ) -> FetchResult:
        shown_url = redact(current.url, limit=URL_ERROR_LIMIT)
        if not 200 <= response.status_code < 300:
            raise FetchError(f"error: HTTP {response.status_code} from {shown_url}")
        media_type = _media_type(response.headers.get("content-type"))
        if media_type not in TEXT_TYPES:
            raise FetchError(
                f"error: unsupported content type {_shown_media_type(media_type)};"
                f" supported: {SUPPORTED_TYPES_TEXT}"
            )
        body, cut = await self._read_capped(response)
        decoder = _decoder(response)
        return FetchResult(
            url=current.url,
            status=response.status_code,
            content_type=media_type,
            text=decoder.decode(body, final=not cut),
            cut=cut,
            hops=tuple(hops),
        )

    async def _read_capped(self, response: httpx.Response) -> tuple[bytes, bool]:
        """Decoded body bytes up to the cap, and whether more would have followed."""
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            room = self._max_bytes - size
            if len(chunk) > room:
                chunks.append(chunk[:room])
                return b"".join(chunks), True
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks), False
