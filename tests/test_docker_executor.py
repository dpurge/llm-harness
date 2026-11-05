"""DockerExecutor offline: a fake or a Python child stands in for the docker client."""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import llm_harness.docker_executor as docker_executor_module
from llm_harness.command_executor import HEAD_BYTES, TAIL_BYTES, CommandRequest
from llm_harness.docker_executor import DockerExecutor

BOUND = 10  # seconds; every await is wrapped so a broken executor fails, not hangs
PYTHON = sys.executable
IMAGE = "python:3.12"
REAL_IMAGE = os.environ.get("LLM_HARNESS_DOCKER_TEST_IMAGE")


def _docker_works() -> bool:
    if not REAL_IMAGE or shutil.which("docker") is None:
        return False
    try:
        done = subprocess.run(
            ["docker", "version"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


class FakeProc:
    """Just enough of asyncio's Process: output is fed by the test, kill ends it."""

    def __init__(self, output: bytes = b"", *, exits: bool = True, code: int = 0):
        self.stdout = asyncio.StreamReader()
        self.returncode: int | None = None
        self.killed = False
        self._done = asyncio.Event()
        if exits:
            self.stdout.feed_data(output)
            self.stdout.feed_eof()
            self.returncode = code
            self._done.set()

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        self.stdout.feed_eof()
        self._done.set()

    async def wait(self) -> int | None:
        await self._done.wait()
        return self.returncode


class FakeSpawn:
    """Records every call; `docker rm -f` gets its own quick process."""

    def __init__(
        self,
        client: FakeProc | None = None,
        kill_hangs: bool = False,
        cleanup_code: int = 0,
    ):
        self.client = client or FakeProc(b"hello\n")
        self.kill_hangs = kill_hangs
        self.cleanup_code = cleanup_code
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.killers: list[FakeProc] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeProc:
        self.calls.append((argv, kwargs))
        if len(argv) > 1 and argv[1] == "rm":
            killer = FakeProc(exits=not self.kill_hangs, code=self.cleanup_code)
            self.killers.append(killer)
            return killer
        return self.client

    @property
    def run_calls(self) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls if argv[1] != "rm"]

    @property
    def kill_calls(self) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls if argv[1] == "rm"]


def make_request(
    argv: tuple[str, ...] = ("python", "-c", "print(1)"),
    cwd: Path | None = None,
    timeout: int = 5,
) -> CommandRequest:
    return CommandRequest(argv, cwd, timeout)


async def run(executor: DockerExecutor, request: CommandRequest):
    return await asyncio.wait_for(executor.execute(request), timeout=BOUND)


def container_name(spawn: FakeSpawn) -> str:
    argv = spawn.run_calls[0]
    return argv[argv.index("--name") + 1]


# (a) the exact docker run argv and the cleanup by name


async def test_runs_docker_with_exact_argv_and_inherits_environment(
    tmp_path: Path,
) -> None:
    spawn = FakeSpawn()
    executor = DockerExecutor(IMAGE, docker="mydocker", spawn=spawn)
    result = await run(executor, make_request(cwd=tmp_path))
    argv = spawn.run_calls[0]
    name = container_name(spawn)
    assert argv == (
        "mydocker", "run", "--rm", "--pull", "never", "--name", name,
        "-v", f"{tmp_path}:/work", "-w", "/work",
        "--entrypoint=", IMAGE, "python", "-c", "print(1)",
    )  # fmt: skip
    kwargs = spawn.calls[0][1]
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT
    assert "env" not in kwargs  # the full host environment, not a scrubbed one
    assert "cwd" not in kwargs
    assert result.exit_code == 0
    assert result.head == "hello\n"
    assert result.program == "python"
    assert not result.timed_out
    assert result.error is None


async def test_no_cwd_means_no_mount() -> None:
    spawn = FakeSpawn()
    await run(DockerExecutor(IMAGE, spawn=spawn), make_request())
    assert "-v" not in spawn.run_calls[0]


async def test_normal_exit_issues_no_docker_rm() -> None:
    spawn = FakeSpawn(FakeProc(b"x"))
    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request())
    assert result.exit_code == 0
    assert spawn.kill_calls == []
    assert len(spawn.calls) == 1


async def test_a_non_zero_client_exit_issues_no_docker_rm() -> None:
    spawn = FakeSpawn(FakeProc(b"x", code=3))
    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request())
    assert result.exit_code == 3
    assert spawn.kill_calls == []


