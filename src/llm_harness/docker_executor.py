"""Run one command in a Docker container: `docker run`, bounded output, remove by name.

`check` matches `argv[0]` by name only, because the host cannot say what exists
in the image. The docker client is started with an argv list (never a shell) and
the full host environment: the container does not inherit it, and the client
needs `DOCKER_HOST`, `DOCKER_CONFIG`, `HOME`, `PATH` and the like.

Killing the client does not stop the container, so on timeout, cancellation and
any other exit before the client has exited the container is removed with
`docker rm -f <name>`, started through the same `spawn` seam. (`docker kill`
fails on a container that was created but not started; `rm -f` removes it in
either state.) After a normal exit the container is already gone (`--rm`), so
no cleanup is issued. That works on every OS, unlike a process group. A failed
cleanup is ignored (the container may be gone) and never waited on for more
than `KILL_WAIT_SECONDS`.
"""

import asyncio
import fnmatch
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from llm_harness.command_executor import (
    CommandRequest,
    CommandResult,
    _Capture,
    _close_pipes,
    _drain,
)
from llm_harness.command_policy import DEFAULT_COMMAND_DENY
from llm_harness.docker_command import (
    docker_run_args,
    mount_source,
    new_container_name,
)

KILL_WAIT_SECONDS = 10
_REAP_WAIT_SECONDS = 2  # after killing a hung cleanup client
_SEPARATORS = re.compile(r"[\\/]")
_MAX_NAME_CHARS = 100

Spawn = Callable[..., Any]  # asyncio.create_subprocess_exec or a test double


class DockerExecutor:
    """Runs a command in a fresh container of `image` when the name policy lets it."""

    def __init__(
        self,
        image: str,
        allow: Sequence[str] = (),
        deny: Sequence[str] = (),
        *,
        docker: str = "docker",
        spawn: Spawn = asyncio.create_subprocess_exec,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._image = image
        self._allow = tuple(allow)
        self._deny = tuple(deny)
        self._docker = docker
        self._spawn = spawn
        self._clock = clock

    def check(self, argv: Sequence[str], cwd: Path | None) -> str | None:
        if not argv or not argv[0]:
            return "argv is empty"
        if "\0" in argv[0]:
            return "program name is empty or contains a NUL character"
        refusal = _name_refusal(argv[0], self._allow, self._deny)
        if refusal is not None:
            return refusal
        if cwd is not None:
            if not Path(cwd).is_dir():
                return (
                    f"working directory {str(cwd)!a} does not exist "
                    "or is not a directory"
                )
            try:
                mount_source(str(cwd))
            except ValueError as problem:
                return str(problem)
        return None

    async def execute(self, request: CommandRequest) -> CommandResult:
        started = self._clock()
        # Checked again here: the file system may have changed since `check`.
        refusal = self.check(request.argv, request.cwd)
        if refusal is not None:
            return self._failure(
                started, request.argv[0] if request.argv else "", refusal
            )
        program = request.argv[0]
        name = new_container_name()
        try:
            args = docker_run_args(
                image=self._image,
                name=name,
                argv=request.argv,
                cwd=str(request.cwd) if request.cwd else None,
            )
        except ValueError as problem:
            return self._failure(started, program, str(problem))
        try:
            proc = await self._spawn(
                self._docker,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return self._failure(
                started,
                program,
                f"docker was not found (looked for {self._docker!a}); "
                "install Docker or fix PATH",
            )
        except OSError as failure:
            reason = failure.strerror or type(failure).__name__
            return self._failure(started, program, f"could not start docker: {reason}")
        capture, timed_out = await self._supervise(proc, name, request.timeout_seconds)
        head, tail = capture.texts()
        return CommandResult(
            exit_code=None if timed_out else proc.returncode,
            timed_out=timed_out,
            seconds=self._clock() - started,
            program=program,
            output_bytes=capture.total,
            head=head,
            tail=tail,
            truncated=capture.truncated,
        )

    async def _supervise(
        self, proc: Any, name: str, timeout_seconds: int
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
            client_exited = proc.returncode is not None
            try:
                if not client_exited:
                    await self._remove_container(name)
            finally:
                # Also when the cleanup step itself is cancelled: never leave the client.
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                _close_pipes(proc)
                await asyncio.shield(proc.wait())
        return capture, timed_out

    async def _remove_container(self, name: str) -> None:
        """`docker rm -f <name>`; a failure or a slow daemon is not an error here."""
        try:
            remover = await self._spawn(
                self._docker,
                "rm",
                "-f",
                name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return
        try:
            await asyncio.wait_for(remover.wait(), timeout=KILL_WAIT_SECONDS)
        except TimeoutError:
            try:
                remover.kill()
            except ProcessLookupError:
                pass
            _close_pipes(remover)
            # Reap it so no zombie or unclosed transport is left; still bounded.
            try:
                await asyncio.wait_for(remover.wait(), timeout=_REAP_WAIT_SECONDS)
            except TimeoutError:
                pass

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


def _name_refusal(argv0: str, allow: Sequence[str], deny: Sequence[str]) -> str | None:
    """Why `argv0` is refused by name, or None. Deny wins; empty allow accepts the rest."""
    name = _SEPARATORS.split(argv0)[-1].casefold()
    label = _shown(argv0)
    if _name_matches(argv0, name, DEFAULT_COMMAND_DENY):
        return f"command {label} is denied by the built-in deny list"
    if _name_matches(argv0, name, deny):
        return f"command {label} is denied by `[tools] command_deny`"
    if allow and not _name_matches(argv0, name, allow):
        return f"command {label} is not in `[tools] command_allow`"
    return None


def _name_matches(argv0: str, name: str, entries: Sequence[str]) -> bool:
    """A path entry equals `argv0` exactly; any other entry is a basename glob."""
    for entry in entries:
        if _SEPARATORS.search(entry):
            if entry == argv0:
                return True
        elif fnmatch.fnmatchcase(name, entry.casefold()):
            return True
    return False


def _shown(argv0: str) -> str:
    """The name, escaped and cut, so control characters cannot reach a terminal."""
    text = ascii(argv0[:_MAX_NAME_CHARS])
    return text if len(argv0) <= _MAX_NAME_CHARS else f"{text}..."
