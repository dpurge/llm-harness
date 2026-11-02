"""Real harmless child processes: no network, no model, nothing outside tmp_path."""

import asyncio
import contextlib
import json
import os
import signal
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from llm_harness import command_executor
from llm_harness.command_executor import (
    HEAD_BYTES,
    TAIL_BYTES,
    CommandRequest,
    PermissionsExecutor,
    scrubbed_env,
)

BOUND = 5  # seconds; every await is wrapped so a broken executor fails, not hangs
PYTHON = sys.executable
SLEEP_30 = "import time; time.sleep(30)"
BASE_ENV = {"PATH": "/usr/bin:/bin"}


def request(
    code: str,
    *,
    cwd: Path | None = None,
    timeout: int = 5,
    argv: tuple[str, ...] | None = None,
) -> CommandRequest:
    return CommandRequest(argv or (PYTHON, "-c", code), cwd, timeout)


async def run_request(
    req: CommandRequest, executor: PermissionsExecutor | None = None
) -> command_executor.CommandResult:
    executor = executor or PermissionsExecutor(environ=BASE_ENV)
    return await asyncio.wait_for(executor.execute(req), timeout=BOUND)


async def wait_until_dead(pid: int) -> bool:
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


async def wait_for_file(path: Path) -> str:
    for _ in range(100):
        if path.exists() and path.read_text().endswith("\n"):
            return path.read_text()
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} was never written")


@pytest.fixture
def pids() -> Iterator[list[int]]:
    """Pids a test reports; any still alive at teardown is killed so none leak."""
    found: list[int] = []
    yield found
    for pid in found:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


# -- exit code, output, cwd ------------------------------------------------


@pytest.mark.parametrize("code", [0, 3])
async def test_exit_code_is_reported(code: int) -> None:
    result = await run_request(request(f"import sys; sys.exit({code})"))
    assert (result.exit_code, result.timed_out, result.error) == (code, False, None)
    assert result.seconds >= 0


async def test_stdout_and_stderr_are_merged_in_order() -> None:
    code = (
        "import sys\n"
        "print('out1', flush=True)\n"
        "print('err1', file=sys.stderr, flush=True)\n"
        "print('out2', flush=True)\n"
    )
    result = await run_request(request(code))
    assert result.head == "out1\nerr1\nout2\n"
    assert result.tail == ""
    assert result.output_bytes == len(result.head)
    assert result.truncated is False


async def test_cwd_is_honoured(tmp_path: Path) -> None:
    result = await run_request(
        request("import os; print(os.path.realpath(os.getcwd()))", cwd=tmp_path)
    )
    assert result.head.strip() == os.path.realpath(tmp_path)


async def test_cwd_defaults_to_the_process_cwd() -> None:
    result = await run_request(
        request("import os; print(os.path.realpath(os.getcwd()))")
    )
    assert result.head.strip() == os.path.realpath(Path.cwd())


async def test_the_program_in_the_result_is_absolute() -> None:
    result = await run_request(request("pass"))
    assert Path(result.program).is_absolute()
    assert result.program == str(Path(PYTHON).absolute())


# -- errors are returned, never raised ---------------------------------------


async def test_a_missing_cwd_is_an_error_string(tmp_path: Path) -> None:
    result = await run_request(request("pass", cwd=tmp_path / "missing"))
    assert result.error is not None and "directory" in result.error
    assert (result.exit_code, result.timed_out, result.output_bytes) == (None, False, 0)


async def test_a_file_as_cwd_is_an_error_string(tmp_path: Path) -> None:
    afile = tmp_path / "f.txt"
    afile.write_text("x")
    result = await run_request(request("pass", cwd=afile))
    assert result.error is not None and "directory" in result.error


async def test_a_program_that_is_not_found_is_an_error_string() -> None:
    result = await run_request(request("", argv=("no-such-program-for-this-test",)))
    assert result.error is not None and "not found" in result.error
    assert result.program == ""


async def test_a_denied_program_is_an_error_string() -> None:
    result = await run_request(request("", argv=("bash", "-c", "echo hi")))
    assert result.error is not None and "denied" in result.error
    assert result.output_bytes == 0


async def test_a_program_outside_the_allow_list_is_an_error_string() -> None:
    executor = PermissionsExecutor(allow=["git"], environ=BASE_ENV)
    result = await run_request(request("pass"), executor)
    assert result.error is not None and "command_allow" in result.error


async def test_a_spawn_oserror_is_an_error_string(tmp_path: Path) -> None:
    script = tmp_path / "broken"
    script.write_text("#!/nonexistent/interpreter\n")
    script.chmod(0o755)
    result = await run_request(request("", argv=(str(script),)))
    assert result.error is not None and result.error.startswith(
        "could not start program"
    )
    assert result.program == str(script)
    assert result.exit_code is None


async def test_a_monkeypatched_spawn_oserror_is_an_error_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    result = await run_request(request("pass"))
    assert result.error == "could not start program: Permission denied"


# -- check, and the re-check before the spawn -----------------------------


