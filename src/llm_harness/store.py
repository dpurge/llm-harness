"""SQLite store: the call ledger and the model backoff state.

Synchronous on purpose; async callers use `asyncio.to_thread`. Only ids, token
counts, costs, times and error kinds are stored, never prompts, replies, error
messages or keys. This module owns the database's `PRAGMA user_version`.
"""

import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from llm_harness.routing import BackoffEntry, backoff_delay

BackoffAction = Literal["fail", "reset", "keep"]

# Entry i upgrades user_version i to i + 1. One statement per element because
# `execute` runs a single statement and `executescript` would end our transaction.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
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
_KNOWN_VERSION = len(_MIGRATIONS)
_BUSY_TIMEOUT_SECONDS = 5


class SchemaTooNewError(Exception):
    """The database was written by a newer version of this program."""


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
    return Path.home() / ".local" / "share" / "llm-harness" / "harness.db"


def _iso(moment: datetime) -> str:
    # Fixed width, so stored times also sort correctly as text.
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _parse(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()  # guards the three fields below
        self._prepared = False
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
        self._prepare_files()
        conn = sqlite3.connect(
            self._path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None
        )
        try:
            self._migrate(conn)
        except BaseException:
            conn.close()
            raise
        return conn

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

    @staticmethod
    def _user_version(conn: sqlite3.Connection) -> int:
        return conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = self._user_version(conn)
        if version > _KNOWN_VERSION:
            raise SchemaTooNewError(f"user_version {version}")
        if version == _KNOWN_VERSION:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have migrated while we waited for the lock.
            version = self._user_version(conn)
            if version > _KNOWN_VERSION:
                raise SchemaTooNewError(f"user_version {version}")
            for index in range(version, _KNOWN_VERSION):
                for statement in _MIGRATIONS[index]:
                    conn.execute(statement)
                # PRAGMA takes no bound parameters; `index` is our own int.
                conn.execute(f"PRAGMA user_version = {index + 1}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

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
