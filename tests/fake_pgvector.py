"""An in-memory stand-in server for `connect_factory` of `PgVectorStore`, for offline tests.

It implements exactly the statements of `src/llm_harness/storage/pgvector.py`,
dispatching on that module's own statement constants by exact text (after the `?` to
`%s` translation the connection layer applies), and the migration statements of
`PostgresRelationalStore.migrate_sync`. A statement it does not know raises
`UnknownStatement`, which fails the test loudly.

It encodes the facts of the spike on pgvector 0.8.7 / PostgreSQL 17.11 (specs/features/
storage-backends-pgvector.md, Approach step 1): exact cosine, euclid and NEGATIVE
inner-product distances, a zero vector with cosine gives NaN and NaN sorts last, NaN is
refused in a vector (22000), different dimensions are refused (22000), `@>` containment
is type-sensitive with several keys as an AND, `ANY(list)` takes a Python list, the
`vector` type fails with 42704 when the extension is not created, and BEGIN, COMMIT
and ROLLBACK have real transaction semantics (DDL included).

It is a test double: the live tests of step 5 are what check it against a real server.
They found three differences that the stand-in does not model, none of which the offline
tests depend on: `payload::text` is jsonb text (keys sorted by length then bytes, numbers
as exact decimals, so 1e22 comes back as the int 10**22), row locks (two writers listing
the same ids in opposite order deadlock unless the batch is sent in id order) and the
float32 range of a vector value (1e39 is refused with `NumericValueOutOfRange`).
"""

import copy
import json
import math
from collections.abc import Callable, Sequence
from typing import Any

import llm_harness.storage.pgvector as pgvector_module
from tests.test_storage_postgres import (
    CREATE_VERSIONS,
    LOCK,
    SELECT_VERSION,
    UPSERT,
)

# What the server says about the extension.
CREATED = "created"
AVAILABLE = "available"  # in pg_available_extensions, not created in this database
MISSING = "missing"  # the server has no pgvector package


class UnknownStatement(AssertionError):
    """The store sent a statement this stand-in does not implement."""


class FakeDatabaseError(Exception):
    """A server error; `sqlstate` is what psycopg's errors carry."""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class FakeCursor:
    def __init__(self, rows: list[tuple] | None = None, rowcount: int = -1) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def close(self) -> None:
        pass


def _translated(statement: str) -> str:
    # The statement constants hold no quoted `?` and no `%`.
    return statement.replace("?", "%s")


def parse_vector(text: str) -> list[float]:
    """pgvector's input function: `[1.0,2.0]`, refusing NaN and infinity (22000)."""
    values = [float(part) for part in text.strip("[]").split(",")]
    if not all(math.isfinite(value) for value in values):
        raise FakeDatabaseError("NaN not allowed in vector", "22000")
    return values


def _distance(operator: str, query: list[float], stored: list[float]) -> float:
    if len(query) != len(stored):
        raise FakeDatabaseError(
            f"different vector dimensions {len(query)} and {len(stored)}", "22000"
        )
    dot = sum(a * b for a, b in zip(query, stored, strict=True))
    if operator == "<#>":
        return -dot
    if operator == "<->":
        return math.dist(query, stored)
    norms = math.hypot(*query) * math.hypot(*stored)
    return 1.0 - dot / norms if norms else math.nan  # a zero vector: NaN, as verified


def _same_json(a: Any, b: Any) -> bool:
    """jsonb equality: type-sensitive, numbers compare by value (never a bool)."""
    a_bool, b_bool = isinstance(a, bool), isinstance(b, bool)
    if a_bool or b_bool:
        return a_bool and b_bool and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def _contains(payload: dict[str, Any], wanted: dict[str, Any]) -> bool:
    return all(
        key in payload and _same_json(payload[key], value)
        for key, value in wanted.items()
    )


def _sort_key(distance: float) -> tuple[bool, float]:
    return (math.isnan(distance), 0.0 if math.isnan(distance) else distance)


