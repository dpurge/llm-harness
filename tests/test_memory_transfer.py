"""`memory_transfer`: the memories YAML writer and the strict reader, and a store round trip.

Pure bytes in and out, no model and no network. The round trip uses a real
`SqliteRelationalStore` on `tmp_path` and an injected clock.
"""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from llm_harness.memory import Memory, MemoryStore, check_timestamp
from llm_harness.memory_transfer import (
    FORMAT_VERSION,
    MAX_ENTRIES,
    MEMORIES_FILENAME,
    MemoriesFileError,
    TransferEntry,
    export_memories,
    parse_memories_file,
)
from llm_harness.state_io import MAX_IMPORT_BYTES
from llm_harness.storage.relational import SqliteRelationalStore

NOW = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
NOW_TEXT = "2026-11-04T10:00:00Z"
OLD = "2026-10-01T08:00:00Z"


def note(
    id: int,
    text: str = "Likes tea",
    keywords: tuple[str, ...] = ("tea",),
    created: str = OLD,
    updated: str = OLD,
    shared: bool = False,
    user_id: str = "alice",
) -> Memory:
    return Memory(id, user_id, text, keywords, created, updated, shared)


def doc(entries: object, **top: object) -> bytes:
    body = {"version": 1, "memories": entries, **top}
    return yaml.safe_dump(body).encode()


def entry(**fields: object) -> dict:
    return {"text": "Likes tea", "keywords": ["tea"], **fields}


# --- the writer and the reader round trip ----------------------------------------------


def test_the_constants_are_the_planned_ones() -> None:
    assert (FORMAT_VERSION, MAX_ENTRIES) == (1, 200)
    assert MEMORIES_FILENAME == "llm-harness-memories.yaml"


def test_export_then_parse_round_trips_every_field() -> None:
    notes = [
        note(2, "Prefers metric — Zażółć gęślą jaźń", ("metric", "units"), shared=True),
        note(1, "Likes tea", created="2026-09-01T08:00:00Z"),
    ]
    data = export_memories(notes, NOW_TEXT)
    assert data.startswith(b"version: 1\n")
    assert parse_memories_file(data) == [
        TransferEntry("Likes tea", ["tea"], "2026-09-01T08:00:00Z", OLD, False, 1),
        TransferEntry(
            "Prefers metric — Zażółć gęślą jaźń", ["metric", "units"], OLD, OLD, True, 2
        ),
    ]


def test_the_file_shape_loads_with_plain_safe_load_and_has_no_user() -> None:
    data = export_memories(
        [note(5, shared=True), note(3, user_id="bob"), note(4)], NOW_TEXT
    )
    loaded = yaml.safe_load(data)
    assert list(loaded) == ["version", "exported", "memories"]
    assert loaded["version"] == 1 and loaded["exported"] == NOW_TEXT
    assert [e["id"] for e in loaded["memories"]] == [3, 4, 5]  # ascending id
    assert [list(e) for e in loaded["memories"]] == [
        ["id", "text", "keywords", "created", "updated"],
        ["id", "text", "keywords", "created", "updated"],
        ["id", "text", "keywords", "created", "updated", "shared"],
    ]
    assert "alice" not in data.decode() and "user_id" not in data.decode()


def test_timestamps_are_written_as_quoted_strings() -> None:
    text = export_memories([note(1)], NOW_TEXT).decode()
    assert f"created: '{OLD}'" in text and f"exported: '{NOW_TEXT}'" in text


