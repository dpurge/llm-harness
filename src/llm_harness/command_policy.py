"""Decide whether a program may run: resolve `argv[0]`, then apply deny and allow.

Pure and synchronous: no subprocess, no event loop. Only `argv[0]` is examined.
A bare name is looked up on the child's PATH, a name containing "/" is resolved
against the working directory. The policy then matches the given basename and
the basename of the real file behind every symlink, case-folded, so a symlink
`safe` -> `bash` or the name `BASH` is still caught.

This guards against mistakes; it is not a sandbox. An interpreter or any tool
that runs code from its arguments (`python -c`, `find -exec`) passes it.
Failures are `CommandDenied` with a short message that names only the program
and its resolved path, never the other arguments.
"""

import fnmatch
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

# Always applied; `[tools] command_deny` can add to it, never remove from it.
# Basename patterns (fnmatch). Interpreters, git and rm are deliberately absent.
DEFAULT_COMMAND_DENY: tuple[str, ...] = (
    # shells
    "sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh",
    # wrappers that run another program
    "sudo", "doas", "su", "runuser", "pkexec", "env", "nice", "nohup",
    "timeout", "gtimeout", "stdbuf", "ionice", "setsid", "chroot", "xargs",
    "script", "time", "watch",
    # system
    "dd", "mkfs*", "diskutil", "shutdown", "reboot", "halt", "launchctl",
    "crontab", "osascript",
    # network
    "curl", "wget", "nc", "ncat", "socat", "telnet", "ssh", "scp", "sftp",
)  # fmt: skip

# Used when the parent PATH is missing or holds no absolute entry.
_FALLBACK_PATH = "/usr/local/bin:/usr/bin:/bin"

# A program name is echoed in messages, so cap it.
_MAX_NAME_CHARS = 100


class CommandDenied(Exception):
    """The command may not run; the message says why and is safe to show."""


def child_path(env_path: str | None) -> str:
    """The PATH for the child: absolute entries only, in order, without repeats.

    An empty entry or a relative one (`.`) would make the lookup depend on the
    working directory, so both are dropped.
    """
    entries: list[str] = []
    for entry in (env_path or "").split(os.pathsep):
        if os.path.isabs(entry) and entry not in entries:
            entries.append(entry)
    return os.pathsep.join(entries) if entries else _FALLBACK_PATH


def resolve_program(argv0: str, cwd: Path, path: str) -> Path:
    """The absolute path of the program `argv0` names; symlinks are kept.

    The result is not passed through `realpath`: a venv's `python` is a symlink
    and only works under its own name.
    """
    if not argv0 or "\0" in argv0:
        raise CommandDenied("program name is empty or contains a NUL character")
    shown = _shown(argv0)
    if "/" in argv0:
        found: str | None = str(Path(cwd).absolute() / argv0)
    else:
        found = shutil.which(argv0, path=path)
    program = Path(found).absolute() if found is not None else None
    if program is None or not program.exists():
        raise CommandDenied(f"program {shown} not found (searched the allowed PATH)")
    if not program.is_file():
        raise CommandDenied(f"program {shown} is not a file ({program})")
    if not os.access(program, os.X_OK):
        raise CommandDenied(f"program {shown} is not executable ({program})")
    return program


def check_policy(
    argv: Sequence[str],
    cwd: Path,
    path: str,
    allow: Sequence[str],
    deny: Sequence[str],
) -> Path:
    """The resolved program when the policy lets it run, else `CommandDenied`.

    Deny wins over allow. An empty `allow` accepts anything not denied; a
    non-empty one accepts only a match. The returned path is absolute and is
    not passed through `realpath`.
    """
    if not argv:
        raise CommandDenied("argv is empty")
    program = resolve_program(argv[0], cwd, path)
    real = Path(os.path.realpath(program))
    label = _label(argv[0], real)
    if _matches(program, real, DEFAULT_COMMAND_DENY):
        raise CommandDenied(f"command {label} is denied by the built-in deny list")
    if _matches(program, real, deny):
        raise CommandDenied(f"command {label} is denied by `[tools] command_deny`")
    if allow and not _matches(program, real, allow):
        raise CommandDenied(f"command {label} is not in `[tools] command_allow`")
    return program


def _matches(program: Path, real: Path, entries: Sequence[str]) -> bool:
    """Whether any entry matches; see `_entry_matches`."""
    names = {program.name.casefold(), real.name.casefold()}
    return any(_entry_matches(entry, names, program, real) for entry in entries)


def _entry_matches(entry: str, names: set[str], program: Path, real: Path) -> bool:
    """A "/" entry equals the resolved or real path exactly; else a basename glob."""
    if "/" in entry:
        return entry in (str(real), str(program))
    pattern = entry.casefold()
    return any(fnmatch.fnmatchcase(name, pattern) for name in names)


def _label(argv0: str, real: Path) -> str:
    """The program for a message: its name, plus the real path when it differs."""
    shown = _shown(argv0)
    return shown if str(real) == argv0 else f"{shown} ({real})"


def _shown(argv0: str) -> str:
    """The name, escaped and cut, so control characters cannot reach a terminal."""
    text = ascii(argv0[:_MAX_NAME_CHARS])
    return text if len(argv0) <= _MAX_NAME_CHARS else f"{text}..."
