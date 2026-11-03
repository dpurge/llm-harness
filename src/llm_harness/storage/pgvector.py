"""The vector store backend on PostgreSQL with the pgvector extension.

Mirrors the observable contract of `qdrant_edge` and `qdrant_remote`: the same
refusals and messages, scores where higher is nearer. It is built on a
`PostgresRelationalStore` for connections (composition: `connect_sync`,
`migrate_sync`, `query_sync`, `execute_sync`), so psycopg is imported by that layer at
first connect, never here and never at construction. Construction connects to nothing.

Verified in the spike on pgvector 0.8.7 / PostgreSQL 17.11 / psycopg 3.3.7
(specs/features/storage-backends-pgvector.md, Approach step 1):

- One table for every collection, with an UNCONSTRAINED `vector` column: collections
  of different dimensions live side by side, and no SQL identifier is ever built from
  user input (a per-collection table name would be truncated at 63 characters).
- Vectors travel as text (`'[1.0,0.0]'::vector`), so the `pgvector` Python package is
  not needed. The original point id is stored as given: no uuid mapping.
- Distance operators `<=>` (cosine), `<->` (euclid) and `<#>` (the NEGATIVE inner
  product). Scores: cosine `1 - d`, euclid `-d`, dot `-d`. A zero vector with cosine
  gives a NaN distance, which PostgreSQL sorts last.
- `payload @> '{"k": v}'::jsonb` is type-sensitive and several keys are an AND.
- pgvector refuses NaN in a vector (22000); the program refuses NaN and infinity
  first, with its own message.

The program NEVER creates the extension: it only checks for it (an administrator creates
it). Every statement text below is a module constant and every user
value (collection, ids, vectors, payload, filter, k) is a parameter.

Error text names the collection and the exception type, never the server's text:
psycopg messages carry host, user and, for constraint violations, the failing row.
"""

import asyncio
import contextlib
import json
import math
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from llm_harness.redact import Secret
from llm_harness.storage.postgres import PostgresRelationalStore, is_undefined_table
from llm_harness.storage.relational import RelationalStoreError, SchemaTooNewError
from llm_harness.storage.vector_common import (
    ORIGINAL_ID_KEY,
    check_name,
    checked_filter,
)
from llm_harness.storage.vectors import (
    DISTANCES,
    VectorHit,
    VectorPoint,
    VectorStoreError,
)

NAMESPACE = "vector"

_CREATE_COLLECTIONS = (
    "CREATE TABLE vector_collections "
    "(name TEXT PRIMARY KEY, dim INTEGER NOT NULL, distance TEXT NOT NULL)"
)
_CREATE_POINTS = (
    "CREATE TABLE vector_points "
    "(collection TEXT NOT NULL, id TEXT NOT NULL, embedding vector NOT NULL, "
    "payload JSONB NOT NULL, PRIMARY KEY (collection, id))"
)
MIGRATIONS = ((_CREATE_COLLECTIONS, _CREATE_POINTS),)

_EXTENSION_VERSION = "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
_EXTENSION_AVAILABLE = "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
_INSERT_COLLECTION = (
    "INSERT INTO vector_collections (name, dim, distance) VALUES (?, ?, ?) "
    "ON CONFLICT (name) DO NOTHING"
)
_SELECT_COLLECTION = "SELECT dim, distance FROM vector_collections WHERE name = ?"
_UPSERT_POINT = (
    "INSERT INTO vector_points (collection, id, embedding, payload) "
    "VALUES (?, ?, ?::vector, ?::jsonb) "
    "ON CONFLICT (collection, id) DO UPDATE "
    "SET embedding = EXCLUDED.embedding, payload = EXCLUDED.payload"
)
_SELECT_COLLECTIONS = "SELECT name, dim, distance FROM vector_collections ORDER BY name"
_DELETE_POINTS = "DELETE FROM vector_points WHERE collection = ? AND id = ANY(?)"

# Built once at import from fixed operator strings, never from user input. The
# placeholders are, in order: query vector, collection, [filter], limit.
_OPERATORS = {"cosine": "<=>", "euclid": "<->", "dot": "<#>"}
_SEARCH = {
    (distance, filtered): (
        f"SELECT id, payload::text, embedding {operator} ?::vector "
        "FROM vector_points WHERE collection = ?"
        f"{' AND payload @> ?::jsonb' if filtered else ''} "
        "ORDER BY 3 ASC LIMIT ?"
    )
    for distance, operator in _OPERATORS.items()
    for filtered in (False, True)
}

# The only place the words appear: a test checks that no statement contains them.
_NOT_INSTALLED = (
    "the pgvector extension is not installed on this PostgreSQL server: install the "
    "pgvector package on the server, then an administrator runs: CREATE EXTENSION vector;"
)
_NOT_CREATED = (
    "the pgvector extension is not created in this database: an administrator must "
    "run, as a superuser in the database named in [storage.vector] url: "
    "CREATE EXTENSION vector;"
)
_SCHEMA_TOO_NEW = (
    "the vector schema in this database is newer than this version of llm-harness"
)
# PostgreSQL sorts a NaN distance last; this is the lowest cosine score, so the hit
# stays last when callers sort by score.
_NAN_COSINE_SCORE = -1.0


