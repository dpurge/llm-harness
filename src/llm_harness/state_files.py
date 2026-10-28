"""Files for the `/export` and `/import` prompt commands of the terminal UI.

Framework-free. The path comes from the user, the file content is untrusted, so:
export creates the file exclusively (never overwrites, never follows a symlink),
import checks that the path is a regular file and its size BEFORE reading and then
reads at most one byte more than the cap. Every failure is a `StateFileError`
whose text is one short line (exception class and `strerror` only, never file
content), already passed through `sanitize_text` and `redact`.
"""

import contextlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from llm_harness.display import sanitize_text
from llm_harness.redact import redact
from llm_harness.state_io import EXPORT_FILENAME, MAX_IMPORT_BYTES, check_size

EXPORT_COMMAND = "/export"
IMPORT_COMMAND = "/import"
IMPORT_USAGE = f"usage: {IMPORT_COMMAND} <path>"

_MAX_LINE_CHARS = 300


class StateFileError(ValueError):
    """A file step failed; `str(exc)` is one safe line with no newline."""


@dataclass(frozen=True)
class StateCommand:
    name: str  # EXPORT_COMMAND or IMPORT_COMMAND
    argument: str  # the rest of the line, stripped; may be empty


def safe_line(text: str) -> str:
    """One line, control characters dropped, secrets masked, length capped."""
    return redact(" ".join(sanitize_text(text).split()), limit=_MAX_LINE_CHARS)


def parse_command(text: str) -> StateCommand | None:
    """The command if the FIRST word is `/export` or `/import` (any case), else None.

    `/exportx` is a different word and so not a command.
    """
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    name = parts[0].lower()
    if name not in (EXPORT_COMMAND, IMPORT_COMMAND):
        return None
    return StateCommand(name, parts[1].strip() if len(parts) > 1 else "")


def resolve_path(argument: str) -> Path:
    """`~` expanded, no globbing; empty means the default export name in the cwd."""
    if not argument:
        return Path.cwd() / EXPORT_FILENAME
    return Path(argument).expanduser()


def _os_reason(exc: OSError) -> str:
    # Class name and strerror only: str(exc) would repeat the path.
    reason = type(exc).__name__
    if exc.strerror:
        reason = f"{reason}: {exc.strerror}"
    return safe_line(reason)


def write_export(path: Path, data: bytes) -> None:
    """Create `path` exclusively with mode 0600 and write `data`."""
    parent = path.parent
    if not parent.is_dir():
        raise StateFileError("the parent directory does not exist")
    if path.is_dir():
        raise StateFileError("the path is a directory")
    # O_EXCL fails on any existing entry, a dangling symlink included, and
    # O_NOFOLLOW is a second guard against following a link at the last component.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise StateFileError(f"{safe_line(str(path))} already exists") from None
    except OSError as exc:
        raise StateFileError(_os_reason(exc)) from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        # Do not leave half a state file behind; the file is ours (just created).
        with contextlib.suppress(OSError):
            path.unlink()
        raise StateFileError(_os_reason(exc)) from None


def read_import(path: Path) -> bytes:
    """The bytes of a regular file of at most `MAX_IMPORT_BYTES`; never reads more.

    Raises `StateFileError` or `StateImportError` (size), both one safe line.
    """
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise StateFileError("not a regular file")
        # O_NONBLOCK: opening a FIFO that lost the race must not wait for a writer.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StateFileError("not a regular file")
            check_size(info.st_size)
            data = handle.read(MAX_IMPORT_BYTES + 1)
    except OSError as exc:
        raise StateFileError(_os_reason(exc)) from None
    check_size(len(data))  # the file grew between fstat and read
    return data
