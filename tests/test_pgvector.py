"""`PgVectorStore` on the stand-in server of `tests/fake_pgvector.py`: no server, no socket.

The shared contract (names, ids, messages, sorted scores, close) runs on this backend in
`tests/test_storage_vectors.py` (the `pgvector_fake` parameter). This file holds what is
specific to it: the extension messages, the schema migration, statement hygiene, secrets,
error text, scores per distance, the NaN rule and the transaction. What only a real
pgvector server can show is in the live tests of step 5.
"""

import asyncio
import math
import sys
from typing import Any

import pytest

import llm_harness.storage.pgvector as pgvector_module
from llm_harness.redact import Secret
from llm_harness.storage.pgvector import MIGRATIONS, NAMESPACE, PgVectorStore
from llm_harness.storage.vectors import VectorPoint, VectorStoreError
from tests.fake_pgvector import (
    AVAILABLE,
    MISSING,
    FakeDatabaseError,
    FakePgVectorServer,
)

USER = "db-secret-user"
PASSWORD = "sk-test-SECRET-123"
HOST = "db-secret-host.example"
URL = f"postgresql://{USER}@{HOST}:5432/vectors_test"
SECRET_TEXT = (
    f'connection to server at "{HOST}" failed: password authentication failed for '
    f'user "{USER}" (password {PASSWORD}) DETAIL: Failing row contains (secret-row-42)'
)
LONGEST_NAME = "a" + "b" * 63


@pytest.fixture
def server() -> FakePgVectorServer:
    return FakePgVectorServer()


def make_store(
    server: FakePgVectorServer, password: Secret | None = None
) -> PgVectorStore:
    return PgVectorStore(URL, password, connect_factory=server)


def point(id: str, vector: list[float], **payload: Any) -> VectorPoint:
    return VectorPoint(id=id, vector=vector, payload=payload)


async def ready(
    store: PgVectorStore, name: str = "tools", dim: int = 3, distance: str = "cosine"
) -> PgVectorStore:
    await store.ensure_collection(name, dim, distance)
    return store


def points_sql(server: FakePgVectorServer) -> list[str]:
    return [sql for sql in server.statements if "vector_points" in sql]


# --- construction, repr, the driver ------------------------------------------------


def test_construction_connects_to_nothing(server: FakePgVectorServer) -> None:
    make_store(server, Secret(PASSWORD))
    assert server.calls == [] and server.connections == []


async def test_without_psycopg_construction_works_and_first_use_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)  # `import psycopg` now fails
    store = PgVectorStore(URL)
    with pytest.raises(VectorStoreError, match="uv sync --extra postgres"):
        await store.ensure_collection("tools", 3)
    await store.close()


def test_repr_has_the_label_and_the_password_state_only(
    server: FakePgVectorServer,
) -> None:
    assert (
        repr(make_store(server, Secret(PASSWORD)))
        == f"PgVectorStore({HOST}:5432/vectors_test, password=set)"
    )
    assert (
        repr(make_store(server))
        == f"PgVectorStore({HOST}:5432/vectors_test, password=none)"
    )
    for text in (repr(make_store(server, Secret(PASSWORD))), str(make_store(server))):
        assert USER not in text and PASSWORD not in text and "postgresql://" not in text


# --- the extension ------------------------------------------------------------------


async def test_a_server_without_the_package_gets_the_install_message(
    server: FakePgVectorServer,
) -> None:
    server.extension = MISSING
    with pytest.raises(VectorStoreError) as failed:
        await make_store(server).ensure_collection("tools", 3)
    assert str(failed.value) == (
        "the pgvector extension is not installed on this PostgreSQL server: install the "
        "pgvector package on the server, then an administrator runs: CREATE EXTENSION vector;"
    )