def test_check_accepts_a_good_command_and_refuses_bad_ones(tmp_path: Path) -> None:
    executor = PermissionsExecutor(environ=BASE_ENV)
    assert executor.check((PYTHON, "-c", "pass"), None) is None
    assert "denied" in (executor.check(("bash",), None) or "")
    assert "not found" in (
        executor.check(("no-such-program-for-this-test",), None) or ""
    )
    assert "directory" in (executor.check((PYTHON,), tmp_path / "missing") or "")
    assert "argv is empty" in (executor.check((), None) or "")


def test_check_applies_the_configured_deny_list() -> None:
    executor = PermissionsExecutor(deny=["python*", "Python*"], environ=BASE_ENV)
    assert "denied" in (executor.check((PYTHON,), None) or "")


async def test_execute_checks_again_after_check_accepted(tmp_path: Path) -> None:
    script = tmp_path / "tool.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    executor = PermissionsExecutor(environ=BASE_ENV)
    assert executor.check((str(script),), None) is None
    script.chmod(script.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    result = await run_request(request("", argv=(str(script),)), executor)
    assert result.error is not None and "not executable" in result.error
    assert result.output_bytes == 0

    script.unlink()
    result = await run_request(request("", argv=(str(script),)), executor)
    assert result.error is not None and "not found" in result.error


# -- environment and stdin ----------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, {"PATH": "/usr/local/bin:/usr/bin:/bin"}),
        ({"PATH": "/a:rel:/b::.:/a"}, {"PATH": "/a:/b"}),
        (
            {
                "PATH": "/x",
                "HOME": "/h",
                "USER": "u",
                "LOGNAME": "l",
                "LANG": "C",
                "TZ": "UTC",
                "TMPDIR": "/t",
            },
            {
                "PATH": "/x",
                "HOME": "/h",
                "USER": "u",
                "LOGNAME": "l",
                "LANG": "C",
                "TZ": "UTC",
                "TMPDIR": "/t",
            },
        ),
        (
            {"PATH": "/x", "LC_ALL": "C", "LC_CTYPE": "UTF-8", "LCX": "no"},
            {"PATH": "/x", "LC_ALL": "C", "LC_CTYPE": "UTF-8"},
        ),
        (
            {
                "PATH": "/x",
                "API_TOKEN": "t",
                "OPENAI_API_KEY": "k",
                "KUBECONFIG": "/k",
                "FOO": "z",
                "home": "/lower",
            },
            {"PATH": "/x"},
        ),
    ],
)
def test_scrubbed_env_is_an_allow_list(
    environ: dict[str, str], expected: dict[str, str]
) -> None:
    assert scrubbed_env(environ) == expected


async def test_the_child_sees_only_allow_listed_variables() -> None:
    environ = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/test",
        "API_TOKEN": "x",
        "OPENAI_API_KEY": "y",
        "LC_ALL": "C",
        "FOO": "z",
    }
    result = await run_request(
        request("import os, json; print(json.dumps(sorted(os.environ)))"),
        PermissionsExecutor(environ=environ),
    )
    names = set(json.loads(result.head))
    assert {"PATH", "HOME", "LC_ALL"} <= names
    assert names.isdisjoint({"API_TOKEN", "OPENAI_API_KEY", "FOO"})


async def test_the_child_path_drops_relative_and_empty_entries() -> None:
    executor = PermissionsExecutor(environ={"PATH": ".::rel/bin:/usr/bin:/bin"})
    result = await run_request(
        request("import os; print(os.environ['PATH'])"), executor
    )
    assert result.head.strip() == "/usr/bin:/bin"


async def test_the_default_environment_is_the_process_environment_at_execute_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_TOKEN", "secret-value")
    executor = PermissionsExecutor()
    result = await run_request(
        request("import os; print('API_TOKEN' in os.environ)"), executor
    )
    assert result.head.strip() == "False"


async def test_stdin_is_devnull() -> None:
    result = await run_request(request("import sys; print(repr(sys.stdin.read()))"))
    assert result.head.strip() == "''"


# -- timeout and process group ------------------------------------------------


async def test_a_child_past_the_timeout_is_killed(pids: list[int]) -> None:
    code = f"import os; print(os.getpid(), flush=True)\n{SLEEP_30}"
    result = await run_request(request(code, timeout=1))
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.error is None
    assert 1 <= result.seconds < BOUND
    pid = int(result.head.split()[0])
    pids.append(pid)
    assert await wait_until_dead(pid)


GRANDCHILD = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "print(g.pid, flush=True)\n"
)


async def test_a_grandchild_dies_with_the_group_on_timeout(pids: list[int]) -> None:
    result = await run_request(request(GRANDCHILD + SLEEP_30, timeout=2))
    assert result.timed_out is True
    grandchild = int(result.head.split()[0])
    pids.append(grandchild)
    assert await wait_until_dead(grandchild)


