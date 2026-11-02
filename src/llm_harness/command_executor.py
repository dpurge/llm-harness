"""Run one command for the model: policy check, scrubbed environment, bounded output.

The child gets no stdin, a minimal environment (no API keys or tokens), its own
session and process group, and a deadline that covers reading its output and
waiting for its exit together. Afterwards the whole group is killed and the
child is reaped, whether it exited, timed out or the call was cancelled, so a
grandchild cannot outlive the call. Output is read in chunks and only the first
and last bytes are kept, while the rest is drained so the child never blocks on
a full pipe.

Failures that are not the child's own (bad working directory, program refused
or missing, spawn error) are returned in `CommandResult.error` with a short
message, never raised. `CancelledError` is re-raised after the cleanup.
"""

import asyncio
import os
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from llm_harness.command_policy import CommandDenied, check_policy, child_path

HEAD_BYTES = 8192
TAIL_BYTES = 8192
READ_CHUNK_BYTES = 4096
# Variables the child may see; everything else (keys, tokens, KUBECONFIG) is dropped.
ENV_ALLOWED_NAMES = frozenset({"HOME", "USER", "LOGNAME", "LANG", "TZ", "TMPDIR"})
ENV_ALLOWED_PREFIX = "LC_"


@dataclass(frozen=True)
class CommandRequest:
    argv: tuple[str, ...]
    cwd: Path | None
    timeout_seconds: int


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    timed_out: bool
    seconds: float
    program: str  # resolved absolute path, "" if it was never resolved
    output_bytes: int  # every byte the child wrote, kept or not
    head: str
    tail: str  # empty when everything fits in `head`
    truncated: bool
    error: str | None = None


class CommandExecutor(Protocol):
    def check(self, argv: Sequence[str], cwd: Path | None) -> str | None:
        """None when the command may run, else the reason it is refused."""
        ...

    async def execute(self, request: CommandRequest) -> CommandResult: ...


def scrubbed_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Only the allow-listed variables, with PATH reduced to its absolute entries."""
    env = {
        name: value
        for name, value in environ.items()
        if name in ENV_ALLOWED_NAMES or name.startswith(ENV_ALLOWED_PREFIX)
    }
    env["PATH"] = child_path(environ.get("PATH"))
    return env


class _Capture:
    """The first `HEAD_BYTES` and a rolling last `TAIL_BYTES`; memory stays bounded."""

    def __init__(self) -> None:
        self.total = 0
        self._head = bytearray()
        self._tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = HEAD_BYTES - len(self._head)
        self._head += chunk[:room]
        self._tail += chunk[room:]
        if len(self._tail) > TAIL_BYTES:
            del self._tail[:-TAIL_BYTES]

    @property
    def truncated(self) -> bool:
        return self.total > HEAD_BYTES + TAIL_BYTES

    def texts(self) -> tuple[str, str]:
        """(head, tail); a small output is all in `head` so nothing repeats."""
        if self.truncated:
            return _decode(self._head), _decode(self._tail)
        return _decode(self._head + self._tail), ""


def _decode(data: bytes | bytearray) -> str:
    return bytes(data).decode("utf-8", errors="replace")


class PermissionsExecutor:
    """Runs a command on this machine when the allow/deny policy lets it."""

    def __init__(
        self,
        allow: Sequence[str] = (),
        deny: Sequence[str] = (),
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._allow = tuple(allow)
        self._deny = tuple(deny)
        self._environ = environ
        self._clock = clock

    def check(self, argv: Sequence[str], cwd: Path | None) -> str | None:
        try:
            self._approved_program(argv, cwd)
        except CommandDenied as refusal:
            return str(refusal)
        return None

    async def execute(self, request: CommandRequest) -> CommandResult:
        started = self._clock()
        # Checked again here: the file system may have changed since `check`.
        try:
            program = self._approved_program(request.argv, request.cwd)
        except CommandDenied as refusal:
            return self._failure(started, "", str(refusal))
        try:
            proc = await asyncio.create_subprocess_exec(
                str(program),
                *request.argv[1:],
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=scrubbed_env(self._environment()),
                cwd=_working_directory(request.cwd),
                start_new_session=True,
            )
        except OSError as failure:
            reason = failure.strerror or type(failure).__name__
            return self._failure(
                started, str(program), f"could not start program: {reason}"
            )
        capture, timed_out = await _supervise(proc, request.timeout_seconds)
        head, tail = capture.texts()
        return CommandResult(
            exit_code=None if timed_out else proc.returncode,
            timed_out=timed_out,
            seconds=self._clock() - started,
            program=str(program),
            output_bytes=capture.total,
            head=head,
            tail=tail,
            truncated=capture.truncated,
        )

    def _environment(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _approved_program(self, argv: Sequence[str], cwd: Path | None) -> Path:
        directory = _working_directory(cwd)
        if not directory.is_dir():
            raise CommandDenied(
                f"working directory {str(directory)!a} does not exist or is not a directory"
            )
        path = child_path(self._environment().get("PATH"))
        return check_policy(argv, directory, path, self._allow, self._deny)

    def _failure(self, started: float, program: str, message: str) -> CommandResult:
        return CommandResult(
            exit_code=None,
            timed_out=False,
            seconds=self._clock() - started,
            program=program,
            output_bytes=0,
            head="",
            tail="",
            truncated=False,
            error=message,
        )


def _working_directory(cwd: Path | None) -> Path:
    return Path.cwd() if cwd is None else Path(cwd)


async def _supervise(
    proc: asyncio.subprocess.Process, timeout_seconds: int
) -> tuple[_Capture, bool]:
    """Read the output and wait for the exit under one deadline, then clean up."""
    capture = _Capture()
    timed_out = False
    try:
        async with asyncio.timeout(timeout_seconds):
            await _drain(proc, capture)
            await proc.wait()
    except TimeoutError:
        timed_out = True
    finally:
        _kill_group(proc.pid)
        _close_pipes(proc)
        await asyncio.shield(proc.wait())
    return capture, timed_out


async def _drain(proc: asyncio.subprocess.Process, capture: _Capture) -> None:
    """Read to end of file; keep reading after the cap so the child never blocks."""
    stream = proc.stdout
    if stream is None:
        raise RuntimeError("child was started without a stdout pipe")
    while chunk := await stream.read(READ_CHUNK_BYTES):
        capture.feed(chunk)


def _kill_group(pid: int) -> None:
    """SIGKILL the child's process group; it is gone or not ours is not an error."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _close_pipes(proc: asyncio.subprocess.Process) -> None:
    """Close our end of the pipes so `wait()` cannot depend on them.

    asyncio finishes `wait()` only once the pipes are closed. A reader paused
    with unread data, or a process that left the group (`setsid`) and still holds
    the pipe, would otherwise block it forever after the kill. Uses the private
    `_transport`, as `asyncio.subprocess.Process` has no public way to do this.
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        transport.close()
