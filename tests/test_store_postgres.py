"""The usage ledger `Store` on a relational store object (the Postgres path), offline.

A fake relational store with `dialect = "postgres"` and a recording fake connection
stand in for the server; what only a real server can show (identity ids, exact
floats, the advisory lock serialising two writers) is in `tests/test_live_postgres.py`.
The SQLite ledger itself is pinned by `tests/test_store.py`, which stays unchanged.
"""

import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

import llm_harness.store as store_module
from llm_harness.routing import BackoffEntry
from llm_harness.storage.relational import (
    RelationalStoreError,
    SchemaTooNewError,
    SqliteRelationalStore,
)
from llm_harness.store import (
    Store,
    expected_schema_versions,
    optional_schema_versions,
)
from tests.test_store import T0, call, fail

HOST = "db-secret-host.example"
USER = "db-secret-user"
PASSWORD = "sk-test-SECRET-123"
SERVER_TEXT = f"connection to server at {HOST} failed for user {USER}: bad password"
ROW_TEXT = "Failing row contains (1, secret-row-value)"
POSTGRES_NOTICE = (
    "usage database unavailable ({type}): postgres; usage and backoff are not saved"
)

# The SQLite ledger DDL exactly as it shipped before this feature, copied literally so
# a change to the tuple in store.py cannot silently change what this test compares.
OLD_SQLITE_DDL = (
    (
        """CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY,
            time TEXT NOT NULL,
            model_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            input_tokens INTEGER NULL,
            output_tokens INTEGER NULL,
            reasoning_tokens INTEGER NULL,
            cost_usd REAL NULL,
            seconds REAL NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('ok','error','cancelled')),
            error_kind TEXT NULL,
            fallback_from TEXT NULL
        )""",
        """CREATE TABLE model_backoff (
            model_id TEXT PRIMARY KEY,
            failures INTEGER NOT NULL,
            until TEXT NOT NULL
        )""",
    ),
)


class FakeConnection:
    """Records every statement; `rows_for` and `fail_on` script the server's answers."""

    def __init__(
        self,
        rows_for: Callable[[str], list[tuple]],
        fail_on: Callable[[str], Exception | None],
    ) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.closes = 0
        self._rows_for = rows_for
        self._fail_on = fail_on
        self._rows: list[tuple] = []

    def execute(self, sql: str, params: Any = ()) -> "FakeConnection":
        self.executed.append((sql, params))
        failure = self._fail_on(sql)
        if failure is not None:
            raise failure
        self._rows = self._rows_for(sql)
        return self

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def close(self) -> None:
        self.closes += 1

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.executed]


class FakeRelational:
    """The slice of a relational store that `Store` uses, with scripted failures."""

    def __init__(self, dialect: str = "postgres") -> None:
        self.dialect = dialect
        self.label = f"{HOST}:5432/ledger_db"
        self.migrations: list[tuple[str, Any, bool]] = []
        self.connections: list[FakeConnection] = []
        self.migrate_error: Exception | None = None
        self.connect_error: Exception | None = None
        self.rows: Callable[[str], list[tuple]] = lambda sql: []
        self.fail: Callable[[str], Exception | None] = lambda sql: None

    def migrate_sync(
        self, namespace: str, migrations: Any, *, adopt_user_version: bool = False
    ) -> int:
        self.migrations.append((namespace, migrations, adopt_user_version))
        if self.migrate_error is not None:
            raise self.migrate_error
        return len(migrations)

    def connect_sync(self) -> FakeConnection:
        if self.connect_error is not None:
            raise self.connect_error
        connection = FakeConnection(
            lambda sql: self.rows(sql), lambda sql: self.fail(sql)
        )
        self.connections.append(connection)
        return connection


@pytest.fixture
def relational() -> FakeRelational:
    return FakeRelational()


@pytest.fixture
def store(relational: FakeRelational) -> Store:
    return Store(relational=relational)


def column_names(statement: str) -> list[str]:
    body = statement[statement.index("(") + 1 : statement.rindex(")")]
    return [line.split()[0] for line in body.splitlines() if line.strip()]


def table_name(statement: str) -> str:
    match = re.match(r"CREATE TABLE (\w+)", statement)
    assert match
    return match.group(1)


# --- migrations by dialect ---------------------------------------------------------


def test_sqlite_ddl_and_notice_are_unchanged_by_the_postgres_work(tmp_path: Path):
    # Pins that the SQLite statements and the SQLite notice text did not change.
    assert store_module._MIGRATIONS == OLD_SQLITE_DDL
    assert store_module._NAMESPACE == "ledger"
    blocker = tmp_path / "file"
    blocker.write_text("")
    path = blocker / "harness.db"  # its parent is a file: makedirs fails (OSError)
    sqlite_store = Store(path)
    sqlite_store.record_attempt(call(), "keep", attempt_started=T0, is_manual=False)
    assert sqlite_store.pop_notice() == (
        f"usage database unavailable (FileExistsError): {path}; "
        "usage and backoff are not saved"
    )