async def test_docker_rm_is_started_without_stdio() -> None:
    spawn = FakeSpawn(FakeProc(exits=False))
    await run(DockerExecutor(IMAGE, spawn=spawn), make_request(timeout=1))
    kwargs = spawn.calls[1][1]
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL


async def test_timeout_removes_container_and_client() -> None:
    spawn = FakeSpawn(FakeProc(exits=False))
    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request(timeout=1))
    assert result.timed_out
    assert result.exit_code is None
    assert spawn.kill_calls == [("docker", "rm", "-f", container_name(spawn))]
    assert spawn.client.killed


async def test_cancellation_removes_container_and_client_then_reraises() -> None:
    spawn = FakeSpawn(FakeProc(exits=False))
    task = asyncio.create_task(
        DockerExecutor(IMAGE, spawn=spawn).execute(make_request(timeout=60))
    )
    for _ in range(100):
        if spawn.calls:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=BOUND)
    assert spawn.kill_calls == [("docker", "rm", "-f", container_name(spawn))]
    assert spawn.client.killed


async def test_a_hanging_docker_rm_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = 0.05
    monkeypatch.setattr(docker_executor_module, "KILL_WAIT_SECONDS", bound)
    spawn = FakeSpawn(FakeProc(exits=False), kill_hangs=True)
    started = time.monotonic()
    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request(timeout=1))
    elapsed = time.monotonic() - started
    assert result.timed_out
    assert spawn.killers[0].killed
    assert spawn.killers[0].returncode is not None  # reaped, not left running
    assert elapsed < 1 + bound + 1  # the 1 s command timeout + cleanup bound + margin


async def test_a_failing_docker_rm_exit_still_returns_timed_out() -> None:
    spawn = FakeSpawn(FakeProc(exits=False), cleanup_code=1)
    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request(timeout=1))
    assert result.timed_out
    assert result.exit_code is None
    assert result.error is None
    assert spawn.kill_calls == [("docker", "rm", "-f", container_name(spawn))]
    assert spawn.client.killed


async def test_a_failing_docker_rm_spawn_is_ignored() -> None:
    inner = FakeSpawn(FakeProc(exits=False))

    async def spawn(*argv: str, **kwargs: Any) -> FakeProc:
        if argv[1] == "rm":
            raise OSError("no docker")
        return await inner(*argv, **kwargs)

    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request(timeout=1))
    assert result.timed_out
    assert inner.client.killed


async def test_seconds_come_from_the_clock() -> None:
    ticks = iter([10.0, 12.5])
    executor = DockerExecutor(IMAGE, spawn=FakeSpawn(), clock=lambda: next(ticks))
    result = await run(executor, make_request())
    assert result.seconds == 2.5


# (b) a real child process behind the spawn seam


