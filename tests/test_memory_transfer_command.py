"""`/memory export` and `/memory import`: the parser and `handle_memory_text`.

Offline: a throwaway SQLite file, the real tool registry, `tmp_path` files and no model.
The front ends are tested in their own files (GUI, TUI, plain mode).
"""

import asyncio
import os
import threading
from pathlib import Path

import pytest
import yaml

import llm_harness.memory_command as memory_command_module
import llm_harness.memory_tools as memory_tools_module
from llm_harness.builtin_tools import build_tool_registry
from llm_harness.config import StorageConfig, VectorConfig
from llm_harness.embeddings import Embedder
from llm_harness.memory import MemoryStore
from llm_harness.memory_command import (
    MEMORY_OFF,
    NOT_ADMIN_EXPORT,
    NOT_ADMIN_IMPORT,
    SLOW_ACTIONS,
    USAGE,
    MemoryCommand,
    handle_memory_text,
    parse_memory_command,
)
from llm_harness.memory_transfer import MEMORIES_FILENAME, parse_memories_file
from llm_harness.storage import Storage, open_storage
from tests.fakes import ConceptEmbedder, FakeVectorStore
from tests.test_memory_vectors_wiring import (
    embedding_model,
    failing_factory,
    vector_registry,
)
from tests.platform_marks import posix_only

PAST = "2026-10-01T08:00:00Z"
EARLIER = "2026-09-01T08:00:00Z"
FUTURE = "2999-01-01T00:00:00Z"
SECRET = "sk-abcdefgh12345678"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path,
        relational_path=tmp_path / "memory.db",
    )


class Rig:
    def __init__(self, storage: Storage, tools=None) -> None:
        self.storage = storage
        self.tools = tools or build_tool_registry([], memory=True, user_id="ann")
        self.store = MemoryStore(storage.relational)

    async def say(
        self, text: str, *, user_id: str = "ann", admins: tuple[str, ...] = ()
    ) -> str:
        answer = await handle_memory_text(
            text, self.tools, self.storage, user_id, memory_admins=admins
        )
        assert answer is not None
        return answer


@pytest.fixture
def rig(storage: Storage) -> Rig:
    return Rig(storage)


def entries_yaml(*entries: str) -> bytes:
    return ("version: 1\nmemories:\n" + "".join(f"- {e}\n" for e in entries)).encode()


def note(text: str, keywords: str = "[tea]", extra: str = "") -> str:
    return f"{{text: '{text}', keywords: {keywords}{extra}}}"


def write(path: Path, *entries: str) -> Path:
    path.write_bytes(entries_yaml(*entries))
    return path


class Spy:
    """Records the thread of every call and passes it on to the real function."""

    def __init__(self, module, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.real = getattr(module, name)
        self.threads: list[int] = []
        monkeypatch.setattr(module, name, self)

    def __call__(self, *args, **kwargs):
        self.threads.append(threading.get_ident())
        return self.real(*args, **kwargs)


def forbid(monkeypatch: pytest.MonkeyPatch, module, name: str) -> list[object]:
    calls: list[object] = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError(f"{name} must not be called")

    monkeypatch.setattr(module, name, refuse)
    return calls


# --- parser (15) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("/memory export", MemoryCommand("export")),
        ("/memory export /tmp/a.yaml", MemoryCommand("export", query="/tmp/a.yaml")),
        ("/memory export --shared", MemoryCommand("export", shared=True)),
        (
            "/memory export --shared /tmp/a.yaml",
            MemoryCommand("export", query="/tmp/a.yaml", shared=True),
        ),
        ("/memory import /tmp/a.yaml", MemoryCommand("import", query="/tmp/a.yaml")),
        (
            "/memory import --shared ~/my notes.yaml",
            MemoryCommand("import", query="~/my notes.yaml", shared=True),
        ),
        ("/memory import", MemoryCommand("usage")),
        ("/memory import --shared", MemoryCommand("usage")),
        ("/memory export ./--shared", MemoryCommand("export", query="./--shared")),
        (
            "/memory export /tmp/a --shared",
            MemoryCommand("export", query="/tmp/a --shared"),
        ),
        (
            "/memory import /tmp/a  b.yaml  ",
            MemoryCommand("import", query="/tmp/a  b.yaml"),
        ),
    ],
)
def test_the_transfer_forms(line: str, expected: MemoryCommand) -> None:
    assert parse_memory_command(line) == expected