@dataclass(frozen=True)
class PgVectorStatus:
    """What `--storage-status` shows. `collections` is None when the catalog table does
    not exist, and also when the extension is absent (the catalog is not looked at)."""

    extension_version: str | None
    extension_available: bool
    collections: list[tuple[str, int, str]] | None


def _unknown_collection(name: str) -> VectorStoreError:
    return VectorStoreError(
        f"unknown collection {name!r}: call ensure_collection first"
    )


@contextlib.contextmanager
def _guard(name: str) -> Iterator[None]:
    try:
        yield
    except VectorStoreError:
        raise
    except RelationalStoreError as exc:
        # Raised by the connection layer itself: its text is safe by construction.
        raise VectorStoreError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - driver errors have no common class
        # Exception, not BaseException: cancellation and Ctrl-C pass through.
        raise VectorStoreError(
            f"vector store failed on collection {name!r}: {type(exc).__name__}"
        ) from None


def _score(distance: str, value: float) -> float:
    if math.isnan(value):
        return _NAN_COSINE_SCORE if distance == "cosine" else -math.inf
    return 1.0 - value if distance == "cosine" else -value


def _vector_text(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


class PgVectorStore:
    """Collections in one PostgreSQL database. Construction connects to nothing.

    After `close()` every call raises `VectorStoreError`, like the other backends.
    Dimension and distance are cached by `ensure_collection`, so a wrong vector length
    is refused before it is sent and a collection not ensured in this process is
    "unknown". There is one connection per call; `close()` releases nothing.
    """

    def __init__(
        self,
        url: str,
        password: Secret | None = None,
        *,
        connect_factory: Any = None,
    ) -> None:
        self._relational = PostgresRelationalStore(
            url, password, connect_factory=connect_factory
        )
        self._has_password = password is not None
        self._collections: dict[str, tuple[int, str]] = {}
        self._closed = False
        self._prepared = False
        self._prepare_lock = threading.Lock()

    @property
    def label(self) -> str:
        """`host:port/dbname`: no user name, no password, no query string."""
        return self._relational.label

    def __repr__(self) -> str:
        # Neither the url nor the user nor the password.
        return (
            f"PgVectorStore({self.label}, "
            f"password={'set' if self._has_password else 'none'})"
        )

    async def ensure_collection(
        self, name: str, dim: int, distance: str = "cosine"
    ) -> None:
        check_name(name)
        if isinstance(dim, bool) or not isinstance(dim, int) or dim < 1:
            raise VectorStoreError(f"the dimension must be a positive int, got {dim!r}")
        if distance not in DISTANCES:
            raise VectorStoreError(
                f"unknown distance {distance!r}: use one of {', '.join(DISTANCES)}"
            )
        self._check_open()
        held = self._collections.get(name)
        if held is not None:
            self._check_matches(name, held[0], held[1], dim, distance)
            return
        with _guard(name):
            await asyncio.to_thread(self._ensure_sync, name, dim, distance)
        self._collections[name] = (dim, distance)

    async def upsert(self, collection: str, points: Sequence[VectorPoint]) -> None:
        check_name(collection)
        dim, _ = self._known(collection)
        with _guard(collection):
            # Every point is checked before anything is sent: a batch is written
            # whole or not at all (and the write itself is one transaction).
            rows = [self._row(collection, dim, point) for point in points]
            if not rows:
                return
            # Always lock rows in the same (id) order: two writers that list the same
            # ids in opposite order would otherwise deadlock (found on a real server).
            # The sort is stable, so for a repeated id the last point still wins.
            rows.sort(key=lambda row: row[1])
            await asyncio.to_thread(self._upsert_sync, rows)

    async def search(
        self,
        collection: str,
        vector: Sequence[float],
        k: int,
        filter: Mapping[str, str | int | bool] | None = None,
    ) -> list[VectorHit]:
        check_name(collection)
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise VectorStoreError(f"k must be a positive int, got {k!r}")
        dim, distance = self._known(collection)
        self._check_vector(collection, dim, vector)
        conditions = checked_filter(filter)
        with _guard(collection):
            self._check_finite(collection, vector)
            statement = _SEARCH[distance, bool(conditions)]
            params: list[Any] = [_vector_text(vector), collection]
            if conditions:
                params.append(json.dumps(dict(conditions)))
            params.append(k)
            rows = await asyncio.to_thread(
                self._relational.query_sync, statement, params
            )
        # The SQL order stands: no re-sort, so a NaN hit stays last.
        return [
            VectorHit(
                id=row_id, score=_score(distance, value), payload=json.loads(text)
            )
            for row_id, text, value in rows
        ]

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        check_name(collection)
        self._known(collection)
        if not ids:
            return
        with _guard(collection):
            await asyncio.to_thread(
                self._relational.execute_sync, _DELETE_POINTS, (collection, list(ids))
            )

    async def close(self) -> None:
        self._closed = True
        self._collections = {}

    def read_status_sync(self) -> PgVectorStatus:
        """The extension state and the catalog, for `--storage-status`.

        A read-only connection (`connect_read_only_sync`, connect timeout at most 5 s)
        and SELECTs only: no lock, no BEGIN, no DDL. Raises `PostgresConnectError` when
        the connection fails; any other database error propagates as the driver's type.
        """
        with closing(self._relational.connect_read_only_sync()) as conn:
            row = conn.execute(_EXTENSION_VERSION).fetchone()
            if row is None:
                available = conn.execute(_EXTENSION_AVAILABLE).fetchone() is not None
                return PgVectorStatus(None, available, None)
            try:
                rows = conn.execute(_SELECT_COLLECTIONS).fetchall()
            except Exception as exc:
                if not is_undefined_table(exc):
                    raise
                return PgVectorStatus(row[0], True, None)
            return PgVectorStatus(
                row[0], True, [(name, dim, distance) for name, dim, distance in rows]
            )

    def _check_open(self) -> None:
        if self._closed:
            raise VectorStoreError("the vector store is closed")

    def _known(self, name: str) -> tuple[int, str]:
        self._check_open()
        known = self._collections.get(name)
        if known is None:
            raise _unknown_collection(name)
        return known

    @staticmethod
    def _check_matches(
        name: str, stored_dim: int, stored_distance: str, dim: int, distance: str
    ) -> None:
        if stored_dim != dim:
            raise VectorStoreError(
                f"collection {name!r} holds {stored_dim}-dimensional vectors, got {dim}; "
                "re-index the collection or use another name"
            )
        if stored_distance != distance:
            raise VectorStoreError(
                f"collection {name!r} uses the {stored_distance!r} distance, got {distance!r}; "
                "re-index the collection or use another name"
            )

    @staticmethod
    def _check_vector(name: str, dim: int, vector: Sequence[float]) -> None:
        if len(vector) != dim:
            raise VectorStoreError(
                f"collection {name!r} holds {dim}-dimensional vectors, "
                f"got a vector of {len(vector)}"
            )

    @staticmethod
    def _check_finite(name: str, vector: Sequence[float]) -> None:
        if not all(math.isfinite(value) for value in vector):
            raise VectorStoreError(
                f"collection {name!r}: a vector may not hold NaN or infinity"
            )

    def _row(
        self, name: str, dim: int, point: VectorPoint
    ) -> tuple[str, str, str, str]:
        self._check_vector(name, dim, point.vector)
        self._check_finite(name, point.vector)
        if ORIGINAL_ID_KEY in point.payload:
            raise VectorStoreError(
                f"the payload key {ORIGINAL_ID_KEY!r} is reserved for the point id"
            )
        try:
            payload = json.dumps(dict(point.payload), allow_nan=False)
        except ValueError:
            raise VectorStoreError(
                f"collection {name!r}: a payload value may not be NaN or infinity"
            ) from None
        return (name, point.id, _vector_text(point.vector), payload)

    def _prepare(self) -> None:
        """Check the extension and migrate, once per instance (a repeat is harmless)."""
        with self._prepare_lock:
            if self._prepared:
                return
            self._check_extension()
            try:
                self._relational.migrate_sync(NAMESPACE, MIGRATIONS)
            except SchemaTooNewError:
                raise VectorStoreError(_SCHEMA_TOO_NEW) from None
            self._prepared = True

    def _check_extension(self) -> None:
        with closing(self._relational.connect_sync()) as conn:
            if conn.execute(_EXTENSION_VERSION).fetchone() is not None:
                return
            available = conn.execute(_EXTENSION_AVAILABLE).fetchone() is not None
        raise VectorStoreError(_NOT_CREATED if available else _NOT_INSTALLED)

    def _ensure_sync(self, name: str, dim: int, distance: str) -> None:
        self._prepare()
        with closing(self._relational.connect_sync()) as conn:
            # The insert wins once, whoever races: everyone then compares with the row.
            conn.execute(_INSERT_COLLECTION, (name, dim, distance))
            stored_dim, stored_distance = conn.execute(
                _SELECT_COLLECTION, (name,)
            ).fetchone()
        self._check_matches(name, stored_dim, stored_distance, dim, distance)

    def _upsert_sync(self, rows: Sequence[tuple[str, str, str, str]]) -> None:
        with closing(self._relational.connect_sync()) as conn:
            conn.execute("BEGIN")
            try:
                for row in rows:
                    conn.execute(_UPSERT_POINT, row)
            except BaseException:
                # A dead connection cannot roll back; closing it makes the server
                # abort the transaction, and the original failure is what matters.
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