# --- refusals of the whole file ---------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"x" * (MAX_IMPORT_BYTES + 1), "file is larger than 1000000 bytes"),
        (b"\xff\xfe\x00", "file is not UTF-8 text"),
        (b"a: [unclosed", "not valid YAML (or uses an unsupported tag)"),
        (b"version: 1\nmemories: !!python/object:os.system x", "not valid YAML"),
        (
            b"version: 1\nmemories: [{text: a, keywords: [x], created: 2026-13-45}]",
            "not valid YAML",
        ),  # the loader's constructor raises a plain ValueError
        (b"version: " + b"9" * 5000, "not valid YAML"),
        (b"a: &x [1]\nb: *x", "YAML aliases are not supported"),
        (b"- 1\n- 2", "top level must be a mapping"),
        (b"memories: []", "memories file has no version"),
        (b"version: 2\nmemories: []", "unsupported memories file version 2"),
        (b"version: '1'\nmemories: []", "unsupported memories file version"),
        (b"version: true\nmemories: []", "unsupported memories file version"),
        (b"version: 1.0\nmemories: []", "unsupported memories file version"),
        (b"version: 1\nmemories: []\nuser_id: bob", "unknown key in the memories file"),
        (b"version: 1\nmemories: {}", "memories must be a list"),
        (b"version: 1", "memories must be a list"),
        (b"version: 1\nmemories: [1]", "every memory must be a mapping"),
    ],
)
def test_a_bad_file_is_refused_with_a_fixed_phrase(data: bytes, message: str) -> None:
    with pytest.raises(MemoriesFileError) as raised:
        parse_memories_file(data)
    assert str(raised.value).startswith(message)
    assert "\n" not in str(raised.value)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"extra": 1}, "unknown key in a memory"),
        ({"user_id": "bob"}, "unknown key in a memory"),
        ({"text": 5}, "memory text must be a string"),
        ({"keywords": "tea"}, "memory keywords must be a list"),
        ({"id": "7"}, "memory id must be an integer"),
        ({"id": True}, "memory id must be an integer"),
        ({"shared": "yes"}, "memory shared must be true or false"),
        ({"shared": 1}, "memory shared must be true or false"),
    ],
)
def test_a_bad_entry_refuses_the_file(fields: dict, message: str) -> None:
    with pytest.raises(MemoriesFileError, match=message):
        parse_memories_file(doc([entry(**fields)]))


def test_an_entry_without_text_or_keywords_refuses_the_file() -> None:
    with pytest.raises(MemoriesFileError, match="text must be a string"):
        parse_memories_file(doc([{"keywords": ["tea"]}]))
    with pytest.raises(MemoriesFileError, match="keywords must be a list"):
        parse_memories_file(doc([{"text": "Likes tea"}]))


def test_no_message_carries_a_value_from_the_file() -> None:
    secret = "sk-abcdefgh12345678"
    bad = [
        doc([entry(**{secret: 1})]),
        doc([entry(text=secret + "x")], **{secret: 1}),
        doc([entry(id=secret)]),
        doc([entry(shared=secret)]),
        doc([entry(keywords=secret)]),
        doc([secret]),
        f"version: {secret}\nmemories: []".encode(),
        doc(secret),
        b"version: " + b"9" * 5000 + b"\nmemories: []",
    ]
    for data in bad:
        with pytest.raises(MemoriesFileError) as raised:
            parse_memories_file(data)
        assert secret not in str(raised.value)
        assert "\n" not in str(raised.value)


def test_exactly_200_entries_parse_and_201_do_not() -> None:
    assert (
        len(parse_memories_file(doc([entry() for _ in range(MAX_ENTRIES)])))
        == MAX_ENTRIES
    )
    with pytest.raises(MemoriesFileError) as raised:
        parse_memories_file(doc([entry() for _ in range(MAX_ENTRIES + 1)]))
    assert str(raised.value) == "too many memories (201, at most 200); split the file"


# --- a nesting bomb and an alias bomb ---------------------------------------------------

NESTED = "YAML is nested too deeply"


def bombs() -> list[bytes]:
    # flow brackets, and block sequences opened on one line (`- - - x`)
    return [b"[" * 200_000, b"- " * 50_000 + b"x"]


def test_a_nesting_bomb_is_refused_quickly() -> None:
    for data in bombs():
        started = time.monotonic()
        with pytest.raises(MemoriesFileError, match=NESTED):
            parse_memories_file(data)
        assert time.monotonic() - started < 5


async def test_a_nesting_bomb_is_refused_inside_a_worker_thread() -> None:
    for data in bombs():
        with pytest.raises(MemoriesFileError, match=NESTED):
            await asyncio.to_thread(parse_memories_file, data)


