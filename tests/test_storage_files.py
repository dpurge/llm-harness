"""The local file store: round trip, modes, atomicity, path and symlink safety."""

import asyncio
import os
import stat
from pathlib import Path

import pytest

from llm_harness.storage.files import FileStoreError, LocalFileStore


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "files"


@pytest.fixture
def store(root: Path) -> LocalFileStore:
    return LocalFileStore(root)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "secret.txt").write_bytes(b"secret")
    return directory


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


async def test_round_trip(store: LocalFileStore) -> None:
    await store.put("a/b/c.bin", b"\x00\xffdata")
    assert await store.get("a/b/c.bin") == b"\x00\xffdata"
    assert await store.exists("a/b/c.bin")


async def test_overwrite_replaces_content(store: LocalFileStore) -> None:
    await store.put("f", b"first, longer")
    await store.put("f", b"second")
    assert await store.get("f") == b"second"


@pytest.mark.parametrize("umask", [0, 0o277])
async def test_modes_do_not_depend_on_the_umask(
    store: LocalFileStore, root: Path, umask: int
) -> None:
    previous = os.umask(umask)
    try:
        await store.put("a/b/c.txt", b"x")
    finally:
        os.umask(previous)
    for directory in (root, root / "a", root / "a" / "b", root / ".tmp"):
        assert _mode(directory) == 0o700, directory
    assert _mode(root / "a" / "b" / "c.txt") == 0o600


async def test_construction_and_reads_create_nothing(tmp_path: Path) -> None:
    root = tmp_path / "files"
    store = LocalFileStore(root)
    store.namespace("notes")
    assert not await store.exists("a")
    assert await store.list() == []
    assert not await store.delete("a")
    with pytest.raises(FileStoreError, match="no such file: a"):
        await store.get("a")
    assert list(tmp_path.iterdir()) == []


async def test_failed_replace_keeps_the_original_and_leaves_no_temp_file(
    store: LocalFileStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.put("f", b"original")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(FileStoreError, match="cannot write f") as caught:
        await store.put("f", b"new")
    monkeypatch.undo()
    assert str(root) not in str(caught.value)
    assert await store.get("f") == b"original"
    assert list((root / ".tmp").iterdir()) == []


@pytest.mark.parametrize(
    "path",
    [
        "/abs",
        "",
        "..",
        "a/../b",
        ".",
        "a/./b",
        "a//b",
        "a/",
        ".hidden",
        "a/.b",
        ".tmp/x",
        "a\0b",
        "a\\b",
        "x" * 256,
        "a/" * 513,
    ],
)
async def test_invalid_paths_are_rejected_by_every_method(
    store: LocalFileStore, path: str
) -> None:
    for call in (
        store.put(path, b"x"),
        store.get(path),
        store.exists(path),
        store.delete(path),
    ):
        with pytest.raises(FileStoreError):
            await call


@pytest.mark.parametrize("prefix", ["/abs", "a/../b", "a//b", ".tmp"])
async def test_invalid_list_prefix_is_rejected(
    store: LocalFileStore, prefix: str
) -> None:
    with pytest.raises(FileStoreError):
        await store.list(prefix)


async def test_path_at_the_length_limits_is_accepted(store: LocalFileStore) -> None:
    await store.put("x" * 255, b"1")
    assert await store.get("x" * 255) == b"1"


async def test_error_message_includes_the_relative_path_but_not_the_root(
    store: LocalFileStore, root: Path
) -> None:
    with pytest.raises(FileStoreError) as caught:
        await store.get("dir/missing.txt")
    assert "dir/missing.txt" in str(caught.value)
    assert str(root) not in str(caught.value)


async def test_put_of_text_is_rejected_with_the_fix(
    store: LocalFileStore, root: Path
) -> None:
    with pytest.raises(FileStoreError, match="encode"):
        await store.put("f", "text")  # type: ignore[arg-type]
    assert not root.exists()


async def test_symlinked_directory_pointing_outside_is_refused(
    store: LocalFileStore, root: Path, outside: Path
) -> None:
    await store.put("seed", b"x")
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileStoreError, match="symlink: link/new.txt"):
        await store.put("link/new.txt", b"x")
    assert not (outside / "new.txt").exists()
    with pytest.raises(FileStoreError, match="symlink"):
        await store.get("link/secret.txt")
    with pytest.raises(FileStoreError, match="symlink"):
        await store.list("link")
    with pytest.raises(FileStoreError, match="symlink"):
        await store.delete("link/secret.txt")
    assert (outside / "secret.txt").read_bytes() == b"secret"
    assert not await store.exists("link/secret.txt")


async def test_symlinked_directory_inside_the_root_is_followed(
    store: LocalFileStore, root: Path
) -> None:
    await store.put("real/f", b"x")
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    assert await store.get("alias/f") == b"x"