class FakePgVectorServer:
    """Shared state: a second store sees what an earlier one stored, like a real server.

    The instance is the `connect_factory`. Tests script it through `extension`,
    `fail_when` (called with the translated statement and its parameters; return an
    exception to raise it) and `refuse_connections`.
    """

    def __init__(self, extension: str = CREATED, version: str = "0.8.7") -> None:
        self.extension = extension
        self.extension_version = version
        self.schema_versions: dict[str, int] = {}
        self.tables: set[str] = set()
        self.collections: dict[str, tuple[int, str]] = {}
        self.points: dict[tuple[str, str], tuple[list[float], dict[str, Any]]] = {}
        self.calls: list[dict[str, Any]] = []
        self.connections: list[FakeConnection] = []
        self.fail_when: Callable[[str, Any], Exception | None] | None = None
        self.refuse_connections: Exception | None = None
        self._handlers = self._build_handlers()

    def __call__(self, **kwargs: Any) -> "FakeConnection":
        self.calls.append(kwargs)
        if self.refuse_connections is not None:
            raise self.refuse_connections
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    @property
    def statements(self) -> list[str]:
        return [sql for conn in self.connections for sql, _ in conn.executed]

    @property
    def executed(self) -> list[tuple[str, Any]]:
        return [entry for conn in self.connections for entry in conn.executed]

    def stored_ids(self, collection: str) -> list[str]:
        return sorted(id for coll, id in self.points if coll == collection)

    def snapshot(self) -> tuple:
        return copy.deepcopy(
            (self.schema_versions, self.tables, self.collections, self.points)
        )

    def restore(self, state: tuple) -> None:
        self.schema_versions, self.tables, self.collections, self.points = state

    def _build_handlers(self) -> dict[str, Callable[..., FakeCursor]]:
        module = pgvector_module
        handlers: dict[str, Callable[..., FakeCursor]] = {
            LOCK: lambda conn, params: FakeCursor([(None,)]),
            CREATE_VERSIONS.replace("?", "%s"): self._create_versions,
            SELECT_VERSION: self._select_version,
            UPSERT: self._upsert_version,
            module._CREATE_COLLECTIONS: self._create_collections,
            module._CREATE_POINTS: self._create_points,
            module._EXTENSION_VERSION: self._extension_version,
            module._EXTENSION_AVAILABLE: self._extension_available,
            module._SELECT_COLLECTIONS: self._select_collections,
            _translated(module._INSERT_COLLECTION): self._insert_collection,
            _translated(module._SELECT_COLLECTION): self._select_collection,
            _translated(module._UPSERT_POINT): self._upsert_point,
            _translated(module._DELETE_POINTS): self._delete_points,
        }
        for (distance, filtered), statement in module._SEARCH.items():
            operator = module._OPERATORS[distance]
            handlers[_translated(statement)] = (
                lambda conn, params, op=operator, flt=filtered: self._search(
                    op, flt, params
                )
            )
        return handlers

    def handle(self, conn: "FakeConnection", sql: str, params: Any) -> FakeCursor:
        if self.fail_when is not None:
            failure = self.fail_when(sql, params)
            if failure is not None:
                raise failure
        if sql == "BEGIN":
            conn.transaction_state = self.snapshot()
            return FakeCursor()
        if sql == "COMMIT":
            conn.transaction_state = None
            return FakeCursor()
        if sql == "ROLLBACK":
            if conn.transaction_state is not None:
                self.restore(conn.transaction_state)
                conn.transaction_state = None
            return FakeCursor()
        handler = self._handlers.get(sql)
        if handler is None:
            raise UnknownStatement(f"the fake server does not know: {sql!r}")
        return handler(conn, params)

    # --- migrations ---------------------------------------------------------------

    def _create_versions(self, conn: Any, params: Any) -> FakeCursor:
        self.tables.add("schema_versions")
        return FakeCursor()

    def _select_version(self, conn: Any, params: Any) -> FakeCursor:
        version = self.schema_versions.get(params[0])
        return FakeCursor([] if version is None else [(version,)])

    def _upsert_version(self, conn: Any, params: Any) -> FakeCursor:
        self.schema_versions[params[0]] = params[1]
        return FakeCursor(rowcount=1)

    def _create_collections(self, conn: Any, params: Any) -> FakeCursor:
        self._create_table("vector_collections")
        return FakeCursor()

    def _create_points(self, conn: Any, params: Any) -> FakeCursor:
        if self.extension != CREATED:
            raise FakeDatabaseError('type "vector" does not exist', "42704")
        self._create_table("vector_points")
        return FakeCursor()

    def _create_table(self, name: str) -> None:
        if name in self.tables:
            raise FakeDatabaseError(f'relation "{name}" already exists', "42P07")
        self.tables.add(name)

    def _require(self, table: str) -> None:
        if table not in self.tables:
            raise FakeDatabaseError(f'relation "{table}" does not exist', "42P01")

    # --- the extension ------------------------------------------------------------

    def _extension_version(self, conn: Any, params: Any) -> FakeCursor:
        return FakeCursor(
            [(self.extension_version,)] if self.extension == CREATED else []
        )

    def _extension_available(self, conn: Any, params: Any) -> FakeCursor:
        return FakeCursor([(1,)] if self.extension in (CREATED, AVAILABLE) else [])

    # --- catalog and points -------------------------------------------------------

    def _insert_collection(self, conn: Any, params: Any) -> FakeCursor:
        self._require("vector_collections")
        name, dim, distance = params
        if name in self.collections:  # ON CONFLICT (name) DO NOTHING
            return FakeCursor(rowcount=0)
        self.collections[name] = (dim, distance)
        return FakeCursor(rowcount=1)

    def _select_collection(self, conn: Any, params: Any) -> FakeCursor:
        self._require("vector_collections")
        held = self.collections.get(params[0])
        return FakeCursor([] if held is None else [held])

    def _select_collections(self, conn: Any, params: Any) -> FakeCursor:
        self._require("vector_collections")
        return FakeCursor(
            [
                (name, dim, dist)
                for name, (dim, dist) in sorted(self.collections.items())
            ]
        )

    def _upsert_point(self, conn: Any, params: Any) -> FakeCursor:
        self._require("vector_points")
        collection, id, vector_text, payload_text = params
        # ON CONFLICT (collection, id) DO UPDATE: one row per id, the latest wins.
        self.points[collection, id] = (
            parse_vector(vector_text),
            json.loads(payload_text),
        )
        return FakeCursor(rowcount=1)

    def _delete_points(self, conn: Any, params: Any) -> FakeCursor:
        self._require("vector_points")
        collection, ids = params
        assert isinstance(ids, list), "ANY(?) takes the ids as one list parameter"
        doomed = [(collection, id) for id in ids if (collection, id) in self.points]
        for key in doomed:
            del self.points[key]
        return FakeCursor(rowcount=len(doomed))

    def _search(
        self, operator: str, filtered: bool, params: Sequence[Any]
    ) -> FakeCursor:
        self._require("vector_points")
        query = parse_vector(params[0])
        collection = params[1]
        wanted = json.loads(params[2]) if filtered else {}
        limit = params[-1]
        scored = [
            (_distance(operator, query, vector), id, payload)
            for (coll, id), (vector, payload) in self.points.items()
            if coll == collection and _contains(payload, wanted)
        ]
        scored.sort(
            key=lambda item: _sort_key(item[0])
        )  # stable; NaN last, as PostgreSQL
        # `payload::text`: the payload comes back as a string, not a parsed value.
        return FakeCursor(
            [(id, json.dumps(payload), d) for d, id, payload in scored[:limit]]
        )


class FakeConnection:
    """Records every statement with its parameters; closes are counted."""

    def __init__(self, server: FakePgVectorServer) -> None:
        self._server = server
        self.executed: list[tuple[str, Any]] = []
        self.closes = 0
        self.transaction_state: tuple | None = None

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self.executed.append((sql, params))
        return self._server.handle(self, sql, params)

    def close(self) -> None:
        self.closes += 1
        # A connection that goes away mid-transaction aborts it, as on a real server.
        if self.transaction_state is not None:
            self._server.restore(self.transaction_state)
            self.transaction_state = None