def test_a_sqlite_dialect_store_gets_the_sqlite_tuple(relational: FakeRelational):
    relational.dialect = "sqlite"
    Store(relational=relational).backoff_entries()
    assert relational.migrations == [("ledger", OLD_SQLITE_DDL, True)]


def test_a_postgres_dialect_store_gets_the_postgres_tuple(relational: FakeRelational):
    Store(relational=relational).backoff_entries()
    assert relational.migrations == [
        ("ledger", store_module._POSTGRES_MIGRATIONS, True)
    ]


def test_an_unknown_dialect_is_refused_with_the_known_ones_named(
    relational: FakeRelational,
):
    relational.dialect = "oracle"
    with pytest.raises(ValueError, match=r"'oracle'.*sqlite, postgres"):
        Store(relational=relational)


def test_the_postgres_tuple_uses_identity_and_double_precision():
    statements = store_module._POSTGRES_MIGRATIONS[0]
    calls_ddl = statements[0]
    assert "id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY" in calls_ddl
    assert "cost_usd DOUBLE PRECISION NULL" in calls_ddl
    assert "seconds DOUBLE PRECISION NOT NULL" in calls_ddl
    for statement in statements:
        assert " REAL" not in statement
        assert "INTEGER PRIMARY KEY" not in statement


def test_both_dialects_create_the_same_tables_and_columns():
    for sqlite_step, postgres_step in zip(
        OLD_SQLITE_DDL, store_module._POSTGRES_MIGRATIONS, strict=True
    ):
        assert [table_name(s) for s in sqlite_step] == [
            table_name(s) for s in postgres_step
        ]
        assert [column_names(s) for s in sqlite_step] == [
            column_names(s) for s in postgres_step
        ]


def test_every_dialect_has_the_same_number_of_steps_so_versions_stay_aligned():
    counts = {
        dialect: len(migrations)
        for dialect, migrations in store_module._MIGRATIONS_BY_DIALECT.items()
    }
    assert set(counts) == {"sqlite", "postgres"}
    assert len(set(counts.values())) == 1
    assert expected_schema_versions() == {"ledger": counts["sqlite"]}
    assert set(optional_schema_versions()) == {"memory", "vector"}


# --- construction ------------------------------------------------------------------


def test_neither_path_nor_relational_is_refused():
    with pytest.raises(ValueError, match="exactly one of `path` or `relational`"):
        Store()


def test_both_path_and_relational_are_refused(
    tmp_path: Path, relational: FakeRelational
):
    with pytest.raises(ValueError, match="exactly one of `path` or `relational`"):
        Store(tmp_path / "x.db", relational=relational)


def test_construction_connects_and_migrates_nothing(relational: FakeRelational):
    Store(relational=relational)
    assert relational.migrations == []
    assert relational.connections == []


def test_a_path_store_and_a_relational_store_both_work(tmp_path: Path):
    path_store = Store(tmp_path / "data" / "harness.db")
    path_store.record_attempt(call(), "fail", attempt_started=T0, is_manual=False)
    assert set(path_store.backoff_entries()) == {"m"}
    relational_store = Store(
        relational=SqliteRelationalStore(tmp_path / "other" / "harness.db")
    )
    relational_store.record_attempt(call(), "fail", attempt_started=T0, is_manual=False)
    assert set(relational_store.backoff_entries()) == {"m"}
    assert path_store.pop_notice() is None
    assert relational_store.pop_notice() is None


# --- statements on the fake connection ---------------------------------------------


def test_record_attempt_fail_runs_begin_insert_backoff_commit_in_order(
    store: Store, relational: FakeRelational
):
    fail(store)
    (connection,) = relational.connections
    kinds = [
        re.match(r"(\w+(?: IMMEDIATE| INTO \w+| FROM \w+)?)", s).group(1)  # type: ignore[union-attr]
        for s in connection.statements
    ]
    assert kinds == [
        "BEGIN IMMEDIATE",
        "INSERT INTO llm_calls",
        "SELECT",
        "INSERT INTO model_backoff",
        "COMMIT",
    ]
    assert (
        "?" in connection.statements[1]
    )  # SQLite placeholders: the adapter translates
    assert connection.closes == 1


def test_record_attempt_reset_deletes_the_backoff_row(
    store: Store, relational: FakeRelational
):
    store.record_attempt(call(), "reset", attempt_started=T0, is_manual=False)
    (connection,) = relational.connections
    assert connection.statements[0] == "BEGIN IMMEDIATE"
    assert connection.statements[1].startswith("INSERT INTO llm_calls")
    assert connection.statements[2] == "DELETE FROM model_backoff WHERE model_id = ?"
    assert connection.statements[3] == "COMMIT"


def test_record_attempt_keep_writes_only_the_call(
    store: Store, relational: FakeRelational
):
    store.record_attempt(call(), "keep", attempt_started=T0, is_manual=False)
    (connection,) = relational.connections
    assert [s.split(" (")[0] for s in connection.statements] == [
        "BEGIN IMMEDIATE",
        "INSERT INTO llm_calls",
        "COMMIT",
    ]


