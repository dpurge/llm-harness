"""Path-text helpers shared by config validation and the command policy."""

import os
from types import ModuleType


def has_separator(text: str, path_module: ModuleType = os.path) -> bool:
    """Whether `text` names a path (holds "/" or the platform's separator)."""
    separators = ("/", path_module.sep, path_module.altsep)
    return any(sep and sep in text for sep in separators)


def has_posix_modes() -> bool:
    """Whether file mode bits mean anything here; on Windows they are fixed values."""
    return os.name != "nt"


def nofollow_flag() -> int:
    """`O_NOFOLLOW`, or 0 where the platform has none (Windows)."""
    return getattr(os, "O_NOFOLLOW", 0)


def nonblock_flag() -> int:
    """`O_NONBLOCK`, or 0 where the platform has none (Windows, which has no FIFOs)."""
    return getattr(os, "O_NONBLOCK", 0)


def binary_flag() -> int:
    """`O_BINARY`, or 0 where it does not exist; Windows would translate newlines."""
    return getattr(os, "O_BINARY", 0)


def fchmod_private(descriptor: int) -> None:
    """Set mode 0600 on an open file where the platform can (not Windows)."""
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)


def executable_suffixes() -> tuple[str, ...]:
    """Lowercase extensions Windows runs as programs (`PATHEXT`); empty elsewhere."""
    if has_posix_modes():
        return ()
    raw = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return tuple(part.lower() for part in raw.split(";") if part)
