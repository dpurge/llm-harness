"""`PostgresRelationalStore` on a fake psycopg connection: no server, no socket.

What only a real server can show (the advisory lock serialising writers, DDL rollback,
exact floats) is in `tests/test_live_postgres.py`.
"""

import asyncio
import hashlib
import re
import sqlite3
import sys
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from llm_harness.redact import Secret
from llm_harness.storage.postgres import (
    ADVISORY_LOCK_KEY,
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    PostgresRelationalStore,
    translate_sql,
)
from llm_harness.storage.relational import (
    RelationalStoreError,
    SchemaTooNewError,
    SqliteRelationalStore,
    database_error_classes,
)
from llm_harness.store import _MIGRATIONS

USER = "db-secret-user"
PASSWORD = "sk-test-SECRET-123"
HOST = "db-secret-host.example"
URL = f"postgresql://{USER}@{HOST}:5432/ledger_db"
LOCK = "SELECT pg_advisory_xact_lock(%s)"
CREATE_VERSIONS = (
    "CREATE TABLE IF NOT EXISTS schema_versions "
    "(namespace TEXT PRIMARY KEY, version INTEGER NOT NULL)"
)
SELECT_VERSION = "SELECT version FROM schema_versions WHERE namespace = %s"
UPSERT = (
    "INSERT INTO schema_versions (namespace, version) VALUES (%s, %s) "
    "ON CONFLICT(namespace) DO UPDATE SET version = excluded.version"
)


class FakeCursor:
    def __init__(self, rows: list[tuple], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


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

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self.executed.append((sql, params))
        failure = self._fail_on(sql)
        if failure is not None:
            raise failure
        return FakeCursor(self._rows_for(sql), rowcount=7)

    def close(self) -> None:
        self.closes += 1

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.executed]


