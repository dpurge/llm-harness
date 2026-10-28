import os
import sqlite3
import stat
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_harness.routing import BackoffEntry
from llm_harness.store import CallRecord, Store, default_db_path

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
LEDGER_TABLES = {"llm_calls", "model_backoff", "schema_versions"}


def call(model_id: str = "m", **fields) -> CallRecord:
    defaults = {
        "time": T0,
        "model_id": model_id,
        "provider_id": "p",
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "cost_usd": None,
        "seconds": 0.0,
        "outcome": "error",
        "error_kind": "connection",
        "fallback_from": None,
    }
    return CallRecord(**(defaults | fields))


def fail(
    store: Store, model_id: str = "m", *, started: datetime = T0, manual: bool = False
) -> None:
    store.record_attempt(
        call(model_id), "fail", attempt_started=started, is_manual=manual
    )


def query(path: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "harness.db"


def test_default_db_path_is_under_local_share():
    assert default_db_path() == Path.home() / ".local/share/llm-harness/harness.db"


def test_constructor_does_not_touch_the_disk(db: Path):
    Store(db)
    assert not db.parent.exists()


def test_first_use_creates_directory_file_and_schema(db: Path):
    assert Store(db).backoff_entries() == {}
    assert db.is_file()
    assert query(db, "SELECT * FROM schema_versions") == [("ledger", 1)]
    tables = query(db, "SELECT name FROM sqlite_master WHERE type = 'table'")
    assert {name for (name,) in tables} == LEDGER_TABLES
    columns = query(db, "SELECT name FROM pragma_table_info('llm_calls')")
    assert [name for (name,) in columns] == [
        "id",
        "time",
        "model_id",
        "provider_id",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cost_usd",
        "seconds",
        "outcome",
        "error_kind",
        "fallback_from",
    ]


def test_modes_are_0700_and_0600(db: Path):
    Store(db).backoff_entries()
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_modes_hold_under_umask_zero(db: Path):
    old = os.umask(0)
    try:
        fail(Store(db))
    finally:
        os.umask(old)
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_existing_loose_modes_are_tightened(db: Path):
    db.parent.mkdir(mode=0o755)
    db.touch(mode=0o644)
    os.chmod(db.parent, 0o755)
    os.chmod(db, 0o644)
    Store(db).backoff_entries()
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_fail_increments_and_sets_until_with_the_ladder(db: Path):
    store = Store(db)
    expected_minutes = [1, 2, 3, 5, 10, 15, 30, 60, 60]
    for failures, minutes in enumerate(expected_minutes, start=1):
        # manual picks always count, so repeated failures walk the ladder
        fail(store, manual=True)
        entry = store.backoff_entries()["m"]
        assert entry == BackoffEntry("m", failures, T0 + timedelta(minutes=minutes))


def test_until_counts_from_the_end_of_the_attempt(db: Path):
    store = Store(db)
    store.record_attempt(
        call(seconds=30.0), "fail", attempt_started=T0, is_manual=False
    )
    assert store.backoff_entries()["m"].until == T0 + timedelta(seconds=30, minutes=1)


def test_until_is_stored_as_utc_iso_8601(db: Path):
    fail(Store(db), started=T0.astimezone(timezone(timedelta(hours=2))))
    [(text,)] = query(db, "SELECT until FROM model_backoff")
    assert text == "2026-10-27T12:01:00.000000+00:00"


def test_reset_deletes_the_row(db: Path):
    store = Store(db)
    fail(store)
    store.record_attempt(
        call(outcome="ok", error_kind=None),
        "reset",
        attempt_started=T0,
        is_manual=False,
    )
    assert store.backoff_entries() == {}
    assert query(db, "SELECT COUNT(*) FROM llm_calls") == [(2,)]


def test_keep_leaves_the_row_alone(db: Path):
    store = Store(db)
    fail(store)
    before = store.backoff_entries()
    store.record_attempt(
        call(error_kind="bad_request"),
        "keep",
        attempt_started=T0 + timedelta(hours=1),
        is_manual=True,
    )
    assert store.backoff_entries() == before
    assert query(db, "SELECT COUNT(*) FROM llm_calls") == [(2,)]


def test_keep_without_a_row_creates_none(db: Path):
    store = Store(db)
    store.record_attempt(call(), "keep", attempt_started=T0, is_manual=False)
    assert store.backoff_entries() == {}


def test_failure_after_the_backoff_expired_increments(db: Path):
    store = Store(db)
    fail(store)
    fail(store, started=T0 + timedelta(minutes=2))
    assert store.backoff_entries()["m"].failures == 2


def test_automatic_failure_inside_an_active_backoff_is_not_counted_again(db: Path):
    store = Store(db)
    fail(store)
    fail(store, started=T0 + timedelta(seconds=30))
    assert store.backoff_entries()["m"].failures == 1


def run_in_threads(targets: list) -> None:
    barrier = threading.Barrier(len(targets))

    def runner(target) -> None:
        barrier.wait()
        target()

    threads = [threading.Thread(target=runner, args=(t,)) for t in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()


def test_two_clients_failing_on_one_outage_increment_once(db: Path):
    Store(db).backoff_entries()  # create the schema first
    stores = [Store(db), Store(db)]
    run_in_threads([lambda s=s: fail(s) for s in stores])
    assert stores[0].backoff_entries()["m"].failures == 1
    assert query(db, "SELECT COUNT(*) FROM llm_calls") == [(2,)]
    assert all(s.pop_notice() is None for s in stores)


def test_two_threads_sharing_one_store_increment_once(db: Path):
    store = Store(db)
    run_in_threads([lambda: fail(store), lambda: fail(store)])
    assert store.backoff_entries()["m"].failures == 1


def test_manual_picks_always_increment(db: Path):
    Store(db).backoff_entries()
    stores = [Store(db), Store(db)]
    run_in_threads([lambda s=s: fail(s, manual=True) for s in stores])
    assert stores[0].backoff_entries()["m"].failures == 2


def test_two_first_time_openers_both_work(db: Path):
    stores = [Store(db), Store(db)]
    run_in_threads(
        [lambda s=s, i=i: fail(s, model_id=f"m{i}") for i, s in enumerate(stores)]
    )
    assert set(stores[0].backoff_entries()) == {"m0", "m1"}
    assert query(db, "SELECT * FROM schema_versions") == [("ledger", 1)]


def test_a_second_store_on_the_same_file_sees_the_data(db: Path):
    fail(Store(db))
    entries = Store(db).backoff_entries()
    assert entries["m"] == BackoffEntry("m", 1, T0 + timedelta(minutes=1))


def test_ledger_row_holds_every_field(db: Path):
    record = call(
        "m1",
        input_tokens=12,
        output_tokens=31,
        reasoning_tokens=5,
        cost_usd=0.25,
        seconds=2.5,
        outcome="ok",
        error_kind=None,
        fallback_from="m0",
    )
    Store(db).record_attempt(record, "reset", attempt_started=T0, is_manual=False)
    assert query(db, "SELECT * FROM llm_calls") == [
        (
            1,
            "2026-10-27T12:00:00.000000+00:00",
            "m1",
            "p",
            12,
            31,
            5,
            0.25,
            2.5,
            "ok",
            None,
            "m0",
        )
    ]


def test_outcome_check_constraint_rejects_unknown_values(db: Path):
    store = Store(db)
    store.record_attempt(
        call(outcome="weird"),  # type: ignore[arg-type]
        "keep",
        attempt_started=T0,
        is_manual=False,
    )
    # The constraint error is an sqlite3.Error, so the store disables itself.
    assert store.pop_notice() is not None
    assert query(db, "SELECT COUNT(*) FROM llm_calls") == [(0,)]


def test_failed_write_rolls_back_the_whole_transaction(db: Path):
    store = Store(db)
    store.record_attempt(
        call(outcome="weird", error_kind="x"),  # type: ignore[arg-type]
        "fail",
        attempt_started=T0,
        is_manual=True,
    )
    assert query(db, "SELECT COUNT(*) FROM model_backoff") == [(0,)]


def test_injection_in_a_model_id_is_stored_literally(db: Path):
    evil = "x'); DROP TABLE llm_calls;--"
    store = Store(db)
    fail(store, model_id=evil)
    assert store.backoff_entries()[evil].failures == 1
    assert query(db, "SELECT model_id FROM llm_calls") == [(evil,)]
    tables = query(db, "SELECT name FROM sqlite_master WHERE type = 'table'")
    assert {name for (name,) in tables} == LEDGER_TABLES


GARBAGE = bytes(range(256)) * 16


def assert_disabled_with_one_notice(store: Store, db: Path, exc_type: str) -> None:
    before = db.read_bytes()
    assert store.backoff_entries() == {}
    fail(store)
    assert store.backoff_entries() == {}
    notice = store.pop_notice()
    assert notice == (
        f"usage database unavailable ({exc_type}): {db}; "
        "usage and backoff are not saved"
    )
    assert store.pop_notice() is None
    assert db.read_bytes() == before
    assert db.exists()
    assert sorted(p.name for p in db.parent.iterdir()) == [db.name]


def test_garbage_file_disables_the_store(db: Path):
    db.parent.mkdir()
    db.write_bytes(GARBAGE)
    assert_disabled_with_one_notice(Store(db), db, "DatabaseError")


def test_newer_user_version_disables_the_store(db: Path):
    db.parent.mkdir()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE t (a)")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    assert_disabled_with_one_notice(Store(db), db, "SchemaTooNewError")
    assert query(db, "PRAGMA user_version") == [(2,)]


def test_a_failure_on_write_first_also_disables_with_one_notice(db: Path):
    db.parent.mkdir()
    db.write_bytes(GARBAGE)
    store = Store(db)
    fail(store)
    fail(store)
    assert store.pop_notice() is not None
    assert store.pop_notice() is None


def test_unusable_directory_disables_the_store(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    store = Store(blocker / "harness.db")
    assert store.backoff_entries() == {}
    notice = store.pop_notice()
    assert notice is not None
    assert notice.startswith("usage database unavailable (")
    assert str(blocker / "harness.db") in notice
    assert store.pop_notice() is None


def test_notice_names_only_the_exception_type_not_its_message(db: Path):
    db.parent.mkdir()
    db.write_bytes(GARBAGE)
    store = Store(db)
    store.backoff_entries()
    notice = store.pop_notice()
    assert notice is not None
    assert "not a database" not in notice


# The ledger schema exactly as shipped before `schema_versions` existed; copied
# literally so a change to `_MIGRATIONS` cannot silently change this fixture.
OLD_LLM_CALLS = """CREATE TABLE llm_calls (
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
        )"""
OLD_MODEL_BACKOFF = """CREATE TABLE model_backoff (
            model_id TEXT PRIMARY KEY,
            failures INTEGER NOT NULL,
            until TEXT NOT NULL
        )"""


def write_old_format_file(db: Path, user_version: int = 1) -> None:
    db.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(OLD_LLM_CALLS)
        conn.execute(OLD_MODEL_BACKOFF)
        conn.executemany(
            "INSERT INTO llm_calls (time, model_id, provider_id, input_tokens, "
            "output_tokens, reasoning_tokens, cost_usd, seconds, outcome, "
            "error_kind, fallback_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-10-27T12:00:00.000000+00:00", "m1", "p", 1, 2, 3, 0.5, 1.5)
                + ("ok", None, "m0"),
                ("2026-10-27T12:01:00.000000+00:00", "m2", "p", None, None, None)
                + (None, 0.1, "error", "connection", None),
                ("2026-10-27T12:02:00.000000+00:00", "m1", "q", 7, 8, None)
                + (1.25, 2.0, "cancelled", None, "m2"),
            ],
        )
        conn.executemany(
            "INSERT INTO model_backoff VALUES (?, ?, ?)",
            [
                ("m2", 3, "2026-10-27T12:04:00.000000+00:00"),
                ("m9", 1, "2026-10-27T12:05:00.000000+00:00"),
            ],
        )
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.commit()


def ledger_rows(db: Path) -> tuple[list[tuple], list[tuple]]:
    return (
        query(db, "SELECT * FROM llm_calls ORDER BY id"),
        query(db, "SELECT * FROM model_backoff ORDER BY model_id"),
    )


def test_an_old_populated_file_is_adopted_without_touching_its_rows(db: Path):
    write_old_format_file(db)
    before = ledger_rows(db)
    assert len(before[0]) == 3 and len(before[1]) == 2
    assert set(Store(db).backoff_entries()) == {"m2", "m9"}
    assert ledger_rows(db) == before
    assert query(db, "SELECT * FROM schema_versions") == [("ledger", 1)]
    # Additive: an older build still finds the version it expects.
    assert query(db, "PRAGMA user_version") == [(1,)]
    tables = query(db, "SELECT name FROM sqlite_master WHERE type = 'table'")
    assert {name for (name,) in tables} == LEDGER_TABLES


def test_reopening_an_adopted_file_is_a_no_op(db: Path):
    write_old_format_file(db)
    Store(db).backoff_entries()
    before = (ledger_rows(db), query(db, "SELECT * FROM schema_versions"))
    store = Store(db)
    assert store.backoff_entries() != {}
    assert (ledger_rows(db), query(db, "SELECT * FROM schema_versions")) == before
    assert store.pop_notice() is None


def test_an_adopted_file_accepts_new_writes(db: Path):
    write_old_format_file(db)
    before = ledger_rows(db)[0]
    fail(Store(db), model_id="m3")
    assert ledger_rows(db)[0][:3] == before
    assert len(ledger_rows(db)[0]) == 4


def test_a_legacy_user_version_too_new_disables_the_store(db: Path):
    write_old_format_file(db, user_version=5)
    before = ledger_rows(db)
    assert_disabled_with_one_notice(Store(db), db, "SchemaTooNewError")
    assert ledger_rows(db) == before
    assert query(db, "PRAGMA user_version") == [(5,)]


def test_two_stores_adopting_an_old_file_at_once_both_work(db: Path):
    write_old_format_file(db)
    before = ledger_rows(db)
    stores = [Store(db), Store(db)]
    run_in_threads([lambda s=s: s.backoff_entries() for s in stores])
    assert all(s.pop_notice() is None for s in stores)
    assert ledger_rows(db) == before
    assert query(db, "SELECT * FROM schema_versions") == [("ledger", 1)]


def test_schema_too_new_error_is_importable_from_the_store_module():
    from llm_harness.storage.relational import SchemaTooNewError
    from llm_harness.store import SchemaTooNewError as reexported

    assert reexported is SchemaTooNewError
