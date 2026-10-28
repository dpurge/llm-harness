"""SQLite store: the call ledger and the model backoff state.

Synchronous on purpose; async callers use `asyncio.to_thread`. Only ids, token
counts, costs, times and error kinds are stored, never prompts, replies, error
messages or keys. Runs on `SqliteRelationalStore` as the `ledger` namespace.
"""

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from llm_harness.config import default_data_dir
from llm_harness.routing import BackoffEntry, backoff_delay
from llm_harness.storage.relational import (
    Migration,
    SchemaTooNewError,
    SqliteRelationalStore,
)

__all__ = ["SchemaTooNewError"]  # re-exported: it moved to storage.relational

BackoffAction = Literal["fail", "reset", "keep"]

# Entry i upgrades ledger version i to i + 1. Files written before schema_versions
# existed carry the version in `PRAGMA user_version`; `migrate_sync` adopts it
# (additively: it only records the row and leaves user_version untouched).
_MIGRATIONS: tuple[Migration, ...] = (
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
_NAMESPACE = "ledger"


def expected_schema_versions() -> dict[str, int]:
    """The schema version this build expects per namespace, for read-only diagnostics."""
    return {_NAMESPACE: len(_MIGRATIONS)}


@dataclass(frozen=True)
class CallRecord:
    time: datetime  # start of the attempt
    model_id: str
    provider_id: str
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    seconds: float
    outcome: Literal["ok", "error", "cancelled"]
    error_kind: str | None
    fallback_from: str | None


def default_db_path() -> Path:
    return default_data_dir() / "harness.db"


def _iso(moment: datetime) -> str:
    # Fixed width, so stored times also sort correctly as text.
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _parse(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._relational = SqliteRelationalStore(path)
        self._lock = threading.Lock()  # guards the three fields below
        self._migrated = False
        self._disabled = False
        self._notice: str | None = None

    def backoff_entries(self) -> dict[str, BackoffEntry]:
        if self._is_disabled():
            return {}
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT model_id, failures, until FROM model_backoff"
                ).fetchall()
            return {
                model_id: BackoffEntry(model_id, failures, _parse(until))
                for model_id, failures, until in rows
            }
        except (sqlite3.Error, OSError, SchemaTooNewError, ValueError) as exc:
            self._disable(exc)
            return {}

    def record_attempt(
        self,
        call: CallRecord,
        backoff: BackoffAction,
        *,
        attempt_started: datetime,
        is_manual: bool,
    ) -> None:
        """Write one ledger row and update the model's backoff in one transaction."""
        if self._is_disabled():
            return
        try:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._insert_call(conn, call)
                    self._update_backoff(
                        conn, call, backoff, attempt_started, is_manual
                    )
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
        except (sqlite3.Error, OSError, SchemaTooNewError, ValueError) as exc:
            self._disable(exc)

    def pop_notice(self) -> str | None:
        with self._lock:
            notice, self._notice = self._notice, None
            return notice

    def _is_disabled(self) -> bool:
        with self._lock:
            return self._disabled

    def _disable(self, exc: Exception) -> None:
        # Type only: a sqlite3 message can quote file content.
        with self._lock:
            if self._disabled:
                return
            self._disabled = True
            self._notice = (
                f"usage database unavailable ({type(exc).__name__}): {self._path}; "
                "usage and backoff are not saved"
            )

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            migrated = self._migrated
        if not migrated:
            # Concurrent first callers may both migrate; the second is a no-op.
            self._relational.migrate_sync(
                _NAMESPACE, _MIGRATIONS, adopt_user_version=True
            )
            with self._lock:
                self._migrated = True
        return self._relational.connect_sync()

    @staticmethod
    def _insert_call(conn: sqlite3.Connection, call: CallRecord) -> None:
        conn.execute(
            "INSERT INTO llm_calls (time, model_id, provider_id, input_tokens, "
            "output_tokens, reasoning_tokens, cost_usd, seconds, outcome, "
            "error_kind, fallback_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(call.time),
                call.model_id,
                call.provider_id,
                call.input_tokens,
                call.output_tokens,
                call.reasoning_tokens,
                call.cost_usd,
                call.seconds,
                call.outcome,
                call.error_kind,
                call.fallback_from,
            ),
        )

    @staticmethod
    def _update_backoff(
        conn: sqlite3.Connection,
        call: CallRecord,
        backoff: BackoffAction,
        attempt_started: datetime,
        is_manual: bool,
    ) -> None:
        if backoff == "keep":
            return
        if backoff == "reset":
            conn.execute(
                "DELETE FROM model_backoff WHERE model_id = ?", (call.model_id,)
            )
            return
        row = conn.execute(
            "SELECT failures, until FROM model_backoff WHERE model_id = ?",
            (call.model_id,),
        ).fetchone()
        # A row that is still active when this attempt began means another client
        # already counted this outage; a manual pick always counts.
        if row is not None and not is_manual and _parse(row[1]) > attempt_started:
            return
        failures = (row[0] if row is not None else 0) + 1
        failed_at = attempt_started + timedelta(seconds=call.seconds)
        until = failed_at + backoff_delay(failures)
        conn.execute(
            "INSERT INTO model_backoff (model_id, failures, until) VALUES (?, ?, ?) "
            "ON CONFLICT(model_id) DO UPDATE SET "
            "failures = excluded.failures, until = excluded.until",
            (call.model_id, failures, _iso(until)),
        )