def test_export_and_import_run_in_the_task_slot() -> None:
    assert {"add", "export", "import"} <= set(SLOW_ACTIONS)


# --- memory off (16) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "line", ["/memory export /tmp/x.yaml", "/memory import /tmp/x"]
)
async def test_memory_off_answers_before_any_filesystem_access(
    line: str, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = forbid(monkeypatch, memory_command_module, "read_import")
    writes = forbid(monkeypatch, memory_command_module, "write_export")
    tools = build_tool_registry([], memory=True, user_id="ann")
    no_tools = build_tool_registry([], memory=False, user_id="ann")
    assert await handle_memory_text(line, tools, None, "ann") == MEMORY_OFF
    assert await handle_memory_text(line, tools, storage, None) == MEMORY_OFF
    assert await handle_memory_text(line, no_tools, storage, "ann") == MEMORY_OFF
    assert await handle_memory_text(line, None, storage, "ann") == MEMORY_OFF
    assert reads == [] and writes == []


# --- export (17, 18, 19) ------------------------------------------------------------------


@posix_only
async def test_export_writes_a_faithful_0600_file(rig: Rig, tmp_path: Path) -> None:
    first = await rig.store.save("ann", "Ann likes tea", ["tea"])
    mine = await rig.store.save("ann", "Ann shares jam", ["jam"])
    await rig.store.set_shared("ann", mine.id, True)
    await rig.store.save("bob", "Bob secret plan", ["plan"])
    other = await rig.store.save("bob", "Bob shared fact", ["fact"])
    await rig.store.set_shared("bob", other.id, True)
    target = tmp_path / "out.yaml"
    answer = await rig.say(f"/memory export {target}", admins=("ann",))
    assert answer == f"exported 2 memories to {target}"
    assert target.stat().st_mode & 0o777 == 0o600
    parsed = parse_memories_file(target.read_bytes())
    assert [(e.text, e.keywords, e.shared) for e in parsed] == [
        ("Ann likes tea", ["tea"], False),
        ("Ann shares jam", ["jam"], True),
    ]
    assert (parsed[0].created, parsed[0].updated) == (
        first.created_at,
        first.updated_at,
    )
    assert "Bob" not in target.read_text()


@posix_only
async def test_export_without_a_path_writes_the_default_name_in_the_cwd(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await rig.store.save("ann", "Ann likes tea", ["tea"])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    answer = await rig.say("/memory export")
    assert answer.startswith("exported 1 memories to ")
    assert (work / MEMORIES_FILENAME).stat().st_mode & 0o777 == 0o600
    assert MEMORIES_FILENAME == "llm-harness-memories.yaml"


@posix_only
async def test_export_never_overwrites(rig: Rig, tmp_path: Path) -> None:
    await rig.store.save("ann", "Ann likes tea", ["tea"])
    existing = tmp_path / "existing.yaml"
    existing.write_text("keep me")
    victim = tmp_path / "victim.txt"
    victim.write_text("victim")
    dangling = tmp_path / "dangling.yaml"
    dangling.symlink_to(tmp_path / "nowhere.txt")
    link = tmp_path / "link.yaml"
    link.symlink_to(victim)
    for path in (existing, dangling, link):
        answer = await rig.say(f"/memory export {path}")
        assert answer == f"{path} already exists"
    assert existing.read_text() == "keep me"
    assert victim.read_text() == "victim"
    assert not (tmp_path / "nowhere.txt").exists()


async def test_export_refuses_bad_targets_and_empty_stores(
    rig: Rig, tmp_path: Path
) -> None:
    target = tmp_path / "out.yaml"
    assert await rig.say(f"/memory export {target}") == "no memories to export"
    assert not target.exists()
    await rig.store.save("ann", "Ann likes tea", ["tea"])
    missing_parent = await rig.say(f"/memory export {tmp_path / 'no' / 'a.yaml'}")
    assert missing_parent == "the parent directory does not exist"
    assert await rig.say(f"/memory export {tmp_path}") == "the path is a directory"
    unknown = await rig.say("/memory export ~nosuchuser_x9/a.yaml")
    assert "\n" not in unknown and "nosuchuser" not in unknown


async def test_export_of_more_than_200_notes_still_writes_and_warns(
    rig: Rig, tmp_path: Path
) -> None:
    for number in range(201):
        await rig.store.save("ann", f"note number {number}", ["bulk"])
    target = tmp_path / "big.yaml"
    answer = await rig.say(f"/memory export {target}")
    assert answer == (
        f"exported 201 memories to {target}; import takes at most 200 per file"
    )
    assert len(yaml.safe_load(target.read_text())["memories"]) == 201


@pytest.mark.parametrize("admins", [(), ("bob",)])
@pytest.mark.parametrize(
    ("verb", "refusal"),
    [("export", NOT_ADMIN_EXPORT), ("import", NOT_ADMIN_IMPORT)],
)
async def test_shared_needs_an_admin_before_any_store_or_file_access(
    rig: Rig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    admins: tuple[str, ...],
    verb: str,
    refusal: str,
) -> None:
    reads = forbid(monkeypatch, memory_command_module, "read_import")
    writes = forbid(monkeypatch, memory_command_module, "write_export")
    store_calls: list[str] = []
    for name in ("list", "has_text", "save", "get", "search"):

        async def spy(self, *args, _name=name, **kwargs):
            store_calls.append(_name)
            raise AssertionError("no store call for a non-admin")

        monkeypatch.setattr(MemoryStore, name, spy)
    path = tmp_path / "does-not-exist" / "x.yaml"
    answer = await rig.say(f"/memory {verb} --shared {path}", admins=admins)
    assert answer == refusal
    assert reads == [] and writes == [] and store_calls == []
    assert not path.parent.exists()


async def test_an_admin_export_with_shared_includes_everyones_shared_notes(
    rig: Rig, tmp_path: Path
) -> None:
    await rig.store.save("ann", "Ann private", ["mine"])
    await rig.store.save("bob", "Bob private", ["his"])
    bob_shared = await rig.store.save("bob", "Bob shared", ["fact"], shared=True)
    await rig.store.save("ann", "Ann shared", ["fact"], shared=True)
    target = tmp_path / "all.yaml"
    answer = await rig.say(f"/memory export --shared {target}", admins=("ann",))
    assert answer == f"exported 3 memories to {target}"
    parsed = parse_memories_file(target.read_bytes())
    assert sorted((e.text, e.shared) for e in parsed) == [
        ("Ann private", False),
        ("Ann shared", True),
        ("Bob shared", True),
    ]
    assert bob_shared.id in [e.id for e in parsed]
    assert "Bob private" not in target.read_text()


# --- import (20 to 23) --------------------------------------------------------------------


async def test_import_saves_the_entries_as_the_importers_own_notes(
    rig: Rig, tmp_path: Path
) -> None:
    source = write(
        tmp_path / "in.yaml",
        *(
            note(
                f"note {n}",
                "[tea]",
                f", id: 77, created: '{EARLIER}', updated: '{PAST}'",
            )
            for n in range(5)
        ),
    )
    answer = await rig.say(f"/memory import {source}", user_id="bob")
    assert answer == (
        "imported 5 of 5 memories; skipped 0 already known; rejected 0 invalid"
    )
    saved = await rig.store.list("bob")
    assert len(saved) == 5 and {m.user_id for m in saved} == {"bob"}
    assert 77 not in {m.id for m in saved}
    assert {(m.created_at, m.updated_at) for m in saved} == {(EARLIER, PAST)}
    assert await rig.store.list("ann") == []
    assert "note" not in answer.replace("memories", "")  # no note text echoed
    assert "#1" in await rig.say("/memory", user_id="bob")


async def test_each_skip_is_counted_and_never_echoed(rig: Rig, tmp_path: Path) -> None:
    await rig.store.save("ann", "own known", ["tea"])
    await rig.store.save("bob", "shared known", ["tea"], shared=True)
    source = write(
        tmp_path / "in.yaml",
        note("fresh one"),
        note("own known"),
        note("shared known"),
        note("twice"),
        note("twice"),
        note(f"key {SECRET}"),
        '{text: "two\\nlines", keywords: [tea]}',
        note("x" * 1501),
        note("bad keyword", "[Not_Lowercase_Secret]"),
        note("fresh two"),
    )
    answer = await rig.say(f"/memory import {source}")
    assert answer == (
        "imported 3 of 10 memories; skipped 3 already known; rejected 4 invalid"
    )
    for hidden in (SECRET, "Not_Lowercase_Secret", "two", "twice"):
        assert hidden not in answer
    texts = {m.text for m in await rig.store.list("ann") if m.user_id == "ann"}
    assert texts == {"own known", "fresh one", "twice", "fresh two"}


async def test_shared_entries_become_personal_unless_an_admin_asks(
    rig: Rig, tmp_path: Path
) -> None:
    def source(name: str) -> Path:
        return write(
            tmp_path / name,
            note(f"{name} one", extra=", shared: true"),
            note(f"{name} two", extra=", shared: true"),
            note(f"{name} plain"),
        )

    plain = await rig.say(f"/memory import {source('a')}", user_id="bob")
    assert plain.endswith("; 2 shared entries imported as personal")
    admin = await rig.say(f"/memory import {source('b')}", admins=("ann",))
    assert admin.endswith("; 2 shared entries imported as personal")
    assert not any(m.shared for m in await rig.store.list("bob"))
    shared = await rig.say(f"/memory import --shared {source('c')}", admins=("ann",))
    assert shared == (
        "imported 3 of 3 memories; skipped 0 already known; rejected 0 invalid"
    )
    listing = await rig.say("/memory", user_id="carol")
    assert listing.count("[shared]") == 2 and "c plain" not in listing


async def test_timestamps_kept_missing_filled_bad_replaced_and_counted(
    rig: Rig, tmp_path: Path
) -> None:
    before = rig.store.current_timestamp()
    source = write(
        tmp_path / "in.yaml",
        note("kept", extra=f", created: '{EARLIER}', updated: '{PAST}'"),
        note("future", extra=f", created: '{EARLIER}', updated: '{FUTURE}'"),
        note("malformed", extra=", created: 'yesterday', updated: '" + PAST + "'"),
        note("unquoted", extra=", created: 2026-10-01T08:00:00Z"),
        note("reversed", extra=f", created: '{PAST}', updated: '{EARLIER}'"),
    )
    answer = await rig.say(f"/memory import {source}")
    assert answer == (
        "imported 5 of 5 memories; skipped 0 already known; rejected 0 invalid"
        "; 4 entries got the current time (bad or future timestamps)"
    )
    by_text = {m.text: m for m in await rig.store.list("ann")}
    assert (by_text["kept"].created_at, by_text["kept"].updated_at) == (EARLIER, PAST)
    for text in ("future", "malformed", "unquoted", "reversed"):
        stamps = (by_text[text].created_at, by_text[text].updated_at)
        assert stamps[0] == stamps[1] and stamps[0] >= before, text


async def test_missing_timestamps_are_filled_silently(rig: Rig, tmp_path: Path) -> None:
    before = rig.store.current_timestamp()
    source = write(
        tmp_path / "in.yaml",
        note("neither"),
        note("only created", extra=f", created: '{PAST}'"),
        note("only updated", extra=f", updated: '{PAST}'"),
    )
    answer = await rig.say(f"/memory import {source}")
    assert answer == (
        "imported 3 of 3 memories; skipped 0 already known; rejected 0 invalid"
    )
    by_text = {m.text: m for m in await rig.store.list("ann")}
    assert by_text["neither"].created_at >= before
    assert by_text["neither"].created_at == by_text["neither"].updated_at
    for text in ("only created", "only updated"):  # the one given value fills the other
        assert (by_text[text].created_at, by_text[text].updated_at) == (PAST, PAST)


# --- refusals (24) ------------------------------------------------------------------------


def refusal_files(tmp_path: Path) -> dict[str, Path]:
    files = {
        "not-utf8": tmp_path / "bad.yaml",
        "invalid": tmp_path / "invalid.yaml",
        "alias": tmp_path / "alias.yaml",
        "version": tmp_path / "version.yaml",
        "unknown": tmp_path / "unknown.yaml",
        "types": tmp_path / "types.yaml",
        "many": tmp_path / "many.yaml",
        "big": tmp_path / "big.yaml",
        "dir": tmp_path / "adir",
        "missing": tmp_path / "missing.yaml",
    }
    files["not-utf8"].write_bytes(b"\xff\xfe\x00bad")
    files["invalid"].write_text("a: [unclosed")
    files["alias"].write_text("version: 1\nmemories: &a []\nx: *a\n")
    files["version"].write_text("version: 2\nmemories: []\n")
    files["unknown"].write_text("version: 1\nmemories: []\nextra: 1\n")
    files["types"].write_text("version: 1\nmemories:\n- {text: 5, keywords: [a]}\n")
    write(files["many"], *(note(f"n{n}") for n in range(201)))
    files["big"].write_bytes(b"#" * 1_000_001)
    files["dir"].mkdir()
    return files


async def test_each_file_refusal_is_one_line_and_saves_nothing(
    rig: Rig, tmp_path: Path
) -> None:
    files = refusal_files(tmp_path)
    expected = {
        "not-utf8": "file is not UTF-8 text",
        "invalid": "not valid YAML (or uses an unsupported tag)",
        "alias": "YAML aliases are not supported",
        "version": "unsupported memories file version 2",
        "unknown": "unknown key in the memories file",
        "types": "memory text must be a string",
        "many": "too many memories (201, at most 200); split the file",
        "big": "file is larger than 1000000 bytes",
        "dir": "not a regular file",
    }
    for name, line in expected.items():
        assert await rig.say(f"/memory import {files[name]}") == line, name
    missing = await rig.say(f"/memory import {files['missing']}")
    assert missing.startswith("FileNotFoundError") and "\n" not in missing
    assert str(tmp_path) not in missing
    assert await rig.store.list("ann") == []


@posix_only
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a POSIX FIFO")
async def test_a_fifo_is_refused_without_hanging(rig: Rig, tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.yaml"
    os.mkfifo(fifo)
    answer = await asyncio.wait_for(rig.say(f"/memory import {fifo}"), timeout=5)
    assert answer == "not a regular file"


async def test_a_hostile_path_never_appears_raw(rig: Rig, tmp_path: Path) -> None:
    await rig.store.save("ann", "Ann likes tea", ["tea"])
    name = "evil\n\x1b[31m\N{RIGHT-TO-LEFT OVERRIDE}.yaml"
    hostile = tmp_path / name
    answers = [await rig.say(f"/memory export {hostile}")]
    assert hostile.exists()
    answers.append(await rig.say(f"/memory export {hostile}"))  # already exists
    answers.append(await rig.say(f"/memory import {tmp_path / 'm\n\x1b.yaml'}"))
    assert "\\u000a" in answers[0] and "\\u001b" in answers[0]
    assert "\\u202e" in answers[0]
    for answer in answers:
        assert not any(
            ch in answer for ch in ("\n\x1b", "\x1b", "\N{RIGHT-TO-LEFT OVERRIDE}")
        )
    assert answers[1].count("\n") == 0 and answers[2].count("\n") == 0


# --- threading and order (25), cancel (26) ------------------------------------------------


async def test_file_work_runs_off_the_event_loop_thread(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await rig.store.save("ann", "Ann likes tea", ["tea"])
    writer = Spy(memory_command_module, "write_export", monkeypatch)
    reader = Spy(memory_command_module, "read_import", monkeypatch)
    parser = Spy(memory_command_module, "parse_memories_file", monkeypatch)
    target = tmp_path / "out.yaml"
    await rig.say(f"/memory export {target}")
    await rig.say(f"/memory import {target}", user_id="bob")
    loop_thread = threading.get_ident()
    for spy in (writer, reader, parser):
        assert len(spy.threads) == 1 and spy.threads[0] != loop_thread


async def test_the_read_and_parse_finish_before_the_first_save(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_read = memory_command_module.read_import
    real_parse = memory_command_module.parse_memories_file
    real_save = MemoryStore.save

    def read(path):
        order.append("read")
        return real_read(path)

    def parse(data):
        order.append("parse")
        return real_parse(data)

    async def save(self, *args, **kwargs):
        order.append("save")
        return await real_save(self, *args, **kwargs)

    monkeypatch.setattr(memory_command_module, "read_import", read)
    monkeypatch.setattr(memory_command_module, "parse_memories_file", parse)
    monkeypatch.setattr(MemoryStore, "save", save)
    source = write(tmp_path / "in.yaml", note("one"), note("two"))
    await rig.say(f"/memory import {source}")
    assert order == ["read", "parse", "save", "save"]


async def test_cancelling_an_import_keeps_the_saved_note_and_a_rerun_finishes(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = write(
        tmp_path / "in.yaml",
        note("first", extra=f", created: '{EARLIER}', updated: '{EARLIER}'"),
        note("second", extra=f", created: '{PAST}', updated: '{PAST}'"),
    )
    real_save = MemoryStore.save
    saves: list[str] = []

    async def blocking_save(self, user_id, text, *args, **kwargs):
        saves.append(text)
        if len(saves) > 1:
            await asyncio.Event().wait()
        return await real_save(self, user_id, text, *args, **kwargs)

    monkeypatch.setattr(MemoryStore, "save", blocking_save)
    task = asyncio.create_task(rig.say(f"/memory import {source}"))
    for _ in range(200):
        if len(saves) == 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(MemoryStore, "save", real_save)
    assert [m.text for m in await rig.store.list("ann")] == ["first"]
    again = await rig.say(f"/memory import {source}")
    assert again == (
        "imported 1 of 2 memories; skipped 1 already known; rejected 0 invalid"
    )
    kept = {m.text: (m.created_at, m.updated_at) for m in await rig.store.list("ann")}
    assert kept == {"first": (EARLIER, EARLIER), "second": (PAST, PAST)}


async def test_cancelling_an_export_leaves_no_half_file(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await rig.store.save("ann", "Ann likes tea", ["tea"])
    real_write = memory_command_module.write_export
    started, release = threading.Event(), threading.Event()

    def slow_write(path, data):
        started.set()
        release.wait(5)
        real_write(path, data)

    monkeypatch.setattr(memory_command_module, "write_export", slow_write)
    target = tmp_path / "out.yaml"
    task = asyncio.create_task(rig.say(f"/memory export {target}"))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(200):  # the thread cannot be interrupted: it may still finish
        if target.exists():
            break
        await asyncio.sleep(0.01)
    assert not target.exists() or len(parse_memories_file(target.read_bytes())) == 1


# --- indexed store (27) -------------------------------------------------------------------


def upserts(storage: Storage) -> int:
    return len([c for c in storage.vectors.calls if c[0] == "upsert"])  # type: ignore[attr-defined]


async def test_an_import_embeds_once_per_saved_note(
    tmp_path: Path, storage_with_vectors: Storage
) -> None:
    provider = ConceptEmbedder()
    tools = vector_registry(lambda: Embedder(provider, embedding_model()))
    rig = Rig(storage_with_vectors, tools)
    await rig.store.save("alice", "already here", ["tea"])
    source = write(
        tmp_path / "in.yaml",
        note("one"),
        note("two"),
        note("three"),
        note("already here"),
    )
    answer = await rig.say(f"/memory import {source}", user_id="alice")
    assert answer == (
        "imported 3 of 4 memories; skipped 1 already known; rejected 0 invalid"
    )
    assert len(provider.calls) == 3
    assert upserts(storage_with_vectors) == 3


async def test_a_failing_embedder_does_not_fail_the_import(
    tmp_path: Path, storage_with_vectors: Storage
) -> None:
    rig = Rig(storage_with_vectors, vector_registry(failing_factory()))
    source = write(
        tmp_path / "in.yaml",
        note("one", extra=f", created: '{PAST}', updated: '{PAST}'"),
        note("two"),
    )
    answer = await rig.say(f"/memory import {source}", user_id="alice")
    lines = answer.splitlines()
    assert lines[0] == (
        "imported 2 of 2 memories; skipped 0 already known; rejected 0 invalid"
    )
    assert len(lines) == 2 and "vector search" in lines[1]  # the one-time notice
    saved = {m.text: m.created_at for m in await rig.store.list("alice")}
    assert saved["one"] == PAST and set(saved) == {"one", "two"}


@pytest.fixture
def storage_with_vectors(storage: Storage) -> Storage:
    storage.vectors = FakeVectorStore()
    return storage


# --- tool safety (32) ---------------------------------------------------------------------


def test_no_tool_reflection_or_orchestrator_module_references_the_transfer() -> None:
    import inspect

    import llm_harness.memory_reflection as reflection_module
    import llm_harness.orchestrator as orchestrator_module

    names = (
        "memory_transfer",
        "export_memories",
        "parse_memories_file",
        "NOT_ADMIN_EXPORT",
        "NOT_ADMIN_IMPORT",
    )
    for module in (memory_tools_module, reflection_module, orchestrator_module):
        source = inspect.getsource(module)
        assert not [name for name in names if name in source], module.__name__


def test_usage_names_the_transfer_forms() -> None:
    lines = USAGE.splitlines()
    assert "/memory export [--shared] [PATH]" in lines[0]
    assert "/memory import [--shared] PATH" in lines[0]
    (export_line,) = (line for line in lines if line.startswith("  export "))
    (import_line,) = (line for line in lines if line.startswith("  import "))
    assert "own notes" in import_line and "--shared" in import_line
    assert "skips known" in import_line and "admins" in import_line
    assert any("not the state /export and /import" in line for line in lines)
    assert "YAML" in export_line
