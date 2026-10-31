"""Pure URL and IP checks that keep the web tools away from private networks.

No network and no DNS here: `check_url` validates the text of a URL, and
`check_addresses` validates the addresses a resolver returned for its host.
Every message is short and safe to show to the model: no IP address, no
user name or password.
"""

import ipaddress
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_URL_CHARS = 2048
ALLOWED_PORTS = (80, 443)
_DEFAULT_PORTS = {"http": 80, "https": 443}

MSG_SCHEME_OR_PORT = "blocked: only http and https URLs on ports 80 and 443 are allowed"
MSG_USERINFO = "blocked: URLs with a user name or password are not allowed"
MSG_BAD_URL = "blocked: not a plain http or https URL"
MSG_BAD_HOST = "blocked: the host is not a plain host name or a canonical IP address"
MSG_NON_PUBLIC_LITERAL = "blocked: the URL points to a non-public address"
MSG_REDIRECT_NON_PUBLIC = "blocked: redirect to a non-public address"
MSG_REDIRECT_DOWNGRADE = "blocked: redirect from https to http"

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_DOTTED_QUAD = re.compile(r"(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}")
# A label that inet_aton-style resolvers read as a number: decimal, octal or hex.
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_HOST_LABEL = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?")

# Ranges refused outright. `is_global` says True for all of them on Python
# 3.13.7 (the first two only differ by `is_reserved`, which may change), so
# they are listed here instead of relying on the flags.
_NEVER_PUBLIC = tuple(
    ipaddress.IPv6Network(text)
    for text in (
        "::/96",  # deprecated IPv4-compatible form, e.g. ::7f00:1
        "64:ff9b::/96",  # NAT64: wraps any IPv4, so even 64:ff9b::7f00:1
        "64:ff9b:1::/48",  # local-use NAT64
        "::ffff:0:0:0/96",  # SIIT translation
        "fec0::/10",  # deprecated site-local
    )
)


class UrlBlocked(Exception):
    """A URL or address the web tools refuse; `str(exc)` is safe to show."""


@dataclass(frozen=True)
class SafeUrl:
    """A URL that passed `check_url`; its address still needs `check_addresses`."""

    scheme: str
    host: str  # lowercase, no trailing dot, IPv6 without brackets
    port: int
    url: str  # normalized, fragment dropped


def non_public_host_message(host: str) -> str:
    """The refusal text for a host that resolves to a non-public address."""
    return f"blocked: {host} resolves to a non-public address"


def is_public_address(address: str | _IPAddress) -> bool:
    """True only for an address on the public internet.

    `is_global` is not enough: it accepts NAT64 (64:ff9b::/96) wrapping a
    loopback or metadata address. NAT64 and the other translation ranges are
    refused outright (a desktop fetcher has no use for them, even for a public
    IPv4 inside), and the IPv4 embedded in mapped, 6to4 and Teredo addresses
    is checked as well.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return True
    if any(ip in network for network in _NEVER_PUBLIC):
        return False
    return all(is_public_address(inner) for inner in _embedded_ipv4(ip))


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address an IPv6 address carries inside it."""
    found: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)  # (server, client)
    return found


def check_addresses(host: str, addresses: Iterable[str | _IPAddress]) -> None:
    """Refuse unless there is at least one address and every one is public.

    A host that resolves to [8.8.8.8, 10.0.0.1] is refused: the connection
    could pick the private one. An IP-literal host is checked like any address.
    """
    resolved = list(addresses)
    if not resolved or not all(is_public_address(item) for item in resolved):
        raise UrlBlocked(non_public_host_message(host))


def check_url(url: str) -> SafeUrl:
    """Validate the text of a URL and return its normalized parts.

    Raises `UrlBlocked`. A literal IP host is classified here too; a name is
    left for `check_addresses` after DNS.
    """
    if len(url) > MAX_URL_CHARS:
        raise UrlBlocked(MSG_BAD_URL)
    # Whitespace and control characters are refused, not stripped, and a
    # backslash is read differently by urllib and by browsers and clients.
    if "\\" in url or any(
        char.isspace() or unicodedata.category(char) == "Cc" for char in url
    ):
        raise UrlBlocked(MSG_BAD_URL)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UrlBlocked(MSG_SCHEME_OR_PORT) from None
    if parts.scheme not in _DEFAULT_PORTS:
        raise UrlBlocked(MSG_SCHEME_OR_PORT)
    if "@" in parts.netloc:
        raise UrlBlocked(MSG_USERINFO)
    if parts.netloc.endswith(":"):  # an empty port
        raise UrlBlocked(MSG_BAD_URL)
    port = _DEFAULT_PORTS[parts.scheme] if port is None else port
    if port not in ALLOWED_PORTS:
        raise UrlBlocked(MSG_SCHEME_OR_PORT)
    host = _normalize_host(parts.hostname)
    is_literal = _is_ip_literal(host)
    if is_literal and not is_public_address(host):
        raise UrlBlocked(MSG_NON_PUBLIC_LITERAL)
    return SafeUrl(
        scheme=parts.scheme,
        host=host,
        port=port,
        url=_rebuild(parts.scheme, host, port, parts.path, parts.query),
    )


def _normalize_host(raw: str | None) -> str:
    """Lowercase host without a trailing dot, or `UrlBlocked`."""
    if not raw:
        raise UrlBlocked(MSG_BAD_URL)
    # Non-ASCII is refused: httpx maps full-width dots to ASCII ones, so a
    # lookalike host would reach a different machine than the one checked.
    if not raw.isascii() or "%" in raw:
        raise UrlBlocked(MSG_BAD_HOST)
    host = raw.lower().removesuffix(".")
    if not host:
        raise UrlBlocked(MSG_BAD_URL)
    if ":" in host:
        _parse_ipv6(host)
        return host
    if host == "localhost" or host.endswith(".localhost"):
        raise UrlBlocked(non_public_host_message(host))
    labels = host.split(".")
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        # Only a canonical dotted quad is an IP; 127.1, 2130706433, 0x7f000001
        # and 0177.0.0.1 are other spellings that resolvers still accept.
        if not _DOTTED_QUAD.fullmatch(host) or any(
            int(label) > 255 for label in labels
        ):
            raise UrlBlocked(MSG_BAD_HOST)
        return host
    if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise UrlBlocked(MSG_BAD_HOST)
    return host


def _parse_ipv6(host: str) -> ipaddress.IPv6Address:
    try:
        return ipaddress.IPv6Address(host)
    except ValueError:
        raise UrlBlocked(MSG_BAD_HOST) from None


def _is_ip_literal(host: str) -> bool:
    return ":" in host or _DOTTED_QUAD.fullmatch(host) is not None


def _rebuild(scheme: str, host: str, port: int, path: str, query: str) -> str:
    shown_host = f"[{host}]" if ":" in host else host
    shown_port = "" if port == _DEFAULT_PORTS[scheme] else f":{port}"
    suffix = f"?{query}" if query else ""
    return f"{scheme}://{shown_host}{shown_port}{path or '/'}{suffix}"
