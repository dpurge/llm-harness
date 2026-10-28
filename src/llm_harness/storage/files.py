"""The file store: an async interface and its local-directory backend.

Framework-free. The path may come from a model, so it is untrusted: every method
validates it first (one function), and the local backend then refuses to leave its
root through a symlink. Error text names the relative path, never the absolute root.

Not safe against another process that swaps a directory for a symlink between the
check and the use; the root is owned by the user (mode 0700), so that is out of scope.
"""

import asyncio
import contextlib
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Protocol

MAX_SEGMENT_CHARS = 255
MAX_PATH_CHARS = 1024

# The store's own scratch directory; hidden, so no valid path can reach it.
_TMP_DIRNAME = ".tmp"
_NAMESPACE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")


class FileStoreError(Exception):
    """A file step failed; `str(exc)` says what to change and never shows the root."""


class FileStore(Protocol):
    """Bytes under `/`-separated relative paths. `list` is last: it shadows the builtin."""

    async def put(self, path: str, data: bytes) -> None: ...

    async def get(self, path: str) -> bytes: ...

    async def exists(self, path: str) -> bool: ...

    async def delete(self, path: str) -> bool:
        """True if a file was removed."""
        ...

    def namespace(self, name: str) -> "FileStore": ...

    async def list(self, prefix: str = "") -> list[str]: ...


def split_path(path: str) -> list[str]:
    """The segments of a valid relative path, else `FileStoreError`."""
    if not isinstance(path, str):
        raise FileStoreError("the path must be a str")
    if not path:
        raise FileStoreError("the path is empty")
    if len(path) > MAX_PATH_CHARS:
        raise FileStoreError(f"the path is longer than {MAX_PATH_CHARS} characters")
    if "\0" in path or "\\" in path:
        raise FileStoreError("the path contains a NUL or backslash character")
    if path.startswith("/"):
        raise FileStoreError(
            f"the path must be relative, without a leading '/': {path}"
        )
    segments = path.split("/")
    for segment in segments:
        if not segment:
            raise FileStoreError(
                f"the path has an empty segment (a leading, trailing or double '/'): {path}"
            )
        if len(segment) > MAX_SEGMENT_CHARS:
            raise FileStoreError(
                f"a path segment is longer than {MAX_SEGMENT_CHARS} characters"
            )
        # Covers '.', '..' and hidden names, which keeps `.tmp` unreachable.
        if segment.startswith("."):
            raise FileStoreError(f"a path segment must not start with '.': {path}")
    return segments


def _os_reason(exc: OSError) -> str:
    # Class name and strerror only: str(exc) would repeat the absolute path.
    reason = type(exc).__name__
    if exc.strerror:
        reason = f"{reason}: {exc.strerror}"
    return reason


def make_private_dirs(directory: Path) -> None:
    """Create `directory` and its missing parents, each explicitly 0700.

    `os.makedirs(mode=...)` is masked by the umask and applies the mode to the
    leaf only, so every directory this call creates is chmod-ed.
    """
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for created in reversed(missing):
        try:
            os.mkdir(created, 0o700)
        except FileExistsError:
            continue  # another call won the race; its mode is not ours to change
        os.chmod(created, 0o700)