async def test_an_extension_not_created_in_the_database_gets_the_admin_message(
    server: FakePgVectorServer,
) -> None:
    server.extension = AVAILABLE
    with pytest.raises(VectorStoreError) as failed:
        await make_store(server).ensure_collection("tools", 3)
    assert str(failed.value) == (
        "the pgvector extension is not created in this database: an administrator must "
        "run, as a superuser in the database named in [storage.vector] url: "
        "CREATE EXTENSION vector;"
    )


@pytest.mark.parametrize("state", ["created", AVAILABLE, MISSING])
async def test_the_program_never_runs_create_extension(state: str) -> None:
    server = FakePgVectorServer(extension=state)
    store = make_store(server)
    for _ in range(2):
        try:
            await store.ensure_collection("tools", 3)
        except VectorStoreError:
            pass
    assert server.statements  # something was recorded, so the check is not vacuous
    assert not any("create extension" in sql.lower() for sql in server.statements)


async def test_a_missing_extension_migrates_nothing_and_a_retry_works_once_it_exists(
    server: FakePgVectorServer,
) -> None:
    server.extension = AVAILABLE
    store = make_store(server)
    with pytest.raises(VectorStoreError):
        await store.ensure_collection("tools", 3)
    assert server.tables == set() and server.schema_versions == {}
    server.extension = "created"  # the administrator ran the one line
    await store.ensure_collection("tools", 3)
    assert server.collections == {"tools": (3, "cosine")}


# --- schema and first use -----------------------------------------------------------


def test_the_migration_is_one_step_of_two_fixed_statements() -> None:
    assert NAMESPACE == "vector"
    assert len(MIGRATIONS) == 1 and len(MIGRATIONS[0]) == 2
    assert MIGRATIONS[0][0].startswith("CREATE TABLE vector_collections (")
    assert MIGRATIONS[0][1].startswith("CREATE TABLE vector_points (")
    assert "embedding vector NOT NULL" in MIGRATIONS[0][1]


async def test_first_use_migrates_once_in_the_vector_namespace(
    server: FakePgVectorServer,
) -> None:
    store = make_store(server)
    await store.ensure_collection("one", 3)
    await store.ensure_collection("two", 4)
    ddl = [sql for sql in server.statements if sql.startswith("CREATE TABLE vector_")]
    assert ddl == list(MIGRATIONS[0])
    assert server.schema_versions == {NAMESPACE: 1}
    versions = [
        params
        for sql, params in server.executed
        if sql.startswith("INSERT INTO schema_versions")
    ]
    assert versions == [(NAMESPACE, 1)]


async def test_a_second_instance_finds_the_schema_and_does_not_repeat_the_ddl(
    server: FakePgVectorServer,
) -> None:
    await make_store(server).ensure_collection("tools", 3)
    await make_store(server).ensure_collection("tools", 3)
    ddl = [sql for sql in server.statements if sql.startswith("CREATE TABLE vector_")]
    assert len(ddl) == 2
    assert server.collections == {"tools": (3, "cosine")}


async def test_a_newer_schema_is_refused_with_our_own_text(
    server: FakePgVectorServer,
) -> None:
    server.schema_versions[NAMESPACE] = 2
    with pytest.raises(VectorStoreError) as failed:
        await make_store(server).ensure_collection("tools", 3)
    assert str(failed.value) == (
        "the vector schema in this database is newer than this version of llm-harness"
    )


