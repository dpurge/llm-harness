"""Argument building and path mapping are pure text, so they run on every platform."""

import pytest

from llm_harness.docker_command import docker_run_args, new_container_name

IMAGE = "python:3.12"
NAME = "llm-harness-cmd-test"
ARGV = ["python", "-c", "print(1)"]


def build(**overrides: object) -> list[str]:
    arguments: dict[str, object] = {
        "image": IMAGE,
        "name": NAME,
        "argv": ARGV,
        "cwd": "/home/x/proj",
    }
    arguments.update(overrides)
    return docker_run_args(**arguments)  # type: ignore[arg-type]


def test_posix_cwd_is_mounted_at_work() -> None:
    assert build() == [
        "run", "--rm", "--pull", "never", "--name", NAME,
        "-v", "/home/x/proj:/work", "-w", "/work",
        "--entrypoint=", IMAGE, "python", "-c", "print(1)",
    ]  # fmt: skip


def test_windows_backslash_cwd_is_passed_unchanged() -> None:
    args = build(cwd="C:\\Users\\x\\proj")
    assert args[args.index("-v") + 1] == "C:\\Users\\x\\proj:/work"


def test_windows_forward_slash_cwd_with_lowercase_drive() -> None:
    args = build(cwd="c:/Users/x/proj")
    assert args[args.index("-v") + 1] == "c:/Users/x/proj:/work"


def test_no_cwd_means_no_mount_and_no_workdir() -> None:
    args = build(cwd=None)
    assert args == [
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        NAME,
        "--entrypoint=",
        IMAGE,
        *ARGV,
    ]
    assert "-v" not in args
    assert "-w" not in args


def test_paths_and_arguments_with_spaces_stay_single_elements() -> None:
    args = build(cwd="C:\\My Projects\\a b", argv=["echo", "hello world"])
    assert "C:\\My Projects\\a b:/work" in args
    assert args[-2:] == ["echo", "hello world"]


def test_entrypoint_is_cleared_after_the_mount_and_before_the_image() -> None:
    args = build()
    assert args.index("-w") < args.index("--entrypoint=") < args.index(IMAGE)
    assert args.index("--entrypoint=") == args.index(IMAGE) - 1
    assert args[args.index(IMAGE) + 1] == ARGV[0]


def test_container_names_are_unique_and_prefixed() -> None:
    first, second = new_container_name(), new_container_name()
    assert first != second
    assert first.startswith("llm-harness-cmd-")
    assert len(first) == len("llm-harness-cmd-") + 32


def test_image_is_never_pulled() -> None:
    args = build()
    assert args[args.index("--pull") + 1] == "never"
    assert args.index("--pull") < args.index(IMAGE)


def test_no_network_limit_or_terminal_flags() -> None:
    args = build()
    for flag in (
        "--network",
        "--memory",
        "--cpus",
        "-i",
        "-t",
        "--interactive",
        "--tty",
    ):
        assert flag not in args


@pytest.mark.parametrize("cwd", ["proj", "./proj", "..\\proj", "C:proj", "C:", ""])
def test_relative_cwd_is_rejected(cwd: str) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        build(cwd=cwd)


@pytest.mark.parametrize("cwd", ["\\\\server\\share\\dir", "//server/share/dir"])
def test_unc_cwd_is_rejected(cwd: str) -> None:
    with pytest.raises(ValueError, match="UNC"):
        build(cwd=cwd)


@pytest.mark.parametrize("cwd", ["/home/a:b", "C:\\Users\\a:b", "C:/x:y"])
def test_colon_outside_the_drive_letter_is_rejected(cwd: str) -> None:
    with pytest.raises(ValueError, match="colon"):
        build(cwd=cwd)


@pytest.mark.parametrize("image", ["", "-v", "--privileged"])
def test_empty_or_dash_image_is_rejected(image: str) -> None:
    with pytest.raises(ValueError, match="image"):
        build(image=image)


@pytest.mark.parametrize("argv", [[], [""], ["", "x"]])
def test_empty_argv_is_rejected(argv: list[str]) -> None:
    with pytest.raises(ValueError, match="command is empty"):
        build(argv=argv)


def test_empty_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="name is empty"):
        build(name="")
