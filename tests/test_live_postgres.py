"""Live check of `PostgresRelationalStore` against a real PostgreSQL server.

Deselected by default (`pytest -m live`). Skips, with a printed reason, when
`LLM_HARNESS_LIVE_POSTGRES_URL` is unset or the server does not answer within 3 s.
The url carries no password; the password comes from
`LLM_HARNESS_LIVE_POSTGRES_PASSWORD` (optional).

Run it:

    docker --context rancher-desktop run -d --rm --name llm-harness-live-postgres -e POSTGRES_PASSWORD=live-test-password -e POSTGRES_DB=llm_harness_live -p 127.0.0.1:15432:5432 postgres:17
    LLM_HARNESS_LIVE_POSTGRES_URL=postgresql://postgres@127.0.0.1:15432/llm_harness_live LLM_HARNESS_LIVE_POSTGRES_PASSWORD=live-test-password uv run pytest -m live tests/test_live_postgres.py
    docker --context rancher-desktop rm -f llm-harness-live-postgres

Every table and namespace has a unique lowercase name with a uuid suffix and is
removed through a raw psycopg connection in a `finally` block. Each test body is
bounded to 30 s.

The usage-ledger and `--storage-status` tests at the end are different: the ledger has FIXED names (tables
`llm_calls` and `model_backoff`, namespace `ledger`), so each of them starts by
dropping those tables and the `ledger` version row, and drops them again afterwards.
That would destroy a real ledger, so they SKIP, with a printed reason, unless the
database name in the url contains `live` or `test` (the docker command above uses
`llm_harness_live`). Never point them at a database that holds usage data.
"""

import asyncio
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from llm_harness.config import (
    HarnessConfig,
    ProviderConfig,
    RelationalConfig,
    StorageConfig,
)
from llm_harness.redact import Secret
from llm_harness.routing import backoff_delay
from llm_harness.storage.postgres import PostgresRelationalStore
from llm_harness.storage.relational import SchemaTooNewError
from llm_harness.storage.status import storage_status
from llm_harness.store import Store, expected_schema_versions
from tests.live_postgres import ledger_target, live_target
from tests.test_store import T0, call

pytestmark = pytest.mark.live

WHOLE_TEST_SECONDS = 30


class Names:
    """Unique names for one test; `drop_all` removes what the test may have created."""

    def __init__(self) -> None:
        self.tables: list[str] = []
        self.namespaces: list[str] = []

    def table(self, label: str) -> str:
        name = f"live_{label}_{uuid.uuid4().hex[:12]}"
        self.tables.append(name)
        return name

    def namespace(self, label: str) -> str:
        name = f"live_{label}_{uuid.uuid4().hex[:12]}"
        self.namespaces.append(name)
        return name


def drop_all(url: str, password: str | None, names: Names) -> None:
    with psycopg.connect(url, password=password, autocommit=True) as raw:
        for table in names.tables:
            raw.execute(f"DROP TABLE IF EXISTS {table}")  # our own hex-suffixed names
        for namespace in names.namespaces:
            try:
                raw.execute(
                    "DELETE FROM schema_versions WHERE namespace = %s", (namespace,)
                )
            except psycopg.errors.UndefinedTable:
                pass  # nothing was ever migrated


def run_bounded(body: Callable[[PostgresRelationalStore, Names], None]) -> None:
    """Run `body(store, names)` under one 30 s bound; remove `names` afterwards."""
    url, password = live_target()
    store = PostgresRelationalStore(url, Secret(password) if password else None)
    names = Names()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        # A hung body raises TimeoutError here; its thread cannot be killed, so the
        # pool is not waited for.
        pool.submit(body, store, names).result(timeout=WHOLE_TEST_SECONDS)
    finally:
        pool.shutdown(wait=False)
        drop_all(url, password, names)


def table_exists(store: PostgresRelationalStore, table: str) -> bool:
    return store.query_sync("SELECT to_regclass(?)", (table,))[0][0] is not None


def test_live_question_mark_round_trip_keeps_types_and_floats_exact() -> None:
    def body(store: PostgresRelationalStore, names: Names) -> None:
        table = names.table("types")
        store.execute_sync(
            f"CREATE TABLE {table} (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "s TEXT, i BIGINT, f DOUBLE PRECISION, n TEXT NULL)"
        )
        values = ("héllo 'quoted' ?", 2**40, 0.000123456789, None)
        assert (
            store.execute_sync(
                f"INSERT INTO {table} (s, i, f, n) VALUES (?, ?, ?, ?)", values
            )
            == 1
        )
        rows = store.query_sync(f"SELECT s, i, f, n FROM {table} WHERE i = ?", (2**40,))
        assert rows == [values]
        assert isinstance(rows[0], tuple)

    run_bounded(body)


