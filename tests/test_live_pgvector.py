"""Live check of `PgVectorStore` against a real PostgreSQL server with pgvector.

Deselected by default (`pytest -m live`). Skips, with a printed reason, when
`LLM_HARNESS_LIVE_PGVECTOR_URL` is unset, the server does not answer within 3 s, or the
database name does not contain `live` or `test`: these tests drop the fixed tables
`vector_points` and `vector_collections` and drop and recreate the `vector` extension, so
they must never meet a database that holds real vectors. The url carries no password; the
password comes from `LLM_HARNESS_LIVE_POSTGRES_PASSWORD`. The "not installed" tests need a
second server WITHOUT pgvector, in `LLM_HARNESS_LIVE_PGVECTOR_PLAIN_URL`; they skip without it.

Run it (the same pgvector server also serves the `pgvector` parameter of
`tests/test_storage_vectors.py`):

    docker --context rancher-desktop run -d --rm --name llm-harness-live-pgvector -e POSTGRES_PASSWORD=live-test-password -e POSTGRES_DB=llm_harness_live -p 127.0.0.1:15433:5432 pgvector/pgvector:pg17
    docker --context rancher-desktop run -d --rm --name llm-harness-live-pgplain -e POSTGRES_PASSWORD=live-test-password -e POSTGRES_DB=llm_harness_live -p 127.0.0.1:15434:5432 postgres:17
    LLM_HARNESS_LIVE_PGVECTOR_URL=postgresql://postgres@127.0.0.1:15433/llm_harness_live LLM_HARNESS_LIVE_PGVECTOR_PLAIN_URL=postgresql://postgres@127.0.0.1:15434/llm_harness_live LLM_HARNESS_LIVE_POSTGRES_PASSWORD=live-test-password uv run pytest -m live tests/test_live_pgvector.py tests/test_storage_vectors.py -v -rs
    docker --context rancher-desktop rm -f llm-harness-live-pgvector
    docker --context rancher-desktop rm -f llm-harness-live-pgplain

Wait a few seconds after `run` until `docker --context rancher-desktop exec llm-harness-live-pgvector
pg_isready -U postgres` answers. The image starts WITHOUT the extension created; the test setup
creates it through a raw administrator connection (`reset_vector_schema`), because the program
itself never does. Every test starts and ends on a database without the two tables and without
the `vector` row of `schema_versions`. Each test body is bounded to 30 s.
"""

import asyncio
import functools
import math
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

import llm_harness.storage.pgvector as pgvector_module
from llm_harness.config import (
    HarnessConfig,
    ProviderConfig,
    StorageConfig,
    VectorConfig,
)
from llm_harness.memory import MemoryStore
from llm_harness.memory_index import IndexedMemoryStore, collection_name
from llm_harness.redact import Secret
from llm_harness.storage.pgvector import MIGRATIONS, PgVectorStore
from llm_harness.storage.postgres import is_undefined_table
from llm_harness.storage.relational import SqliteRelationalStore
from llm_harness.storage.status import storage_status
from llm_harness.storage.vectors import VectorPoint, VectorStoreError
from tests.live_postgres import (
    pgvector_plain_target,
    pgvector_target,
    reset_vector_schema,
)
from tests.test_live_postgres import (
    CLOSED_PORT_SECONDS,
    READ_ONLY_OPTIONS,
    WRONG_PASSWORD,
    with_host_and_port,
)
from tests.test_memory_index_store import NOW, Rig

pytestmark = pytest.mark.live

WHOLE_TEST_SECONDS = 30
ROLE = "live_vector_role"
ROLE_PASSWORD = "live-role-password"


