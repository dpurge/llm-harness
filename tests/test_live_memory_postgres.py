"""Live check of agent memory on a real PostgreSQL server (relational backend "postgres").

Deselected by default (`pytest -m live`). Skips, with a printed reason, when
`LLM_HARNESS_LIVE_PGMEM_URL` (a plain `postgres:17`) is unset, the server does not answer
within 3 s, or the database name does not contain `live` or `test`: these tests drop the
fixed tables `memories`, `memory_vectors`, `memory_vector_meta` and the function
`memories_clear_markers`, so they must never meet a database that holds real memories.
The vector-index test with the real `PgVectorStore` needs a second server,
`LLM_HARNESS_LIVE_PGMEM_VECTOR_URL` (the `pgvector/pgvector:pg17` image), and also drops
the pgvector tables. The urls carry no password; the password comes from
`LLM_HARNESS_LIVE_POSTGRES_PASSWORD`. The same plain server serves the `postgres`
parameter of `tests/test_memory_store.py` (the shared memory tests).

Run it:

    docker --context rancher-desktop run -d --rm --name llm-harness-live-pgmem -e POSTGRES_PASSWORD=live-test-password -e POSTGRES_DB=llm_harness_live -p 127.0.0.1:15436:5432 postgres:17
    docker --context rancher-desktop run -d --rm --name llm-harness-live-pgmemvec -e POSTGRES_PASSWORD=live-test-password -e POSTGRES_DB=llm_harness_live -p 127.0.0.1:15437:5432 pgvector/pgvector:pg17
    LLM_HARNESS_LIVE_PGMEM_URL=postgresql://postgres@127.0.0.1:15436/llm_harness_live LLM_HARNESS_LIVE_PGMEM_VECTOR_URL=postgresql://postgres@127.0.0.1:15437/llm_harness_live LLM_HARNESS_LIVE_POSTGRES_PASSWORD=live-test-password timeout 240 uv run pytest -m live tests/test_live_memory_postgres.py tests/test_memory_store.py -v -rs -p no:cacheprovider
    docker --context rancher-desktop rm -f llm-harness-live-pgmem
    docker --context rancher-desktop rm -f llm-harness-live-pgmemvec

Wait until `docker --context rancher-desktop exec llm-harness-live-pgmem pg_isready -h 127.0.0.1 -U postgres`
answers (the image restarts its server once after initialising) before the pytest line. The
pgvector image starts WITHOUT the extension; the test set-up creates it through a raw
administrator connection (`reset_vector_schema`), because the program itself never does.
Every test starts and ends on a database without the memory tables (`reset_memory_schema`),
so the program must create everything. Each test body is bounded to 30 s.

Where the real server differs from SQLite, the test says so in its docstring:

- `cafe` against `café` (accent folding): pinned, a documented limit.
- Nothing else differed on the fixed corpus: the same notes, the same top hit and the same
  full order for all 13 queries (plain `ts_rank`).
"""

import asyncio
import functools
import threading
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

from llm_harness.builtin_tools import build_tool_registry
from llm_harness.config import (
    RelationalConfig,
    StorageConfig,
    VectorConfig,
)
from llm_harness.embeddings import Embedder
from llm_harness.memory import (
    MEMORY_MIGRATIONS_POSTGRES,
    MemoryStorageError,
    MemoryStore,
    MemoryValidationError,
)
from llm_harness.memory_command import (
    MemoryCommand,
    handle_memory_text,
    parse_memory_command,
    run_memory_command,
)
from llm_harness.memory_index import IndexedMemoryStore, collection_name
from llm_harness.memory_reflection import ReflectionAction, apply_reflection
from llm_harness.memory_tools import (
    MemoryForgetInput,
    MemorySaveInput,
    MemorySearchInput,
    MemoryTools,
    MemoryUpdateInput,
)
from llm_harness.orchestrator import _failure_reason
from llm_harness.redact import Secret, redact
from llm_harness.storage import Storage, open_storage
from llm_harness.storage.pgvector import PgVectorStore
from llm_harness.storage.postgres import PostgresRelationalStore
from llm_harness.storage.qdrant_edge import QdrantEdgeVectorStore
from llm_harness.storage.relational import SqliteRelationalStore
from llm_harness.storage.status import storage_status
from llm_harness.storage.vectors import VectorStore
from llm_harness.tools import ToolContext
from tests.fakes import ConceptEmbedder, FakeVectorStore
from tests.live_postgres import (
    memory_target,
    memory_vector_target,
    reset_memory_schema,
    reset_vector_schema,
)
from tests.test_live_postgres import WRONG_PASSWORD, status_config, with_host_and_port
from tests.test_memory_index_store import NOW, model

pytestmark = pytest.mark.live

WHOLE_TEST_SECONDS = 30
STAMP = "2026-11-02T09:30:15Z"
COLLECTION = collection_name("concepts")


