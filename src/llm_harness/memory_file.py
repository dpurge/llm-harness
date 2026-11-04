"""A text or markdown file as the input of `/memory add @PATH`.

Framework-free and synchronous, so a caller can run it with `asyncio.to_thread`.
The path comes from the human and the content is untrusted: the size comes from
`stat` BEFORE any read, the read stops one byte past the cap, only a regular file of
a known extension is opened, and every failure is a `TextFileError` whose message is
one line with the path shown escaped (`\\uXXXX`) and no file content.
"""

import os
import stat
from pathlib import Path

from llm_harness.display import escape_text
from llm_harness.state_files import resolve_path

MAX_FILE_BYTES = 64 * 1024
MAX_ADD_CHARS = 4000
EXTENSIONS = (".txt", ".md", ".markdown")
_BOM = "﻿"


class TextFileError(ValueError):
    """The text cannot be used; `str(error)` is one safe line with no newline."""


def check_text_length(text: str) -> None:
    """Refuse a text over `MAX_ADD_CHARS`; the one limit of a paste and of a file."""
    if len(text) > MAX_ADD_CHARS:
        raise TextFileError(
            f"text too long ({len(text)} characters, at most {MAX_ADD_CHARS}); "
            "add it in parts"
        )


def _open_regular(path: Path, shown: str) -> bytes:
    """The bytes of a regular file of at most `MAX_FILE_BYTES`; never reads more."""
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        # ValueError: an embedded NUL byte in the path.
        raise TextFileError(f"no such file: {shown}") from None
    except OSError as error:
        raise TextFileError(f"cannot read file: {type(error).__name__}") from None
    if not stat.S_ISREG(info.st_mode):
        raise TextFileError(f"not a regular file: {shown}")
    _check_size(info.st_size)
    try:
        # O_NONBLOCK: opening a FIFO that replaced the file must not wait for a writer.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise TextFileError(f"not a regular file: {shown}")
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        raise TextFileError(f"cannot read file: {type(error).__name__}") from None
    _check_size(len(data))  # the file grew between stat and read
    return data


def _check_size(size: int) -> None:
    if size > MAX_FILE_BYTES:
        raise TextFileError(f"file too large ({size} bytes, at most {MAX_FILE_BYTES})")


def read_text_file(path: str) -> str:
    """The trimmed text of a `.txt`, `.md` or `.markdown` file; `\\n` line ends.

    `~` is expanded. Raises `TextFileError`, in this order: extension, missing file,
    not a regular file, size, UTF-8, empty, over `MAX_ADD_CHARS` characters.
    """
    if not path.strip():
        raise TextFileError("no path given")
    shown = escape_text(path)
    try:
        resolved = resolve_path(path)
    except RuntimeError:  # `~user` of an unknown user
        raise TextFileError(f"no such file: {shown}") from None
    if resolved.suffix.lower() not in EXTENSIONS:
        raise TextFileError("only .txt, .md and .markdown files can be added")
    data = _open_regular(resolved, shown)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TextFileError("file is not valid UTF-8 text") from None
    text = text.removeprefix(_BOM).replace("\r\n", "\n").strip()
    if not text:
        raise TextFileError("file is empty")
    check_text_length(text)
    return text