class FakeServer:
    """The factory handed to the store; `connections` lists every connection it made."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.connections: list[FakeConnection] = []
        self.rows: Callable[[str], list[tuple]] = lambda sql: []
        self.fail: Callable[[str], Exception | None] = lambda sql: None

    def __call__(self, **kwargs: Any) -> FakeConnection:
        self.calls.append(kwargs)
        connection = FakeConnection(
            lambda sql: self.rows(sql), lambda sql: self.fail(sql)
        )
        self.connections.append(connection)
        return connection

    def recorded_version(self, version: int | None) -> None:
        self.rows = lambda sql: (
            [(version,)] if sql == SELECT_VERSION and version is not None else []
        )


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def make_store(
    server: FakeServer, password: Secret | None = None
) -> PostgresRelationalStore:
    return PostgresRelationalStore(URL, password, connect_factory=server)


MIGRATIONS = (
    ("CREATE TABLE ns_a (x INTEGER)", "CREATE INDEX ns_a_x ON ns_a (x)"),
    ("ALTER TABLE ns_a ADD COLUMN y TEXT",),
)


# --- translation ------------------------------------------------------------------


def test_question_marks_become_percent_s_and_params_are_kept() -> None:
    assert translate_sql("SELECT a FROM t WHERE b = ? AND c = ?", (1, "x")) == (
        "SELECT a FROM t WHERE b = %s AND c = %s",
        (1, "x"),
    )


def test_question_marks_inside_quotes_are_left_alone() -> None:
    sql = "SELECT 'what?' AS \"odd?\" FROM t WHERE a = ?"
    assert (
        translate_sql(sql, (1,))[0] == "SELECT 'what?' AS \"odd?\" FROM t WHERE a = %s"
    )


def test_a_doubled_single_quote_stays_inside_the_literal() -> None:
    sql = "SELECT 'it''s ? here' FROM t WHERE a = ?"
    assert translate_sql(sql, (1,))[0] == "SELECT 'it''s ? here' FROM t WHERE a = %s"


def test_a_double_quote_inside_a_single_quoted_literal_does_not_open_an_identifier() -> (
    None
):
    sql = "SELECT 'say \"hi' FROM t WHERE a = ?"
    assert translate_sql(sql, (1,))[0] == "SELECT 'say \"hi' FROM t WHERE a = %s"


def test_every_percent_is_escaped_when_params_are_passed_even_in_quotes() -> None:
    sql = "SELECT '100%' FROM t WHERE name LIKE '%a%' AND b = ? AND c LIKE ?"
    assert translate_sql(sql, (1, "%"))[0] == (
        "SELECT '100%%' FROM t WHERE name LIKE '%%a%%' AND b = %s AND c LIKE %s"
    )


def test_no_params_means_unchanged_sql_and_none() -> None:
    sql = "SELECT '100%' FROM t WHERE name LIKE '%a%' AND x = 'q?'"
    assert translate_sql(sql, ()) == (sql, None)
    assert translate_sql(sql, None) == (sql, None)


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("SELECT ? FROM t", (1, 2)),
        ("SELECT ?, ? FROM t", (1,)),
        ("SELECT 'a?' FROM t", (1,)),
    ],
)
def test_a_placeholder_count_mismatch_raises_before_sending(
    server: FakeServer, sql: str, params: Sequence[Any]
) -> None:
    with pytest.raises(ValueError, match="placeholders"):
        translate_sql(sql, params)
    store = make_store(server)
    with pytest.raises(ValueError, match="placeholders"):
        store.execute_sync(sql, params)
    assert server.connections[0].executed == []


def test_the_mismatch_message_holds_counts_only() -> None:
    with pytest.raises(ValueError) as info:
        translate_sql("SELECT ? FROM t WHERE x = 'secret-row'", ("sk-secret", 2))
    assert "sk-secret" not in str(info.value) and "secret-row" not in str(info.value)


def test_the_driver_gets_translated_sql_and_a_tuple(server: FakeServer) -> None:
    make_store(server).execute_sync("DELETE FROM t WHERE a = ?", [5])
    assert server.connections[0].executed == [("DELETE FROM t WHERE a = %s", (5,))]


def test_with_no_params_the_driver_gets_none(server: FakeServer) -> None:
    make_store(server).execute_sync("SELECT '%'")
    assert server.connections[0].executed == [("SELECT '%'", None)]


# --- BEGIN IMMEDIATE ----------------------------------------------------------------


def test_the_lock_constant_matches_its_derivation() -> None:
    derived = (
        int.from_bytes(hashlib.sha256(b"llm-harness:relational").digest()[:8], "big")
        & 0x7FFF_FFFF_FFFF_FFFF
    )
    assert ADVISORY_LOCK_KEY == derived
    assert 0 < ADVISORY_LOCK_KEY < 2**63


@pytest.mark.parametrize(
    "text",
    [
        "BEGIN IMMEDIATE",
        "begin immediate",
        "Begin  Immediate",
        "  BEGIN IMMEDIATE  ",
        "BEGIN IMMEDIATE;",
        "\nBEGIN\tIMMEDIATE ; \n",
    ],
)
def test_begin_immediate_runs_begin_then_the_advisory_lock(
    server: FakeServer, text: str
) -> None:
    conn = make_store(server).connect_sync()
    conn.execute(text)
    assert server.connections[0].executed == [
        ("BEGIN", None),
        (LOCK, (ADVISORY_LOCK_KEY,)),
    ]


@pytest.mark.parametrize("text", ["BEGIN", "COMMIT", "ROLLBACK"])
def test_plain_transaction_statements_pass_through(
    server: FakeServer, text: str
) -> None:
    make_store(server).connect_sync().execute(text)
    assert server.connections[0].executed == [(text, None)]


@pytest.mark.parametrize(
    "text", ["BEGIN IMMEDIATE x", "BEGIN IMMEDIATELY", "SELECT 'BEGIN IMMEDIATE'"]
)
def test_only_the_exact_statement_is_mapped(server: FakeServer, text: str) -> None:
    make_store(server).connect_sync().execute(text)
    assert server.connections[0].executed == [(text, None)]


# --- connect arguments -------------------------------------------------------------


def connect_kwargs(
    server: FakeServer, url: str, password: Secret | None = None
) -> dict[str, Any]:
    PostgresRelationalStore(url, password, connect_factory=server).connect_sync()
    return server.calls[0]


def test_the_connection_gets_keyword_arguments_and_the_password_only_as_password(
    server: FakeServer,
) -> None:
    kwargs = connect_kwargs(server, URL, Secret(PASSWORD))
    assert kwargs == {
        "host": HOST,
        "port": 5432,
        "dbname": "ledger_db",
        "user": USER,
        "password": PASSWORD,
        "connect_timeout": DEFAULT_CONNECT_TIMEOUT_SECONDS,
        "autocommit": True,
    }
    assert [key for key, value in kwargs.items() if value == PASSWORD] == ["password"]
    assert not any("postgresql://" in str(value) for value in kwargs.values())


def test_the_factory_is_called_with_keywords_only(server: FakeServer) -> None:
    def positional_only(*args: Any, **kwargs: Any) -> FakeConnection:
        assert args == ()
        return server(**kwargs)

    PostgresRelationalStore(URL, connect_factory=positional_only).connect_sync()


def test_no_password_means_no_password_argument(server: FakeServer) -> None:
    assert "password" not in connect_kwargs(server, URL)


def test_a_percent_encoded_user_is_decoded(server: FakeServer) -> None:
    kwargs = connect_kwargs(server, "postgresql://app%40corp%20x@h/d")
    assert kwargs["user"] == "app@corp x"


def test_no_user_and_no_port_mean_no_such_arguments(server: FakeServer) -> None:
    kwargs = connect_kwargs(server, "postgres://h/d")
    assert "user" not in kwargs and "port" not in kwargs
    assert (kwargs["host"], kwargs["dbname"]) == ("h", "d")


def test_sslmode_and_connect_timeout_come_from_the_query(server: FakeServer) -> None:
    kwargs = connect_kwargs(
        server, "postgresql://h/d?sslmode=verify-full&connect_timeout=3"
    )
    assert (kwargs["sslmode"], kwargs["connect_timeout"]) == ("verify-full", 3)
    assert kwargs["autocommit"] is True


def test_the_default_connect_timeout_is_ten_seconds(server: FakeServer) -> None:
    assert DEFAULT_CONNECT_TIMEOUT_SECONDS == 10
    assert connect_kwargs(server, "postgresql://h/d")["connect_timeout"] == 10
    assert "sslmode" not in server.calls[0]


def test_an_ipv6_host_is_passed_bare_and_labelled_as_the_url_spells_it(
    server: FakeServer,
) -> None:
    store = PostgresRelationalStore("postgresql://[::1]:6543/d", connect_factory=server)
    store.connect_sync()
    assert server.calls[0]["host"] == "::1"
    assert store.label == "[::1]:6543/d"


def test_the_database_name_is_percent_decoded(server: FakeServer) -> None:
    assert connect_kwargs(server, "postgresql://h/my%20db")["dbname"] == "my db"


def test_unparseable_urls_raise_without_echoing_them() -> None:
    for url in ("postgresql://h", "postgresql://:5/d", "postgresql://[::1/d-sk-SECRET"):
        with pytest.raises(ValueError) as info:
            PostgresRelationalStore(url)
        assert "SECRET" not in str(info.value) and url not in str(info.value)


# --- laziness ----------------------------------------------------------------------


def test_construction_calls_no_factory(server: FakeServer) -> None:
    make_store(server, Secret(PASSWORD))
    assert server.calls == []


def test_construction_does_not_import_psycopg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)  # an import now raises
    store = PostgresRelationalStore(URL, Secret(PASSWORD))
    assert store.label and repr(store)


def test_first_use_without_psycopg_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)
    store = PostgresRelationalStore(URL, Secret(PASSWORD))
    with pytest.raises(RelationalStoreError, match=r"uv sync --extra postgres") as info:
        store.query_sync("SELECT 1")
    for forbidden in (PASSWORD, HOST, USER):
        assert forbidden not in str(info.value)


def test_the_default_factory_is_psycopg_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    seen: list[dict[str, Any]] = []

    def fake_connect(**kwargs: Any) -> FakeConnection:
        seen.append(kwargs)
        return FakeConnection(lambda sql: [], lambda sql: None)

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    PostgresRelationalStore(URL, Secret(PASSWORD)).connect_sync()
    assert seen[0]["password"] == PASSWORD and seen[0]["host"] == HOST


def test_the_real_psycopg_accepts_the_keyword_names() -> None:
    # No server: only the conninfo the real driver would build from these keywords.
    from psycopg.conninfo import make_conninfo

    kwargs = connect_kwargs(FakeServer(), URL, Secret(PASSWORD))
    kwargs.pop("autocommit")
    info = make_conninfo(**kwargs)
    assert (
        "host=" in info and "connect_timeout=10" in info and "dbname=ledger_db" in info
    )


# --- execute_sync and query_sync ----------------------------------------------------


def test_execute_sync_opens_runs_and_closes_returning_the_rowcount(
    server: FakeServer,
) -> None:
    assert make_store(server).execute_sync("UPDATE t SET a = ?", (1,)) == 7
    (connection,) = server.connections
    assert connection.statements == ["UPDATE t SET a = %s"]
    assert connection.closes == 1


def test_query_sync_returns_rows_as_tuples_and_closes(server: FakeServer) -> None:
    server.rows = lambda sql: [(1, "a"), (2, "b")]
    rows = make_store(server).query_sync("SELECT a, b FROM t")
    assert rows == [(1, "a"), (2, "b")]
    assert all(isinstance(row, tuple) for row in rows)
    assert server.connections[0].closes == 1


def test_each_call_uses_its_own_connection(server: FakeServer) -> None:
    store = make_store(server)
    store.query_sync("SELECT 1")
    store.execute_sync("DELETE FROM t")
    assert len(server.connections) == 2
    assert [c.closes for c in server.connections] == [1, 1]


def test_the_connection_is_closed_when_the_statement_fails(server: FakeServer) -> None:
    server.fail = lambda sql: sqlite3.OperationalError("boom")
    with pytest.raises(sqlite3.OperationalError):
        make_store(server).execute_sync("DELETE FROM t")
    assert server.connections[0].closes == 1


def test_the_wrapper_close_is_idempotent(server: FakeServer) -> None:
    conn = make_store(server).connect_sync()
    conn.close()
    conn.close()
    assert server.connections[0].closes == 1


def test_connect_sync_works_with_closing_and_returns_a_cursor_like(
    server: FakeServer,
) -> None:
    from contextlib import closing

    server.rows = lambda sql: [(3,)]
    with closing(make_store(server).connect_sync()) as conn:
        cursor = conn.execute("SELECT n FROM t WHERE a = ?", (1,))
        assert (cursor.fetchone(), cursor.fetchall(), cursor.rowcount) == (
            (3,),
            [(3,)],
            7,
        )
    assert server.connections[0].closes == 1


# --- label and repr ------------------------------------------------------------------


def test_label_has_host_port_and_database_only(server: FakeServer) -> None:
    store = PostgresRelationalStore(
        f"postgresql://{USER}@{HOST}:6000/ledger_db?sslmode=require&connect_timeout=4",
        Secret(PASSWORD),
    )
    assert store.label == f"{HOST}:6000/ledger_db"


def test_label_defaults_the_port_to_5432() -> None:
    assert (
        PostgresRelationalStore("postgresql://h.example/d").label == "h.example:5432/d"
    )


def test_repr_never_shows_user_password_or_url() -> None:
    with_password = PostgresRelationalStore(URL, Secret(PASSWORD))
    without = PostgresRelationalStore(URL)
    assert repr(with_password) == (
        f"PostgresRelationalStore({HOST}:5432/ledger_db, password=set)"
    )
    assert (
        repr(without)
        == f"PostgresRelationalStore({HOST}:5432/ledger_db, password=none)"
    )
    for text in (repr(with_password), str(with_password), repr(without)):
        assert USER not in text and PASSWORD not in text and "postgresql://" not in text


def test_the_dialect_names() -> None:
    assert PostgresRelationalStore.dialect == "postgres"
    assert SqliteRelationalStore.dialect == "sqlite"


# --- migrate_sync --------------------------------------------------------------------


def test_a_fresh_namespace_runs_every_step_and_records_the_version(
    server: FakeServer,
) -> None:
    server.recorded_version(None)
    assert make_store(server).migrate_sync("ns", MIGRATIONS) == 2
    (connection,) = server.connections
    assert connection.executed == [
        ("BEGIN", None),
        (LOCK, (ADVISORY_LOCK_KEY,)),
        (CREATE_VERSIONS, None),
        (SELECT_VERSION, ("ns",)),
        ("CREATE TABLE ns_a (x INTEGER)", None),
        ("CREATE INDEX ns_a_x ON ns_a (x)", None),
        ("ALTER TABLE ns_a ADD COLUMN y TEXT", None),
        (UPSERT, ("ns", 2)),
        ("COMMIT", None),
    ]
    assert connection.closes == 1


def test_an_up_to_date_namespace_runs_no_migration_and_no_upsert(
    server: FakeServer,
) -> None:
    server.recorded_version(2)
    assert make_store(server).migrate_sync("ns", MIGRATIONS) == 2
    assert server.connections[0].statements == [
        "BEGIN",
        LOCK,
        CREATE_VERSIONS,
        SELECT_VERSION,
        "COMMIT",
    ]


def test_a_partial_namespace_runs_only_the_missing_steps(server: FakeServer) -> None:
    server.recorded_version(1)
    assert make_store(server).migrate_sync("ns", MIGRATIONS) == 2
    assert server.connections[0].executed[4:] == [
        ("ALTER TABLE ns_a ADD COLUMN y TEXT", None),
        (UPSERT, ("ns", 2)),
        ("COMMIT", None),
    ]


def test_a_newer_recorded_version_raises_and_rolls_back(server: FakeServer) -> None:
    server.recorded_version(5)
    with pytest.raises(SchemaTooNewError) as info:
        make_store(server).migrate_sync("ns", MIGRATIONS)
    assert str(info.value) == "namespace 'ns' is at version 5, this program knows 2"
    connection = server.connections[0]
    assert connection.statements[-1] == "ROLLBACK"
    assert "COMMIT" not in connection.statements
    assert connection.closes == 1


def test_a_failing_migration_statement_rolls_back_closes_and_records_nothing(
    server: FakeServer,
) -> None:
    server.recorded_version(None)
    server.fail = lambda sql: (
        sqlite3.OperationalError("quoted row: sk-secret") if "INDEX" in sql else None
    )
    with pytest.raises(sqlite3.OperationalError):
        make_store(server).migrate_sync("ns", MIGRATIONS)
    connection = server.connections[0]
    assert connection.statements[-2:] == ["CREATE INDEX ns_a_x ON ns_a (x)", "ROLLBACK"]
    assert all(
        "INSERT INTO schema_versions" not in sql for sql in connection.statements
    )
    assert "COMMIT" not in connection.statements
    assert connection.closes == 1


def test_a_failing_rollback_does_not_hide_the_original_error(
    server: FakeServer,
) -> None:
    server.recorded_version(None)

    def fail(sql: str) -> Exception | None:
        if "INDEX" in sql:
            return sqlite3.OperationalError("first")
        if sql == "ROLLBACK":
            return sqlite3.InterfaceError("second")
        return None

    server.fail = fail
    with pytest.raises(sqlite3.OperationalError, match="first"):
        make_store(server).migrate_sync("ns", MIGRATIONS)
    assert server.connections[0].closes == 1


def test_adopt_user_version_is_ignored(server: FakeServer) -> None:
    server.recorded_version(None)
    make_store(server).migrate_sync("ns", MIGRATIONS, adopt_user_version=True)
    assert all("user_version" not in sql for sql in server.connections[0].statements)
    assert server.connections[0].executed[-2] == (UPSERT, ("ns", 2))


@pytest.mark.parametrize("namespace", ["Ns", "1ns", "a-b", "", "x" * 65])
def test_an_invalid_namespace_is_refused_before_connecting(
    server: FakeServer, namespace: str
) -> None:
    with pytest.raises(ValueError, match="invalid namespace"):
        make_store(server).migrate_sync(namespace, MIGRATIONS)
    assert server.calls == []


def test_the_ledger_migrations_are_sent_unchanged(server: FakeServer) -> None:
    server.recorded_version(None)
    make_store(server).migrate_sync("ledger", _MIGRATIONS)
    sent = server.connections[0].statements
    for statement in _MIGRATIONS[0]:
        assert statement in sent


# --- async wrappers --------------------------------------------------------------------


async def test_the_async_methods_return_what_the_sync_ones_do(
    server: FakeServer,
) -> None:
    server.rows = lambda sql: [(1,)] if sql == SELECT_VERSION else [(9,)]
    store = make_store(server)
    assert await store.execute("DELETE FROM t WHERE a = ?", (1,)) == 7
    assert await store.query("SELECT n FROM t") == [(9,)]
    assert await store.migrate("ns", MIGRATIONS, adopt_user_version=True) == 2
    assert [c.closes for c in server.connections] == [1, 1, 1]


async def test_async_errors_propagate_unchanged(server: FakeServer) -> None:
    server.fail = lambda sql: sqlite3.OperationalError("x")
    with pytest.raises(sqlite3.OperationalError):
        await make_store(server).query("SELECT 1")
    with pytest.raises(ValueError, match="invalid namespace"):
        await asyncio.wait_for(make_store(server).migrate("Bad", MIGRATIONS), 5)


# --- error classes ---------------------------------------------------------------------


def test_database_error_classes_hold_sqlite_and_psycopg() -> None:
    import psycopg

    classes = database_error_classes()
    assert sqlite3.Error in classes and psycopg.Error in classes


def test_database_error_classes_work_without_psycopg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)
    assert database_error_classes() == (sqlite3.Error,)


# --- the ledger's own statements -------------------------------------------------------

# Copied from `llm_harness.store`; a new statement there must be added here.
LEDGER_STATEMENTS = [
    "SELECT model_id, failures, until FROM model_backoff",
    (
        "INSERT INTO llm_calls (time, model_id, provider_id, input_tokens, "
        "output_tokens, reasoning_tokens, cost_usd, seconds, outcome, "
        "error_kind, fallback_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    ),
    "DELETE FROM model_backoff WHERE model_id = ?",
    "SELECT failures, until FROM model_backoff WHERE model_id = ?",
    (
        "INSERT INTO model_backoff (model_id, failures, until) VALUES (?, ?, ?) "
        "ON CONFLICT(model_id) DO UPDATE SET "
        "failures = excluded.failures, until = excluded.until"
    ),
    (
        "INSERT INTO schema_versions (namespace, version) VALUES (?, ?) "
        "ON CONFLICT(namespace) DO UPDATE SET version = excluded.version"
    ),
    "SELECT version FROM schema_versions WHERE namespace = ?",
]


@pytest.mark.parametrize("sql", LEDGER_STATEMENTS)
def test_every_ledger_statement_translates_with_matching_placeholder_counts(
    sql: str,
) -> None:
    wanted = sql.count("?")
    translated, bound = translate_sql(sql, tuple(range(wanted)) or ())
    if wanted:
        assert translated.count("%s") == wanted and "?" not in translated
        assert bound == tuple(range(wanted))
    else:
        assert (translated, bound) == (sql, None)
    assert re.sub(r"%s", "?", translated) == sql