def bounded[**P, R](test: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Fail a test that runs longer than 30 s (its worker threads cannot be killed)."""

    @functools.wraps(test)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        async with asyncio.timeout(WHOLE_TEST_SECONDS):
            return await test(*args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class Live:
    url: str
    password: str | None

    def store(self) -> PgVectorStore:
        return PgVectorStore(self.url, Secret(self.password) if self.password else None)

    def sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        """A raw administrator connection: what the test checks, the program never uses."""
        with psycopg.connect(self.url, password=self.password, autocommit=True) as raw:
            cursor = raw.execute(statement, params)
            return cursor.fetchall() if cursor.description else []

    def label(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.hostname}:{parts.port}/{parts.path.lstrip('/')}"


@pytest.fixture
def live() -> Iterator[Live]:
    url, password = pgvector_target()
    reset_vector_schema(url, password)
    try:
        yield Live(url, password)
    finally:
        reset_vector_schema(url, password)


def point(id: str, vector: list[float], **payload: str | float | bool) -> VectorPoint:
    return VectorPoint(id=id, vector=vector, payload=payload)


async def fresh(
    live: Live, name: str = "tools", dim: int = 3, distance: str = "cosine"
) -> PgVectorStore:
    store = live.store()
    await store.ensure_collection(name, dim, distance)
    return store


def extensions(live: Live) -> list[tuple]:
    return live.sql("SELECT extname, extversion FROM pg_extension ORDER BY extname")


def public_tables(live: Live) -> set[str]:
    return {
        name
        for (name,) in live.sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
    }


def vector_version(live: Live) -> list[tuple]:
    if "schema_versions" not in public_tables(live):
        return []  # nothing was ever migrated in this database
    return live.sql("SELECT version FROM schema_versions WHERE namespace = 'vector'")


def only_the_type(message: str, collection: str, type_name: str) -> bool:
    return message == f"vector store failed on collection '{collection}': {type_name}"


# --- (a) the extension states ---------------------------------------------------------


@bounded
async def test_an_extension_not_created_gives_the_exact_message_and_creates_nothing(
    live: Live,
) -> None:
    live.sql("DROP EXTENSION vector CASCADE")
    try:
        tables, installed = public_tables(live), extensions(live)
        with pytest.raises(VectorStoreError) as refused:
            await live.store().ensure_collection("tools", 3)
        assert str(refused.value) == pgvector_module._NOT_CREATED
        for private in (
            urlsplit(live.url).hostname,
            "postgres",
            str(urlsplit(live.url).port),
        ):
            assert private not in str(refused.value)
        assert live.password not in str(refused.value)
        # The program never ran CREATE EXTENSION, and nothing else was created.
        assert extensions(live) == installed
        assert public_tables(live) == tables
        assert vector_version(live) == []
    finally:
        live.sql("CREATE EXTENSION IF NOT EXISTS vector")


@bounded
async def test_a_server_without_the_package_gives_the_not_installed_message() -> None:
    url, password = pgvector_plain_target()
    plain = Live(url, password)
    assert (
        plain.sql("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'") == []
    )
    tables, installed = public_tables(plain), extensions(plain)
    with pytest.raises(VectorStoreError) as refused:
        await plain.store().ensure_collection("tools", 3)
    assert str(refused.value) == pgvector_module._NOT_INSTALLED
    assert password not in str(refused.value)
    assert extensions(plain) == installed and public_tables(plain) == tables


def drop_role(live: Live) -> None:
    if live.sql("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)):
        live.sql(f"DROP OWNED BY {ROLE}")  # our own fixed name
        live.sql(f"DROP ROLE {ROLE}")


@bounded
async def test_a_role_without_superuser_rights_can_use_the_store(live: Live) -> None:
    database = urlsplit(live.url).path.lstrip("/")
    drop_role(live)
    # A literal password: CREATE ROLE cannot take a bind parameter.
    live.sql(f"CREATE ROLE {ROLE} LOGIN PASSWORD '{ROLE_PASSWORD}'")
    try:
        live.sql(f'GRANT CONNECT ON DATABASE "{database}" TO {ROLE}')
        live.sql(f"GRANT USAGE, CREATE ON SCHEMA public TO {ROLE}")
        if "schema_versions" in public_tables(live):
            live.sql(f"GRANT ALL ON schema_versions TO {ROLE}")
        parts = urlsplit(live.url)
        role_url = urlunsplit(
            parts._replace(netloc=f"{ROLE}@{parts.hostname}:{parts.port}")
        )
        assert live.sql(f"SELECT rolsuper FROM pg_roles WHERE rolname = '{ROLE}'") == [
            (False,)
        ]
        store = PgVectorStore(role_url, Secret(ROLE_PASSWORD))
        await store.ensure_collection("tools", 3)
        await store.upsert("tools", [point("a", [1.0, 0.0, 0.0], user_id="u1")])
        hits = await store.search("tools", [1.0, 0.0, 0.0], 1, {"user_id": "u1"})
        assert [h.id for h in hits] == ["a"]
    finally:
        live.sql("DROP TABLE IF EXISTS vector_points, vector_collections")
        drop_role(live)


# --- (b) the schema -------------------------------------------------------------------


@bounded
async def test_the_schema_has_the_expected_tables_columns_and_version(
    live: Live,
) -> None:
    await fresh(live)
    assert vector_version(live) == [(len(MIGRATIONS),)]
    columns = live.sql(
        "SELECT table_name, column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_name IN ('vector_collections', 'vector_points') "
        "ORDER BY table_name, ordinal_position"
    )
    assert columns == [
        ("vector_collections", "name", "text", "NO"),
        ("vector_collections", "dim", "integer", "NO"),
        ("vector_collections", "distance", "text", "NO"),
        ("vector_points", "collection", "text", "NO"),
        ("vector_points", "id", "text", "NO"),
        ("vector_points", "embedding", "USER-DEFINED", "NO"),
        ("vector_points", "payload", "jsonb", "NO"),
    ]
    # The unconstrained type: `vector`, not `vector(3)`.
    assert live.sql(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'vector_points'::regclass AND attname = 'embedding'"
    ) == [("vector",)]
    keys = live.sql(
        "SELECT table_name, column_name FROM information_schema.key_column_usage "
        "WHERE table_name IN ('vector_collections', 'vector_points') "
        "ORDER BY table_name, ordinal_position"
    )
    assert keys == [
        ("vector_collections", "name"),
        ("vector_points", "collection"),
        ("vector_points", "id"),
    ]


@bounded
async def test_a_second_instance_on_the_same_database_does_not_migrate_again(
    live: Live,
) -> None:
    first = await fresh(live)
    await first.upsert("tools", [point("kept", [1.0, 0.0, 0.0])])
    table_id = live.sql("SELECT 'vector_points'::regclass::oid")
    await fresh(live)  # CREATE TABLE would fail, or the table would be new, if it ran
    assert live.sql("SELECT 'vector_points'::regclass::oid") == table_id
    assert live.sql("SELECT id FROM vector_points") == [("kept",)]
    assert vector_version(live) == [(1,)]


@bounded
async def test_two_stores_preparing_in_threads_end_with_one_consistent_schema(
    live: Live,
) -> None:
    barrier = threading.Barrier(4)

    def prepare() -> None:
        barrier.wait(timeout=10)
        asyncio.run(live.store().ensure_collection("tools", 3))

    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in [pool.submit(prepare) for _ in range(4)]:
            future.result(timeout=20)
    assert vector_version(live) == [(1,)]
    assert live.sql("SELECT name, dim, distance FROM vector_collections") == [
        ("tools", 3, "cosine")
    ]


@bounded
async def test_two_stores_preparing_through_gather_end_with_one_consistent_schema(
    live: Live,
) -> None:
    await asyncio.gather(
        *(live.store().ensure_collection("tools", 3) for _ in range(2))
    )
    assert vector_version(live) == [(1,)]
    assert live.sql("SELECT count(*) FROM vector_collections") == [(1,)]


# --- (c) the pieces the spike never ran through this module ---------------------------

A, B, C = [1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 1.0, 0.0]
DISTANCES = ["cosine", "euclid", "dot"]


async def seeded_abc(live: Live, distance: str, order: str = "cba") -> PgVectorStore:
    store = await fresh(live, "tools", 3, distance)
    by_name = {"a": A, "b": B, "c": C}
    await store.upsert("tools", [point(name, by_name[name]) for name in order])
    return store


@pytest.mark.parametrize("distance", DISTANCES)
@bounded
async def test_the_bound_limit_returns_exactly_k_nearest(
    live: Live, distance: str
) -> None:
    # `LIMIT ?` as a bound parameter, `ORDER BY 3` on the unaliased distance expression
    # and the `?::vector` text cast, all inside the module's own statement text. The
    # points go in far-to-near, so the order cannot be the insertion order.
    store = await seeded_abc(live, distance)
    assert [h.id for h in await store.search("tools", A, 1)] == ["a"]
    assert [h.id for h in await store.search("tools", A, 2)] == ["a", "b"]
    assert [h.id for h in await store.search("tools", A, 3)] == ["a", "b", "c"]
    assert [h.id for h in await store.search("tools", A, 50)] == ["a", "b", "c"]
    assert [h.id for h in await store.search("tools", C, 1)] == ["c"]


@bounded
async def test_vector_text_round_trips_tiny_and_large_float32_values(
    live: Live,
) -> None:
    store = await fresh(live, "tools", 3)
    await store.upsert(
        "tools",
        [
            point("tiny", [1e-07, 0.0, 1.0]),
            point("big", [-1e22, 1.0, 0.0]),
            point("under", [1e-50, 1.0, 0.0]),  # float32 underflow: stored, not refused
        ],
    )
    assert {h.id for h in await store.search("tools", [0.0, 0.0, 1.0], 10)} == {
        "tiny",
        "big",
        "under",
    }
    assert live.sql("SELECT embedding::text FROM vector_points WHERE id = 'tiny'") == [
        ("[1e-07,0,1]",)
    ]


@bounded
async def test_a_value_outside_float32_is_refused_by_the_server_with_the_type_only(
    live: Live,
) -> None:
    store = await fresh(live, "tools", 3)
    with pytest.raises(VectorStoreError) as refused:
        await store.upsert("tools", [point("big", [1e39, 0.0, 0.0])])
    assert only_the_type(str(refused.value), "tools", "NumericValueOutOfRange")
    assert live.sql("SELECT count(*) FROM vector_points") == [(0,)]


@bounded
async def test_delete_with_any_takes_a_list_of_ids(live: Live) -> None:
    store = await fresh(live, "tools", 3)
    await store.ensure_collection("other", 3)
    names = ["p1", "p2", "p3", "p4", "p5"]
    await store.upsert("tools", [point(n, A) for n in names])
    await store.upsert("other", [point(n, A) for n in names])
    await store.delete("tools", ["p1", "p3", "p5", "never-stored"])
    assert live.sql(
        "SELECT id FROM vector_points WHERE collection = 'tools' ORDER BY id"
    ) == [("p2",), ("p4",)]
    assert live.sql(
        "SELECT count(*) FROM vector_points WHERE collection = 'other'"
    ) == [(5,)]
    await store.delete("tools", ["p2"])  # one id is a list of one
    assert live.sql("SELECT id FROM vector_points WHERE collection = 'tools'") == [
        ("p4",)
    ]


@bounded
async def test_payload_str_int_bool_and_ordinary_floats_round_trip_with_their_type(
    live: Live,
) -> None:
    store = await fresh(live, "tools", 3)
    payload: dict[str, str | int | float | bool] = {
        "text": "héllo 'quoted' \"double\" \\ back",
        "int": 7,
        "negative_int": -42,
        "big_int": 2**70,  # jsonb numeric is exact, so an int stays an int
        "yes": True,
        "no": False,
        "tenth": 0.1,
        "tiny": 1e-07,
        "pi": 3.141592653589793,
        "negative": -2.5,
        "whole": 1.0,
        "zero": 0.0,
        "smallest": 5e-324,
    }
    await store.upsert("tools", [VectorPoint("a", A, payload)])
    (hit,) = await store.search("tools", A, 1)
    assert hit.payload == payload
    for key, value in payload.items():
        assert type(hit.payload[key]) is type(value), (
            key
        )  # bool is not int, 1.0 is float


@bounded
async def test_a_very_large_float_comes_back_as_an_int_jsonb_keeps_decimal_digits(
    live: Live,
) -> None:
    """Observed on pgvector/pgvector:pg17 (PostgreSQL 17.11): jsonb stores numbers as
    `numeric` and `payload::text` prints them without an exponent.

    - 1e22: the text is `10000000000000000000000`; `json.loads` gives the int 10**22,
      which EQUALS 1e22 (that float is exactly 10**22) but is an int, not a float.
    - 1.5e300 and 1.7976931348623157e308: the text is the shortest decimal digits of the
      float followed by zeros, a different number from the float's exact binary value,
      so the int is NOT equal to the float. This is jsonb's behaviour, not the adapter's;
      nothing in the program stores a payload float (the memory index stores `user_id`).
    """
    store = await fresh(live, "tools", 3)
    for name, value in (
        ("e22", 1e22),
        ("e300", 1.5e300),
        ("max", 1.7976931348623157e308),
    ):
        await store.upsert("tools", [point(name, A, v=value)])
    found = {h.id: h.payload["v"] for h in await store.search("tools", A, 10)}
    assert found["e22"] == 10**22 and type(found["e22"]) is int
    assert found["e22"] == 1e22
    assert found["e300"] == 15 * 10**299 and type(found["e300"]) is int
    assert found["e300"] != 1.5e300
    assert found["max"] == 17976931348623157 * 10**292 and type(found["max"]) is int
    assert found["max"] != 1.7976931348623157e308
    assert live.sql("SELECT payload::text FROM vector_points WHERE id = 'e22'") == [
        ('{"v": 10000000000000000000000}',)
    ]


@bounded
async def test_payload_text_is_jsonb_formatted_and_a_key_order_is_not_kept(
    live: Live,
) -> None:
    store = await fresh(live, "tools", 3)
    await store.upsert("tools", [point("a", A, zebra="z", apple=1, ok=True)])
    # `payload::text` puts a space after colons and commas and sorts keys by length
    # and then bytes; the program parses it with json.loads, so only the data counts.
    assert live.sql("SELECT payload::text FROM vector_points") == [
        ('{"ok": true, "apple": 1, "zebra": "z"}',)
    ]
    (hit,) = await store.search("tools", A, 1)
    assert hit.payload == {"zebra": "z", "apple": 1, "ok": True}


@bounded
async def test_an_int_filter_never_matches_a_stored_bool_and_a_bool_filter_is_refused(
    live: Live,
) -> None:
    store = await fresh(live, "tools", 3)
    await store.upsert(
        "tools",
        [
            point("as_bool", A, n=True),
            point("as_int", A, n=1),
            point("as_str", A, n="1"),
        ],
    )
    assert [h.id for h in await store.search("tools", A, 5, {"n": 1})] == ["as_int"]
    assert [h.id for h in await store.search("tools", A, 5, {"n": "1"})] == ["as_str"]
    with pytest.raises(VectorStoreError, match="'n' with a bool"):
        await store.search("tools", A, 5, {"n": True})


@bounded
async def test_a_nul_character_in_a_payload_string_is_refused_by_jsonb_with_the_type_only(
    live: Live,
) -> None:
    """Observed: json.dumps writes the NUL as the six characters `\\u0000`; jsonb refuses
    that escape (psycopg `UntranslatableCharacter`, SQLSTATE 22P05). The program's
    message names the collection and the exception type, never the offending text."""
    store = await fresh(live, "tools", 3)
    with pytest.raises(VectorStoreError) as refused:
        await store.upsert("tools", [point("a", A, note="secret-row-42\u0000")])
    assert only_the_type(str(refused.value), "tools", "UntranslatableCharacter")
    assert "secret-row-42" not in str(refused.value)


@bounded
async def test_a_failed_batch_leaves_no_row_and_keeps_the_old_one(live: Live) -> None:
    store = await fresh(live, "tools", 3)
    await store.upsert("tools", [point("p1", A, version=1)])
    batch = [point("p1", A, version=2), point("p2", B), point("p3", C, note="\u0000")]
    with pytest.raises(VectorStoreError):
        await store.upsert("tools", batch)  # the third point fails on the server
    # Rolled back: the first two statements of the batch had already run.
    assert live.sql("SELECT id, payload::text FROM vector_points ORDER BY id") == [
        ("p1", '{"version": 1}')
    ]
    await store.upsert("tools", [point("p2", B)])  # and the store still works
    assert live.sql("SELECT count(*) FROM vector_points") == [(2,)]


@bounded
async def test_a_ten_thousand_character_payload_string_round_trips(live: Live) -> None:
    store = await fresh(live, "tools", 3)
    text = "".join(chr(97 + i % 26) for i in range(10_000))
    await store.upsert("tools", [point("a", A, text=text)])
    (hit,) = await store.search("tools", A, 1)
    assert hit.payload == {"text": text}


# --- (d) scores against the real server -----------------------------------------------

EXPECTED_SCORES = {
    "cosine": [1.0, 0.9 / math.sqrt(0.82), 0.0],  # 0.99388...
    "euclid": [-0.0, -math.sqrt(0.02), -math.sqrt(2.0)],  # -0.14142, -1.41421
    "dot": [1.0, 0.9, 0.0],
}


@pytest.mark.parametrize("distance", DISTANCES)
@bounded
async def test_scores_of_the_spike_vectors_and_nearest_first_order(
    live: Live, distance: str
) -> None:
    store = await seeded_abc(live, distance)
    hits = await store.search("tools", A, 3)
    assert [h.id for h in hits] == ["a", "b", "c"]
    # pgvector stores float32, hence the tolerance.
    assert [h.score for h in hits] == pytest.approx(EXPECTED_SCORES[distance], abs=1e-6)
    assert hits[0].score >= hits[1].score >= hits[2].score


@bounded
async def test_a_stored_zero_vector_with_cosine_is_last_with_score_minus_one(
    live: Live,
) -> None:
    store = await fresh(live, "tools", 3, "cosine")
    await store.upsert(
        "tools",
        [point("zero", [0.0, 0.0, 0.0]), point("c", C), point("b", B), point("a", A)],
    )
    ((raw_distance,),) = live.sql(
        "SELECT embedding <=> '[1,0,0]'::vector FROM vector_points WHERE id = 'zero'"
    )
    assert math.isnan(raw_distance)
    hits = await store.search("tools", A, 10)
    assert [h.id for h in hits] == ["a", "b", "c", "zero"]
    assert [h.score for h in hits][:3] == pytest.approx(
        EXPECTED_SCORES["cosine"], abs=1e-6
    )
    assert hits[-1].score == -1.0
    # With k smaller than the count the NaN hit is the one that is cut.
    assert [h.id for h in await store.search("tools", A, 3)] == ["a", "b", "c"]


@bounded
async def test_a_zero_query_vector_with_cosine_does_not_crash_and_is_deterministic(
    live: Live,
) -> None:
    """Observed: every distance is NaN, so every hit scores -1.0 and PostgreSQL keeps
    the rows in the order it scanned them (here the insertion order). The order is
    only checked for repeatability, not promised."""
    store = await fresh(live, "tools", 3, "cosine")
    await store.upsert(
        "tools", [point(n, v) for n, v in (("a", A), ("b", B), ("c", C))]
    )
    first = await store.search("tools", [0.0, 0.0, 0.0], 10)
    again = await store.search("tools", [0.0, 0.0, 0.0], 10)
    assert {h.id for h in first} == {"a", "b", "c"}
    assert [h.score for h in first] == [-1.0, -1.0, -1.0]
    assert [h.id for h in first] == [h.id for h in again]
    assert len(await store.search("tools", [0.0, 0.0, 0.0], 2)) == 2


# --- (e) names and ids ----------------------------------------------------------------


@bounded
async def test_a_64_character_collection_name_works_end_to_end(live: Live) -> None:
    name = "a" + "b" * 63
    store = await fresh(live, name, 3)
    await store.upsert(name, [point("x", A, user_id="u1"), point("y", C, user_id="u2")])
    assert [h.id for h in await store.search(name, A, 5, {"user_id": "u1"})] == ["x"]
    await store.delete(name, ["x"])
    assert [h.id for h in await store.search(name, A, 5)] == ["y"]
    assert live.sql("SELECT name FROM vector_collections") == [(name,)]
    again = live.store()  # a new instance finds it again under the full name
    await again.ensure_collection(name, 3)
    assert [h.id for h in await again.search(name, A, 5)] == ["y"]


HOSTILE_IDS = [
    "it's",
    'say "hi"',
    "100%",
    "%s",
    "%(x)s",
    "what?",
    "?",
    "back\\slash",
    "ünï-日本語-🙂",
    "a;--",
    "x'); DROP TABLE vector_points;--",
    "z" * 500,
]


@bounded
async def test_ids_with_quotes_percent_question_marks_backslashes_and_unicode_round_trip(
    live: Live,
) -> None:
    store = await fresh(live, "tools", 3)
    await store.upsert("tools", [point(i, A, tag="x") for i in HOSTILE_IDS])
    hits = await store.search("tools", A, 50)
    assert sorted(h.id for h in hits) == sorted(HOSTILE_IDS)
    await store.delete("tools", HOSTILE_IDS[:-1])  # every one through ANY(?)
    assert [h.id for h in await store.search("tools", A, 50)] == ["z" * 500]
    assert "vector_points" in public_tables(live)


@bounded
async def test_hostile_payload_keys_and_values_are_data_and_filters_find_them(
    live: Live,
) -> None:
    store = await fresh(live, "tools", 3)
    keys = [
        "it's",
        '"quoted"',
        "a%b",
        "?",
        "back\\slash",
        "日本語",
        "; drop table vector_points; --",
        "",
    ]
    for index, key in enumerate(keys):
        await store.upsert("tools", [VectorPoint(f"k{index}", A, {key: "v'%?"})])
    hits = await store.search("tools", A, 50)
    assert sorted(h.id for h in hits) == sorted(f"k{i}" for i in range(len(keys)))
    for index, key in enumerate(keys):
        found = await store.search("tools", A, 50, {key: "v'%?"})
        assert [h.id for h in found] == [f"k{index}"], key
    assert "vector_points" in public_tables(live)


@bounded
async def test_collections_named_like_the_tables_or_like_each_other_stay_apart(
    live: Live,
) -> None:
    store = live.store()
    names = ["vector_points", "vector_collections", "schema_versions", "a_1", "a_10"]
    for name in names:
        await store.ensure_collection(name, 3)
        await store.upsert(name, [point(f"id_of_{name}", A)])
    for name in names:
        assert [h.id for h in await store.search(name, A, 10)] == [f"id_of_{name}"]
    assert vector_version(live) == [(1,)]


# --- (f) concurrency, with separate instances -----------------------------------------


@bounded
async def test_eight_concurrent_ensure_calls_leave_one_catalog_row(live: Live) -> None:
    await asyncio.gather(
        *(live.store().ensure_collection("tools", 3) for _ in range(8))
    )
    assert live.sql("SELECT name, dim, distance FROM vector_collections") == [
        ("tools", 3, "cosine")
    ]
    assert vector_version(live) == [(1,)]


@bounded
async def test_two_instances_upserting_the_same_fifty_ids_leave_fifty_rows(
    live: Live,
) -> None:
    ids = [f"id{n:02d}" for n in range(50)]
    stores = [await fresh(live) for _ in range(2)]
    await asyncio.gather(
        *(
            store.upsert("tools", [point(i, A, writer=index) for i in ids])
            for index, store in enumerate(stores)
        )
    )
    assert live.sql("SELECT count(*) FROM vector_points") == [(50,)]
    writers = {h.payload["writer"] for h in await stores[0].search("tools", A, 100)}
    assert len(writers) in (1, 2)  # a whole row comes from one writer or the other


@bounded
async def test_two_instances_upserting_the_same_ids_in_opposite_order_never_deadlock(
    live: Live,
) -> None:
    """Found by this test: with the rows sent in the caller's order, two writers that
    list the same ids in opposite order locked rows crosswise and the server aborted one
    of them with `DeadlockDetected` in every round tried. The batch is now sent in id
    order, so concurrent batches queue instead of deadlocking."""
    ids = [f"id{n:02d}" for n in range(50)]
    for _ in range(5):
        stores = [await fresh(live) for _ in range(2)]
        await asyncio.gather(
            stores[0].upsert("tools", [point(i, A, writer=0) for i in ids]),
            stores[1].upsert("tools", [point(i, A, writer=1) for i in reversed(ids)]),
        )
    assert live.sql("SELECT count(*) FROM vector_points") == [(50,)]


@bounded
async def test_a_batch_with_a_repeated_id_keeps_the_last_payload_in_every_order(
    live: Live,
) -> None:
    store = await fresh(live)
    await store.upsert(
        "tools", [point("b", A, v=1), point("a", A, v=1), point("b", A, v=2)]
    )
    found = {h.id: h.payload for h in await store.search("tools", A, 10)}
    assert found == {"a": {"v": 1}, "b": {"v": 2}}


@bounded
async def test_concurrent_ensure_with_different_dimensions_has_one_winner(
    live: Live,
) -> None:
    dims = [2, 3, 4, 5, 6, 7]
    results = await asyncio.gather(
        *(live.store().ensure_collection("tools", d) for d in dims),
        return_exceptions=True,
    )
    ((winner, _, _),) = live.sql("SELECT dim, name, distance FROM vector_collections")
    assert [d for d, r in zip(dims, results, strict=True) if r is None] == [winner]
    for dim, result in zip(dims, results, strict=True):
        if dim != winner:
            assert isinstance(result, VectorStoreError)
            assert str(result) == (
                f"collection 'tools' holds {winner}-dimensional vectors, got {dim}; "
                "re-index the collection or use another name"
            )


@bounded
async def test_concurrent_upserts_and_searches_do_not_raise(live: Live) -> None:
    stores = [await fresh(live) for _ in range(6)]

    async def write(store: PgVectorStore, number: int) -> None:
        for round_ in range(5):
            await store.upsert(
                "tools",
                [point(f"w{number}_{round_}_{n}", A, writer=number) for n in range(10)],
            )

    async def read(store: PgVectorStore) -> None:
        for _ in range(10):
            await store.search("tools", A, 5, {"writer": 0})

    await asyncio.gather(
        *(write(store, n) for n, store in enumerate(stores[:3])),
        *(read(store) for store in stores[3:]),
    )
    assert live.sql("SELECT count(*) FROM vector_points") == [(150,)]


# --- (g) --storage-status on the real server ------------------------------------------


def status_config(url: str, password: str | None) -> HarnessConfig:
    return HarnessConfig(
        providers={
            "p": ProviderConfig(type="ollama", base_url="http://localhost:11434")
        },
        models=[],
        storage=StorageConfig(vector=VectorConfig(backend="pgvector", url=url)),
        postgres_password=Secret(password) if password else None,
    )


async def vector_lines(url: str, password: str | None, data_dir: Path) -> list[str]:
    lines = await storage_status(
        status_config(url, password),
        relational_path=data_dir / "never-used.db",
        data_dir=data_dir,
    )
    start = next(i for i, line in enumerate(lines) if line.startswith("vector:"))
    end = next(i for i, line in enumerate(lines) if line.startswith("files:"))
    return lines[start:end]


def server_version(live: Live) -> str:
    ((version,),) = live.sql(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    )
    return version


@bounded
async def test_status_shows_the_extension_version_then_the_catalog_states(
    live: Live, tmp_path: Path
) -> None:
    version = server_version(live)  # 0.8.7 on pgvector/pgvector:pg17, read, not assumed
    assert re.fullmatch(r"0\.8\.\d+", version)
    head = [f"vector: pgvector  {live.label()}", f"  extension: vector {version}"]
    before = (public_tables(live), extensions(live))
    # Tables absent.
    assert live.store().read_status_sync().extension_version == version
    assert await vector_lines(live.url, live.password, tmp_path) == [
        *head,
        "  collections: not set up yet (it is set up on first use)",
    ]
    assert (public_tables(live), extensions(live)) == before  # status created nothing
    # Empty catalog.
    store = await fresh(live, "zeta", 4, "dot")
    live.sql("DELETE FROM vector_collections")
    assert await vector_lines(live.url, live.password, tmp_path) == [
        *head,
        "  no collections yet",
    ]
    # Two collections, sorted by name.
    await store.ensure_collection("zeta2", 4, "dot")
    await store.ensure_collection("alpha", 3, "euclid")
    assert await vector_lines(live.url, live.password, tmp_path) == [
        *head,
        "  collection alpha: 3 dimensions, euclid",
        "  collection zeta2: 4 dimensions, dot",
    ]
    assert list(tmp_path.iterdir()) == []


@bounded
async def test_status_with_the_extension_available_but_not_created(
    live: Live, tmp_path: Path
) -> None:
    live.sql("DROP EXTENSION vector CASCADE")
    try:
        status = live.store().read_status_sync()
        assert (status.extension_version, status.extension_available) == (None, True)
        assert await vector_lines(live.url, live.password, tmp_path) == [
            f"vector: pgvector  {live.label()}",
            (
                "  extension: not created in this database "
                "(an administrator runs: CREATE EXTENSION vector;)"
            ),
        ]
        assert extensions(live) == [("plpgsql", "1.0")]  # status never created it
    finally:
        live.sql("CREATE EXTENSION IF NOT EXISTS vector")


@bounded
async def test_status_with_the_package_not_installed(tmp_path: Path) -> None:
    url, password = pgvector_plain_target()
    plain = Live(url, password)
    assert await vector_lines(url, password, tmp_path) == [
        f"vector: pgvector  {plain.label()}",
        (
            "  extension: not installed on this server "
            "(install the pgvector package, then run: CREATE EXTENSION vector;)"
        ),
    ]


@bounded
async def test_the_status_connection_really_refuses_writes(live: Live) -> None:
    seen: list[dict[str, Any]] = []

    def recording_factory(**kwargs: Any) -> psycopg.Connection:
        seen.append(kwargs)
        return psycopg.connect(**kwargs)

    await fresh(live)  # the catalog table must exist for an INSERT to reach the check
    PgVectorStore(
        live.url,
        Secret(live.password) if live.password else None,
        connect_factory=recording_factory,
    ).read_status_sync()
    (kwargs,) = seen
    assert kwargs["options"] == READ_ONLY_OPTIONS and kwargs["connect_timeout"] == 5
    with psycopg.connect(**kwargs) as read_only:
        assert read_only.execute("SELECT name FROM vector_collections").fetchall() == [
            ("tools",)
        ]
        with pytest.raises(psycopg.Error) as refused:
            read_only.execute(
                "INSERT INTO vector_collections (name, dim, distance) VALUES ('x', 1, 'dot')"
            )
    assert refused.value.sqlstate == "25006"  # read_only_sql_transaction
    assert live.sql("SELECT name FROM vector_collections") == [("tools",)]


@bounded
async def test_a_real_missing_table_error_is_recognised_and_other_errors_are_not(
    live: Live,
) -> None:
    with psycopg.connect(live.url, password=live.password) as raw:
        with pytest.raises(psycopg.errors.UndefinedTable) as missing:
            raw.execute("SELECT * FROM vector_collections")
        assert missing.value.sqlstate == "42P01"
        assert is_undefined_table(missing.value)
        raw.rollback()
        with pytest.raises(psycopg.errors.SyntaxError) as other:
            raw.execute("SELEKT 1")
        assert not is_undefined_table(other.value)


@bounded
async def test_a_wrong_password_prints_one_cannot_connect_line(
    live: Live, tmp_path: Path
) -> None:
    lines = await vector_lines(live.url, WRONG_PASSWORD, tmp_path)
    if "cannot connect" not in "".join(lines):
        pytest.skip("the server accepted a wrong password (trust authentication)")
    assert lines == [
        f"vector: pgvector  {live.label()}",
        "  cannot connect (OperationalError)",
    ]
    text = "\n".join(lines)
    assert WRONG_PASSWORD not in text and (
        not live.password or live.password not in text
    )


@bounded
async def test_a_closed_port_prints_one_cannot_connect_line_within_seconds(
    live: Live, tmp_path: Path
) -> None:
    closed = with_host_and_port(live.url, "127.0.0.1:1")
    started = time.monotonic()
    lines = await vector_lines(closed, live.password, tmp_path)
    assert time.monotonic() - started < CLOSED_PORT_SECONDS
    assert lines == [
        "vector: pgvector  127.0.0.1:1/" + urlsplit(live.url).path.lstrip("/"),
        "  cannot connect (OperationalError)",
    ]
    assert not live.password or live.password not in "\n".join(lines)


# --- (h) errors with the real server --------------------------------------------------


@bounded
async def test_a_wrong_password_on_a_store_names_the_collection_and_the_type_only(
    live: Live,
) -> None:
    store = PgVectorStore(live.url, Secret(WRONG_PASSWORD))
    with pytest.raises(VectorStoreError) as refused:
        await store.ensure_collection("tools", 3)
    message = str(refused.value)
    if "OperationalError" not in message:
        pytest.skip("the server accepted a wrong password (trust authentication)")
    assert only_the_type(message, "tools", "OperationalError")
    for secret in (
        WRONG_PASSWORD,
        live.password,
        urlsplit(live.url).hostname,
        "postgres",
    ):
        assert secret not in message
    assert WRONG_PASSWORD not in repr(store)


@bounded
async def test_a_closed_port_on_a_store_names_the_collection_and_the_type_only(
    live: Live,
) -> None:
    store = PgVectorStore(
        with_host_and_port(live.url, "127.0.0.1:1"),
        Secret(live.password) if live.password else None,
    )
    started = time.monotonic()
    with pytest.raises(VectorStoreError) as refused:
        await store.ensure_collection("tools", 3)
    assert time.monotonic() - started < 15  # the url's default connect timeout is 10 s
    assert only_the_type(str(refused.value), "tools", "OperationalError")


@bounded
async def test_server_side_data_errors_never_leak_a_row_or_server_text(
    live: Live,
) -> None:
    store = await fresh(live, "tools", 3)
    for bad in (
        point("a", A, secret="row-text-42\u0000"),  # jsonb refuses the NUL escape
        point("row-id-42", [1e39, 0.0, 0.0]),  # outside float32
    ):
        with pytest.raises(VectorStoreError) as refused:
            await store.upsert("tools", [bad])
        message = str(refused.value)
        assert re.fullmatch(r"vector store failed on collection 'tools': \w+", message)
        for text in (
            "row-text-42",
            "row-id-42",
            "Failing row",
            "DETAIL",
            "vector_points",
        ):
            assert text not in message


# --- (i) the memory index end to end (criterion 14) -----------------------------------


@bounded
async def test_memory_saved_and_searched_by_meaning_on_the_real_server_then_reindexed(
    live: Live, tmp_path: Path
) -> None:
    db = tmp_path / "memory.db"
    rig = Rig(
        db
    )  # its provider, embedder factory and SQLite helpers, not its fake store
    vectors = live.store()
    rig.store = IndexedMemoryStore(
        SqliteRelationalStore(db), vectors, rig.factory, lambda: NOW
    )
    allergy = await rig.store.save(
        "u1", "Partner is allergic to peanuts", ["allergy", "diet"]
    )
    trip = await rig.store.save("u1", "Planning a hotel budget", ["trip"])
    other = await rig.store.save(
        "u2", "Allergic to peanuts, avoid the meal", ["allergy"]
    )
    assert rig.store.index_off_reason is None
    assert live.sql("SELECT count(*) FROM vector_points") == [(3,)]

    # By meaning: "cook" and "her" share the food and person concepts with the note and no
    # word with it, so the keyword search of plain SQLite finds nothing.
    query = "cook for her"
    assert await MemoryStore(SqliteRelationalStore(db)).search("u1", query) == []
    found = await rig.store.search("u1", query)
    assert found[0].id == allergy.id
    assert other.id not in [memory.id for memory in found]  # the user_id filter held
    assert {memory.id for memory in found} <= {allergy.id, trip.id}
    assert [m.id for m in await rig.store.search("u2", query)] == [other.id]

    # The documented backend-switch workaround: no markers, no vectors, then reindex.
    rig.sql("DELETE FROM memory_vectors")
    reset_vector_schema(live.url, live.password)
    reopened = IndexedMemoryStore(
        SqliteRelationalStore(db), live.store(), rig.factory, lambda: NOW
    )
    assert await reopened.reindex("u1") == (2, 2)
    assert live.sql("SELECT id FROM vector_points ORDER BY id") == sorted(
        [(str(allergy.id),), (str(trip.id),)]
    )
    assert reopened.index_off_reason is None
    again = await reopened.search("u1", query)
    assert again[0].id == allergy.id
    assert other.id not in [memory.id for memory in again]
    assert (
        collection_name("concepts")
        == live.sql("SELECT name FROM vector_collections")[0][0]
    )
