"""The relational store: an async interface, its SQLite backend and per-namespace schema versions.

Each feature owns a namespace and a list of migrations; `migrate` brings that
namespace's tables up to date and records the version in `schema_versions`, so
features evolve independently in one database file. Convention (not enforced): a
namespace's tables are prefixed `<namespace>_`. The one exception is the
grandfathered `ledger` namespace, whose tables are `llm_calls` and `model_backoff`.

The SQLite backend opens one connection per call, so the async methods run the
blocking `*_sync` methods in a worker thread without sharing a connection. The
`*_sync` methods are public for synchronous callers such as the ledger in
`llm_harness.store`. Construction touches nothing; the directory (0700) and the
file (0600) are created on first use.
"""

import asyncio
import os
import re
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# One tuple of single statements per version step: `Cursor.execute` runs one
# statement, and `executescript` would end our transaction.
Migration = tuple[str, ...]

_NAMESPACE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_BUSY_TIMEOUT_SECONDS = 5


class SchemaTooNewError(Exception):
    """The database was written by a newer version of this program."""


@dataclass(frozen=True)
class ReadOnlySchema:
    """What a database file records about its schema, read without changing it."""

    # None when the file has no `schema_versions` table (it predates the table, or
    # nothing was migrated yet); then only `user_version` says anything.
    versions: dict[str, int] | None
    user_version: int


def read_schema_versions(path: Path) -> ReadOnlySchema:
    """Read the recorded schema versions of an existing file; never creates or writes.

    Raises `sqlite3.Error` when the file is not a readable SQLite database. This
    does not go through `SqliteRelationalStore`, which creates and migrates.
    """
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_versions'"
        ).fetchone()
        versions = (
            dict(conn.execute("SELECT namespace, version FROM schema_versions"))
            if has_table
            else None
        )
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    return ReadOnlySchema(versions=versions, user_version=user_version)


class RelationalStore(Protocol):
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one statement with bound parameters; returns the affected row count."""
        ...

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]: ...

    async def migrate(
        self,
        namespace: str,
        migrations: Sequence[Migration],
        *,
        adopt_user_version: bool = False,
    ) -> int:
        """Apply `migrations[recorded version:]` atomically; returns the resulting version."""
        ...


def _check_namespace(namespace: str) -> None:
    if not _NAMESPACE_PATTERN.fullmatch(namespace):
        raise ValueError(
            f"invalid namespace {namespace!r}: use lowercase letters, digits and "
            "underscores, starting with a letter, at most 64 characters"
        )


class SqliteRelationalStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()  # guards `_prepared`
        self._prepared = False

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

    def connect_sync(self) -> sqlite3.Connection:
        """An autocommit connection for callers that need their own transaction."""
        self._prepare_files()
        return sqlite3.connect(
            self._path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None
        )

    def migrate_sync(
        self,
        namespace: str,
        migrations: Sequence[Migration],
        *,
        adopt_user_version: bool = False,
    ) -> int:
        _check_namespace(namespace)
        with closing(self.connect_sync()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = self._apply(conn, namespace, migrations, adopt_user_version)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return version

    @staticmethod
    def _apply(
        conn: sqlite3.Connection,
        namespace: str,
        migrations: Sequence[Migration],
        adopt_user_version: bool,
    ) -> int:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_versions "
            "(namespace TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        # Read inside the transaction: a concurrent migrator may have finished
        # while we waited for the write lock, and then this call is a no-op.
        row = conn.execute(
            "SELECT version FROM schema_versions WHERE namespace = ?", (namespace,)
        ).fetchone()
        if row is not None:
            version = row[0]
        elif adopt_user_version:
            # Read only: an older build keeps opening the file by its user_version.
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        else:
            version = 0
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

    def _prepare_files(self) -> None:
        with self._lock:
            if self._prepared:
                return
        directory = self._path.parent
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        # Create the file ourselves so it never exists with a looser mode.
        os.close(os.open(self._path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(self._path, 0o600)
        with self._lock:
            self._prepared = True