def test_live_percent_in_sql_works_with_and_without_params() -> None:
    def body(store: PostgresRelationalStore, names: Names) -> None:
        assert store.query_sync("SELECT 'a%b'") == [("a%b",)]
        assert store.query_sync("SELECT 'a%b' || CAST(? AS TEXT)", ("!",)) == [
            ("a%b!",)
        ]
        assert store.query_sync("SELECT 'x' LIKE '%x%' AND ? = ?", (1, 1)) == [(True,)]

    run_bounded(body)


def test_live_rowcount_of_update_and_delete() -> None:
    def body(store: PostgresRelationalStore, names: Names) -> None:
        table = names.table("rows")
        store.execute_sync(f"CREATE TABLE {table} (a INTEGER, b TEXT)")
        for a in (1, 1, 2):
            store.execute_sync(f"INSERT INTO {table} VALUES (?, ?)", (a, "x"))
        assert (
            store.execute_sync(f"UPDATE {table} SET b = ? WHERE a = ?", ("y", 1)) == 2
        )
        assert store.execute_sync(f"DELETE FROM {table} WHERE b = ?", ("x",)) == 1
        assert store.execute_sync(f"DELETE FROM {table}") == 2

    run_bounded(body)


def test_live_migrate_creates_records_and_is_a_no_op_the_second_time() -> None:
    def body(store: PostgresRelationalStore, names: Names) -> None:
        table, namespace = names.table("mig"), names.namespace("mig")
        migrations = ((f"CREATE TABLE {table} (x INTEGER)",),)
        assert store.migrate_sync(namespace, migrations) == 1
        assert table_exists(store, table)
        versions = "SELECT version FROM schema_versions WHERE namespace = ?"
        assert store.query_sync(versions, (namespace,)) == [(1,)]
        # CREATE TABLE without IF NOT EXISTS would fail if it ran again.
        assert store.migrate_sync(namespace, migrations) == 1
        # A second step runs alone.
        more = (*migrations, (f"ALTER TABLE {table} ADD COLUMN y TEXT",))
        assert store.migrate_sync(namespace, more, adopt_user_version=True) == 2
        assert store.query_sync(versions, (namespace,)) == [(2,)]

    run_bounded(body)


def test_live_a_recorded_higher_version_raises_schema_too_new() -> None:
    def body(store: PostgresRelationalStore, names: Names) -> None:
        table, namespace = names.table("new"), names.namespace("new")
        migrations = ((f"CREATE TABLE {table} (x INTEGER)",),)
        store.migrate_sync(namespace, migrations)
        store.execute_sync(
            "UPDATE schema_versions SET version = ? WHERE namespace = ?", (5, namespace)
        )
        with pytest.raises(SchemaTooNewError, match="is at version 5"):
            store.migrate_sync(namespace, migrations)
        assert store.query_sync(
            "SELECT version FROM schema_versions WHERE namespace = ?", (namespace,)
        ) == [(5,)]

    run_bounded(body)


def test_live_a_failing_second_statement_rolls_back_the_first_and_the_version() -> None:
    def body(store: PostgresRelationalStore, names: Names) -> None:
        table, namespace = names.table("fail"), names.namespace("fail")
        migrations = (
            (
                f"CREATE TABLE {table} (x INTEGER)",
                f"SELECT * FROM {table}_does_not_exist",
            ),
        )
        with pytest.raises(psycopg.errors.UndefinedTable):
            store.migrate_sync(namespace, migrations)
        assert not table_exists(store, table)  # DDL rolled back
        assert (
            store.query_sync(
                "SELECT version FROM schema_versions WHERE namespace = ?", (namespace,)
            )
            == []
        )

    run_bounded(body)


def test_live_four_threads_migrating_one_namespace_create_one_table() -> None:
    def body(store: PostgresRelationalStore, names: Names) -> None:
        table, namespace = names.table("race"), names.namespace("race")
        migrations = (
            (f"CREATE TABLE {table} (x INTEGER)",),
            (f"ALTER TABLE {table} ADD COLUMN y TEXT",),
        )
        barrier = threading.Barrier(4)

        def migrate() -> int:
            barrier.wait(timeout=10)
            return store.migrate_sync(namespace, migrations)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [
                future.result() for future in [pool.submit(migrate) for _ in range(4)]
            ]
        assert results == [2, 2, 2, 2]  # a lost race would have raised DuplicateTable
        assert table_exists(store, table)
        assert store.query_sync(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = ?",
            (table,),
        ) == [(2,)]
        assert store.query_sync(
            "SELECT version FROM schema_versions WHERE namespace = ?", (namespace,)
        ) == [(2,)]

    run_bounded(body)