class RealClientSpawn:
    """Launches a Python child instead of docker; `docker rm -f` is a no-op child."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.client: asyncio.subprocess.Process | None = None
        self.kill_args: tuple[str, ...] | None = None

    async def __call__(self, *argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        if argv[1] == "rm":
            self.kill_args = argv
            return await asyncio.create_subprocess_exec(PYTHON, "-c", "pass", **kwargs)
        self.client = await asyncio.create_subprocess_exec(
            PYTHON, "-c", self.code, **kwargs
        )
        return self.client


async def test_real_client_output_keeps_head_and_tail() -> None:
    total = HEAD_BYTES + TAIL_BYTES + 5000
    code = (
        "import sys; "
        f"sys.stdout.write('H' * {HEAD_BYTES} + 'M' * 5000 + 'T' * {TAIL_BYTES})"
    )
    spawn = RealClientSpawn(code)
    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request())
    assert result.exit_code == 0
    assert result.output_bytes == total
    assert result.truncated
    assert result.head == "H" * HEAD_BYTES
    assert result.tail == "T" * TAIL_BYTES
    assert spawn.kill_args is None


async def test_real_client_stderr_is_merged_into_output() -> None:
    spawn = RealClientSpawn("import sys; sys.stderr.write('oops')")
    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request())
    assert result.head == "oops"


async def test_real_client_timeout_kills_the_client_without_hanging() -> None:
    spawn = RealClientSpawn("import time; time.sleep(30)")
    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request(timeout=1))
    assert result.timed_out
    assert result.exit_code is None
    assert spawn.client is not None
    assert spawn.client.returncode is not None
    assert spawn.kill_args is not None
    assert spawn.kill_args[1:3] == ("rm", "-f")


# (c) check()


def test_check_allows_anything_not_denied_when_allow_is_empty() -> None:
    assert DockerExecutor(IMAGE).check(["python", "-c", "1"], None) is None


@pytest.mark.parametrize("program", ["bash", "/bin/sh", "BASH", "C:\\x\\cmd", "curl"])
def test_check_applies_the_built_in_deny_list_by_name(program: str) -> None:
    reason = DockerExecutor(IMAGE).check([program], None)
    assert reason is not None
    assert "built-in deny list" in reason


def test_check_does_not_resolve_the_program_on_the_host() -> None:
    # Not on the host PATH, so a host policy would say "not found".
    assert DockerExecutor(IMAGE).check(["only-in-the-image"], None) is None


def test_check_user_deny_adds_to_the_list() -> None:
    reason = DockerExecutor(IMAGE, deny=["git"]).check(["/usr/bin/git"], None)
    assert reason is not None
    assert "command_deny" in reason


def test_check_allow_list_limits_commands() -> None:
    executor = DockerExecutor(IMAGE, allow=["python*"])
    assert executor.check(["python3"], None) is None
    reason = executor.check(["ls"], None)
    assert reason is not None
    assert "command_allow" in reason


def test_check_deny_beats_allow() -> None:
    executor = DockerExecutor(IMAGE, allow=["git", "bash"], deny=["git"])
    assert executor.check(["git"], None) is not None
    assert executor.check(["bash"], None) is not None  # built-in deny too


def test_check_empty_argv_is_refused() -> None:
    assert DockerExecutor(IMAGE).check([], None) == "argv is empty"


def test_check_missing_cwd_is_refused(tmp_path: Path) -> None:
    reason = DockerExecutor(IMAGE).check(["python"], tmp_path / "missing")
    assert reason is not None
    assert "does not exist" in reason


def test_check_file_as_cwd_is_refused(tmp_path: Path) -> None:
    afile = tmp_path / "f"
    afile.write_text("x")
    assert DockerExecutor(IMAGE).check(["python"], afile) is not None


def test_check_existing_cwd_is_allowed(tmp_path: Path) -> None:
    assert DockerExecutor(IMAGE).check(["python"], tmp_path) is None


@pytest.mark.parametrize(
    ("directory", "fragment"),
    [
        ("relative/dir", "must be absolute"),
        ("//server/share", "UNC"),
        ("/tmp/a:b", "colon"),
    ],
)
def test_check_unmountable_cwd_is_refused(
    monkeypatch: pytest.MonkeyPatch, directory: str, fragment: str
) -> None:
    # The path text decides, so pretend the directory exists on any host.
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    reason = DockerExecutor(IMAGE).check(["python"], Path(directory))
    assert reason is not None
    assert fragment in reason or fragment in reason.replace("\\", "/")


async def test_execute_refuses_a_denied_command_without_spawning() -> None:
    spawn = FakeSpawn()
    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request(("bash",)))
    assert result.error is not None
    assert "built-in deny list" in result.error
    assert result.program == "bash"
    assert spawn.calls == []


async def test_execute_reports_a_bad_image_without_spawning() -> None:
    spawn = FakeSpawn()
    result = await run(DockerExecutor("", spawn=spawn), make_request())
    assert result.error is not None
    assert "image" in result.error
    assert spawn.calls == []


# (d) docker missing


async def test_docker_not_found_is_an_error_result() -> None:
    async def spawn(*argv: str, **kwargs: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request())
    assert result.error is not None
    assert "docker was not found" in result.error
    assert "install Docker or fix PATH" in result.error
    assert result.exit_code is None
    assert not result.timed_out


async def test_other_spawn_failure_is_an_error_result() -> None:
    async def spawn(*argv: str, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    result = await run(DockerExecutor(IMAGE, spawn=spawn), make_request())
    assert result.error is not None
    assert "could not start docker" in result.error


# (e) real docker, opt-in


@pytest.mark.skipif(
    not _docker_works(),
    reason=(
        "needs env LLM_HARNESS_DOCKER_TEST_IMAGE (a local image with `echo`) "
        "and a working `docker version`"
    ),
)
async def test_real_docker_runs_echo(tmp_path: Path) -> None:
    assert REAL_IMAGE is not None
    result = await run(
        DockerExecutor(REAL_IMAGE),
        make_request(("echo", "hello"), cwd=tmp_path, timeout=60),
    )
    assert result.error is None
    assert result.exit_code == 0
    assert result.head.strip() == "hello"