async def test_a_grandchild_holding_the_pipe_is_bounded_by_the_timeout(
    pids: list[int],
) -> None:
    # The child exits at once, but the grandchild keeps stdout open.
    result = await run_request(request(GRANDCHILD, timeout=2))
    assert result.timed_out is True
    grandchild = int(result.head.split()[0])
    pids.append(grandchild)
    assert await wait_until_dead(grandchild)


async def test_a_grandchild_is_killed_after_a_normal_exit_too(pids: list[int]) -> None:
    code = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(g.pid, flush=True)\n"
    )
    result = await run_request(request(code))
    assert (result.exit_code, result.timed_out) == (0, False)
    grandchild = int(result.head.split()[0])
    pids.append(grandchild)
    assert await wait_until_dead(grandchild)


async def test_cancel_kills_the_group_and_reraises(
    tmp_path: Path, pids: list[int]
) -> None:
    pid_file = tmp_path / "pid"
    code = (
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()) + '\\n')\n"
        "time.sleep(30)\n"
    )
    executor = PermissionsExecutor(environ=BASE_ENV)
    task = asyncio.create_task(executor.execute(request(code, timeout=30)))
    pid = int((await asyncio.wait_for(wait_for_file(pid_file), timeout=BOUND)).strip())
    pids.append(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=BOUND)
    assert await wait_until_dead(pid)


# -- output cap ----------------------------------------------------------------


async def test_a_megabyte_is_capped_and_drained() -> None:
    code = (
        "import sys\n"
        "sys.stdout.buffer.write(b'A' * 500_000 + b'B' * 500_000)\n"
        "sys.stdout.buffer.flush()\n"
    )
    result = await run_request(request(code, timeout=10))
    assert (result.exit_code, result.timed_out) == (0, False)
    assert result.output_bytes == 1_000_000
    assert result.truncated is True
    assert len(result.head.encode()) <= HEAD_BYTES
    assert len(result.tail.encode()) <= TAIL_BYTES
    assert result.head == "A" * HEAD_BYTES
    assert result.tail == "B" * TAIL_BYTES


async def test_output_just_over_the_head_stays_in_head_without_duplication() -> None:
    size = HEAD_BYTES + 100
    result = await run_request(request(f"import sys; sys.stdout.write('x' * {size})"))
    assert result.output_bytes == size
    assert result.head == "x" * size
    assert result.tail == ""
    assert result.truncated is False


async def test_output_at_the_cap_boundary() -> None:
    size = HEAD_BYTES + TAIL_BYTES
    result = await run_request(
        request(f"import sys; sys.stdout.write('x' * {size + 1})")
    )
    assert result.truncated is True
    assert (len(result.head), len(result.tail)) == (HEAD_BYTES, TAIL_BYTES)


async def test_small_output_is_not_truncated() -> None:
    result = await run_request(request("print('hello')"))
    assert (result.head, result.tail, result.truncated) == ("hello\n", "", False)
    assert result.output_bytes == 6


async def test_no_output() -> None:
    result = await run_request(request("pass"))
    assert (result.head, result.tail, result.output_bytes, result.truncated) == (
        "",
        "",
        0,
        False,
    )


async def test_non_utf8_bytes_are_replaced() -> None:
    code = (
        "import sys; sys.stdout.buffer.write(b'\\xff\\xfe'); sys.stdout.buffer.flush()"
    )
    result = await run_request(request(code))
    assert result.head == "\ufffd\ufffd"
    assert result.output_bytes == 2


async def test_seconds_come_from_the_injected_clock() -> None:
    ticks = iter([10.0, 12.5])
    executor = PermissionsExecutor(environ=BASE_ENV, clock=lambda: next(ticks))
    result = await run_request(request("pass"), executor)
    assert result.seconds == 2.5


async def test_a_reader_behind_a_flood_does_not_hang_cleanup_on_cancel(
    tmp_path: Path, pids: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The child floods the pipe, then sleeps. One-byte reads keep the reader far
    # behind, so asyncio has paused the stream with unread data when the call is
    # cancelled; the cleanup must still return (wait() hangs in that state).
    monkeypatch.setattr(command_executor, "READ_CHUNK_BYTES", 1)
    pid_file = tmp_path / "pid"
    code = (
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()) + '\\n')\n"
        "sys.stdout.buffer.write(b'A' * 5_000_000)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(30)\n"
    )
    executor = PermissionsExecutor(environ=BASE_ENV)
    task = asyncio.create_task(executor.execute(request(code, timeout=30)))
    pids.append(int((await asyncio.wait_for(wait_for_file(pid_file), BOUND)).strip()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=BOUND)
    assert await wait_until_dead(pids[-1])


async def test_a_holder_that_left_the_group_does_not_hang_the_call(
    pids: list[int],
) -> None:
    # start_new_session escapes the group kill, as setsid would; only the call
    # itself must come back, bounded by the timeout. The fixture kills the holder.
    code = (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " start_new_session=True)\n"
        "print(g.pid, flush=True)\n"
    )
    result = await run_request(request(code, timeout=1))
    pids.append(int(result.head.split()[0]))
    assert result.timed_out is True