def test_live_connect_sync_transactions_roll_back_and_commit() -> None:
    def body(store: PostgresRelationalStore, names: Names) -> None:
        table = names.table("tx")
        store.execute_sync(f"CREATE TABLE {table} (a INTEGER)")
        conn = store.connect_sync()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"INSERT INTO {table} VALUES (?)", (1,))
            conn.execute("ROLLBACK")
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(f"INSERT INTO {table} VALUES (?)", (2,))
            conn.execute("COMMIT")
        finally:
            conn.close()
        assert store.query_sync(f"SELECT a FROM {table}") == [(2,)]

    run_bounded(body)


def test_live_begin_immediate_serialises_writers_so_no_update_is_lost() -> None:
    def body(store: PostgresRelationalStore, names: Names) -> None:
        table = names.table("counter")
        store.execute_sync(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, n INTEGER)")
        store.execute_sync(f"INSERT INTO {table} VALUES (1, 0)")
        barrier = threading.Barrier(2)

        def increment() -> None:
            conn = store.connect_sync()
            try:
                barrier.wait(timeout=10)
                conn.execute("BEGIN IMMEDIATE")
                (current,) = conn.execute(
                    f"SELECT n FROM {table} WHERE id = 1"
                ).fetchone()
                time.sleep(
                    0.2
                )  # long enough for the other writer to read the same value
                conn.execute(f"UPDATE {table} SET n = ? WHERE id = 1", (current + 1,))
                conn.execute("COMMIT")
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(increment), pool.submit(increment)]:
                future.result()
        assert store.query_sync(f"SELECT n FROM {table} WHERE id = 1") == [(2,)]

    run_bounded(body)


def test_live_a_wrong_password_raises_a_psycopg_error_without_the_password() -> None:
    url, password = live_target()
    wrong = "wrong-password-LIVE-xyz"
    store = PostgresRelationalStore(url, Secret(wrong))

    def attempt() -> None:
        store.query_sync("SELECT 1")

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(attempt)
        try:
            future.result(timeout=WHOLE_TEST_SECONDS)
        except psycopg.Error as exc:
            assert wrong not in str(exc) and wrong not in repr(exc)
            assert wrong not in repr(store)
            if password:
                assert password not in str(exc)
        else:
            pytest.skip("the server accepted a wrong password (trust authentication)")
    finally:
        pool.shutdown(wait=False)


# --- the usage ledger on Postgres ----------------------------------------------------

LOST_UPDATE_ROUNDS = 5


def reset_ledger(url: str, password: str | None) -> None:
    with psycopg.connect(url, password=password, autocommit=True) as raw:
        raw.execute("DROP TABLE IF EXISTS llm_calls, model_backoff")
        try:
            raw.execute("DELETE FROM schema_versions WHERE namespace = 'ledger'")
        except psycopg.errors.UndefinedTable:
            pass  # nothing was ever migrated


