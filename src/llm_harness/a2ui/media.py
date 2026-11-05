"""Which URLs an A2UI Image, Video or AudioPlayer may load.

Pure functions, no I/O. A model must not make the page fetch from an arbitrary
server (a tracking pixel, a hostile host), so only three forms pass:

* a path on this server: one leading `/`, never `//`, no backslash, no
  whitespace or control character, no `..` segment (also percent-encoded);
* a `data:image/<png|jpeg|gif|webp>;base64,...` image of at most
  `MAX_DATA_URL_CHARS` characters (never for video or audio, never SVG);
* `http://` or `https://` plus a host that EQUALS an entry of `allowed_hosts`
  (exact match: no wildcard, no suffix match, no user info; a port is allowed).

Everything else is refused with a reason from the fixed `REASONS` vocabulary;
the reason never repeats the URL.
"""

import re
from typing import Literal
from urllib.parse import unquote, urlsplit

MediaKind = Literal["image", "video", "audio"]

MAX_DATA_URL_CHARS = 1_000_000

REASON_EMPTY = "empty url"
REASON_CHARACTERS = "unsafe characters"
REASON_TRAVERSAL = "path traversal"
REASON_RELATIVE = "relative or protocol-relative url"
REASON_SCHEME = "scheme not allowed"
REASON_DATA_TYPE = "data url type not allowed"
REASON_DATA_SIZE = "data url too large"
REASON_USERINFO = "user info not allowed"
REASON_HOST = "host not allowed"
REASON_NOT_TEXT = "url is not text"
REASONS = frozenset(
    {
        REASON_EMPTY,
        REASON_CHARACTERS,
        REASON_TRAVERSAL,
        REASON_RELATIVE,
        REASON_SCHEME,
        REASON_DATA_TYPE,
        REASON_DATA_SIZE,
        REASON_USERINFO,
        REASON_HOST,
        REASON_NOT_TEXT,
    }
)

_DATA_IMAGE = re.compile(
    r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}"
)


def media_url_allowed(
    url: object, allowed_hosts: tuple[str, ...], kind: MediaKind = "image"
) -> bool:
    """True when `url` may be loaded as `kind` (see the module text)."""
    return media_url_refusal(url, allowed_hosts, kind) is None


def media_url_refusal(
    url: object, allowed_hosts: tuple[str, ...], kind: MediaKind = "image"
) -> str | None:
    """None when the URL is allowed, else one of `REASONS`."""
    if not isinstance(url, str):
        return REASON_NOT_TEXT
    if not url:
        return REASON_EMPTY
    # Non-ASCII is refused everywhere: str.lower() maps lookalikes such as the
    # Kelvin sign to plain ASCII letters, which could pass a host comparison.
    if (
        not url.isascii()
        or "\\" in url
        or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url)
    ):
        return REASON_CHARACTERS
    if url.startswith("//"):
        return REASON_RELATIVE
    if url.startswith("/"):
        return _path_refusal(url)
    if url[:5].lower() == "data:":
        return _data_refusal(url, kind)
    return _web_refusal(url, allowed_hosts)


def _path_refusal(url: str) -> str | None:
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    for segment in path.split("/"):
        decoded = unquote(segment)
        if decoded == ".." or "/" in decoded or "\\" in decoded:
            return REASON_TRAVERSAL
    return None


def _data_refusal(url: str, kind: MediaKind) -> str | None:
    if kind != "image":
        return REASON_DATA_TYPE
    if len(url) > MAX_DATA_URL_CHARS:
        return REASON_DATA_SIZE
    return None if _DATA_IMAGE.fullmatch(url) else REASON_DATA_TYPE


def _web_refusal(url: str, allowed_hosts: tuple[str, ...]) -> str | None:
    try:
        parts = urlsplit(url)
        _port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return REASON_HOST
    if parts.scheme not in ("http", "https"):
        return REASON_RELATIVE if not parts.scheme else REASON_SCHEME
    if "@" in parts.netloc:
        return REASON_USERINFO
    host = parts.hostname
    if not host or host not in allowed_hosts:
        return REASON_HOST
    return None
