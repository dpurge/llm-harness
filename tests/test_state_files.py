"""The pure file helpers behind the TUI `/export` and `/import` commands."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from llm_harness import state_files
from llm_harness.state_files import (
    StateFileError,
    parse_command,
    read_import,
    resolve_path,
    write_export,
)
from llm_harness.state_io import EXPORT_FILENAME, MAX_IMPORT_BYTES, StateImportError


@pytest.mark.parametrize(
    ("text", "name", "argument"),
    [
        ("/export", "/export", ""),
        ("  /EXPORT  ", "/export", ""),
        ("/Export out.yaml", "/export", "out.yaml"),
        ("/import   a b.yaml  ", "/import", "a b.yaml"),
        ("/IMPORT x", "/import", "x"),
    ],
)
def test_parse_command_matches_the_first_word_case_insensitively(
    text: str, name: str, argument: str
) -> None:
    command = parse_command(text)
    assert command is not None
    assert (command.name, command.argument) == (name, argument)


@pytest.mark.parametrize(
    "text", ["", "  ", "/exportx", "/imports a", "hello /export", "/quit", "export"]
)
def test_parse_command_ignores_everything_else(text: str) -> None:
    assert parse_command(text) is None


def test_resolve_path_defaults_to_the_cwd_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_path("") == Path.cwd() / EXPORT_FILENAME


def test_resolve_path_expands_the_home_and_does_not_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_path("~/a.yaml") == tmp_path / "a.yaml"
    assert resolve_path("*.yaml") == Path("*.yaml")


def test_write_export_creates_a_0600_file(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    write_export(target, b"goal: x\n")
    assert target.read_bytes() == b"goal: x\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_export_refuses_an_existing_file_and_keeps_it(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    target.write_bytes(b"old")
    with pytest.raises(StateFileError, match="already exists"):
        write_export(target, b"new")
    assert target.read_bytes() == b"old"


def test_write_export_refuses_a_missing_parent(tmp_path: Path) -> None:
    with pytest.raises(StateFileError, match="parent directory does not exist"):
        write_export(tmp_path / "nope" / "out.yaml", b"x")
    assert not (tmp_path / "nope").exists()


def test_write_export_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(StateFileError, match="is a directory"):
        write_export(tmp_path, b"x")


def test_write_export_does_not_follow_a_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    link = tmp_path / "link.yaml"
    link.symlink_to(victim)
    with pytest.raises(StateFileError, match="already exists"):
        write_export(link, b"new")
    assert victim.read_bytes() == b"precious"
    assert link.is_symlink()


def test_write_export_does_not_follow_a_dangling_symlink(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    link = tmp_path / "link.yaml"
    link.symlink_to(missing)
    with pytest.raises(StateFileError, match="already exists"):
        write_export(link, b"new")
    assert not missing.exists()


def test_write_export_reports_an_os_error_as_class_and_strerror(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(StateFileError) as caught:
            write_export(locked / "out.yaml", b"x")
    finally:
        locked.chmod(0o700)
    assert str(caught.value) == "PermissionError: Permission denied"


def test_write_export_removes_its_half_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_fdopen(descriptor: int, mode: str):
        os.close(descriptor)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state_files.os, "fdopen", broken_fdopen)
    target = tmp_path / "out.yaml"
    with pytest.raises(StateFileError, match="OSError: No space left on device"):
        write_export(target, b"x")
    assert not target.exists()


def test_read_import_reads_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "in.yaml"
    target.write_bytes(b"a: 1\n")
    assert read_import(target) == b"a: 1\n"


def test_read_import_reads_a_file_of_exactly_the_cap(tmp_path: Path) -> None:
    target = tmp_path / "in.yaml"
    target.write_bytes(b"x" * MAX_IMPORT_BYTES)
    assert len(read_import(target)) == MAX_IMPORT_BYTES


def test_read_import_refuses_an_oversized_file_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "in.yaml"
    target.write_bytes(b"x" * (MAX_IMPORT_BYTES + 1))

    real_fdopen = os.fdopen

    def forbidden_fdopen(descriptor: int, mode: str):
        handle = real_fdopen(descriptor, mode)

        class Guarded:
            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> None:
                handle.close()

            def fileno(self) -> int:
                return handle.fileno()

            def read(self, *args: object) -> bytes:
                raise AssertionError("the file was read")

        return Guarded()

    monkeypatch.setattr(state_files.os, "fdopen", forbidden_fdopen)
    with pytest.raises(StateImportError, match="larger than 1000000 bytes"):
        read_import(target)


def test_read_import_missing_file(tmp_path: Path) -> None:
    with pytest.raises(StateFileError) as caught:
        read_import(tmp_path / "nope.yaml")
    assert str(caught.value) == "FileNotFoundError: No such file or directory"


def test_read_import_directory(tmp_path: Path) -> None:
    with pytest.raises(StateFileError, match="not a regular file"):
        read_import(tmp_path)


def test_read_import_unreadable_file(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    target = tmp_path / "in.yaml"
    target.write_bytes(b"a: 1\n")
    target.chmod(0)
    with pytest.raises(StateFileError) as caught:
        read_import(target)
    assert str(caught.value) == "PermissionError: Permission denied"


def test_read_import_does_not_hang_on_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    # A subprocess with a timeout: a hang would block the test run itself.
    code = (
        "import sys; from pathlib import Path; "
        "from llm_harness.state_files import read_import, StateFileError\n"
        "try:\n    read_import(Path(sys.argv[1]))\n"
        "except StateFileError as e:\n    print(e)\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code, str(fifo)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert done.stdout.strip() == "not a regular file"


def test_read_import_refuses_a_device(tmp_path: Path) -> None:
    with pytest.raises(StateFileError, match="not a regular file"):
        read_import(Path("/dev/zero"))
