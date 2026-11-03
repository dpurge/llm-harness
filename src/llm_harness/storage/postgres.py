"""The relational store on a PostgreSQL server (psycopg 3, synchronous, one connection per call).

Same shape as `SqliteRelationalStore`: async methods run the public `*_sync` methods
in a worker thread, and the ledger (`llm_harness.store`) uses `connect_sync()` with
SQLite-flavoured SQL. The adapter bridges the differences; each was verified in a
spike on PostgreSQL 17.11 with psycopg 3.3.7:

- `?` placeholders fail on psycopg, so they are translated to `%s`.
- A bare `%` is rejected when parameters are passed (even inside quotes), so every
  `%` is escaped as `%%` then; with no parameters `None` is passed and `%` is literal
  (`params=()` would fail).
- `BEGIN IMMEDIATE` is a syntax error. It becomes `BEGIN` plus a transaction-scoped
  advisory lock, which gives SQLite's "one writer at a time": a second connection's
  `pg_try_advisory_xact_lock` was False while it was held and True after `COMMIT`.
- Plain `BEGIN`, `COMMIT` and `ROLLBACK` work on an `autocommit=True` connection, and
  DDL rolls back with them, so `migrate_sync` stays atomic.

The optional extra `postgres` provides psycopg (`uv sync --extra postgres`); this
module imports without it and psycopg is imported at first connect, never at
construction. Construction connects to nothing.

Errors: the driver's own exception types propagate (callers catch
`database_error_classes()`). Their text names host, port and user, and a constraint
violation quotes the failing row, so nothing here prints or logs it, and nothing this
module raises itself contains a url, a host, a user, a password or server text.
"""

import asyncio
import re
from collections.abc import Callable, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from llm_harness.redact import Secret
from llm_harness.storage.relational import (
    Migration,
    RelationalStoreError,
    SchemaTooNewError,
    _check_namespace,
)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_PORT = 5432
# `--storage-status` waits at most this long for the server (less when the url asks for less).
STATUS_CONNECT_TIMEOUT_SECONDS = 5
_READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
_UNDEFINED_TABLE_SQLSTATE = "42P01"

# One fixed key for every advisory lock this module takes. Derivation:
# int.from_bytes(hashlib.sha256(b"llm-harness:relational").digest()[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF
# A literal, so another application sharing the database can be told the key.
ADVISORY_LOCK_KEY = 6678241091312392647

_MISSING_DRIVER = (
    "the postgres backend needs the optional package psycopg: "
    "run `uv sync --extra postgres`"
)
_BEGIN_IMMEDIATE = re.compile(r"\s*BEGIN\s+IMMEDIATE\s*;?\s*", re.IGNORECASE)
_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)"


def is_undefined_table(exc: Exception) -> bool:
    """True for a missing table (SQLSTATE 42P01), read from the attribute psycopg sets."""
    return getattr(exc, "sqlstate", None) == _UNDEFINED_TABLE_SQLSTATE


class PostgresConnectError(RelationalStoreError):
    """The server could not be reached; the text is the driver's exception type only."""

    def __init__(self, cause_type: str) -> None:
        super().__init__(f"cannot connect ({cause_type})")


def translate_sql(
    sql: str, params: Sequence[Any] | None
) -> tuple[str, Sequence[Any] | None]:
    """The SQL and parameters to hand to psycopg for SQLite-flavoured `sql`.

    No parameters: unchanged, with `None`. Parameters: `?` becomes `%s` outside
    single-quoted literals (a doubled `''` is an escaped quote) and double-quoted
    identifiers, and every `%` becomes `%%`. Raises `ValueError` before anything is
    sent when the `?` count differs from `len(params)`.

    Not understood: `--` and `/* */` comments and dollar-quoted strings. The callers'
    SQL is controlled program text, never user input, so this is accepted.
    """
    if not params:
        return sql, None
    out: list[str] = []
    quote: str | None = None
    found = 0
    for char in sql:
        if quote is not None:
            # A doubled quote closes and reopens the literal: the same state.
            if char == quote:
                quote = None
            out.append("%%" if char == "%" else char)
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            found += 1
            out.append("%s")
        else:
            out.append("%%" if char == "%" else char)
    if found != len(params):
        raise ValueError(
            f"the statement has {found} placeholders but {len(params)} parameters were given"
        )
    return "".join(out), tuple(params)