class LocalFileStore:
    """Files under one directory. Construction creates nothing; `put` does."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(self, path: str, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise FileStoreError(
                f"put needs bytes, got {type(data).__name__}: encode text first, e.g. text.encode('utf-8')"
            )
        segments = split_path(path)
        await asyncio.to_thread(self._put, path, segments, data)

    async def get(self, path: str) -> bytes:
        segments = split_path(path)
        return await asyncio.to_thread(self._get, path, segments)

    async def exists(self, path: str) -> bool:
        segments = split_path(path)
        return await asyncio.to_thread(self._exists, path, segments)

    async def delete(self, path: str) -> bool:
        segments = split_path(path)
        return await asyncio.to_thread(self._delete, path, segments)

    def namespace(self, name: str) -> "LocalFileStore":
        if not isinstance(name, str) or not _NAMESPACE_PATTERN.fullmatch(name):
            raise FileStoreError(
                "a namespace name is a lowercase letter then up to 63 of a-z, 0-9 and '_'"
            )
        return LocalFileStore(self._root / name)

    def _checked_dir(self, path: str, segments: list[str]) -> str:
        """The directory for `segments` under the root, after resolving every symlink in it."""
        real_root = os.path.realpath(self._root)
        directory = os.path.join(self._root, *segments)
        real = os.path.realpath(directory)
        if os.path.commonpath([real_root, real]) != real_root:
            raise FileStoreError(f"the path leaves the store through a symlink: {path}")
        return directory

    def _put(self, path: str, segments: list[str], data: bytes) -> None:
        parent = self._checked_dir(path, segments[:-1])
        target = os.path.join(parent, segments[-1])
        tmp_dir = self._root / _TMP_DIRNAME
        tmp_path = tmp_dir / secrets.token_hex(16)
        try:
            make_private_dirs(Path(parent))
            make_private_dirs(tmp_dir)
            # O_NOFOLLOW below guards the file, not the directory: refuse a `.tmp` link.
            if not stat.S_ISDIR(os.lstat(tmp_dir).st_mode):
                raise FileStoreError(
                    "the store's temporary directory is not a directory"
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            descriptor = os.open(tmp_path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(
                    handle.fileno(), 0o600
                )  # the umask masks the mode given to open
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Replaces a symlink at the target itself and never writes through it.
            os.replace(tmp_path, target)
        except OSError as exc:
            raise FileStoreError(f"cannot write {path}: {_os_reason(exc)}") from None
        finally:
            # After a successful replace the temp name is gone and this is a no-op.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    def _get(self, path: str, segments: list[str]) -> bytes:
        parent = self._checked_dir(path, segments[:-1])
        try:
            # O_NOFOLLOW refuses a final symlink; O_NONBLOCK keeps a FIFO from hanging the open.
            descriptor = os.open(
                os.path.join(parent, segments[-1]),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except (FileNotFoundError, NotADirectoryError):
            raise FileStoreError(f"no such file: {path}") from None
        except OSError as exc:
            raise FileStoreError(f"cannot read {path}: {_os_reason(exc)}") from None
        try:
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise FileStoreError(f"not a regular file: {path}")
                return handle.read()
        except OSError as exc:
            raise FileStoreError(f"cannot read {path}: {_os_reason(exc)}") from None

    def _exists(self, path: str, segments: list[str]) -> bool:
        try:
            parent = self._checked_dir(path, segments[:-1])
        except FileStoreError:
            return False  # a path that leaves the store names nothing in it
        return self._is_regular(os.path.join(parent, segments[-1]))

    def _delete(self, path: str, segments: list[str]) -> bool:
        parent = self._checked_dir(path, segments[:-1])
        target = os.path.join(parent, segments[-1])
        # Only regular files: a symlink or directory is not ours to remove.
        if not self._is_regular(target):
            return False
        try:
            os.unlink(target)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise FileStoreError(f"cannot delete {path}: {_os_reason(exc)}") from None
        return True

    def _list(self, prefix: str, segments: list[str]) -> list[str]:
        directory = self._checked_dir(prefix, segments)
        found: list[str] = []
        # os.walk does not descend into symlinked directories (followlinks=False).
        for current, dirnames, filenames in os.walk(directory):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for name in filenames:
                full = os.path.join(current, name)
                if name.startswith(".") or not self._is_regular(full):
                    continue
                relative = os.path.relpath(full, self._root).replace(os.sep, "/")
                try:
                    split_path(relative)
                except FileStoreError:
                    continue  # a name `get` could never open
                found.append(relative)
        return sorted(found)

    @staticmethod
    def _is_regular(full_path: str) -> bool:
        try:
            # lstat: a final symlink is not a regular file here.
            return stat.S_ISREG(os.lstat(full_path).st_mode)
        except OSError:
            return False

    # Last in the class: after this `def`, the name `list` in the class body is the method.
    async def list(self, prefix: str = "") -> list[str]:
        segments = split_path(prefix) if prefix else []
        return await asyncio.to_thread(self._list, prefix, segments)