def test_an_alias_bomb_is_refused_without_expanding() -> None:
    levels = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    for letter, prev in zip("bcdefgh", "abcdefg", strict=True):
        levels.append(f"{letter}: &{letter} [" + ", ".join([f"*{prev}"] * 9) + "]")
    data = "\n".join(levels).encode()
    started = time.monotonic()
    with pytest.raises(MemoriesFileError, match="aliases are not supported"):
        parse_memories_file(data)
    assert time.monotonic() - started < 1


# --- entries are passed on raw ----------------------------------------------------------


def test_entries_are_not_validated_by_the_reader() -> None:
    entries = parse_memories_file(
        doc(
            [
                entry(text="key sk-abcdefgh12345678"),
                entry(text="two\nlines"),
                entry(text=""),
                entry(keywords=[1, "Bad Word"]),
            ]
        )
    )
    assert [e.text for e in entries] == [
        "key sk-abcdefgh12345678",
        "two\nlines",
        "",
        "Likes tea",
    ]
    assert entries[3].keywords == [1, "Bad Word"]


def test_timestamps_of_any_yaml_type_or_missing_parse_without_error() -> None:
    data = (
        b"version: 1\nmemories:\n"
        b"  - {text: a, keywords: [x], created: 2026-10-01T08:00:00Z, updated: 5}\n"
        b"  - {text: b, keywords: [x], created: '2026-10-01T08:00:00Z'}\n"
        b"  - {text: c, keywords: [x], updated: '2026-10-01T08:00:00Z'}\n"
        b"  - {text: d, keywords: [x]}\n"
    )
    first, second, third, fourth = parse_memories_file(data)
    assert isinstance(first.created, datetime) and first.updated == 5
    assert (second.created, second.updated) == ("2026-10-01T08:00:00Z", None)
    assert (third.created, third.updated) == (None, "2026-10-01T08:00:00Z")
    assert (fourth.created, fourth.updated) == (None, None)  # missing is valid


def test_a_missing_exported_key_is_fine() -> None:
    assert parse_memories_file(b"version: 1\nmemories: []") == []


# --- a round trip through the store -----------------------------------------------------


async def test_export_then_import_into_an_empty_store_keeps_text_stamps_and_order(
    tmp_path: Path,
) -> None:
    source = MemoryStore(SqliteRelationalStore(tmp_path / "a.db"), lambda: NOW)
    stamps = [
        ("2026-08-01T08:00:00Z", "2026-10-03T08:00:00Z"),
        ("2026-08-02T08:00:00Z", "2026-08-02T08:00:00Z"),
        ("2026-08-03T08:00:00Z", "2026-09-15T08:00:00Z"),
    ]
    await source.save(
        "alice", "Alice one", ["one"], created_at=stamps[0][0], updated_at=stamps[0][1]
    )
    await source.save(
        "bob", "Bob private", ["bob"], created_at=stamps[1][0], updated_at=stamps[1][1]
    )
    await source.save(
        "bob",
        "Bob shared — ünï",
        ["bob"],
        True,
        created_at=stamps[2][0],
        updated_at=stamps[2][1],
    )
    exported = [
        m for m in await source.list("alice") if m.user_id == "alice" or m.shared
    ]
    data = export_memories(exported, source.current_timestamp())

    target = MemoryStore(SqliteRelationalStore(tmp_path / "b.db"), lambda: NOW)
    for item in parse_memories_file(data):
        assert isinstance(item.created, str) and isinstance(item.updated, str)
        await target.save(
            "carol",
            item.text,  # type: ignore[arg-type]
            item.keywords,  # type: ignore[arg-type]
            created_at=check_timestamp(item.created, NOW_TEXT),
            updated_at=check_timestamp(item.updated, NOW_TEXT),
        )
    before = await source.list("alice")
    after = await target.list("carol")
    key = lambda m: (m.text, m.keywords, m.created_at, m.updated_at)
    assert [key(m) for m in after] == [key(m) for m in before]
    assert {m.id for m in after} == {1, 2}  # new ids from an empty store
    assert all(m.user_id == "carol" and not m.shared for m in after)