class _Connection:
    """An autocommit psycopg connection that speaks the ledger's SQLite dialect."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self._closed = False

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """Run one statement; returns the psycopg cursor (`fetchone`, `fetchall`, `rowcount`)."""
        if _BEGIN_IMMEDIATE.fullmatch(sql):
            self._raw.execute("BEGIN")
            return self._raw.execute(_LOCK_SQL, (ADVISORY_LOCK_KEY,))
        statement, bound = translate_sql(sql, params)
        return self._raw.execute(statement, bound)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._raw.close()


@dataclass(frozen=True)
class _Target:
    host: str
    port: int | None
    dbname: str
    user: str | None
    sslmode: str | None
    connect_timeout: int

    @property
    def label(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port or DEFAULT_PORT}/{self.dbname}"


def _parse_url(url: str) -> _Target:
    # The config layer already validated the url; this only splits it. The error
    # text carries no part of the url.
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
        query = parse_qs(parts.query)
    except ValueError:
        raise ValueError("the postgres url cannot be parsed") from None
    dbname = unquote(parts.path.lstrip("/"))
    if not host or not dbname:
        raise ValueError("the postgres url needs a host and a database name")
    timeout = query.get("connect_timeout")
    sslmode = query.get("sslmode")
    return _Target(
        host=host,
        port=port,
        dbname=dbname,
        user=unquote(parts.username) if parts.username else None,
        sslmode=sslmode[0] if sslmode else None,
        connect_timeout=int(timeout[0]) if timeout else DEFAULT_CONNECT_TIMEOUT_SECONDS,
    )


def _default_connect(**kwargs: Any) -> Any:
    try:
        import psycopg
    except ImportError:
        raise RelationalStoreError(_MISSING_DRIVER) from None
    return psycopg.connect(**kwargs)


class PostgresRelationalStore:
    """The relational store on one PostgreSQL database. Construction connects to nothing.

    `connect_factory` replaces `psycopg.connect` (tests); it is called with keyword
    arguments only, so the password never travels inside a connection string.
    """

    dialect = "postgres"

    def __init__(
        self,
        url: str,
        password: Secret | None = None,
        *,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._target = _parse_url(url)
        self._password = password
        self._connect_factory = connect_factory

    @property
    def label(self) -> str:
        """`host:port/dbname`: no user name, no password, no query string."""
        return self._target.label

    def __repr__(self) -> str:
        # Neither the url nor the user nor the password.
        return (
            f"PostgresRelationalStore({self.label}, "
            f"password={'set' if self._password else 'none'})"
        )

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await asyncio.to_thread(self.execute_sync, sql, params)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        return await asyncio.to_thread(self.query_sync, sql, params)

    async def migrate(
        self,
        namespace: str,
        migrations: Sequence[Migration],
        *,
        adopt_user_version: bool = False,
    ) -> int:
        return await asyncio.to_thread(
            self.migrate_sync,
            namespace,
            migrations,
            adopt_user_version=adopt_user_version,
        )

    def execute_sync(self, sql: str, params: Sequence[Any] = ()) -> int:
        with closing(self.connect_sync()) as conn:
            return conn.execute(sql, params).rowcount

    def query_sync(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with closing(self.connect_sync()) as conn:
            return conn.execute(sql, params).fetchall()

    def connect_sync(self) -> _Connection:
        """An autocommit connection for callers that need their own transaction."""
        factory = self._connect_factory or _default_connect
        return _Connection(factory(**self._connect_kwargs()))

    def _connect_kwargs(self) -> dict[str, Any]:
        target = self._target
        kwargs: dict[str, Any] = {"host": target.host, "dbname": target.dbname}
        if target.port is not None:
            kwargs["port"] = target.port
        if target.user is not None:
            kwargs["user"] = target.user
        if self._password is not None:
            kwargs["password"] = self._password.reveal()
        if target.sslmode is not None:
            kwargs["sslmode"] = target.sslmode
        kwargs["connect_timeout"] = target.connect_timeout
        kwargs["autocommit"] = True
        return kwargs

    def connect_read_only_sync(self) -> _Connection:
        """A read-only autocommit connection for `--storage-status`.

        The server refuses every write on it, and the connect timeout is at most
        `STATUS_CONNECT_TIMEOUT_SECONDS`. Raises `PostgresConnectError` (type name
        only) when the connection fails.
        """
        kwargs = self._connect_kwargs()
        kwargs["connect_timeout"] = min(
            STATUS_CONNECT_TIMEOUT_SECONDS, kwargs["connect_timeout"]
        )
        kwargs["options"] = _READ_ONLY_OPTIONS
        factory = self._connect_factory or _default_connect
        try:
            return _Connection(factory(**kwargs))
        except Exception as exc:  # noqa: BLE001 - any connect failure, shown by type
            raise PostgresConnectError(type(exc).__name__) from None

    def read_schema_versions_sync(self) -> dict[str, int] | None:
        """The recorded schema versions, or None when `schema_versions` does not exist.

        For `--storage-status`: a read-only session (`connect_read_only_sync`), one
        SELECT, no lock and no DDL. Raises `PostgresConnectError` when the connection
        fails; any other database error propagates as the driver's own type.
        """
        with closing(self.connect_read_only_sync()) as conn:
            try:
                rows = conn.execute(
                    "SELECT namespace, version FROM schema_versions"
                ).fetchall()
            except Exception as exc:
                if is_undefined_table(exc):
                    return None
                raise
        return {namespace: version for namespace, version in rows}

    def migrate_sync(
        self,
        namespace: str,
        migrations: Sequence[Migration],
        *,
        adopt_user_version: bool = False,
    ) -> int:
        """Apply `migrations[recorded version:]` in one transaction.

        `adopt_user_version` is accepted for the protocol and ignored: it exists for
        SQLite files that predate `schema_versions`, and no Postgres database does.
        """
        _check_namespace(namespace)
        with closing(self.connect_sync()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = self._apply(conn, namespace, migrations)
            except BaseException:
                # A dead connection cannot roll back; closing it makes the server
                # abort the transaction, and the original failure is what matters.
                with suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return version

    @staticmethod
    def _apply(
        conn: _Connection, namespace: str, migrations: Sequence[Migration]
    ) -> int:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_versions "
            "(namespace TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        # Read inside the transaction, after the advisory lock: a concurrent migrator
        # may have finished while we waited, and then this call is a no-op.
        row = conn.execute(
            "SELECT version FROM schema_versions WHERE namespace = ?", (namespace,)
        ).fetchone()
        version = row[0] if row is not None else 0
        if version > len(migrations):
            raise SchemaTooNewError(
                f"namespace {namespace!r} is at version {version}, "
                f"this program knows {len(migrations)}"
            )
        for steps in migrations[version:]:
            for statement in steps:
                conn.execute(statement)
        if row is None or version < len(migrations):
            conn.execute(
                "INSERT INTO schema_versions (namespace, version) VALUES (?, ?) "
                "ON CONFLICT(namespace) DO UPDATE SET version = excluded.version",
                (namespace, len(migrations)),
            )
        return len(migrations)