def run_ledger(body: Callable[[Store, str, str | None], None]) -> None:
    """Run `body(store, url, password)` on a clean ledger under one 30 s bound."""
    url, password = ledger_target()
    reset_ledger(url, password)
    store = Store(
        relational=PostgresRelationalStore(url, Secret(password) if password else None)
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(body, store, url, password).result(timeout=WHOLE_TEST_SECONDS)
        assert store.pop_notice() is None  # a silently disabled ledger proves nothing
    finally:
        pool.shutdown(wait=False)
        reset_ledger(url, password)


def raw_query(url: str, password: str | None, sql: str, params: tuple = ()) -> list:
    with psycopg.connect(url, password=password, autocommit=True) as raw:
        return raw.execute(sql, params).fetchall()


def record(store: Store, **fields: object) -> None:
    store.record_attempt(
        call(**fields),
        "keep",
        attempt_started=T0,
        is_manual=False,  # type: ignore[arg-type]
    )


def test_live_ledger_tables_have_an_identity_id_and_double_precision_floats() -> None:
    def body(store: Store, url: str, password: str | None) -> None:
        record(store)
        columns = dict(
            raw_query(
                url,
                password,
                "SELECT column_name, data_type || '/' || is_identity "
                "FROM information_schema.columns WHERE table_name = 'llm_calls'",
            )
        )
        assert columns["id"] == "bigint/YES"
        assert columns["cost_usd"] == "double precision/NO"
        assert columns["seconds"] == "double precision/NO"

    run_ledger(body)


def test_live_two_inserts_get_increasing_ids() -> None:
    def body(store: Store, url: str, password: str | None) -> None:
        record(store)
        record(store)
        ids = [r[0] for r in raw_query(url, password, "SELECT id FROM llm_calls")]
        assert len(ids) == 2 and ids[0] < ids[1]

    run_ledger(body)


def test_live_a_float_round_trips_exactly() -> None:
    def body(store: Store, url: str, password: str | None) -> None:
        record(store, cost_usd=0.000123456789, seconds=0.1234567890123)
        assert raw_query(url, password, "SELECT cost_usd, seconds FROM llm_calls") == [
            (0.000123456789, 0.1234567890123)
        ]

    run_ledger(body)


def test_live_a_failure_is_stored_in_the_backoff_and_a_reset_removes_it() -> None:
    def body(store: Store, url: str, password: str | None) -> None:
        record_failure = call(seconds=2.5)
        store.record_attempt(
            record_failure, "fail", attempt_started=T0, is_manual=False
        )
        entries = store.backoff_entries()
        expected_until = T0 + timedelta(seconds=2.5) + backoff_delay(1)
        assert (entries["m"].failures, entries["m"].until) == (1, expected_until)
        store.record_attempt(call(), "reset", attempt_started=T0, is_manual=False)
        assert store.backoff_entries() == {}

    run_ledger(body)


def test_live_two_threads_recording_a_failure_never_lose_an_increment() -> None:
    # `is_manual=True` on purpose: a manual pick always counts. The non-manual path
    # deliberately skips a second failure while the row is still active, so with it
    # the final count would depend on who wins, not on whether an update was lost.
    def body(store: Store, url: str, password: str | None) -> None:
        store.backoff_entries()  # migrate first, so the race is on the ledger only
        barrier = threading.Barrier(2)

        def attempt() -> None:
            barrier.wait(timeout=10)
            store.record_attempt(call(), "fail", attempt_started=T0, is_manual=True)

        for _ in range(LOST_UPDATE_ROUNDS):
            with ThreadPoolExecutor(max_workers=2) as pool:
                for future in [pool.submit(attempt), pool.submit(attempt)]:
                    future.result()
        assert store.pop_notice() is None
        assert store.backoff_entries()["m"].failures == 2 * LOST_UPDATE_ROUNDS
        (rows,) = raw_query(url, password, "SELECT count(*) FROM llm_calls")
        assert rows == (2 * LOST_UPDATE_ROUNDS,)

    run_ledger(body)


def test_live_the_ledger_version_matches_what_the_code_expects() -> None:
    def body(store: Store, url: str, password: str | None) -> None:
        record(store)
        assert raw_query(
            url,
            password,
            "SELECT version FROM schema_versions WHERE namespace = 'ledger'",
        ) == [(expected_schema_versions()["ledger"],)]

    run_ledger(body)


def test_live_a_wrong_password_disables_the_ledger_with_a_type_only_notice() -> None:
    url, password = live_target()  # touches no table, so no scratch-database check
    wrong = "wrong-password-LIVE-xyz"
    store = Store(relational=PostgresRelationalStore(url, Secret(wrong)))
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(store.backoff_entries).result(timeout=WHOLE_TEST_SECONDS)
    finally:
        pool.shutdown(wait=False)
    notice = store.pop_notice()
    if notice is None:
        pytest.skip("the server accepted a wrong password (trust authentication)")
    # The shape alone excludes the host, user, database and server text; the user is
    # `postgres` in the docker command, so it cannot be searched for as a word.
    assert re.fullmatch(
        r"usage database unavailable \(\w+\): postgres; usage and backoff are not saved",
        notice,
    )
    for secret in (wrong, password, urlsplit(url).hostname):
        if secret:
            assert secret not in notice


# --- `--storage-status` on Postgres --------------------------------------------------

READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"
WRONG_PASSWORD = "wrong-password-LIVE-xyz"
CLOSED_PORT_SECONDS = 5 + 3  # the status bound plus slack for a slow machine


def status_config(url: str, password: str | None) -> HarnessConfig:
    return HarnessConfig(
        providers={
            "p": ProviderConfig(type="ollama", base_url="http://localhost:11434")
        },
        models=[],
        storage=StorageConfig(relational=RelationalConfig(backend="postgres", url=url)),
        postgres_password=Secret(password) if password else None,
    )


def run_status(url: str, password: str | None, data_dir: Path) -> list[str]:
    return asyncio.run(
        storage_status(
            status_config(url, password),
            relational_path=data_dir / "never-used.db",
            data_dir=data_dir,
        )
    )


def with_host_and_port(url: str, host_and_port: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=host_and_port))


