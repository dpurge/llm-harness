"""The `/memory` command: parser, list/show/delete, isolation, escaping, plain CLI.

Offline: a throwaway SQLite file under `tmp_path`, a fake clock, a scripted provider.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import llm_harness.cli as cli_module
from llm_harness.builtin_tools import build_tool_registry
from llm_harness.config import (
    AGENTS_FILE,
    CONFIG_FILE,
    MODELS_FILE,
    HarnessConfig,
    StorageConfig,
    VectorConfig,
)
from llm_harness.memory import Memory, MemoryStore
from llm_harness.memory_command import (
    MEMORY_OFF,
    USAGE,
    MemoryCommand,
    handle_memory_text,
    parse_memory_command,
    run_memory_command,
)
from llm_harness.storage import Storage, open_storage
from tests.test_cli import (
    AGENTS_YAML,
    CONFIG_TOML,
    MODELS_YAML,
    reply_events,
    run_cli,
    scripted,
)

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
        ("/memory forget 1", USAGE_COMMAND),
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

    async def list(self, user_id: str, limit: int = 50, offset: int = 0):
        return [self._memory]

    async def get(self, user_id: str, id: int):
        return self._memory

    async def forget(self, user_id: str, id: int) -> bool:
        return True


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


def memory_config(config_dir: Path, *, memory: bool = True) -> None:
    flag = "true" if memory else "false"
    (config_dir / CONFIG_FILE).write_text(
        f'user_id = "ann"\n{CONFIG_TOML}\n[tools]\nmemory = {flag}\n'
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
