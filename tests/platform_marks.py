"""Marks for tests that exercise POSIX-only behaviour."""

import sys

import pytest

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs POSIX file modes, symlinks, executables, signals or termios",
)