async def test_ensure_twice_on_one_instance_connects_once(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    connections = len(server.connections)
    await store.ensure_collection("tools", 3)
    assert len(server.connections) == connections


async def test_a_catalog_row_with_another_shape_is_refused_whoever_wins_the_race(
    server: FakePgVectorServer,
) -> None:
    store = make_store(server)

    def competitor_wins(sql: str, params: Any) -> None:
        # Another process inserts between our check and our insert: ours does nothing.
        if sql.startswith("INSERT INTO vector_collections"):
            server.collections.setdefault("tools", (768, "dot"))

    server.fail_when = competitor_wins
    with pytest.raises(VectorStoreError) as wrong_dim:
        await store.ensure_collection("tools", 1024, "dot")
    assert str(wrong_dim.value) == (
        "collection 'tools' holds 768-dimensional vectors, got 1024; "
        "re-index the collection or use another name"
    )
    with pytest.raises(VectorStoreError) as wrong_distance:
        await store.ensure_collection("tools", 768, "cosine")
    assert str(wrong_distance.value) == (
        "collection 'tools' uses the 'dot' distance, got 'cosine'; "
        "re-index the collection or use another name"
    )
    with pytest.raises(VectorStoreError, match="call ensure_collection first"):
        await store.search("tools", [0.0] * 768, 1)  # a refused ensure caches nothing


async def test_two_instances_ensuring_the_same_collection_leave_one_catalog_row(
    server: FakePgVectorServer,
) -> None:
    first, second = make_store(server), make_store(server)
    await asyncio.gather(
        first.ensure_collection("tools", 3), second.ensure_collection("tools", 3)
    )
    assert server.collections == {"tools": (3, "cosine")}


# --- closing, connections ------------------------------------------------------------


async def test_close_connects_to_nothing_and_later_use_is_refused(
    server: FakePgVectorServer,
) -> None:
    store = make_store(server)
    await store.close()
    await store.close()
    assert server.connections == []
    for call in (
        store.ensure_collection("tools", 3),
        store.upsert("tools", [point("a", [1.0, 0.0, 0.0])]),
        store.search("tools", [1.0, 0.0, 0.0], 1),
        store.delete("tools", ["a"]),
    ):
        with pytest.raises(VectorStoreError, match="the vector store is closed"):
            await call


async def test_every_connection_the_store_opens_is_closed(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    await store.upsert("tools", [point("a", [1.0, 0.0, 0.0])])
    await store.search("tools", [1.0, 0.0, 0.0], 1)
    await store.delete("tools", ["a"])
    assert server.connections and all(c.closes == 1 for c in server.connections)


async def test_empty_upsert_and_delete_do_not_connect(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    connections = len(server.connections)
    await store.upsert("tools", [])
    await store.delete("tools", [])
    assert len(server.connections) == connections


# --- secrets and error text ----------------------------------------------------------


async def test_the_password_travels_only_as_the_password_argument(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server, Secret(PASSWORD)))
    await store.upsert("tools", [point("a", [1.0, 0.0, 0.0], note="x")])
    await store.search("tools", [1.0, 0.0, 0.0], 1, {"note": "x"})
    assert server.calls
    for kwargs in server.calls:
        assert kwargs["password"] == PASSWORD
        assert [key for key, value in kwargs.items() if value == PASSWORD] == [
            "password"
        ]
    assert PASSWORD not in repr(server.executed)
    assert USER not in repr(server.executed) and HOST not in repr(server.executed)


@pytest.mark.parametrize("call", ["ensure", "upsert", "search", "delete"])
async def test_a_server_error_reaches_the_caller_as_collection_and_type_only(
    server: FakePgVectorServer, call: str
) -> None:
    store = await ready(make_store(server, Secret(PASSWORD)))

    def explode(sql: str, params: Any) -> Exception | None:
        if call != "ensure" and not sql.startswith(("SELECT id", "DELETE", "INSERT")):
            return None
        if call == "ensure" and "vector_collections" not in sql:
            return None
        return FakeDatabaseError(SECRET_TEXT, "28P01")

    server.fail_when = explode
    actions = {
        "ensure": lambda: store.ensure_collection("other", 3),
        "upsert": lambda: store.upsert("tools", [point("a", [1.0, 0.0, 0.0])]),
        "search": lambda: store.search("tools", [1.0, 0.0, 0.0], 1),
        "delete": lambda: store.delete("tools", ["a"]),
    }
    with pytest.raises(VectorStoreError) as failed:
        await actions[call]()
    text = str(failed.value)
    name = "other" if call == "ensure" else "tools"
    assert text == f"vector store failed on collection '{name}': FakeDatabaseError"
    for secret in (HOST, USER, PASSWORD, "secret-row-42", "28P01"):
        assert secret not in text
    assert failed.value.__cause__ is None and failed.value.__suppress_context__


async def test_a_refused_connection_is_reported_by_type_only(
    server: FakePgVectorServer,
) -> None:
    server.refuse_connections = ConnectionError(SECRET_TEXT)
    with pytest.raises(VectorStoreError) as failed:
        await make_store(server, Secret(PASSWORD)).ensure_collection("tools", 3)
    assert (
        str(failed.value)
        == "vector store failed on collection 'tools': ConnectionError"
    )


async def test_cancellation_is_not_swallowed(server: FakePgVectorServer) -> None:
    store = await ready(make_store(server))
    task = asyncio.ensure_future(store.search("tools", [1.0, 0.0, 0.0], 1))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_the_guard_lets_cancellation_and_vector_errors_through() -> None:
    with pytest.raises(asyncio.CancelledError), pgvector_module._guard("tools"):
        raise asyncio.CancelledError
    with pytest.raises(KeyboardInterrupt), pgvector_module._guard("tools"):
        raise KeyboardInterrupt
    with (
        pytest.raises(VectorStoreError, match="^mine$"),
        pgvector_module._guard("tools"),
    ):
        raise VectorStoreError("mine")


# --- the transaction -----------------------------------------------------------------


async def test_a_failing_statement_rolls_the_whole_batch_back(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    await store.upsert("tools", [point("old", [1.0, 0.0, 0.0], v=1)])
    inserts = 0

    def fail_second_insert(sql: str, params: Any) -> Exception | None:
        nonlocal inserts
        if sql.startswith("INSERT INTO vector_points"):
            inserts += 1
            if inserts == 2:
                return FakeDatabaseError(SECRET_TEXT, "23505")
        return None

    server.fail_when = fail_second_insert
    with pytest.raises(VectorStoreError) as failed:
        await store.upsert(
            "tools",
            [point("old", [0.0, 1.0, 0.0], v=2), point("x", [0.0, 0.0, 1.0])],
        )
    assert (
        str(failed.value)
        == "vector store failed on collection 'tools': FakeDatabaseError"
    )
    assert server.stored_ids("tools") == ["old"]
    assert server.points["tools", "old"] == ([1.0, 0.0, 0.0], {"v": 1})  # not replaced
    last = server.connections[-1]
    sent = [sql for sql, _ in last.executed]
    assert sent[0] == "BEGIN" and sent[-1] == "ROLLBACK"
    assert last.closes == 1


async def test_a_failing_rollback_does_not_hide_the_original_error(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))

    def fail(sql: str, params: Any) -> Exception | None:
        if sql.startswith("INSERT INTO vector_points"):
            return FakeDatabaseError(SECRET_TEXT, "23505")
        if sql == "ROLLBACK":
            return ConnectionError(SECRET_TEXT)
        return None

    server.fail_when = fail
    with pytest.raises(VectorStoreError) as failed:
        await store.upsert("tools", [point("a", [1.0, 0.0, 0.0])])
    assert str(failed.value).endswith(": FakeDatabaseError")
    assert server.connections[-1].closes == 1


async def test_a_good_batch_is_one_connection_one_transaction(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    before = len(server.connections)
    await store.upsert(
        "tools", [point("a", [1.0, 0.0, 0.0]), point("b", [0.0, 1.0, 0.0])]
    )
    assert len(server.connections) == before + 1
    sql = [text for text, _ in server.connections[-1].executed]
    assert sql[0] == "BEGIN" and sql[-1] == "COMMIT" and len(sql) == 4
    assert server.stored_ids("tools") == ["a", "b"]


async def test_vectors_travel_as_text_and_ids_as_one_list(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    await store.upsert("tools", [point("a", [1.0, 0.5, 1e-07], k="v")])
    insert = [
        p for s, p in server.executed if s.startswith("INSERT INTO vector_points")
    ]
    assert insert == [("tools", "a", "[1.0,0.5,1e-07]", '{"k": "v"}')]
    await store.delete("tools", ["a", "b"])
    assert [p for s, p in server.executed if s.startswith("DELETE")] == [
        ("tools", ["a", "b"])
    ]


# --- scores --------------------------------------------------------------------------

SPIKE = [
    point("a", [1.0, 0.0, 0.0]),
    point("b", [0.9, 0.1, 0.0]),
    point("c", [0.0, 1.0, 0.0]),
]


@pytest.mark.parametrize(
    ("distance", "scores"),
    [
        ("cosine", [1.0, 0.99388, 0.0]),
        ("euclid", [0.0, -0.14142, -1.41421]),
        ("dot", [1.0, 0.9, 0.0]),
    ],
)
async def test_scores_follow_the_spike_for_every_distance(
    server: FakePgVectorServer, distance: str, scores: list[float]
) -> None:
    store = await ready(make_store(server), distance=distance)
    await store.upsert("tools", SPIKE)
    hits = await store.search("tools", [1.0, 0.0, 0.0], 3)
    assert [hit.id for hit in hits] == ["a", "b", "c"]
    assert [hit.score for hit in hits] == pytest.approx(scores, abs=1e-5)


async def test_a_stored_zero_vector_comes_back_last_with_the_lowest_cosine_score(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    # The zero vector goes in first: only the SQL order can put it last.
    await store.upsert("tools", [point("zero", [0.0, 0.0, 0.0])] + SPIKE)
    hits = await store.search("tools", [1.0, 0.0, 0.0], 10)
    assert [hit.id for hit in hits] == ["a", "b", "c", "zero"]
    assert hits[-1].score == -1.0
    assert not any(math.isnan(hit.score) for hit in hits)
    assert [hit.id for hit in await store.search("tools", [1.0, 0.0, 0.0], 3)] == [
        "a",
        "b",
        "c",
    ]


async def test_a_zero_query_vector_ranks_every_hit_last_in_stored_order(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    await store.upsert("tools", SPIKE)
    hits = await store.search("tools", [0.0, 0.0, 0.0], 3)
    assert [hit.id for hit in hits] == ["a", "b", "c"]
    assert {hit.score for hit in hits} == {-1.0}


# --- NaN and infinity -----------------------------------------------------------------

NON_FINITE = [math.nan, math.inf, -math.inf]


@pytest.mark.parametrize("bad", NON_FINITE)
async def test_a_non_finite_stored_vector_refuses_the_whole_batch_before_sending(
    server: FakePgVectorServer, bad: float
) -> None:
    store = await ready(make_store(server))
    connections = len(server.connections)
    with pytest.raises(VectorStoreError, match="a vector may not hold NaN or infinity"):
        await store.upsert(
            "tools", [point("ok", [1.0, 0.0, 0.0]), point("bad", [1.0, bad, 0.0])]
        )
    assert len(server.connections) == connections and server.stored_ids("tools") == []


@pytest.mark.parametrize("bad", NON_FINITE)
async def test_a_non_finite_query_vector_is_refused_before_sending(
    server: FakePgVectorServer, bad: float
) -> None:
    store = await ready(make_store(server))
    connections = len(server.connections)
    with pytest.raises(VectorStoreError, match="a vector may not hold NaN or infinity"):
        await store.search("tools", [1.0, bad, 0.0], 1)
    assert len(server.connections) == connections


@pytest.mark.parametrize("bad", NON_FINITE)
async def test_a_non_finite_payload_float_is_refused_with_a_clear_message(
    server: FakePgVectorServer, bad: float
) -> None:
    store = await ready(make_store(server))
    connections = len(server.connections)
    with pytest.raises(
        VectorStoreError, match="payload value may not be NaN or infinity"
    ):
        await store.upsert("tools", [point("a", [1.0, 0.0, 0.0], weight=bad)])
    assert len(server.connections) == connections


# --- payload and filter ----------------------------------------------------------------


async def test_a_payload_round_trips_with_its_types(server: FakePgVectorServer) -> None:
    store = await ready(make_store(server))
    payload = {"s": "héllo ' \" % ? \\", "i": 3, "f": 0.5, "t": True, "n": False}
    await store.upsert("tools", [VectorPoint("a", [1.0, 0.0, 0.0], payload)])
    (hit,) = await store.search("tools", [1.0, 0.0, 0.0], 1)
    assert hit.payload == payload
    assert type(hit.payload["i"]) is int and type(hit.payload["f"]) is float
    assert hit.payload["t"] is True and hit.payload["n"] is False
    assert hit.id == "a" and "_id" not in hit.payload


async def test_a_filter_is_type_sensitive_and_travels_as_json_text(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    await store.upsert(
        "tools",
        [
            point("int", [1.0, 0.0, 0.0], rank=5),
            point("str", [1.0, 0.0, 0.0], rank="5"),
        ],
    )
    assert [
        h.id for h in await store.search("tools", [1.0, 0.0, 0.0], 5, {"rank": 5})
    ] == ["int"]
    assert [
        h.id for h in await store.search("tools", [1.0, 0.0, 0.0], 5, {"rank": "5"})
    ] == ["str"]
    searches = [
        p for s, p in server.executed if s.startswith("SELECT id, payload::text")
    ]
    assert [p[2] for p in searches] == ['{"rank": 5}', '{"rank": "5"}']
    assert searches[0][-1] == 5  # LIMIT is a parameter too


async def test_a_bool_filter_is_refused_without_connecting(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server))
    connections = len(server.connections)
    with pytest.raises(VectorStoreError, match="'active' with a bool"):
        await store.search("tools", [1.0, 0.0, 0.0], 1, {"active": True})
    assert len(server.connections) == connections


# --- no SQL from user data ---------------------------------------------------------------


async def test_no_user_value_appears_inside_any_sql_text(
    server: FakePgVectorServer,
) -> None:
    hostile_ids = ["it's", 'dq"id', "100%", "what?", "a;DROP TABLE vector_points;--"]
    payload_key = "k'ey%?\"; DROP"
    store = await ready(make_store(server), LONGEST_NAME, 3, "euclid")
    await store.upsert(
        LONGEST_NAME,
        [point(id, [1.0, 0.0, 0.0], **{payload_key: "val'%?"}) for id in hostile_ids],
    )
    hits = await store.search(
        LONGEST_NAME, [1.0, 0.0, 0.0], 10, {payload_key: "val'%?"}
    )
    assert sorted(hit.id for hit in hits) == sorted(hostile_ids)
    await store.delete(LONGEST_NAME, hostile_ids[:2])
    assert server.stored_ids(LONGEST_NAME) == sorted(hostile_ids[2:])

    texts = server.statements
    assert any(LONGEST_NAME in repr(params) for _, params in server.executed)
    for sql in texts:
        assert LONGEST_NAME not in sql
        assert payload_key not in sql and "val'" not in sql
        for id in hostile_ids:
            assert id not in sql


async def test_every_statement_text_is_a_fixed_constant(
    server: FakePgVectorServer,
) -> None:
    store = await ready(make_store(server), "tools", 3, "dot")
    await store.upsert("tools", [point("a", [1.0, 0.0, 0.0], u="x")])
    await store.search("tools", [1.0, 0.0, 0.0], 1, {"u": "x"})
    first = set(server.statements)
    for _ in range(2):  # more calls with other values add no new text
        await store.upsert("tools", [point("b'%?", [0.0, 1.0, 0.0], u="y'")])
        await store.search("tools", [0.0, 1.0, 0.0], 2, {"u": "y'"})
        await store.delete("tools", ["b'%?"])
    assert set(server.statements) - first <= {
        pgvector_module._DELETE_POINTS.replace("?", "%s")
    }