async def test_get_refuses_a_final_symlink(
    store: LocalFileStore, root: Path, outside: Path
) -> None:
    await store.put("seed", b"x")
    (root / "link").symlink_to(outside / "secret.txt")
    with pytest.raises(FileStoreError, match="link"):
        await store.get("link")
    assert not await store.exists("link")
    assert not await store.delete("link")
    assert (root / "link").is_symlink()


async def test_put_replaces_a_final_symlink_and_leaves_the_target_alone(
    store: LocalFileStore, root: Path, outside: Path
) -> None:
    await store.put("seed", b"x")
    (root / "link").symlink_to(outside / "secret.txt")
    await store.put("link", b"new")
    assert (outside / "secret.txt").read_bytes() == b"secret"
    assert not (root / "link").is_symlink()
    assert await store.get("link") == b"new"


async def test_get_of_a_fifo_does_not_hang(store: LocalFileStore, root: Path) -> None:
    await store.put("seed", b"x")
    os.mkfifo(root / "pipe")
    with pytest.raises(FileStoreError, match="not a regular file: pipe"):
        await asyncio.wait_for(store.get("pipe"), timeout=5)
    assert not await store.exists("pipe")


async def test_tmp_directory_that_is_a_symlink_is_refused(
    store: LocalFileStore, root: Path, outside: Path
) -> None:
    root.mkdir()
    (root / ".tmp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(FileStoreError, match="temporary directory"):
        await store.put("f", b"x")
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


async def test_list_is_sorted_recursive_and_filters_by_prefix(
    store: LocalFileStore,
) -> None:
    for path in ("b.txt", "a/z.txt", "a/b/c.txt", "ab/x.txt", "a.txt"):
        await store.put(path, b"x")
    assert await store.list() == ["a.txt", "a/b/c.txt", "a/z.txt", "ab/x.txt", "b.txt"]
    assert await store.list("a") == ["a/b/c.txt", "a/z.txt"]
    assert await store.list("a/b") == ["a/b/c.txt"]
    assert await store.list("missing") == []
    assert await store.list("a.txt") == []


async def test_list_excludes_tmp_symlinks_and_other_non_regular_files(
    store: LocalFileStore, root: Path, outside: Path
) -> None:
    await store.put("keep", b"x")
    (root / "file_link").symlink_to(outside / "secret.txt")
    (root / "dir_link").symlink_to(outside, target_is_directory=True)
    (root / "empty_dir").mkdir()
    os.mkfifo(root / "pipe")
    (root / ".tmp" / "stray").write_bytes(b"x")
    assert await store.list() == ["keep"]


async def test_delete_returns_true_then_false(store: LocalFileStore) -> None:
    await store.put("a/f", b"x")
    assert await store.delete("a/f") is True
    assert await store.delete("a/f") is False
    assert not await store.exists("a/f")


async def test_exists_is_false_for_a_directory(store: LocalFileStore) -> None:
    await store.put("a/f", b"x")
    assert not await store.exists("a")
    assert not await store.delete("a")
    assert await store.exists("a/f")


async def test_put_onto_a_directory_fails_and_leaves_no_temp_file(
    store: LocalFileStore, root: Path
) -> None:
    await store.put("a/f", b"x")
    with pytest.raises(FileStoreError, match="cannot write a"):
        await store.put("a", b"x")
    assert list((root / ".tmp").iterdir()) == []


async def test_namespaces_are_isolated(store: LocalFileStore, root: Path) -> None:
    first, second = store.namespace("first"), store.namespace("second_2")
    await first.put("f", b"one")
    assert await first.get("f") == b"one"
    assert not await second.exists("f")
    assert await second.list() == []
    assert (root / "first" / "f").read_bytes() == b"one"
    with pytest.raises(FileStoreError):
        await second.get("../first/f")


@pytest.mark.parametrize(
    "name", ["", "A", "1a", "a-b", "a b", "a/b", "..", ".tmp", "a" * 65, "a\n", "é"]
)
def test_invalid_namespace_names_are_rejected(store: LocalFileStore, name: str) -> None:
    with pytest.raises(FileStoreError, match="namespace"):
        store.namespace(name)


def test_namespace_name_of_64_characters_is_accepted(store: LocalFileStore) -> None:
    store.namespace("a" * 64)


async def test_concurrent_puts_to_one_path_leave_one_complete_content(
    store: LocalFileStore, root: Path
) -> None:
    first, second = b"1" * 200_000, b"2" * 150_000
    await asyncio.gather(
        *(store.put("f", first if i % 2 else second) for i in range(10))
    )
    assert await store.get("f") in (first, second)
    assert list((root / ".tmp").iterdir()) == []
    assert await store.list() == ["f"]