def bounded[**P, R](test: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Fail a test that runs longer than 30 s (its worker threads cannot be killed)."""

    @functools.wraps(test)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        async with asyncio.timeout(WHOLE_TEST_SECONDS):
            return await test(*args, **kwargs)

    return wrapper


def clock() -> datetime:
    return NOW


@dataclass(frozen=True)
class Live:
    url: str
    password: str | None

    def relational(self, url: str | None = None, password: str | None = None):
        secret = password if password is not None else self.password
        return PostgresRelationalStore(
            url or self.url, Secret(secret) if secret else None
        )

    def store(self) -> MemoryStore:
        """A NEW store on a NEW connection source: separate instances never share state."""
        return MemoryStore(self.relational(), clock)

    def sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        """A raw administrator connection: what the test checks, the program never uses."""
        with psycopg.connect(self.url, password=self.password, autocommit=True) as raw:
            cursor = raw.execute(statement, params)
            return cursor.fetchall() if cursor.description else []

    def memory_version(self) -> list[tuple]:
        return self.sql(
            "SELECT version FROM schema_versions WHERE namespace = 'memory'"
        )


@pytest.fixture
def live() -> Iterator[Live]:
    url, password = memory_target()
    reset_memory_schema(url, password)
    try:
        yield Live(url, password)
    finally:
        reset_memory_schema(url, password)


@pytest.fixture
def live_vector() -> Iterator[Live]:
    url, password = memory_vector_target()
    reset_vector_schema(url, password)
    reset_memory_schema(url, password)
    try:
        yield Live(url, password)
    finally:
        reset_vector_schema(url, password)
        reset_memory_schema(url, password)


# --- (a) the fixed corpus of the spike, on both engines -------------------------------

CORPUS = [
    ("Partner is allergic to peanuts", ["allergy", "peanuts", "partner"]),
    ("Likes green tea in the morning", ["tea", "green tea", "drinks"]),
    ("Cooks pasta on Sundays for the family", ["cooking", "pasta", "sunday"]),
    ("Runs five kilometers every morning", ["running", "exercise"]),
    ("Prefers window seats on long flights", ["flights", "travel", "seats"]),
    ("Favourite colour is blue", ["colour", "blue"]),
    ("Allergic reaction to penicillin", ["allergy", "penicillin", "medicine"]),
    ("Visited café in Paris last summer", ["cafe", "paris", "travel"]),
    ("Peanut butter sandwich for lunch", ["peanut", "lunch", "food"]),
    ("Morning meetings start at nine", ["meetings", "morning", "work"]),
]
QUERIES = [
    "peanuts",
    "peanut allergy",
    "morning",
    "running",
    "cooking pasta",
    "travel",
    "allergic",
    "cafe",
    "café",
    "tea",
    "flight seat",
    "blue colour",
    "kilometer",
]
# What SQLite (bm25) and PostgreSQL 17.11 (plain ts_rank) both returned in the spike,
# as ids in insertion order (note 1 is the first corpus entry).
EXPECTED_IDS = {
    "peanuts": [9, 1],
    "peanut allergy": [1, 9, 7],
    "morning": [10, 4, 2],
    "running": [4],
    "cooking pasta": [3],
    "travel": [8, 5],
    "allergic": [7, 1],
    "cafe": [8],
    "café": [8],
    "tea": [2],
    "flight seat": [5],
    "blue colour": [6],
    "kilometer": [4],
}
# (query word, word in the saved note): does the note match? (SQLite, PostgreSQL)
WORD_PAIRS = {
    ("run", "running"): (True, True),
    ("cook", "cooking"): (True, True),
    ("cook", "cooks"): (True, True),
    ("peanut", "peanuts"): (True, True),
    ("flight", "flights"): (True, True),
    ("allergy", "allergic"): (False, False),
    ("cafe", "café"): (True, False),
    ("happy", "happiness"): (True, True),
    ("index", "indices"): (False, False),
    ("meet", "meeting"): (True, True),
    ("colour", "color"): (False, False),
    ("travel", "travelling"): (True, True),
}


async def seeded(store: MemoryStore) -> None:
    for text, keywords in CORPUS:
        await store.save("u1", text, keywords)


@bounded
async def test_the_fixed_corpus_gives_the_same_notes_top_hit_and_order_on_both_engines(
    live: Live, tmp_path: Path
) -> None:
    sqlite_store = MemoryStore(SqliteRelationalStore(tmp_path / "memory.db"), clock)
    postgres_store = live.store()
    await seeded(sqlite_store)
    await seeded(postgres_store)
    for query in QUERIES:
        on_sqlite = [m.id for m in await sqlite_store.search("u1", query, 10)]
        on_postgres = [m.id for m in await postgres_store.search("u1", query, 10)]
        assert set(on_postgres) == set(on_sqlite), query
        assert on_postgres[:1] == on_sqlite[:1], query
        assert on_postgres == on_sqlite, query  # the spike showed no order difference
        assert on_postgres == EXPECTED_IDS[query], query


@bounded
async def test_word_pairs_match_alike_on_both_engines_except_the_accent_one(
    live: Live, tmp_path: Path
) -> None:
    """`cafe` against `café` is a DOCUMENTED LIMIT, pinned here: SQLite folds the accent
    (`unicode61`) and matches; PostgreSQL's `english` configuration does not fold it and
    the note is not found. Folding would need the `unaccent` extension, which an
    administrator creates and the program never does (out of scope of this feature).
    """
    sqlite_store = MemoryStore(SqliteRelationalStore(tmp_path / "memory.db"), clock)
    postgres_store = live.store()
    for index, (query_word, note_word) in enumerate(WORD_PAIRS):
        user = f"pair{index}"
        for store in (sqlite_store, postgres_store):
            await store.save(user, f"note about {note_word}", ["misc"])
        found = (
            bool(await sqlite_store.search(user, query_word, 5)),
            bool(await postgres_store.search(user, query_word, 5)),
        )
        assert found == WORD_PAIRS[(query_word, note_word)], (query_word, note_word)
    assert WORD_PAIRS[("cafe", "café")] == (True, False)
    # The other direction of the accent: both fold nothing for an accented query word
    # against a plain note on Postgres, and SQLite folds both ways.
    await postgres_store.save("accent", "Visited the cafe", ["misc"])
    assert await postgres_store.search("accent", "café", 5) == []
    await sqlite_store.save("accent", "Visited the cafe", ["misc"])
    assert len(await sqlite_store.search("accent", "café", 5)) == 1


# --- (c) hostile input -----------------------------------------------------------------

HOSTILE_QUERIES = [
    "it's",
    'say "hi"',
    "a & b",
    "a | b",
    "!a",
    "a:*",
    "(a",
    "a)",
    "((()))",
    "back\\slash",
    "'; drop table memories; --",
    "x" * 5000,
    "word " * 1000,
    "\U0001f600 \U0001f680",
    "the a an of",
    "",
    "   ",
    "a <-> b",
    "-tea",
    "tea OR coffee -milk",
    "'quoted' \"double\" \\ %s %(x)s ?",
    "tea\x00bag",
]
HOSTILE_TEXT = (
    'It\'s a "quoted" & piped | note! with a:* tag, (parens), back\\slash and '
    "'; drop table memories; -- zebra"
)


@bounded
async def test_hostile_queries_never_raise_and_the_tables_survive(live: Live) -> None:
    store = live.store()
    await store.save("u1", "Likes tea in the morning", ["tea"])
    for query in HOSTILE_QUERIES:
        assert isinstance(await store.search("u1", query), list), repr(query)
    assert live.sql("SELECT count(*) FROM memories") == [(1,)]


@bounded
async def test_a_note_full_of_hostile_characters_comes_back_exactly(live: Live) -> None:
    store = live.store()
    saved = await store.save("u1", HOSTILE_TEXT, ["zebra"])
    assert saved.text == HOSTILE_TEXT
    assert await store.get("u1", saved.id) == saved
    assert await store.list("u1") == [saved]
    assert await store.search("u1", "zebra") == [saved]
    assert await store.search("u1", HOSTILE_TEXT) == [saved]  # as the query itself
    updated = await store.update("u1", saved.id, text=HOSTILE_TEXT + " again")
    assert updated is not None and updated.text == HOSTILE_TEXT + " again"
    assert live.sql("SELECT text FROM memories") == [(HOSTILE_TEXT + " again",)]


@bounded
async def test_a_huge_id_from_a_tool_or_command_finds_nothing_and_raises_nothing(
    live: Live,
) -> None:
    store = live.store()
    await store.save("u1", "a fact", ["x"])
    huge = 10**30
    assert await store.get("u1", huge) is None
    assert await store.update("u1", huge, text="x") is None
    assert await store.forget("u1", huge) is False


@bounded
async def test_a_nul_character_never_reaches_the_server_in_text_or_keywords(
    live: Live,
) -> None:
    """Observed end to end: the program's own validation refuses it before any SQL (text:
    control characters, keywords: the lowercase-word pattern), so no driver or server
    text exists. In a QUERY the NUL is not a `\\w` character, so it only splits words."""
    store = live.store()
    with pytest.raises(MemoryValidationError, match="control characters"):
        await store.save("u1", "line\x00two", ["x"])
    with pytest.raises(MemoryValidationError, match="lowercase English"):
        await store.save("u1", "a fact", ["a\x00b"])
    saved = await store.save("u1", "Likes tea", ["tea"])
    with pytest.raises(MemoryValidationError, match="control characters"):
        await store.update("u1", saved.id, text="no\x00pe")
    assert [m.id for m in await store.search("u1", "tea\x00bag")] == [saved.id]
    assert live.sql("SELECT count(*) FROM memories") == [(1,)]


@bounded
async def test_a_nul_character_in_the_user_id_is_a_clean_error_without_server_text(
    live: Live,
) -> None:
    """The user id is the one string the validation lets a NUL through (it only checks
    for a non-empty string; it comes from config.toml, never from the model). psycopg
    refuses the NUL before any statement is sent."""
    store = live.store()
    with pytest.raises(Exception) as refused:
        await store.save("u\x00x", "a fact", ["x"])
    message = str(refused.value)
    assert isinstance(refused.value, MemoryStorageError), type(refused.value)
    assert message == "memory storage failed (DataError)", message
    assert live.sql("SELECT count(*) FROM memories") == [(0,)]


@bounded
async def test_a_lone_surrogate_in_text_is_a_client_side_error_without_server_text(
    live: Live,
) -> None:
    """Observed: `unicodedata.category` of a lone surrogate is `Cs`, not `Cc`, so the
    validation lets it pass; the driver cannot encode it as UTF-8 and nothing is sent."""
    store = live.store()
    with pytest.raises(UnicodeEncodeError) as refused:
        await store.save("u1", "bad \ud800 text", ["x"])
    assert "surrogates not allowed" in str(refused.value)
    assert live.sql("SELECT count(*) FROM memories") == [(0,)]


# --- (d) the schema --------------------------------------------------------------------


def columns(live: Live, table: str) -> list[tuple]:
    return live.sql(
        "SELECT column_name, data_type, is_nullable, is_identity, identity_generation, "
        "is_generated FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )


@bounded
async def test_the_first_use_creates_the_whole_schema_at_version_three(
    live: Live,
) -> None:
    assert len(MEMORY_MIGRATIONS_POSTGRES) == 3
    assert live.sql("SELECT to_regclass('memories')") == [(None,)]
    await live.store().save("u1", "a fact", ["x"])
    assert live.memory_version() == [(3,)]
    assert columns(live, "memories") == [
        ("id", "bigint", "NO", "YES", "ALWAYS", "NEVER"),
        ("user_id", "text", "NO", "NO", None, "NEVER"),
        ("text", "text", "NO", "NO", None, "NEVER"),
        ("keywords", "text", "NO", "NO", None, "NEVER"),
        ("created_at", "text", "NO", "NO", None, "NEVER"),
        ("updated_at", "text", "NO", "NO", None, "NEVER"),
        ("search", "tsvector", "YES", "NO", None, "ALWAYS"),
        ("shared", "integer", "NO", "NO", None, "NEVER"),
    ]
    assert live.sql(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'memories' AND column_name = 'shared'"
    ) == [("0",)]
    ((expression,),) = live.sql(
        "SELECT generation_expression FROM information_schema.columns "
        "WHERE table_name = 'memories' AND column_name = 'search'"
    )
    assert "to_tsvector" in expression and "english" in expression
    assert columns(live, "memory_vectors") == [
        ("collection", "text", "NO", "NO", None, "NEVER"),
        ("memory_id", "bigint", "NO", "NO", None, "NEVER"),
    ]
    assert columns(live, "memory_vector_meta") == [
        ("collection", "text", "NO", "NO", None, "NEVER"),
        ("model_id", "text", "NO", "NO", None, "NEVER"),
        ("dim", "integer", "NO", "NO", None, "NEVER"),
    ]


@bounded
async def test_the_gin_index_the_function_and_both_triggers_exist(live: Live) -> None:
    await live.store().save("u1", "a fact", ["x"])
    indexes = dict(
        live.sql(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'memories'"
        )
    )
    assert "USING gin (search)" in indexes["memories_search"]
    assert "(user_id)" in indexes["memories_user_id"]
    assert live.sql(
        "SELECT l.lanname, p.prorettype::regtype::text FROM pg_proc p "
        "JOIN pg_language l ON l.oid = p.prolang WHERE p.proname = 'memories_clear_markers'"
    ) == [("plpgsql", "trigger")]
    triggers = live.sql(
        "SELECT tgname, tgrelid::regclass::text, p.proname, "
        "pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
        "WHERE NOT tgisinternal ORDER BY tgname"
    )
    assert [(name, table, function) for name, table, function, _ in triggers] == [
        ("memories_vectors_gone", "memories", "memories_clear_markers"),
        ("memories_vectors_stale", "memories", "memories_clear_markers"),
    ]
    definitions = {name: definition for name, _, _, definition in triggers}
    assert "AFTER DELETE" in definitions["memories_vectors_gone"]
    assert "AFTER UPDATE OF text, keywords" in definitions["memories_vectors_stale"]
    assert all("FOR EACH ROW" in definition for definition in definitions.values())


@bounded
async def test_a_second_instance_does_not_migrate_again(live: Live) -> None:
    first = live.store()
    saved = await first.save("u1", "a fact", ["x"])
    table, function = (
        live.sql("SELECT 'memories'::regclass::oid"),
        live.sql("SELECT 'memories_clear_markers'::regproc::oid"),
    )
    second = live.store()
    assert (
        await second.get("u1", saved.id) == saved
    )  # CREATE would fail if it ran again
    assert live.sql("SELECT 'memories'::regclass::oid") == table
    assert live.sql("SELECT 'memories_clear_markers'::regproc::oid") == function
    assert live.memory_version() == [(3,)]
    assert live.sql("SELECT count(*) FROM memories") == [(1,)]


@bounded
async def test_two_instances_using_memory_first_in_threads_leave_one_schema(
    live: Live,
) -> None:
    barrier = threading.Barrier(4)

    def first_use(number: int) -> int:
        barrier.wait(timeout=10)
        return asyncio.run(live.store().save("u1", f"note {number}", ["x"])).id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = [
            f.result(timeout=20) for f in [pool.submit(first_use, n) for n in range(4)]
        ]
    assert sorted(ids) == [1, 2, 3, 4]
    assert live.memory_version() == [(3,)]
    assert live.sql("SELECT count(*) FROM memories") == [(4,)]


@bounded
async def test_two_instances_using_memory_first_through_gather_leave_one_schema(
    live: Live,
) -> None:
    saved = await asyncio.gather(
        *(live.store().save("u1", f"note {n}", ["x"]) for n in range(4))
    )
    assert sorted(m.id for m in saved) == [1, 2, 3, 4]
    assert live.memory_version() == [(3,)]


@bounded
async def test_storage_status_prints_the_memory_schema_line(
    live: Live, tmp_path: Path
) -> None:
    await live.store().save("u1", "a fact", ["x"])
    lines = await storage_status(
        status_config(live.url, live.password),
        relational_path=tmp_path / "never-used.db",
        data_dir=tmp_path,
    )
    assert "  schema: memory version 3 (the code expects 3)" in lines
    assert list(tmp_path.iterdir()) == []  # status created nothing


@bounded
async def test_validation_failures_create_no_table_and_the_ledger_is_left_alone(
    live: Live,
) -> None:
    """The Postgres twin of the SQLite 'touch no file' and 'ledger left alone' tests."""
    store = live.store()
    with pytest.raises(MemoryValidationError):
        await store.save("u1", "", ["x"])
    assert live.sql("SELECT to_regclass('memories')") == [(None,)]
    await store.save("u1", "a fact", ["x"])
    assert live.sql("SELECT to_regclass('llm_calls')") == [(None,)]


@bounded
async def test_the_search_column_follows_updates_and_forgets_and_stop_words_stay(
    live: Live,
) -> None:
    """The Postgres twins of the SQLite FTS-row tests: a generated column needs no trigger."""
    store = live.store()
    saved = await store.save("u1", "Likes zebra stripes", ["animal"])
    assert await store.search("u1", "zebra") == [saved]
    updated = await store.update("u1", saved.id, text="Likes giraffe spots")
    assert await store.search("u1", "zebra") == []
    assert await store.search("u1", "giraffe") == [updated]
    assert "giraff" in live.sql("SELECT search::text FROM memories")[0][0]
    assert "zebra" not in live.sql("SELECT search::text FROM memories")[0][0]
    assert await store.forget("u1", saved.id) is True
    assert live.sql(
        "SELECT count(*) FROM memories WHERE search @@ to_tsquery('giraff')"
    ) == [(0,)]
    stop = await store.save("u1", "Walks on the beach", ["the"])
    assert await store.search("u1", "the beach") == [stop]
    assert await store.search("u1", "the") == []  # only the query side drops it
    assert "the" in live.sql("SELECT keywords FROM memories")[0][0]


@bounded
async def test_hundreds_of_rows_inserted_directly_are_listed_searched_and_paged(
    live: Live,
) -> None:
    """The Postgres twin of the SQLite 600-entry test (rows go in by raw SQL)."""
    store = live.store()
    await store.save("u1", "seed entry", ["seed"])
    live.sql(
        "INSERT INTO memories (user_id, text, keywords, created_at, updated_at) "
        "SELECT 'u1', 'bulk note ' || n || ' about topic' || n, 'bulk', %s, %s "
        "FROM generate_series(0, 598) AS n",
        (STAMP, STAMP),
    )
    assert len(await store.list("u1", limit=1000)) == 600
    assert [m.text for m in await store.search("u1", "topic598")] == [
        "bulk note 598 about topic598"
    ]
    extra = await store.save("u1", "one more", ["more"])
    assert extra.id == 601
    assert len(await store.search("u1", "bulk", k=99)) == 10


# --- (e) the marker triggers -----------------------------------------------------------


def embedder() -> Embedder:
    return Embedder(ConceptEmbedder(), model())


def markers(live: Live) -> list[tuple]:
    return live.sql(
        "SELECT collection, memory_id FROM memory_vectors ORDER BY memory_id"
    )


@bounded
async def test_marker_triggers_follow_text_and_keyword_changes_and_forgets(
    live: Live,
) -> None:
    indexed = IndexedMemoryStore(live.relational(), FakeVectorStore(), embedder, clock)
    plain = live.store()  # same database; its writes do not re-index
    kept = await indexed.save("u1", "Likes tea", ["tea"])
    note = await indexed.save("u1", "Partner is allergic to peanuts", ["allergy"])
    assert indexed.index_off_reason is None
    assert markers(live) == [(COLLECTION, kept.id), (COLLECTION, note.id)]

    await plain.update("u1", note.id, keywords=["allergy", "diet"])
    assert markers(live) == [(COLLECTION, kept.id)]  # keywords cleared only this one
    assert await indexed.reindex("u1") == (1, 1)
    assert len(markers(live)) == 2

    await plain.update("u1", note.id, text="Partner avoids peanuts")
    assert markers(live) == [(COLLECTION, kept.id)]  # a real text change clears it
    await indexed.reindex("u1")

    # What the program sets on every update is `updated_at`; alone it keeps the marker
    # (raw SQL: the program cannot update only that, it needs text or keywords).
    live.sql("UPDATE memories SET updated_at = 'later' WHERE id = %s", (note.id,))
    assert len(markers(live)) == 2
    with pytest.raises(MemoryValidationError, match="text, keywords or both"):
        await plain.update("u1", note.id)

    # Observed: the trigger fires for a column in the SET list even when the value is the
    # same (SQLite behaves alike), so an update that rewrites the same text clears it.
    await plain.update("u1", note.id, text="Partner avoids peanuts")
    assert markers(live) == [(COLLECTION, kept.id)]
    await indexed.reindex("u1")

    assert await plain.forget("u1", note.id) is True
    assert markers(live) == [(COLLECTION, kept.id)]  # forget cleared the marker
    assert await plain.forget("u1", kept.id) is True
    assert markers(live) == []


@bounded
async def test_the_indexed_store_reindexes_after_its_own_update(live: Live) -> None:
    indexed = IndexedMemoryStore(live.relational(), FakeVectorStore(), embedder, clock)
    note = await indexed.save("u1", "Likes tea", ["tea"])
    await indexed.update("u1", note.id, text="Likes green tea")
    assert markers(live) == [
        (COLLECTION, note.id)
    ]  # cleared by the trigger, then re-added
    assert await indexed.forget("u1", note.id) is True
    assert markers(live) == []
    assert live.sql("SELECT count(*) FROM memory_vector_meta") == [(1,)]


@bounded
async def test_the_marker_upsert_does_nothing_the_second_time(live: Live) -> None:
    indexed = IndexedMemoryStore(live.relational(), FakeVectorStore(), embedder, clock)
    note = await indexed.save("u1", "Likes tea", ["tea"])
    relational = live.relational()
    first = await relational.execute(
        "INSERT INTO memory_vectors (collection, memory_id) VALUES (?, ?) "
        "ON CONFLICT (collection, memory_id) DO NOTHING",
        (COLLECTION, note.id),
    )
    second = await relational.execute(
        "INSERT INTO memory_vectors (collection, memory_id) VALUES (?, ?) "
        "ON CONFLICT (collection, memory_id) DO NOTHING",
        ("another_collection", note.id),
    )
    assert (first, second) == (0, 1)  # already there; a new collection is a new row
    assert sorted(markers(live)) == sorted(
        [("another_collection", note.id), (COLLECTION, note.id)]
    )


# --- (f) the vector index end to end ---------------------------------------------------

QUERY_BY_MEANING = "cook for her"


async def exercise_the_index(
    live: Live,
    make_vectors: Callable[[], VectorStore],
    drop_vector_data: Callable[[], None],
) -> None:
    def indexed_store(vectors: VectorStore) -> IndexedMemoryStore:
        return IndexedMemoryStore(live.relational(), vectors, embedder, clock)

    store = indexed_store(make_vectors())
    allergy = await store.save(
        "u1", "Partner is allergic to peanuts", ["allergy", "diet"]
    )
    trip = await store.save("u1", "Planning a hotel budget", ["trip"])
    other = await store.save("u2", "Allergic to peanuts, avoid the meal", ["allergy"])
    assert store.index_off_reason is None
    assert len(markers(live)) == 3

    # No keyword in common: the plain keyword search finds nothing, so a hit is the vector path.
    assert await live.store().search("u1", QUERY_BY_MEANING) == []
    found = await store.search("u1", QUERY_BY_MEANING)
    assert found[0].id == allergy.id
    assert other.id not in [m.id for m in found]  # u2's note never reaches u1
    assert {m.id for m in found} <= {allergy.id, trip.id}
    assert [m.id for m in await store.search("u2", QUERY_BY_MEANING)] == [other.id]

    # Switching the vector backend: no markers, no vectors, then reindex u1 only.
    live.sql("DELETE FROM memory_vectors")
    drop_vector_data()
    reopened = indexed_store(make_vectors())
    assert await live.store().search("u1", QUERY_BY_MEANING) == []
    assert await reopened.reindex("u1") == (2, 2)
    meaning = await reopened.search("u1", QUERY_BY_MEANING)
    assert meaning[0].id == allergy.id
    assert {memory_id for _, memory_id in markers(live)} == {allergy.id, trip.id}

    # `/memory reindex` reports how many it repaired.
    live.sql("DELETE FROM memory_vectors")
    answer = await run_memory_command(reopened, "u1", MemoryCommand("reindex"))
    assert answer == "reindexed 2 of 2 memories"
    assert reopened.index_off_reason is None
    again = await reopened.search("u1", QUERY_BY_MEANING)
    assert again[0].id == allergy.id and other.id not in [m.id for m in again]


@bounded
async def test_postgres_memory_with_qdrant_edge_searches_by_meaning_and_reindexes(
    live: Live, tmp_path: Path
) -> None:
    roots = iter([tmp_path / "vectors-1", tmp_path / "vectors-2"])
    opened: list[QdrantEdgeVectorStore] = []

    def make_vectors() -> QdrantEdgeVectorStore:
        vectors = QdrantEdgeVectorStore(next(roots))  # a new directory is dropped data
        opened.append(vectors)
        return vectors

    try:
        await exercise_the_index(live, make_vectors, lambda: None)
    finally:
        for vectors in opened:
            await vectors.close()


@bounded
async def test_postgres_memory_with_the_real_pgvector_store_searches_by_meaning(
    live_vector: Live,
) -> None:
    def make_vectors() -> PgVectorStore:
        secret = Secret(live_vector.password) if live_vector.password else None
        return PgVectorStore(live_vector.url, secret)

    await exercise_the_index(
        live_vector,
        make_vectors,
        lambda: reset_vector_schema(live_vector.url, live_vector.password),
    )
    assert live_vector.sql("SELECT count(*) FROM vector_points") == [(2,)]


# --- (f2) shared notes: the real ALTER TABLE, the real filters --------------------------


@bounded
async def test_a_v2_database_with_rows_is_upgraded_by_step_three_and_rows_stay_private(
    live: Live,
) -> None:
    """The first production `ALTER TABLE`, on a real server: old rows read `shared = 0`."""
    await live.relational().migrate("memory", MEMORY_MIGRATIONS_POSTGRES[:2])
    assert live.memory_version() == [(2,)]
    live.sql(
        "INSERT INTO memories (user_id, text, keywords, created_at, updated_at) "
        "VALUES ('alice', 'Likes green tea', 'tea', 'then', 'then'), "
        "('alice', 'Prefers metric units', 'metric', 'then', 'then')"
    )
    store = live.store()
    notes = await store.list("alice")
    assert [(m.text, m.shared) for m in notes] == [
        ("Prefers metric units", False),
        ("Likes green tea", False),
    ]
    assert live.memory_version() == [(3,)]
    assert live.sql("SELECT shared FROM memories ORDER BY id") == [(0,), (0,)]
    assert [m.text for m in await store.search("alice", "tea")] == ["Likes green tea"]
    assert await store.search("bob", "tea") == []  # an old row is private
    assert await store.get("bob", notes[0].id) is None
    await live.store().get("alice", 1)  # a second instance: no migration, no error
    assert live.memory_version() == [(3,)]


@bounded
async def test_shared_notes_are_read_by_another_user_and_owner_only_to_change(
    live: Live,
) -> None:
    store = live.store()
    mine = await store.save("bob", "Bob drinks tea", ["tea"])
    note = await store.save("alice", "Team rule: tea is free", ["tea", "rule"])
    private = await store.save("alice", "Alice private tea habit", ["tea"])
    live.sql(
        "INSERT INTO memory_vectors (collection, memory_id) VALUES ('c', %s), ('c', %s)",
        (note.id, private.id),
    )

    published = await store.set_shared("alice", note.id, True)
    assert published is not None and published.shared is True
    assert live.sql("SELECT shared FROM memories WHERE id = %s", (note.id,)) == [(1,)]
    assert published.updated_at == note.updated_at
    assert [r[1] for r in markers(live)] == [private.id]  # only the flipped note's

    other = live.store()  # another instance, as another person's program
    assert await other.get("bob", note.id) == published
    assert await other.get("bob", private.id) is None
    assert {m.id for m in await other.search("bob", "tea")} == {mine.id, note.id}
    assert await other.list("bob") == [published, mine]
    for user in ("alice", "bob"):
        assert await other.update(user, note.id, text="changed") is None
        assert await other.forget(user, note.id) is False
    assert live.sql("SELECT text FROM memories WHERE id = %s", (note.id,)) == [
        ("Team rule: tea is free",)
    ]
    assert await other.set_shared("bob", note.id, False) is None  # owner only
    assert await other.set_shared("alice", 999, True) is None

    back = await other.set_shared("alice", note.id, False)
    assert back is not None and back.shared is False
    assert await other.get("bob", note.id) is None
    assert [m.id for m in await other.search("bob", "tea")] == [mine.id]
    assert await other.forget("alice", note.id) is True


@bounded
async def test_set_shared_with_a_driver_failure_is_a_type_name_only_error(
    live: Live,
) -> None:
    store = live.store()
    note = await store.save("alice", "a fact", ["x"])
    live.sql("DROP TABLE memory_vectors")  # the marker delete now fails on the server
    with pytest.raises(MemoryStorageError) as info:
        await store.set_shared("alice", note.id, True)
    assert str(info.value) == "memory storage failed (UndefinedTable)"


@bounded
async def test_the_real_pgvector_store_filters_on_the_shared_payload_and_unpublish_removes_it(
    live_vector: Live,
) -> None:
    secret = Secret(live_vector.password) if live_vector.password else None
    vectors = PgVectorStore(live_vector.url, secret)
    store = IndexedMemoryStore(live_vector.relational(), vectors, embedder, clock)

    def payloads() -> list[tuple]:
        return live_vector.sql(
            "SELECT id, payload FROM vector_points ORDER BY id::bigint"
        )

    note = await store.save("alice", "Partner is allergic to peanuts", ["allergy"])
    tea = await store.save("alice", "Likes tea", ["tea"])
    assert store.index_off_reason is None
    assert payloads() == [
        (str(note.id), {"user_id": "alice"}),
        (str(tea.id), {"user_id": "alice"}),
    ]

    # Private: another user finds nothing, by keyword or by meaning.
    assert await store.search("bob", QUERY_BY_MEANING) == []

    published = await store.set_shared("alice", note.id, True)
    assert published is not None and published.shared is True
    assert store.index_off_reason is None
    assert live_vector.sql(
        "SELECT id FROM vector_points WHERE payload @> '{\"shared\": 1}'::jsonb"
    ) == [(str(note.id),)]
    assert live_vector.sql(
        "SELECT payload ? 'shared' FROM vector_points WHERE id = %s", (str(note.id),)
    ) == [(True,)]
    assert len(markers(live_vector)) == 2  # cleared by the flip, written again

    # No keyword in common with the note: a hit is the shared vector search.
    assert await live_vector.store().search("bob", QUERY_BY_MEANING) == []
    found = await store.search("bob", QUERY_BY_MEANING)
    assert [m.id for m in found] == [
        note.id
    ]  # the shared one; alice's tea stays hidden
    assert found[0].shared is True
    # The owner finds it through both searches, listed once.
    assert [m.id for m in await store.search("alice", QUERY_BY_MEANING)].count(
        note.id
    ) == 1

    # Reindex of another user embeds the shared note once per collection.
    assert await store.reindex("bob") == (0, 0)

    await store.set_shared("alice", note.id, False)
    assert live_vector.sql(
        "SELECT payload ? 'shared' FROM vector_points WHERE id = %s", (str(note.id),)
    ) == [(False,)]
    assert live_vector.sql(
        "SELECT count(*) FROM vector_points WHERE payload @> '{\"shared\": 1}'::jsonb"
    ) == [(0,)]
    assert await store.search("bob", QUERY_BY_MEANING) == []
    assert payloads()[0] == (str(note.id), {"user_id": "alice"})


@bounded
async def test_a_shared_note_unindexed_by_a_flip_is_embedded_by_another_users_reindex(
    live_vector: Live,
) -> None:
    secret = Secret(live_vector.password) if live_vector.password else None
    store = IndexedMemoryStore(
        live_vector.relational(),
        PgVectorStore(live_vector.url, secret),
        embedder,
        clock,
    )
    note = await live_vector.store().save("alice", "Likes tea", ["tea"])
    await live_vector.store().set_shared(
        "alice", note.id, True
    )  # plain store, no vectors
    assert await store.reindex("bob") == (1, 1)
    assert live_vector.sql("SELECT payload FROM vector_points") == [
        ({"user_id": "alice", "shared": 1},)
    ]
    assert await store.reindex("carol") == (0, 0)  # the marker is per note id


async def command(store: MemoryStore, text: str, user: str, admins=("alice",)) -> str:
    parsed = parse_memory_command(text)
    assert parsed is not None
    return await run_memory_command(store, user, parsed, memory_admins=admins)


@bounded
async def test_publish_and_unpublish_commands_on_the_real_postgres_store(
    live: Live,
) -> None:
    store = live.store()
    note = await store.save("alice", "Team rule: tea is free", ["tea"])
    other = live.store()  # another instance, as bob's program
    publish = f"/memory publish {note.id}"
    assert await command(other, publish, "bob") == (
        "only memory admins may publish or unpublish shared memories"
    )
    assert await command(other, publish, "bob", admins=("bob",)) == (
        f"no memory #{note.id}"  # a private note of another user is never revealed
    )
    assert await command(other, publish, "alice") == f"published memory #{note.id}"
    assert await command(other, publish, "alice") == (
        f"memory #{note.id} is already shared"
    )
    assert live.sql("SELECT shared FROM memories WHERE id = %s", (note.id,)) == [(1,)]
    assert (await command(store, "/memory", "bob")).startswith(
        f"#{note.id} [shared] [tea] (updated "
    )
    assert f"#{note.id} [shared]" in await command(store, "/memory search tea", "bob")
    assert f"#{note.id} [shared]" in await command(
        store, f"/memory show {note.id}", "bob"
    )
    unpublish = f"/memory unpublish {note.id}"
    assert await command(store, unpublish, "alice") == f"unpublished memory #{note.id}"
    assert (
        await command(store, unpublish, "alice") == f"memory #{note.id} is not shared"
    )
    assert await command(store, "/memory", "bob") == "(no memories)"
    assert (
        await command(store, "/memory search tea", "bob") == 'no memories match "tea"'
    )


@bounded
async def test_publish_command_with_the_real_pgvector_store_sets_the_payload(
    live_vector: Live,
) -> None:
    secret = Secret(live_vector.password) if live_vector.password else None
    store = IndexedMemoryStore(
        live_vector.relational(),
        PgVectorStore(live_vector.url, secret),
        embedder,
        clock,
    )
    note = await store.save("alice", "Partner is allergic to peanuts", ["allergy"])
    assert await command(store, f"/memory publish {note.id}", "alice") == (
        f"published memory #{note.id}"
    )
    assert [m.id for m in await store.search("bob", QUERY_BY_MEANING)] == [note.id]
    assert await command(store, f"/memory unpublish {note.id}", "alice") == (
        f"unpublished memory #{note.id}"
    )
    assert await store.search("bob", QUERY_BY_MEANING) == []


NOT_ADMIN_FORGET = "only memory admins may forget shared memories"


@bounded
async def test_forget_command_on_the_real_postgres_store(live: Live) -> None:
    store = live.store()
    note = await store.save("alice", "Team rule: tea is free", ["tea"])
    private = await store.save("alice", "Alice private", ["secret"])
    await store.set_shared("alice", note.id, True)
    await live.store().save("bob", "Bob private", ["bobby"])
    other = live.store()  # another instance, as the admin's program
    forget = f"/memory forget {note.id}"
    # The creator is not an admin; a non-admin never causes a lookup.
    assert await command(other, forget, "alice", admins=("carol",)) == NOT_ADMIN_FORGET
    assert await command(other, "/memory forget 999", "bob", admins=()) == (
        NOT_ADMIN_FORGET
    )
    assert live.sql("SELECT count(*) FROM memories WHERE id = %s", (note.id,)) == [(1,)]
    # A private note of another user: never revealed, never deleted.
    assert await command(
        other, f"/memory forget {private.id}", "carol", ("carol",)
    ) == (f"no memory #{private.id}")
    assert await command(
        other, f"/memory forget {private.id}", "alice", ("alice",)
    ) == (f"memory #{private.id} is not shared; use /memory delete {private.id}")
    assert live.sql("SELECT count(*) FROM memories WHERE id = %s", (private.id,)) == [
        (1,)
    ]
    for memory_id in (note.id, private.id):  # marker rows the delete trigger must clear
        live.sql(
            "INSERT INTO memory_vectors (collection, memory_id) VALUES ('c1', %s)",
            (memory_id,),
        )
    # The admin forgets it for everyone.
    assert await command(other, forget, "carol", admins=("carol",)) == (
        f"forgot shared memory #{note.id}: Team rule: tea is free"
    )
    assert live.sql("SELECT count(*) FROM memories WHERE id = %s", (note.id,)) == [(0,)]
    assert markers(live) == [
        ("c1", private.id)
    ]  # only the forgotten note's marker went
    assert await command(store, forget, "carol", admins=("carol",)) == (
        f"no memory #{note.id}"
    )
    for user in ("alice", "bob", "carol"):
        assert f"#{note.id} " not in await command(store, "/memory", user)
        assert await command(store, "/memory search tea", user) == (
            'no memories match "tea"'
        )
    assert live.sql("SELECT count(*) FROM memories") == [
        (2,)
    ]  # both private notes stay


@bounded
async def test_forget_command_with_the_real_pgvector_store_removes_the_point(
    live_vector: Live,
) -> None:
    secret = Secret(live_vector.password) if live_vector.password else None
    store = IndexedMemoryStore(
        live_vector.relational(),
        PgVectorStore(live_vector.url, secret),
        embedder,
        clock,
    )
    note = await store.save("alice", "Partner is allergic to peanuts", ["allergy"])
    await command(store, f"/memory publish {note.id}", "alice")
    assert [m.id for m in await store.search("bob", QUERY_BY_MEANING)] == [note.id]
    assert live_vector.sql("SELECT count(*) FROM vector_points") == [(1,)]
    assert live_vector.sql("SELECT count(*) FROM memory_vectors") == [(1,)]
    assert await command(
        store, f"/memory forget {note.id}", "carol", admins=("carol",)
    ) == (f"forgot shared memory #{note.id}: Partner is allergic to peanuts")
    assert live_vector.sql(
        "SELECT count(*) FROM vector_points WHERE id = %s", (str(note.id),)
    ) == [(0,)]
    assert live_vector.sql("SELECT count(*) FROM vector_points") == [(0,)]
    assert live_vector.sql("SELECT count(*) FROM memory_vectors") == [(0,)]
    assert await store.search("bob", QUERY_BY_MEANING) == []
    assert await store.search("alice", QUERY_BY_MEANING) == []
    assert store.index_off_reason is None


# --- (g) concurrency, with separate store instances ------------------------------------


@bounded
async def test_twenty_concurrent_first_use_saves_leave_twenty_rows_with_unique_ids(
    live: Live,
) -> None:
    saved = await asyncio.gather(
        *(live.store().save("u1", f"concurrent note {n}", ["x"]) for n in range(20))
    )
    assert len({m.id for m in saved}) == 20
    assert live.sql("SELECT count(*), count(DISTINCT id) FROM memories") == [(20, 20)]
    assert len(await live.store().list("u1", limit=100)) == 20


@bounded
async def test_concurrent_saves_updates_searches_and_forgets_raise_nothing(
    live: Live,
) -> None:
    seed = live.store()
    seeded_notes = [
        await seed.save("u1", f"seed note {n} tea", ["tea"]) for n in range(10)
    ]

    async def save(n: int) -> None:
        await live.store().save("u1", f"fresh note {n} tea", ["tea"])

    async def update(note_id: int) -> None:
        await live.store().update("u1", note_id, text=f"changed {note_id} tea")

    async def search(_: int) -> None:
        await live.store().search("u1", "tea note")

    async def forget(note_id: int) -> None:
        await live.store().forget("u1", note_id)

    forgotten = [m.id for m in seeded_notes[5:]]
    await asyncio.gather(
        *(save(n) for n in range(10)),
        *(update(m.id) for m in seeded_notes[:5]),
        *(search(n) for n in range(10)),
        *(forget(i) for i in forgotten),
    )
    remaining = {m.id for m in await live.store().list("u1", limit=100)}
    assert not remaining & set(forgotten)
    assert len(remaining) == 5 + 10  # five updated seeds and ten fresh notes


@bounded
async def test_two_instances_saving_the_same_text_at_once_keep_two_rows(
    live: Live,
) -> None:
    first, second = await asyncio.gather(
        live.store().save("u1", "Likes tea", ["tea"]),
        live.store().save("u1", "Likes tea", ["tea"]),
    )
    assert first.id != second.id
    assert live.sql("SELECT count(*) FROM memories WHERE text = 'Likes tea'") == [(2,)]


# --- (h) user isolation ----------------------------------------------------------------


@bounded
async def test_one_user_never_sees_or_changes_another_users_memories(
    live: Live,
) -> None:
    store = live.store()
    mine = await store.save("alice", "Uses a kayak every weekend", ["kayak"])
    theirs = await store.save("bob", "Rides a kayak too", ["kayak"])
    other_instance = live.store()  # a fresh instance holds no cached state either
    for reader in (store, other_instance):
        assert await reader.search("alice", "kayak") == [mine]
        assert await reader.search("bob", "kayak") == [theirs]
        assert await reader.get("alice", theirs.id) is None
        assert await reader.get("bob", mine.id) is None
        assert await reader.list("alice") == [mine]
        assert await reader.list("bob") == [theirs]
        assert await reader.update("alice", theirs.id, text="hijacked") is None
        assert await reader.update("bob", mine.id, keywords=["hijacked"]) is None
        assert await reader.forget("alice", theirs.id) is False
        assert await reader.forget("bob", mine.id) is False
    assert await store.get("bob", theirs.id) == theirs
    assert await store.get("alice", mine.id) == mine
    assert await store.search("carol", "kayak") == []


# --- (i) errors and the security sweep -------------------------------------------------


@dataclass(frozen=True)
class Broken:
    """A target the memory paths cannot use, and the strings no shown text may hold."""

    label: str
    url: str
    password: str | None
    forbidden: tuple[str, ...]

    def relational(self) -> PostgresRelationalStore:
        return PostgresRelationalStore(
            self.url, Secret(self.password) if self.password else None
        )

    def store(self) -> MemoryStore:
        return MemoryStore(self.relational(), clock)

    def storage(self, tmp_path: Path) -> Storage:
        config = StorageConfig(
            relational=RelationalConfig(backend="postgres", url=self.url),
            vector=VectorConfig(backend="none"),
        )
        return open_storage(
            config,
            tmp_path,
            postgres_password=Secret(self.password) if self.password else None,
        )


# Words a driver message holds and a program message must not (case-sensitive).
DRIVER_WORDS = (
    "connection",
    "FATAL",
    "DETAIL",
    "Failing row",
    "authentication",
    "refused",
    "psycopg",
    "server at",
    "role ",
    "database ",
)


def broken_targets(live: Live) -> list[Broken]:
    parts = urlsplit(live.url)
    common = (
        "127.0.0.1",
        str(parts.port),
        "postgres",
        parts.path.lstrip("/"),
        WRONG_PASSWORD,
        *([live.password] if live.password else ()),
        *DRIVER_WORDS,
    )
    return [
        Broken("wrong password", live.url, WRONG_PASSWORD, common),
        Broken(
            "closed port",
            with_host_and_port(live.url, "127.0.0.1:1"),
            live.password,
            common,
        ),
    ]


def require_a_real_failure(live: Live, broken: Broken) -> None:
    if broken.label == "wrong password":
        try:
            psycopg.connect(
                broken.url, password=broken.password, connect_timeout=3
            ).close()
        except psycopg.Error:
            return
        pytest.skip("the server accepted a wrong password (trust authentication)")


def assert_clean(
    text: str, broken: Broken, where: str, type_name: str | None = None
) -> None:
    for word in broken.forbidden:
        assert word not in text, f"{where} ({broken.label}) shows {word!r}: {text!r}"
    if type_name is not None:
        assert type_name in text, f"{where}: no type name in {text!r}"


async def collect_texts(live: Live, broken: Broken, tmp_path: Path) -> dict[str, str]:
    """Every text the program would show for a failing database, per path."""
    texts: dict[str, str] = {}

    async def attempt(where: str, call: Callable[[], Awaitable[object]]) -> None:
        try:
            result = await call()
        except Exception as error:  # noqa: BLE001  (the point is to read what it says)
            texts[where] = f"{type(error).__name__}: {error}"
        else:
            texts[where] = f"answered: {result}"

    store = broken.store()
    await attempt("store.save", lambda: store.save("u1", "a fact", ["x"]))
    await attempt("store.update", lambda: store.update("u1", 1, text="y"))
    await attempt("store.forget", lambda: store.forget("u1", 1))
    await attempt("store.get", lambda: store.get("u1", 1))
    await attempt("store.search", lambda: store.search("u1", "tea"))
    await attempt("store.list", lambda: store.list("u1"))

    storage = broken.storage(tmp_path)
    registry = build_tool_registry([], memory=True, user_id="u1")
    ctx = ToolContext(
        storage=storage,
        user_id="u1",
        agent_id="orchestrator",
        tags=frozenset({"orchestrator"}),
        allowed_tools=frozenset(tool.name for tool in MemoryTools().tools()),
    )
    calls = {
        "memory_search": MemorySearchInput(query="tea"),
        "memory_save": MemorySaveInput(text="a fact", keywords=["x"]),
        "memory_update": MemoryUpdateInput(id=1, text="y"),
        "memory_forget": MemoryForgetInput(id=1),
    }
    for name, arguments in calls.items():
        tool = registry.get(name)
        assert tool is not None
        try:
            await tool.run(arguments, ctx)
        except Exception as error:  # noqa: BLE001
            # The observation of orchestrator.py `_call_tool`, written the same way.
            texts[f"tool {name}"] = (
                f"tool {name} failed: {type(error).__name__}: {redact(str(error))}"
            )
        else:
            texts[f"tool {name}"] = "answered"

    for command in (
        "/memory",
        "/memory list 2",
        "/memory show 1",
        "/memory delete 1",
        "/memory reindex",
    ):
        await attempt(
            command, lambda c=command: handle_memory_text(c, registry, storage, "u1")
        )

    for action in (
        ReflectionAction("save", text="a fact", keywords=("x",)),
        ReflectionAction("update", id=1, text="y"),
        ReflectionAction("forget", id=1),
    ):
        try:
            await apply_reflection(store, "u1", action)
        except Exception as error:  # noqa: BLE001
            texts[f"reflection {action.action}"] = (
                f"memory reflection failed: {_failure_reason(error)}"
            )
    try:  # the recall of orchestrator.py `_recall` searches first, and the reflection too
        await store.search("u1", "tea")
    except Exception as error:  # noqa: BLE001
        texts["recall span"] = f"{type(error).__name__}: {error}"

    indexed = IndexedMemoryStore(
        broken.relational(), FakeVectorStore(), embedder, clock
    )
    assert await indexed.reindex("u1") is None
    texts["index off_reason"] = indexed.index_off_reason or ""
    texts["index notice"] = indexed.take_notice() or ""
    texts["/memory reindex answer"] = await run_memory_command(
        IndexedMemoryStore(broken.relational(), FakeVectorStore(), embedder, clock),
        "u1",
        MemoryCommand("reindex"),
    )
    return texts


@pytest.mark.parametrize("kind", ["wrong password", "closed port"])
@bounded
async def test_no_memory_path_shows_host_port_user_password_database_or_server_text(
    live: Live, tmp_path: Path, kind: str
) -> None:
    broken = next(b for b in broken_targets(live) if b.label == kind)
    require_a_real_failure(live, broken)
    texts = await collect_texts(live, broken, tmp_path)
    assert len(texts) >= 20, sorted(texts)
    for where, text in texts.items():
        assert_clean(text, broken, where)
        assert "OperationalError" in text or text.startswith(("answered", "usage")), (
            where,
            text,
        )


@pytest.mark.parametrize("kind", ["wrong password", "closed port"])
@bounded
async def test_a_failing_database_is_one_memory_storage_error_with_the_type_name_only(
    live: Live, kind: str
) -> None:
    broken = next(b for b in broken_targets(live) if b.label == kind)
    require_a_real_failure(live, broken)
    store = broken.store()
    for call in (
        lambda: store.save("u1", "a fact", ["x"]),
        lambda: store.update("u1", 1, text="y"),
        lambda: store.forget("u1", 1),
        lambda: store.get("u1", 1),
        lambda: store.search("u1", "tea"),
        lambda: store.list("u1"),
    ):
        with pytest.raises(MemoryStorageError) as refused:
            await call()
        assert str(refused.value) == "memory storage failed (OperationalError)"
        assert refused.value.__cause__ is None and refused.value.__suppress_context__


@bounded
async def test_the_memory_command_answers_one_line_when_the_database_fails(
    live: Live, tmp_path: Path
) -> None:
    broken = broken_targets(live)[1]  # the closed port needs no server cooperation
    registry = build_tool_registry([], memory=True, user_id="u1")
    answer = await handle_memory_text(
        "/memory list", registry, broken.storage(tmp_path), "u1"
    )
    assert answer == "memory storage failed (OperationalError)"


@bounded
async def test_a_closed_port_fails_within_seconds(live: Live) -> None:
    broken = broken_targets(live)[1]
    started = asyncio.get_running_loop().time()
    with pytest.raises(MemoryStorageError):
        await broken.store().search("u1", "tea")
    assert asyncio.get_running_loop().time() - started < 12
