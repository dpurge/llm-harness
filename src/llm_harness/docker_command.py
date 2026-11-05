"""Pure `docker run` argument building and host-path mapping for command execution.

Nothing here starts a process or touches the filesystem. Paths are handled as
text so a Windows path can be checked on any host, and the source is passed to
Docker unchanged (Docker Desktop accepts `C:\\Users\\x\\proj:/work`). There is no
`--network`, CPU or memory flag, and stdin is not attached.
"""

import uuid
from collections.abc import Sequence

CONTAINER_NAME_PREFIX = "llm-harness-cmd-"
CONTAINER_WORKDIR = "/work"
_SEPARATORS = ("/", "\\")


def new_container_name() -> str:
    """A unique name, so the container can be removed by name after a timeout."""
    return CONTAINER_NAME_PREFIX + uuid.uuid4().hex


def _has_drive_prefix(path: str) -> bool:
    return (
        len(path) >= 3
        and path[0].isascii()
        and path[0].isalpha()
        and path[1] == ":"
        and path[2] in _SEPARATORS
    )


def mount_source(cwd: str) -> str:
    """The host directory to mount, or ValueError saying how to fix `cwd`."""
    if cwd.startswith(("\\\\", "//")):
        raise ValueError(
            f"working directory {cwd!r} is a network (UNC) path, which is not "
            "supported; use a local directory with a drive letter such as C:\\work"
        )
    if _has_drive_prefix(cwd):
        rest = cwd[2:]
    elif cwd.startswith("/"):
        rest = cwd
    else:
        raise ValueError(
            f"working directory {cwd!r} must be absolute: use a POSIX path "
            "such as /home/x/proj or a Windows drive path such as C:\\Users\\x\\proj"
        )
    if ":" in rest:
        raise ValueError(
            f"working directory {cwd!r} contains a colon, which would break the "
            "docker -v syntax; use a directory name without ':'"
        )
    return cwd


def docker_run_args(
    *, image: str, name: str, argv: Sequence[str], cwd: str | None
) -> list[str]:
    """The arguments after the `docker` program name; ValueError on unusable input."""
    if not image:
        raise ValueError("docker image is empty; set the image name in the config")
    if image.startswith("-"):
        raise ValueError(
            f"docker image {image!r} starts with '-' and would be read as an option; "
            "use an image name such as python:3.12"
        )
    if not name:
        raise ValueError("container name is empty; use new_container_name()")
    if not argv or not argv[0]:
        raise ValueError("command is empty; give the program to run as argv[0]")
    mount: list[str] = []
    if cwd is not None:
        mount = [
            "-v",
            f"{mount_source(cwd)}:{CONTAINER_WORKDIR}",
            "-w",
            CONTAINER_WORKDIR,
        ]
    # `docker run` pulls a missing image by default; never pull, fail instead.
    # `--entrypoint=` clears the image's ENTRYPOINT so argv[0] is the program that
    # runs, not an argument to it; docker stops option parsing at the image.
    return [
        "run", "--rm", "--pull", "never", "--name", name, *mount,
        "--entrypoint=", image, *argv,
    ]  # fmt: skip
