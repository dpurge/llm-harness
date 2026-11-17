"""Secret masking for anything that may reach logs or error output."""

import re
from collections.abc import Iterable

REDACTED = "[REDACTED:api_key]"
_KEY_SHAPE = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
# Credential-bearing header or assignment: mask the value, keep the name and
# any Bearer/Basic scheme. Prose such as "Authorization: required" is masked too.
_HEADER = re.compile(
    r"""((?:authorization|(?:x-)?api-key)["']?\s*[:=]\s*["']?(?:(?:bearer|basic)\s+)?)"""
    r"""[^\s"',;]+""",
    re.IGNORECASE,
)
# Bearer token rule: 8+ token characters that contain a digit or run 20+
# characters, so "bearer of bad news" is left alone.
_TOKEN_CHARS = r"[A-Za-z0-9._~+/=-]"
_BEARER = re.compile(
    rf"\b(bearer\s+)(?={_TOKEN_CHARS}*\d|{_TOKEN_CHARS}{{20}}){_TOKEN_CHARS}{{8,}}",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"(?<=://)[^\s/@:]+:[^\s/@]+(?=@)")
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_-]{35}")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")


class Secret:
    """Holds a secret string; every printable form is masked."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:
        return REDACTED


def redact(text: str, secrets: Iterable[str] = (), limit: int = 300) -> str:
    """Mask secrets, then truncate to `limit`.

    Redaction runs first so a secret cut by the limit cannot leak a fragment.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _HEADER.sub(rf"\g<1>{REDACTED}", text)
    text = _URL_CREDENTIALS.sub(REDACTED, text)
    text = _BEARER.sub(rf"\g<1>{REDACTED}", text)
    for pattern in (_KEY_SHAPE, _GOOGLE_KEY, _JWT):
        text = pattern.sub(REDACTED, text)
    if len(text) > limit:
        return text[:limit] + "…"
    return text