def test_an_error_inside_the_transaction_rolls_back_and_never_commits(
    store: Store, relational: FakeRelational
):
    relational.fail = lambda sql: (
        psycopg.errors.CheckViolation(ROW_TEXT)
        if sql.startswith("INSERT INTO model_backoff")
        else None
    )
    fail(store)
    (connection,) = relational.connections
    assert connection.statements[-1] == "ROLLBACK"
    assert "COMMIT" not in connection.statements
    assert connection.closes == 1
    assert store.pop_notice() == POSTGRES_NOTICE.format(type="CheckViolation")


def test_backoff_entries_reads_rows_into_entries(
    store: Store, relational: FakeRelational
):
    until = T0 + timedelta(minutes=5)
    relational.rows = lambda sql: [("m", 2, until.isoformat())]
    assert store.backoff_entries() == {"m": BackoffEntry("m", 2, until)}
    assert relational.connections[0].statements == [
        "SELECT model_id, failures, until FROM model_backoff"
    ]


def test_the_ledger_is_migrated_once_per_store(
    store: Store, relational: FakeRelational
):
    fail(store)
    fail(store)
    store.backoff_entries()
    assert len(relational.migrations) == 1


# --- errors: the notice never shows host, user, password, server text or a row ------


def assert_secret_free(notice: str) -> None:
    for text in (HOST, USER, PASSWORD, SERVER_TEXT, ROW_TEXT, "bad password"):
        assert text not in notice


FAILURES: list[tuple[str, Callable[[FakeRelational], None]]] = [
    (
        "OperationalError",
        lambda r: setattr(
            r, "connect_error", psycopg.errors.OperationalError(SERVER_TEXT)
        ),
    ),
    (
        "RelationalStoreError",
        lambda r: setattr(r, "connect_error", RelationalStoreError(SERVER_TEXT)),
    ),
    (
        "OperationalError",
        lambda r: setattr(
            r, "migrate_error", psycopg.errors.OperationalError(SERVER_TEXT)
        ),
    ),
    (
        "SchemaTooNewError",
        lambda r: setattr(r, "migrate_error", SchemaTooNewError(SERVER_TEXT)),
    ),
    (
        "UndefinedTable",
        lambda r: setattr(
            r,
            "fail",
            lambda sql: psycopg.errors.UndefinedTable(ROW_TEXT + SERVER_TEXT),
        ),
    ),
]
FAILURE_IDS = ["connect-op", "connect-store", "migrate-op", "too-new", "statement"]


@pytest.mark.parametrize(("type_name", "arm"), FAILURES, ids=FAILURE_IDS)
def test_a_record_attempt_failure_disables_with_a_type_only_postgres_notice(
    relational: FakeRelational,
    type_name: str,
    arm: Callable[[FakeRelational], None],
):
    arm(relational)
    store = Store(relational=relational)
    fail(store)
    notice = store.pop_notice()
    assert notice == POSTGRES_NOTICE.format(type=type_name)
    assert notice is not None
    assert_secret_free(notice)


@pytest.mark.parametrize(("type_name", "arm"), FAILURES, ids=FAILURE_IDS)
def test_a_backoff_entries_failure_disables_with_the_same_notice(
    relational: FakeRelational,
    type_name: str,
    arm: Callable[[FakeRelational], None],
):
    arm(relational)
    store = Store(relational=relational)
    assert store.backoff_entries() == {}
    assert store.pop_notice() == POSTGRES_NOTICE.format(type=type_name)


def test_a_disabled_store_returns_nothing_and_touches_nothing_again(
    relational: FakeRelational,
):
    relational.connect_error = psycopg.errors.OperationalError(SERVER_TEXT)
    store = Store(relational=relational)
    fail(store)
    relational.connect_error = None  # the server is back, but the store stays off
    assert store.backoff_entries() == {}
    fail(store)
    assert relational.connections == []
    assert relational.migrations == [
        ("ledger", store_module._POSTGRES_MIGRATIONS, True)
    ]


def test_the_notice_is_delivered_once(relational: FakeRelational):
    relational.connect_error = RelationalStoreError(SERVER_TEXT)
    store = Store(relational=relational)
    fail(store)
    assert store.pop_notice() is not None
    assert store.pop_notice() is None


def test_an_error_that_is_not_a_database_error_still_propagates(
    relational: FakeRelational,
):
    relational.connect_error = KeyError("bug")
    with pytest.raises(KeyError):
        fail(Store(relational=relational))


def test_the_error_tuple_covers_sqlite_psycopg_and_the_store_errors_and_is_cached():
    errors = store_module._database_errors()
    for expected in (
        psycopg.Error,
        psycopg.errors.OperationalError,
        RelationalStoreError,
        SchemaTooNewError,
        OSError,
        ValueError,
    ):
        assert issubclass(expected, errors)
    import sqlite3

    assert issubclass(sqlite3.OperationalError, errors)
    assert store_module._database_errors() is errors