def public_tables(url: str, password: str | None) -> set[str]:
    return {
        name
        for (name,) in raw_query(
            url,
            password,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'",
        )
    }


def drop_ledger_and_versions(url: str, password: str | None) -> None:
    with psycopg.connect(url, password=password, autocommit=True) as raw:
        raw.execute("DROP TABLE IF EXISTS llm_calls, model_backoff, schema_versions")


def test_live_status_without_schema_versions_reads_nothing_and_creates_nothing(
    tmp_path: Path,
) -> None:
    url, password = ledger_target()
    drop_ledger_and_versions(url, password)
    try:
        before = public_tables(url, password)
        lines = run_status(url, password, tmp_path)
        assert lines[1] == "  schema: nothing to read yet"
        assert public_tables(url, password) == before
        assert "schema_versions" not in before
    finally:
        drop_ledger_and_versions(url, password)


def test_live_status_after_the_ledger_is_migrated_shows_its_version(
    tmp_path: Path,
) -> None:
    def body(store: Store, url: str, password: str | None) -> None:
        record(store)
        lines = run_status(url, password, tmp_path)
        expected = expected_schema_versions()["ledger"]
        label = f"{urlsplit(url).hostname}:{urlsplit(url).port or 5432}"
        assert lines[0].startswith(f"relational: postgres  {label}/")
        assert (
            f"  schema: ledger version {expected} (the code expects {expected})"
            in lines
        )
        assert any(line.startswith("vector:") for line in lines)
        assert any(line.startswith("files:") for line in lines)
        assert list(tmp_path.iterdir()) == []  # status wrote nothing locally

    run_ledger(body)


def test_live_the_status_connection_really_refuses_writes() -> None:
    url, password = ledger_target()
    seen: list[dict[str, Any]] = []

    def recording_factory(**kwargs: Any) -> psycopg.Connection:
        seen.append(kwargs)
        return psycopg.connect(**kwargs)

    PostgresRelationalStore(
        url, Secret(password) if password else None, connect_factory=recording_factory
    ).read_schema_versions_sync()
    (kwargs,) = seen
    assert kwargs["options"] == READ_ONLY_OPTIONS and kwargs["connect_timeout"] == 5
    scratch = f"live_readonly_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(url, password=password, autocommit=True) as writable:
        try:
            writable.execute(f"CREATE TABLE {scratch} (x INTEGER)")  # our own name
            with psycopg.connect(**kwargs) as read_only:
                assert read_only.execute(
                    f"SELECT count(*) FROM {scratch}"
                ).fetchone() == (0,)
                with pytest.raises(psycopg.Error) as refused:
                    read_only.execute(f"INSERT INTO {scratch} VALUES (1)")
            assert refused.value.sqlstate == "25006"  # read_only_sql_transaction
            assert writable.execute(f"SELECT count(*) FROM {scratch}").fetchone() == (
                0,
            )
        finally:
            writable.execute(f"DROP TABLE IF EXISTS {scratch}")


def test_live_a_wrong_password_prints_one_cannot_connect_line(tmp_path: Path) -> None:
    url, password = live_target()
    lines = run_status(url, WRONG_PASSWORD, tmp_path)
    if not lines[1].startswith("  cannot connect"):
        pytest.skip("the server accepted a wrong password (trust authentication)")
    assert lines[1] == "  cannot connect (OperationalError)"
    assert sum("cannot connect" in line for line in lines) == 1
    text = "\n".join(lines)
    for secret in (WRONG_PASSWORD, password):
        if secret:
            assert secret not in text


def test_live_a_closed_port_prints_one_cannot_connect_line_within_seconds(
    tmp_path: Path,
) -> None:
    url, password = live_target()
    closed = with_host_and_port(url, "127.0.0.1:1")
    started = time.monotonic()
    lines = run_status(closed, password, tmp_path)
    assert time.monotonic() - started < CLOSED_PORT_SECONDS
    assert lines[1] == "  cannot connect (OperationalError)"
    assert sum("cannot connect" in line for line in lines) == 1
    assert any(line.startswith("vector:") for line in lines)
    assert not password or password not in "\n".join(lines)
