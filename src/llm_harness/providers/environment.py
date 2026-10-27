"""Shared helper for adapters whose SDK constructor reads ambient variables."""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

# os.environ is process-wide; without the lock two threads could interleave the
# removal and restoration and one would restore a half-hidden snapshot.
_LOCK = threading.Lock()


@contextmanager
def hidden_environment(
    prefixes: tuple[str, ...] = (), names: tuple[str, ...] = ()
) -> Iterator[None]:
    """Hide variables matching a prefix or exact name, then restore them.

    SDK constructors fall back to the environment for settings that the config
    must own (keys, projects, base URLs), and not every one can be blocked
    through an argument. Only wrap the synchronous constructor call: the lock
    is held for the whole window, so never await inside it.
    """
    with _LOCK:
        hidden = {
            name: value
            for name, value in os.environ.items()
            if name.startswith(prefixes) or name in names
        }
        for name in hidden:
            del os.environ[name]
        try:
            yield
        finally:
            os.environ.update(hidden)
