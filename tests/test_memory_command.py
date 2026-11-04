"""The `/memory` command: parser, list/show/delete, isolation, escaping, plain CLI.

Offline: a throwaway SQLite file under `tmp_path`, a fake clock, a scripted provider.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import llm_harness.cli as cli_module
import llm_harness.memory_command as memory_command_module
from llm_harness.builtin_tools import build_tool_registry
from llm_harness.commands import COMMANDS, PLAIN, help_detail
from llm_harness.config import (
    AGENTS_FILE,
    CONFIG_FILE,
    MODELS_FILE,
    HarnessConfig,
    StorageConfig,
    VectorConfig,
)
from llm_harness.memory import MAX_SEARCH_RESULTS, Memory, MemoryStore
from llm_harness.memory_command import (
    MEMORY_OFF,
    NOT_ADMIN_FORGET,
    USAGE,
    VECTORS_OFF,
    MemoryCommand,
    handle_memory_text,
    parse_memory_command,
    run_memory_command,
)
from llm_harness.memory_recall import format_recall_block
from llm_harness.memory_tools import recall_store
from llm_harness.storage import Storage, open_storage
from llm_harness.storage.relational import SqliteRelationalStore
from llm_harness.storage.vectors import VectorStoreError
from llm_harness.types import ChatRequest, Done, StreamEvent, TextDelta
from tests.fake_steps import as_step_reply
from tests.test_cli import (
    AGENTS_YAML,
    CONFIG_TOML,
    MODELS_YAML,
    reply_events,
    run_cli,
    scripted,
)
from tests.test_memory_index_store import Rig

LIST = MemoryCommand("list")
USAGE_COMMAND = MemoryCommand("usage")
START = datetime(2026, 11, 2, 9, 30, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/memory", LIST),
        ("  /memory  ", LIST),
        ("/memory list", LIST),
        ("/memory list 3", MemoryCommand("list", 3)),
        ("/memory   list   2  ", MemoryCommand("list", 2)),
        ("/memory show 7", MemoryCommand("show", 7)),
        ("/memory\tdelete\t12", MemoryCommand("delete", 12)),
        ("/memory list 0", USAGE_COMMAND),
        ("/memory list -1", USAGE_COMMAND),
        ("/memory list x", USAGE_COMMAND),
        ("/memory list 1 2", USAGE_COMMAND),
        ("/memory show", USAGE_COMMAND),
        ("/memory show 1 2", USAGE_COMMAND),
        ("/memory show 0", USAGE_COMMAND),
        ("/memory show -3", USAGE_COMMAND),
        ("/memory show abc", USAGE_COMMAND),
        ("/memory show 1.5", USAGE_COMMAND),
        ("/memory show ²", USAGE_COMMAND),
        ("/memory delete", USAGE_COMMAND),
        ("/memory delete 01", USAGE_COMMAND),
        ("/memory search", USAGE_COMMAND),
        ("/memory search   ", USAGE_COMMAND),
        ("/memory search tea", MemoryCommand("search", query="tea")),
        (
            "/memory  search \t peanut   butter ",
            MemoryCommand("search", query="peanut butter"),
        ),
        ("/memory reindex", MemoryCommand("reindex")),
        ("/memory reindex 1", USAGE_COMMAND),
        ("/memory reindex --all", MemoryCommand("reindex-all")),
        ("/memory reindex --all x", USAGE_COMMAND),
        ("/memory LIST", USAGE_COMMAND),
        ("/memoryx", None),
        ("/Memory", None),
        ("/MEMORY list", None),
        ("what does /memory do?", None),
        ("please /memory list", None),
        ("memory", None),
        ("", None),
        ("   ", None),
    ],
)
def test_parse_memory_command(text: str, expected: MemoryCommand | None) -> None:
    assert parse_memory_command(text) == expected


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path,
        relational_path=tmp_path / "memory.db",
    )


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


@pytest.fixture
def store(storage: Storage) -> MemoryStore:
    return MemoryStore(storage.relational, Clock())


async def run(store: MemoryStore, text: str, user_id: str = "ann", **kwargs) -> str:
    command = parse_memory_command(text)
    assert command is not None
    return await run_memory_command(store, user_id, command, **kwargs)


async def test_an_empty_list_says_so(store: MemoryStore) -> None:
    assert await run(store, "/memory") == "(no memories)"


async def test_list_is_newest_updated_first_with_one_line_per_entry(
    store: MemoryStore,
) -> None:
    await store.save("ann", "likes tea", ["drink"])
    await store.save("ann", "x" * 120, ["long", "text"])
    answer = await run(store, "/memory")
    first, second, footer = answer.splitlines()
    assert first == f"#2 [long, text] (updated 2026-11-02T09:32:15Z) {'x' * 77}..."
    assert second.startswith("#1 [drink] (updated 2026-11-02T09:31:15Z) likes tea")
    assert footer == "page 1 of 1"


async def test_list_pages_and_hints_the_next_page(store: MemoryStore) -> None:
    for number in range(5):
        await store.save("ann", f"note {number}", ["n"])
    page1 = (await run(store, "/memory list", page_size=2)).splitlines()
    assert [line.split()[0] for line in page1[:2]] == ["#5", "#4"]
    assert page1[2:] == ["page 1 of 3", "next page: /memory list 2"]
    page3 = (await run(store, "/memory list 3", page_size=2)).splitlines()
    assert page3[0].startswith("#1 ") and page3[1:] == ["page 3 of 3"]
    assert await run(store, "/memory list 4", page_size=2) == (
        "no page 4: there are 3 page(s)"
    )


async def test_show_prints_every_field(store: MemoryStore) -> None:
    await store.save("ann", "likes green tea", ["drink", "tea"])
    assert (await run(store, "/memory show 1")).splitlines() == [
        "#1",
        "keywords: drink, tea",
        "created: 2026-11-02T09:31:15Z",
        "updated: 2026-11-02T09:31:15Z",
        "likes green tea",
    ]


async def test_delete_prints_what_it_deleted_then_the_entry_is_gone(
    store: MemoryStore,
) -> None:
    await store.save("ann", "likes tea", ["drink"])
    assert await run(store, "/memory delete 1") == "deleted memory #1: likes tea"
    assert await run(store, "/memory show 1") == "no memory #1"
    assert await run(store, "/memory delete 1") == "no memory #1"
    assert await run(store, "/memory") == "(no memories)"


async def test_the_deleted_text_is_clipped_to_200_characters(
    store: MemoryStore,
) -> None:
    await store.save("ann", "y" * 300, ["long"])
    answer = await run(store, "/memory delete 1")
    assert answer == f"deleted memory #1: {'y' * 197}..."


async def test_users_never_see_or_delete_each_others_memories(
    store: MemoryStore,
) -> None:
    await store.save("ann", "ann secret plan", ["plan"])
    await store.save("bob", "bob note", ["note"])
    assert "ann secret plan" not in await run(store, "/memory", "bob")
    assert await run(store, "/memory show 1", "bob") == "no memory #1"
    assert await run(store, "/memory delete 1", "bob") == "no memory #1"
    assert "ann secret plan" in await run(store, "/memory show 1", "ann")


class StubStore:
    """Holds one entry that `MemoryStore` would refuse to save."""

    def __init__(self, memory: Memory) -> None:
        self._memory = memory
        self.searches: list[tuple[str, str, int]] = []

    async def list(self, user_id: str, limit: int = 50, offset: int = 0):
        return [self._memory]

    async def get(self, user_id: str, id: int):
        return self._memory

    async def forget(self, user_id: str, id: int) -> bool:
        return True

    async def search(self, user_id: str, query: str, k: int = 5):
        self.searches.append((user_id, query, k))
        return [self._memory]


BIDI = chr(0x202E)
HOSTILE = f"a\x1b]0;pwn\x07b\nc\r\x9bd{BIDI}e"


def hostile_store() -> StubStore:
    return StubStore(Memory(1, "ann", HOSTILE, ("k\x1b",), "t\x1b", f"u{BIDI}"))


@pytest.mark.parametrize("text", ["/memory", "/memory show 1", "/memory delete 1"])
async def test_control_characters_are_escaped_not_passed_on(text: str) -> None:
    answer = await run(hostile_store(), text)  # type: ignore[arg-type]
    assert "\x1b" not in answer and "\x07" not in answer and "\r" not in answer
    assert "\x9b" not in answer and BIDI not in answer
    assert "\\u001b" in answer


async def test_a_list_line_never_contains_a_newline() -> None:
    answer = await run(hostile_store(), "/memory")  # type: ignore[arg-type]
    assert len(answer.splitlines()) == 2
    assert "a\\u001b]0;pwn\\u0007b\\u000ac\\u000d\\u009bd\\u202ee" in answer


async def test_show_keeps_newlines_of_the_text_but_escapes_the_rest() -> None:
    answer = await run(hostile_store(), "/memory show 1")  # type: ignore[arg-type]
    assert answer.splitlines()[-2:] == [
        "a\\u001b]0;pwn\\u0007b",
        "c\\u000d\\u009bd\\u202ee",
    ]


# /memory search


async def test_search_answers_list_lines_best_first(store: MemoryStore) -> None:
    await store.save("ann", "likes tea", ["drink"])
    await store.save("ann", "tea tea tea tea tea tea", ["tea"])
    await store.save("ann", "unrelated", ["x"])
    answer = await run(store, "/memory search tea")
    lines = answer.splitlines()
    assert len(lines) == 2
    by_id = {m.id: m for m in await store.list("ann")}
    expected = [m.id for m in await store.search("ann", "tea", MAX_SEARCH_RESULTS)]
    assert [int(line.split()[0][1:]) for line in lines] == expected
    first = by_id[expected[0]]
    assert lines[0] == (
        f"#{first.id} [{', '.join(first.keywords)}] "
        f"(updated {first.updated_at}) {first.text}"
    )


async def test_search_with_no_hit_echoes_the_escaped_clipped_query(
    store: MemoryStore,
) -> None:
    await store.save("ann", "likes tea", ["drink"])
    assert await run(store, "/memory search coffee") == 'no memories match "coffee"'
    long_query = "zq" * 100
    answer = await run(store, f"/memory search {long_query}")
    assert answer == f'no memories match "{long_query[:77]}..."'
    hostile = await run(store, f"/memory search a{chr(0x1B)}b{BIDI}")
    assert hostile == 'no memories match "a\\u001bb\\u202e"'


async def test_search_asks_the_store_for_at_most_max_search_results() -> None:
    stub = hostile_store()
    await run(stub, "/memory search peanut  butter", "ann")  # type: ignore[arg-type]
    assert stub.searches == [("ann", "peanut butter", MAX_SEARCH_RESULTS)]


async def test_search_returns_at_most_max_search_results(store: MemoryStore) -> None:
    for number in range(MAX_SEARCH_RESULTS + 3):
        await store.save("ann", f"tea note {number}", ["tea"])
    answer = await run(store, "/memory search tea")
    assert len(answer.splitlines()) == MAX_SEARCH_RESULTS


async def test_search_never_shows_another_users_memories(store: MemoryStore) -> None:
    await store.save("ann", "ann secret tea plan", ["plan"])
    await store.save("bob", "bob tea note", ["note"])
    bob = await run(store, "/memory search tea", "bob")
    assert "bob tea note" in bob and "ann secret" not in bob
    assert await run(store, "/memory search secret", "bob") == (
        'no memories match "secret"'
    )
    assert "ann secret tea plan" in await run(store, "/memory search secret", "ann")


async def test_search_escapes_hostile_query_and_hostile_stored_text() -> None:
    answer = await run(hostile_store(), f"/memory search x{chr(0x1B)}{BIDI}")  # type: ignore[arg-type]
    assert len(answer.splitlines()) == 1
    assert "\x1b" not in answer and "\x07" not in answer and "\r" not in answer
    assert "\x9b" not in answer and BIDI not in answer
    assert "a\\u001b]0;pwn\\u0007b\\u000ac\\u000d\\u009bd\\u202ee" in answer


async def test_search_makes_no_write(store: MemoryStore) -> None:
    await store.save("ann", "likes tea", ["drink"])
    before = await store.list("ann")
    await run(store, "/memory search tea")
    await run(store, "/memory search nothing")
    assert await store.list("ann") == before


def test_usage_mentions_search() -> None:
    assert "/memory search WORDS" in USAGE.splitlines()[0]
    assert any(line.startswith("  search ") for line in USAGE.splitlines())


# handle_memory_text: the one entry point the three front ends call


def registry(memory: bool, user_id: str | None):
    return build_tool_registry([], memory=memory, user_id=user_id)


async def test_a_message_that_is_not_a_command_is_left_alone(
    storage: Storage,
) -> None:
    tools = registry(True, "ann")
    assert await handle_memory_text("hello /memory", tools, storage, "ann") is None


async def test_a_usage_error_prints_the_usage_text(storage: Storage) -> None:
    tools = registry(True, "ann")
    assert await handle_memory_text("/memory nope", tools, storage, "ann") == USAGE


@pytest.mark.parametrize(
    ("memory", "user_id"), [(False, "ann"), (True, None), (False, None)]
)
async def test_memory_off_answers_one_line(
    storage: Storage, memory: bool, user_id: str | None
) -> None:
    tools = registry(memory, user_id)
    answer = await handle_memory_text("/memory", tools, storage, user_id)
    assert answer == MEMORY_OFF
    assert "\n" not in MEMORY_OFF


async def test_without_tools_or_storage_memory_is_off() -> None:
    assert await handle_memory_text("/memory", None, None, "ann") == MEMORY_OFF


async def test_handle_lists_what_the_memory_tools_saved(storage: Storage) -> None:
    tools = registry(True, "ann")
    save = tools.get("memory_save")
    assert save is not None
    from llm_harness.tools import ToolContext

    ctx = ToolContext(
        storage=storage,
        user_id="ann",
        agent_id="orchestrator",
        tags=frozenset({"orchestrator"}),
    )
    await save.run(save.input_model(text="likes tea", keywords=["drink"]), ctx)
    answer = await handle_memory_text("/memory", tools, storage, "ann")
    assert answer is not None and "likes tea" in answer


# Shared notes


async def shared_note(store: MemoryStore, text: str = "team rule: tea is free") -> int:
    note = await store.save("ann", text, ["rule"])
    assert await store.set_shared("ann", note.id, True) is not None
    return note.id


async def test_a_shared_note_is_marked_in_list_show_and_search_for_another_user(
    store: MemoryStore,
) -> None:
    note_id = await shared_note(store)
    private = await store.save("bob", "bob drinks tea", ["drink"])
    listing = (await run(store, "/memory", "bob")).splitlines()
    assert listing[0].startswith(f"#{private.id} [drink] ")  # never marked
    assert listing[1].startswith(f"#{note_id} [shared] [rule] (updated ")
    assert listing[1].endswith(") team rule: tea is free")
    assert (await run(store, f"/memory show {note_id}", "bob")).splitlines()[:2] == [
        f"#{note_id} [shared]",
        "keywords: rule",
    ]
    found = (await run(store, "/memory search tea", "bob")).splitlines()
    assert {line.split()[0] for line in found} == {f"#{private.id}", f"#{note_id}"}
    assert sum("[shared]" in line for line in found) == 1


async def test_a_private_note_never_shows_the_marker(store: MemoryStore) -> None:
    note = await store.save("ann", "likes tea", ["drink"])
    for text in ("/memory", f"/memory show {note.id}", "/memory search tea"):
        assert "[shared]" not in await run(store, text)


async def test_deleting_a_shared_note_is_refused_with_the_reason(
    store: MemoryStore,
) -> None:
    note_id = await shared_note(store)
    assert await run(store, f"/memory delete {note_id}", "ann") == (
        f"memory #{note_id} is shared; unpublish it first"
    )
    assert await run(store, f"/memory delete {note_id}", "bob") == (
        f"memory #{note_id} is shared and read-only"
    )
    assert "team rule" in await run(store, f"/memory show {note_id}", "bob")  # kept
    await store.set_shared("ann", note_id, False)
    assert await run(store, f"/memory delete {note_id}", "bob") == (
        f"no memory #{note_id}"
    )
    assert (await run(store, f"/memory delete {note_id}", "ann")).startswith("deleted ")


async def test_a_private_note_of_another_user_still_answers_no_memory(
    store: MemoryStore,
) -> None:
    note = await store.save("ann", "ann secret", ["plan"])
    assert (
        await run(store, f"/memory delete {note.id}", "bob") == f"no memory #{note.id}"
    )


async def test_marker_text_inside_a_private_note_stays_text_after_the_keywords() -> (
    None
):
    stored = Memory(1, "ann", "[shared] fake", ("k",), "t", "u")  # a private note
    line = await run(StubStore(stored), "/memory")  # type: ignore[arg-type]
    assert (
        line.splitlines()[0] == "#1 [k] (updated u) [shared] fake"
    )  # text, not marker


async def test_reindex_and_reindex_all_of_another_user_cover_the_shared_note(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path / "data" / "memory.db")
    plain = MemoryStore(SqliteRelationalStore(rig.db), lambda: START)
    note = await plain.save("ann", "likes tea", ["drink"])
    await plain.save("ann", "ann private", ["plan"])
    await plain.set_shared("ann", note.id, True)
    assert await run(rig.store, "/memory reindex", "bob") == "reindexed 1 of 1 memories"
    assert await run(rig.store, "/memory reindex", "cat") == "reindexed 0 of 0 memories"
    assert await run(rig.store, "/memory reindex --all", "cat") == (
        "reindexed 1 of 1 memories"
    )
    assert set(rig.points()) == {str(note.id)}


# /memory publish and /memory unpublish

ADMINS = ["ann"]
NOT_ADMIN = "only memory admins may publish or unpublish shared memories"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/memory publish 3", MemoryCommand("publish", 3)),
        ("/memory  unpublish\t12", MemoryCommand("unpublish", 12)),
        ("/memory publish", USAGE_COMMAND),
        ("/memory unpublish", USAGE_COMMAND),
        ("/memory publish 0", USAGE_COMMAND),
        ("/memory publish 01", USAGE_COMMAND),
        ("/memory publish x", USAGE_COMMAND),
        ("/memory publish 1 2", USAGE_COMMAND),
        ("/memory unpublish -1", USAGE_COMMAND),
        ("/memory publish ²", USAGE_COMMAND),
    ],
)
def test_parse_publish_and_unpublish(text: str, expected: MemoryCommand) -> None:
    assert parse_memory_command(text) == expected


@pytest.mark.parametrize("verb", ["publish", "unpublish"])
@pytest.mark.parametrize("admins", [[], ["bob"]])
async def test_a_user_outside_memory_admins_can_neither_publish_nor_unpublish(
    store: MemoryStore, verb: str, admins: list[str]
) -> None:
    note = await store.save("ann", "likes tea", ["drink"])
    if verb == "unpublish":
        await store.set_shared("ann", note.id, True)
    answer = await run(store, f"/memory {verb} {note.id}", "ann", memory_admins=admins)
    assert answer == NOT_ADMIN
    after = await store.get("ann", note.id)
    assert after is not None and after.shared is (verb == "unpublish")  # unchanged


async def test_the_default_is_that_nobody_can_publish(store: MemoryStore) -> None:
    note = await store.save("ann", "likes tea", ["drink"])
    assert await run(store, f"/memory publish {note.id}") == NOT_ADMIN


async def test_the_admin_check_comes_before_any_note_lookup(
    store: MemoryStore,
) -> None:
    # An unknown id and another user's id answer the same for a non-admin: no lookup.
    other = await store.save("bob", "bob secret", ["x"])
    assert await run(store, "/memory publish 99", "ann") == NOT_ADMIN
    assert await run(store, f"/memory publish {other.id}", "ann") == NOT_ADMIN


async def test_publish_and_unpublish_answer_and_flip_the_note(
    store: MemoryStore,
) -> None:
    note = await store.save("ann", "likes tea", ["drink"])
    assert await run(store, f"/memory publish {note.id}", memory_admins=ADMINS) == (
        f"published memory #{note.id}"
    )
    assert await run(store, f"/memory publish {note.id}", memory_admins=ADMINS) == (
        f"memory #{note.id} is already shared"
    )
    assert await run(store, f"/memory unpublish {note.id}", memory_admins=ADMINS) == (
        f"unpublished memory #{note.id}"
    )
    assert await run(store, f"/memory unpublish {note.id}", memory_admins=ADMINS) == (
        f"memory #{note.id} is not shared"
    )


async def test_publish_of_another_users_or_an_unknown_note_says_no_memory(
    store: MemoryStore,
) -> None:
    other = await store.save("bob", "bob secret", ["x"])
    shared = await store.save("bob", "bob rule", ["x"])
    await store.set_shared("bob", shared.id, True)
    for verb in ("publish", "unpublish"):
        for note_id in (other.id, shared.id, 99):
            answer = await run(
                store, f"/memory {verb} {note_id}", "ann", memory_admins=ADMINS
            )
            assert answer == f"no memory #{note_id}"
    still = await store.get("bob", shared.id)
    assert still is not None and still.shared is True
    private = await store.get("bob", other.id)
    assert private is not None and private.shared is False


async def test_bad_publish_arguments_print_the_usage() -> None:
    assert await handle_memory_text("/memory publish", None, None, "ann") == USAGE
    assert await handle_memory_text("/memory unpublish x", None, None, "ann") == USAGE


async def test_publish_end_to_end_other_users_see_the_note_until_unpublish(
    store: MemoryStore,
) -> None:
    note = await store.save("ann", "team rule: tea is free", ["rule"])
    await run(store, f"/memory publish {note.id}", "ann", memory_admins=ADMINS)
    listing = await run(store, "/memory", "bob")
    assert f"#{note.id} [shared] [rule]" in listing
    assert "team rule" in await run(store, f"/memory show {note.id}", "bob")
    assert f"#{note.id} [shared]" in await run(store, "/memory search tea", "bob")
    block = format_recall_block(await store.search("bob", "tea", 5))
    assert f"#{note.id} [shared] [rule]" in block
    await run(store, f"/memory unpublish {note.id}", "ann", memory_admins=ADMINS)
    assert await run(store, "/memory", "bob") == "(no memories)"
    assert await run(store, f"/memory show {note.id}", "bob") == f"no memory #{note.id}"
    assert await run(store, "/memory search tea", "bob") == ('no memories match "tea"')
    assert await store.search("bob", "tea", 5) == []


async def test_publish_through_handle_memory_text_needs_the_admin_list(
    storage: Storage,
) -> None:
    tools = registry(True, "ann")
    store = recall_store(tools, storage)
    assert store is not None
    note = await store.save("ann", "likes tea", ["drink"])
    text = f"/memory publish {note.id}"
    assert await handle_memory_text(text, tools, storage, "ann") == NOT_ADMIN
    assert await handle_memory_text(
        text, tools, storage, "ann", memory_admins=["ann"]
    ) == (f"published memory #{note.id}")


async def test_publish_on_an_indexed_store_follows_the_vector_payload(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path / "data" / "memory.db")
    note = await rig.store.save("ann", "likes tea", ["drink"])
    assert await run(
        rig.store, f"/memory publish {note.id}", "ann", memory_admins=ADMINS
    ) == (f"published memory #{note.id}")
    assert rig.points()[str(note.id)].payload == {"user_id": "ann", "shared": 1}
    await run(rig.store, f"/memory unpublish {note.id}", "ann", memory_admins=ADMINS)
    assert rig.points()[str(note.id)].payload == {"user_id": "ann"}


# /memory forget (memory admins only)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/memory forget 3", MemoryCommand("forget", 3)),
        ("/memory  forget\t12", MemoryCommand("forget", 12)),
        ("/memory forget", USAGE_COMMAND),
        ("/memory forget 0", USAGE_COMMAND),
        ("/memory forget 01", USAGE_COMMAND),
        ("/memory forget x", USAGE_COMMAND),
        ("/memory forget 1 2", USAGE_COMMAND),
        ("/memory forget -1", USAGE_COMMAND),
        ("/memory forget ²", USAGE_COMMAND),
    ],
)
def test_parse_forget(text: str, expected: MemoryCommand) -> None:
    assert parse_memory_command(text) == expected


async def published_note(store: MemoryStore, creator: str = "bob", text: str = "rule"):
    note = await store.save(creator, text, ["rule"])
    published = await store.set_shared(creator, note.id, True)
    assert published is not None
    return published


@pytest.mark.parametrize("admins", [None, [], ["bob"]])
async def test_a_user_outside_memory_admins_cannot_forget(
    store: MemoryStore, admins: list[str] | None
) -> None:
    note = await published_note(store, "bob", "team rule")
    kwargs = {} if admins is None else {"memory_admins": admins}
    answer = await run(store, f"/memory forget {note.id}", "ann", **kwargs)
    assert answer == NOT_ADMIN_FORGET
    assert await store.get("bob", note.id) == note


async def test_the_forget_admin_check_comes_before_any_note_lookup(
    store: MemoryStore,
) -> None:
    # An unknown id and another user's private id answer the same for a non-admin.
    private = await store.save("bob", "bob secret", ["x"])
    assert await run(store, "/memory forget 99", "ann") == NOT_ADMIN_FORGET
    assert await run(store, f"/memory forget {private.id}", "ann") == NOT_ADMIN_FORGET
    assert await store.get("bob", private.id) == private


async def test_an_admin_forgets_a_shared_note_of_another_user_for_everyone(
    store: MemoryStore,
) -> None:
    note = await published_note(store, "bob", "team rule: tea is free")
    assert await run(
        store, f"/memory forget {note.id}", "ann", memory_admins=ADMINS
    ) == (f"forgot shared memory #{note.id}: team rule: tea is free")
    for user in ("ann", "bob", "cat"):
        assert await run(store, "/memory", user) == "(no memories)"
        assert await run(store, f"/memory show {note.id}", user) == (
            f"no memory #{note.id}"
        )
        assert await run(store, "/memory search tea", user) == (
            'no memories match "tea"'
        )
    assert await store.forget_shared(note.id) is None  # the row is gone


async def test_a_creator_who_is_admin_forgets_their_own_published_note(
    store: MemoryStore,
) -> None:
    note = await published_note(store, "ann", "my rule")
    assert await run(
        store, f"/memory forget {note.id}", "ann", memory_admins=ADMINS
    ) == (f"forgot shared memory #{note.id}: my rule")
    assert await store.get("ann", note.id) is None


async def test_forget_of_private_and_unknown_notes_deletes_nothing(
    store: MemoryStore,
) -> None:
    theirs = await store.save("bob", "bob secret", ["x"])
    mine = await store.save("ann", "ann secret", ["x"])
    assert await run(
        store, f"/memory forget {theirs.id}", "ann", memory_admins=ADMINS
    ) == (f"no memory #{theirs.id}")
    assert await run(
        store, f"/memory forget {mine.id}", "ann", memory_admins=ADMINS
    ) == (f"memory #{mine.id} is not shared; use /memory delete {mine.id}")
    assert await run(store, "/memory forget 99", "ann", memory_admins=ADMINS) == (
        "no memory #99"
    )
    assert await store.get("bob", theirs.id) == theirs
    assert await store.get("ann", mine.id) == mine


async def test_the_forget_echo_is_escaped_and_clipped(store: MemoryStore) -> None:
    bidi = await published_note(store, "bob", "a\u202eb")
    answer = await run(store, f"/memory forget {bidi.id}", "ann", memory_admins=ADMINS)
    assert answer == f"forgot shared memory #{bidi.id}: a\\u202eb"
    long = await published_note(store, "bob", "x" * 300)
    answer = await run(store, f"/memory forget {long.id}", "ann", memory_admins=ADMINS)
    assert answer == f"forgot shared memory #{long.id}: {'x' * 197}..."


async def test_a_racing_forget_that_finds_no_row_says_no_memory(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = await published_note(store)

    async def gone(memory_id: int) -> None:
        return None

    monkeypatch.setattr(store, "forget_shared", gone)
    assert await run(
        store, f"/memory forget {note.id}", "ann", memory_admins=ADMINS
    ) == (f"no memory #{note.id}")


async def test_delete_of_a_shared_note_is_unchanged_even_for_an_admin(
    store: MemoryStore,
) -> None:
    note = await published_note(store, "ann", "my rule")
    assert await run(
        store, f"/memory delete {note.id}", "ann", memory_admins=ADMINS
    ) == (f"memory #{note.id} is shared; unpublish it first")
    assert await run(
        store, f"/memory delete {note.id}", "bob", memory_admins=["bob"]
    ) == (f"memory #{note.id} is shared and read-only")
    assert await store.get("ann", note.id) == note


async def test_forget_through_handle_memory_text_needs_the_admin_list(
    storage: Storage,
) -> None:
    tools = registry(True, "ann")
    store = recall_store(tools, storage)
    assert store is not None
    note = await published_note(store, "bob", "team rule")
    text = f"/memory forget {note.id}"
    assert await handle_memory_text(text, tools, storage, "ann") == NOT_ADMIN_FORGET
    assert await handle_memory_text(
        text, tools, storage, "ann", memory_admins=["ann"]
    ) == (f"forgot shared memory #{note.id}: team rule")


async def test_forget_on_an_indexed_store_removes_the_point_and_reports_a_failure(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path / "data" / "memory.db")
    note = await published_note(rig.store, "bob", "likes tea")
    assert str(note.id) in rig.points()
    answer = await run(
        rig.store, f"/memory forget {note.id}", "ann", memory_admins=ADMINS
    )
    assert answer == f"forgot shared memory #{note.id}: likes tea"
    assert rig.points() == {} and rig.markers() == []


async def test_a_failed_point_removal_appends_the_one_time_notice(
    tmp_path: Path, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path / "data" / "memory.db")
    note = await published_note(rig.store, "bob", "likes jam")
    rig.vectors.fail_on["delete"] = VectorStoreError("down")
    monkeypatch.setattr(memory_command_module, "recall_store", lambda t, s: rig.store)
    answer = await handle_memory_text(
        f"/memory forget {note.id}",
        registry(True, "ann"),
        storage,
        "ann",
        memory_admins=ADMINS,
    )
    assert answer is not None
    first, notice = answer.split("\n", 1)
    assert first == f"forgot shared memory #{note.id}: likes jam"
    assert "vector search is off" in notice
    assert await rig.store.get("bob", note.id) is None  # the row is gone anyway


def test_usage_and_the_commands_table_list_publish_and_unpublish() -> None:
    first = USAGE.splitlines()[0]
    assert "/memory publish ID" in first and "/memory unpublish ID" in first
    lines = USAGE.splitlines()
    assert any(line.startswith("  publish ") for line in lines)
    assert any(line.startswith("  unpublish ") for line in lines)
    (entry,) = (c for c in COMMANDS if c.name == "/memory")
    # `forget` joined the arguments string with memory-shared-admin, `add` with memory-add-command, `export` and `import` with memory-yaml-transfer
    assert entry.arguments == (
        "[list|search|show|delete|publish|unpublish|forget|reindex|add|export|import]"
    )
    assert help_detail(PLAIN, "memory") == USAGE


# Plain CLI


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cfg"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    (directory / CONFIG_FILE).write_text(CONFIG_TOML)
    (directory / CONFIG_FILE).chmod(0o600)
    (directory / MODELS_FILE).write_text(MODELS_YAML)
    (directory / AGENTS_FILE).write_text(AGENTS_YAML)
    return directory


def memory_config(
    config_dir: Path, *, memory: bool = True, admins: str | None = None
) -> None:
    flag = "true" if memory else "false"
    admin_line = f"memory_admins = {admins}\n" if admins is not None else ""
    (config_dir / CONFIG_FILE).write_text(
        f'user_id = "ann"\n{CONFIG_TOML}\n[tools]\nmemory = {flag}\n{admin_line}'
    )


async def seed(config_dir: Path, data_dir: Path, text: str) -> None:
    config = HarnessConfig.model_validate(
        {"providers": {}, "models": [], "user_id": "ann"}
    )
    path = cli_module._relational_path(config, data_dir)
    storage = open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        data_dir,
        relational_path=path,
    )
    try:
        await MemoryStore(storage.relational).save("ann", text, ["tea"])
    finally:
        await storage.close()


def run_plain(config_dir: Path, data_dir: Path, stdin: str) -> tuple[object, Callable]:
    provider, factory = scripted(*reply_events())
    result = run_cli(
        ["--plain"], config_dir, stdin=stdin, factory=factory, data_dir=data_dir
    )
    return result, provider


def test_plain_lists_shows_and_deletes_without_calling_the_model(
    config_dir: Path, tmp_path: Path
) -> None:
    memory_config(config_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    asyncio.run(seed(config_dir, data_dir, "likes tea"))
    stdin = "/memory\n/memory show 1\n/memory delete 1\n/memory\n"
    result, provider = run_plain(config_dir, data_dir, stdin)
    assert result.code == 0  # type: ignore[attr-defined]
    out = result.out.splitlines()  # type: ignore[attr-defined]
    assert out[0].startswith("#1 [tea] (updated ")
    assert "likes tea" in result.out  # type: ignore[attr-defined]
    assert "deleted memory #1: likes tea" in out
    assert out[-1] == "(no memories)"
    assert provider.requests == []  # type: ignore[attr-defined]


def test_plain_publish_needs_memory_admins_in_the_config(
    config_dir: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    asyncio.run(seed(config_dir, data_dir, "likes tea"))
    stdin = "/memory publish 1\n/memory\n"
    memory_config(config_dir)  # no admin list: nobody can publish
    result, provider = run_plain(config_dir, data_dir, stdin)
    assert result.out.splitlines()[0] == NOT_ADMIN  # type: ignore[attr-defined]
    assert "[shared]" not in result.out  # type: ignore[attr-defined]
    memory_config(config_dir, admins='["bob"]')  # ann is not listed
    result, _ = run_plain(config_dir, data_dir, stdin)
    assert result.out.splitlines()[0] == NOT_ADMIN  # type: ignore[attr-defined]
    memory_config(config_dir, admins='["ann"]')
    result, provider = run_plain(config_dir, data_dir, stdin + "/memory unpublish 1\n")
    out = result.out.splitlines()  # type: ignore[attr-defined]
    assert out[0] == "published memory #1"
    assert out[1].startswith("#1 [shared] [tea] (updated ")
    assert out[-1] == "unpublished memory #1"
    assert provider.requests == []  # type: ignore[attr-defined]


async def seed_shared(data_dir: Path, text: str) -> None:
    config = HarnessConfig.model_validate(
        {"providers": {}, "models": [], "user_id": "ann"}
    )
    path = cli_module._relational_path(config, data_dir)
    storage = open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        data_dir,
        relational_path=path,
    )
    try:
        store = MemoryStore(storage.relational)
        note = await store.save("bob", text, ["tea"])
        await store.set_shared("bob", note.id, True)
    finally:
        await storage.close()


def test_plain_forget_needs_memory_admins_in_the_config(
    config_dir: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    asyncio.run(seed_shared(data_dir, "team rule"))
    stdin = "/memory forget 1\n/memory\n"
    memory_config(config_dir)  # no admin list: nobody can forget
    result, provider = run_plain(config_dir, data_dir, stdin)
    assert result.out.splitlines()[0] == NOT_ADMIN_FORGET  # type: ignore[attr-defined]
    assert "[shared]" in result.out  # type: ignore[attr-defined]
    memory_config(config_dir, admins='["bob"]')  # ann is not listed
    result, _ = run_plain(config_dir, data_dir, stdin)
    assert result.out.splitlines()[0] == NOT_ADMIN_FORGET  # type: ignore[attr-defined]
    memory_config(config_dir, admins='["ann"]')
    result, provider = run_plain(config_dir, data_dir, stdin)
    out = result.out.splitlines()  # type: ignore[attr-defined]
    assert out[0] == "forgot shared memory #1: team rule"
    assert out[-1] == "(no memories)"
    assert provider.requests == []  # type: ignore[attr-defined]


class AddOrStep:
    """Answers an add call with `add_events` and any other request with a step reply."""

    def __init__(self, add_events: list[StreamEvent]) -> None:
        self._add_events = add_events
        self._step_events = as_step_reply([TextDelta(text="Hello"), Done()])
        self.requests: list[ChatRequest] = []

    @property
    def adds(self) -> list[ChatRequest]:
        return [r for r in self.requests if "<pasted-text>" in r.messages[1].content]

    async def stream_chat(self, req: ChatRequest):
        self.requests.append(req)
        is_add = "<pasted-text>" in req.messages[1].content
        for event in self._add_events if is_add else self._step_events:
            yield event

    async def aclose(self) -> None:
        return None


ADD_NOTES = [
    TextDelta(
        text=json.dumps(
            {
                "notes": ["Ann likes green tea", "Ann likes jam"],
                "keywords": ["tea", "jam"],
            }
        )
    ),
    Done(),
]


def run_plain_add(config_dir: Path, data_dir: Path, stdin: str):
    provider = AddOrStep(ADD_NOTES)
    result = run_cli(
        ["--plain"],
        config_dir,
        stdin=stdin,
        factory=lambda _config: provider,
        data_dir=data_dir,
    )
    return result, provider


def test_plain_add_confirm_and_cancel(config_dir: Path, tmp_path: Path) -> None:
    memory_config(config_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    note = tmp_path / "my notes.md"
    note.write_text("Ann likes tea\nand jam\n")
    stdin = (
        "/memory add Ann likes tea\n/memory add confirm\n"
        f"/memory add @{note}\n/memory add cancel\n/memory add confirm\n/memory\n"
    )
    result, provider = run_plain_add(config_dir, data_dir, stdin)
    assert result.code == 0  # type: ignore[attr-defined]
    out = result.out.splitlines()  # type: ignore[attr-defined]
    assert out[0] == "1. [tea] Ann likes green tea"
    assert out[1] == "2. [jam] Ann likes jam"
    assert (
        out[2] == "/memory add confirm to save 2 notes, /memory add cancel to discard"
    )
    assert "saved 2 of 2 memories (#1, #2); skipped 0 already known" in out
    assert "discarded 2 proposed memories" in out
    assert "nothing to confirm" in out
    assert out.count("1. [tea] Ann likes green tea") == 2  # the file add previewed too
    assert "Ann likes tea\nand jam" in provider.adds[1].messages[1].content
    assert str(note) not in provider.adds[1].messages[1].content
    assert len(provider.adds) == 2 and len(provider.requests) == 2  # no turn was run
    assert result.err.count("[memory: proposing...]") == 2  # type: ignore[attr-defined]
    listing = [line for line in out if line.startswith("#")]
    assert [line.split(" (updated")[0] for line in listing] == ["#2 [jam]", "#1 [tea]"]


def test_plain_add_shared_needs_memory_admins_in_the_config(
    config_dir: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    stdin = "/memory add --shared Ann likes tea\n/memory add confirm\n"
    memory_config(config_dir, admins='["bob"]')
    result, provider = run_plain_add(config_dir, data_dir, stdin)
    assert result.out.splitlines() == [  # type: ignore[attr-defined]
        "only memory admins may add shared memories",
        "nothing to confirm",
    ]
    assert provider.requests == []
    memory_config(config_dir, admins='["ann"]')
    result, provider = run_plain_add(config_dir, data_dir, stdin)
    out = result.out.splitlines()  # type: ignore[attr-defined]
    assert out[0] == "1. [shared] [tea] Ann likes green tea"
    assert "saved 2 of 2 memories" in result.out  # type: ignore[attr-defined]


def test_plain_a_second_pasted_line_is_its_own_input(
    config_dir: Path, tmp_path: Path
) -> None:
    """Plain mode reads one line at a time: line 2 of a paste is a normal turn."""
    memory_config(config_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    stdin = "/memory add Ann likes tea\nAnn also likes jam\n/memory add confirm\n"
    result, provider = run_plain_add(config_dir, data_dir, stdin)
    out = result.out.splitlines()  # type: ignore[attr-defined]
    assert len(provider.adds) == 1  # the add saw only line 1
    assert "Ann also likes" not in provider.adds[0].messages[1].content
    assert "Hello" in result.out  # type: ignore[attr-defined]  # line 2 went to the model as a turn
    assert out[-1].startswith("saved 2 of 2 memories")  # the preview survived the turn


def test_plain_reindex_answers_without_calling_the_model(
    config_dir: Path, tmp_path: Path
) -> None:
    memory_config(config_dir)
    result, provider = run_plain(config_dir, tmp_path / "data", "/memory reindex\n")
    assert result.out == VECTORS_OFF + "\n"  # type: ignore[attr-defined]
    assert provider.requests == []  # type: ignore[attr-defined]


def test_plain_memory_off_answers_one_line_and_skips_the_model(
    config_dir: Path, tmp_path: Path
) -> None:
    memory_config(config_dir, memory=False)
    result, provider = run_plain(config_dir, tmp_path / "data", "/memory\n")
    assert result.out == MEMORY_OFF + "\n"  # type: ignore[attr-defined]
    assert provider.requests == []  # type: ignore[attr-defined]


def test_plain_text_that_merely_contains_memory_goes_to_the_model(
    config_dir: Path, tmp_path: Path
) -> None:
    memory_config(config_dir)
    result, provider = run_plain(config_dir, tmp_path / "data", "see /memory\n")
    assert result.code == 0  # type: ignore[attr-defined]
    assert provider.requests  # type: ignore[attr-defined]


def test_plain_memory_export_and_import(config_dir: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    asyncio.run(seed(config_dir, data_dir, "likes tea"))
    target = tmp_path / "out.yaml"
    missing = tmp_path / "missing.yaml"
    stdin = (
        f"/memory export {target}\n"
        f"/memory export --shared {target}\n"
        f"/memory import {target}\n"
        f"/memory import --shared {missing}\n"
        f"/memory import {missing}\n"
    )
    memory_config(config_dir)  # no admin list
    result, provider = run_plain(config_dir, data_dir, stdin)
    out = result.out.splitlines()  # type: ignore[attr-defined]
    assert out[0] == f"exported 1 memories to {target}"
    assert out[1] == "only memory admins may export shared memories"
    assert out[2].startswith("imported 0 of 1 memories; skipped 1 already known")
    assert out[3] == "only memory admins may import shared memories"
    assert out[4].startswith("FileNotFoundError")
    assert target.stat().st_mode & 0o777 == 0o600
    assert provider.requests == []  # type: ignore[attr-defined]
