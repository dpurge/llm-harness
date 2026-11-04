"""Agent memory: short English notes kept per user in the relational store.

Not `specs/memory.md` (the developer log of `/code`). A memory is a short text
plus 1 to 8 keywords; both are indexed by an FTS5 table that triggers keep in
step with `memories`. Every query filters by `user_id`, except that a read also
returns shared notes (`shared = 1`: one global set, readable by every user, changed
only by the creator through `set_shared`). Timestamps come from the injected clock
only. Text or keywords that look like a secret are refused, not stored masked.
Validation errors are `MemoryValidationError` with a short message,
so a tool can return them to the model as an observation.
"""

import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from llm_harness.redact import redact
from llm_harness.storage.relational import (
    Migration,
    RelationalStore,
    database_error_classes,
)
from llm_harness.trace import UNCUT

MEMORY_NAMESPACE = "memory"

# Entry i upgrades memory version i to i + 1. The FTS table is an external-content
# table over `memories`, so the triggers are the only thing that keeps it in sync.
MEMORY_MIGRATIONS: tuple[Migration, ...] = (
    (
        """CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            text TEXT NOT NULL,
            keywords TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX memories_user_id ON memories (user_id)",
        """CREATE VIRTUAL TABLE memories_fts USING fts5(
            text, keywords,
            content='memories', content_rowid='id',
            tokenize='porter unicode61'
        )""",
        """CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts (rowid, text, keywords)
            VALUES (new.id, new.text, new.keywords);
        END""",
        """CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts (memories_fts, rowid, text, keywords)
            VALUES ('delete', old.id, old.text, old.keywords);
        END""",
        """CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts (memories_fts, rowid, text, keywords)
            VALUES ('delete', old.id, old.text, old.keywords);
            INSERT INTO memories_fts (rowid, text, keywords)
            VALUES (new.id, new.text, new.keywords);
        END""",
    ),
    (
        # Vector index bookkeeping (see memory_index.py). A marker row means "this
        # memory is embedded in this collection"; the triggers drop it when the
        # embedded content changes, so staleness is recorded atomically with the write.
        """CREATE TABLE memory_vectors (
            collection TEXT NOT NULL,
            memory_id INTEGER NOT NULL,
            PRIMARY KEY (collection, memory_id)
        )""",
        """CREATE TABLE memory_vector_meta (
            collection TEXT PRIMARY KEY,
            model_id TEXT NOT NULL,
            dim INTEGER NOT NULL
        )""",
        """CREATE TRIGGER memories_vectors_stale AFTER UPDATE OF text, keywords
        ON memories BEGIN
            DELETE FROM memory_vectors WHERE memory_id = old.id;
        END""",
        """CREATE TRIGGER memories_vectors_gone AFTER DELETE ON memories BEGIN
            DELETE FROM memory_vectors WHERE memory_id = old.id;
        END""",
    ),
    (
        # The first production ALTER TABLE: 1 marks a note every user may read.
        # `user_id` stays the creator. The FTS table and triggers do not see the column.
        "ALTER TABLE memories ADD COLUMN shared INTEGER NOT NULL DEFAULT 0",
    ),
)

# The same three steps on PostgreSQL; both tuples must keep the same number of steps
# (the version numbers mean the same thing, and store.py reports `len` of the SQLite one).
# `search` is a generated tsvector with a GIN index instead of an FTS5 table, so no
# trigger maintains it. The marker triggers become one PL/pgSQL function, because a
# Postgres trigger needs a function; `UPDATE OF text, keywords` keeps an
# `updated_at`-only update from clearing the marker, as in SQLite (spike, PostgreSQL 17).
MEMORY_MIGRATIONS_POSTGRES: tuple[Migration, ...] = (
    (
        """CREATE TABLE memories (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id TEXT NOT NULL,
            text TEXT NOT NULL,
            keywords TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            search tsvector GENERATED ALWAYS AS (
                to_tsvector('english', text || ' ' || keywords)
            ) STORED
        )""",
        "CREATE INDEX memories_user_id ON memories (user_id)",
        "CREATE INDEX memories_search ON memories USING gin (search)",
    ),
    (
        """CREATE TABLE memory_vectors (
            collection TEXT NOT NULL,
            memory_id BIGINT NOT NULL,
            PRIMARY KEY (collection, memory_id)
        )""",
        """CREATE TABLE memory_vector_meta (
            collection TEXT PRIMARY KEY,
            model_id TEXT NOT NULL,
            dim INTEGER NOT NULL
        )""",
        """CREATE FUNCTION memories_clear_markers() RETURNS trigger AS $$
        BEGIN
            DELETE FROM memory_vectors WHERE memory_id = OLD.id;
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql""",
        """CREATE TRIGGER memories_vectors_stale AFTER UPDATE OF text, keywords
        ON memories FOR EACH ROW EXECUTE FUNCTION memories_clear_markers()""",
        """CREATE TRIGGER memories_vectors_gone AFTER DELETE
        ON memories FOR EACH ROW EXECUTE FUNCTION memories_clear_markers()""",
    ),
    ("ALTER TABLE memories ADD COLUMN shared INTEGER NOT NULL DEFAULT 0",),
)
_MIGRATIONS_BY_DIALECT: dict[str, tuple[Migration, ...]] = {
    "sqlite": MEMORY_MIGRATIONS,
    "postgres": MEMORY_MIGRATIONS_POSTGRES,
}

MAX_TEXT_CHARS = 1500
MAX_KEYWORDS = 8
MAX_KEYWORD_CHARS = 30
MAX_SEARCH_RESULTS = 10
_MAX_QUERY_TERMS = 32  # a long query must not become a huge MATCH expression

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# ASCII digits only: `\d` would accept other scripts' digits, and `fullmatch` refuses a
# trailing newline that `$` would let through.
_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
# Stored newline-joined, not space-joined: a keyword may contain a space, and FTS5
# treats a newline as a separator just like a space, so the split is lossless.
_KEYWORD_SEPARATOR = "\n"
_KEYWORD_PATTERN = re.compile(r"[a-z0-9]+(?:[- ][a-z0-9]+)*")
_WORD = re.compile(r"\w+")
# Function words that carry no topic. They are dropped from queries only (never from
# saved text, keywords or the FTS index): ORed into a MATCH they would make nearly
# every memory a hit. Compared lowercase and whole, never stemmed or by length, so
# short content words ("ai", "go", "pi") and numbers stay searchable.
_STOP_WORDS = frozenset(
    """
    a about above after again all also am an and any are aren as at be because been
    before being below between both but by can could couldn d did didn do does doesn
    doing don down during each few for from further had hadn has hasn have haven having
    he her here hers herself him himself his how i if in into is isn it its itself ll
    just me more most mustn my myself needn no nor not now of off on once only or other
    our ours ourselves out over own re s shall shan she should shouldn so some such t
    than that the their theirs them themselves then there these they this those through
    to too under until up ve very was wasn we were weren what when where which while
    who whom why will with won would wouldn y you your yours yourself yourselves
    """.split()  # noqa: SIM905  (150 words read better as prose than one per line)
)
_T = TypeVar("_T")
_COLUMNS = "id, user_id, text, keywords, created_at, updated_at, shared"
_SEARCH_SQLITE = (
    "SELECT m.id, m.user_id, m.text, m.keywords, m.created_at, m.updated_at, m.shared "
    "FROM memories_fts JOIN memories AS m ON m.id = memories_fts.rowid "
    "WHERE memories_fts MATCH ? AND (m.user_id = ? OR m.shared = 1) "
    "ORDER BY bm25(memories_fts), m.id DESC LIMIT ?"
)
# Plain ts_rank, not a length-normalised one: it gave the same top hit and order as
# bm25 on every spike query, the normalised variants flipped one.
_SEARCH_POSTGRES = (
    "SELECT m.id, m.user_id, m.text, m.keywords, m.created_at, m.updated_at, m.shared "
    "FROM memories AS m, websearch_to_tsquery('english', ?) AS q "
    "WHERE m.search @@ q AND (m.user_id = ? OR m.shared = 1) "
    "ORDER BY ts_rank(m.search, q) DESC, m.id DESC LIMIT ?"
)


class MemoryValidationError(ValueError):
    """The input is not an acceptable memory; the message says what to change."""


class MemoryStorageError(RuntimeError):
    """A PostgreSQL call of the memory store failed; the message is the type name only.

    A psycopg message names the host, port and user, quotes a failing row or says what
    the server refused, and `redact` knows only key and url shapes, so no front end,
    tool observation, notice or trace may carry it. Raised `from None`: the original
    is not kept either (a traceback prints a chained cause).
    """


@dataclass(frozen=True)
class Memory:
    id: int
    user_id: str
    text: str
    keywords: tuple[str, ...]
    created_at: str
    updated_at: str
    shared: bool = False  # readable by every user; `user_id` is still the creator


def check_timestamp(value: object, now: str) -> str:
    """`value` when it is a real `YYYY-MM-DDTHH:MM:SSZ` stamp not later than `now`.

    A caller-chosen timestamp must not pin a note to the future: `list` and recall
    order by `updated_at`, so a far-future value would sort first until the clock
    caught up. An old value only sorts a note lower, so the lower bound is open. The
    message never echoes `value`: it comes from a file the human did not write.
    """
    if not isinstance(value, str) or not _TIMESTAMP_PATTERN.fullmatch(value):
        raise MemoryValidationError("timestamp must look like 2026-11-02T09:30:15Z")
    try:
        datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError:
        raise MemoryValidationError("timestamp is not a real date and time") from None
    if value > now:  # the same fixed-width format sorts like time
        raise MemoryValidationError("timestamp is later than the current time")
    return value


def _timestamp_pair(
    created_at: object, updated_at: object, now: str
) -> tuple[str, str]:
    """Both stamps for a new row; a missing member never makes the pair invalid.

    Both missing: `now`. One missing: it copies the other, so `updated` is never
    before `created`. Both given: `updated` before `created` is refused.
    """
    if created_at is None and updated_at is None:
        return now, now
    created = None if created_at is None else check_timestamp(created_at, now)
    updated = None if updated_at is None else check_timestamp(updated_at, now)
    created = updated if created is None else created
    updated = created if updated is None else updated
    assert created is not None and updated is not None
    if updated < created:
        raise MemoryValidationError("updated timestamp is earlier than the created one")
    return created, updated


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _check_user_id(user_id: object) -> str:
    if not isinstance(user_id, str) or not user_id:
        raise MemoryValidationError("user_id must be a non-empty string")
    return user_id


def _check_secret_free(value: str, what: str) -> None:
    # The message must not echo the value.
    if redact(value, limit=UNCUT) != value:
        raise MemoryValidationError(
            f"{what} looks like it contains a secret (API key or credential); "
            "memories must not hold secrets, leave it out"
        )


def _check_text(text: object) -> str:
    if not isinstance(text, str):
        raise MemoryValidationError("text must be a string")
    text = text.strip()
    if not 1 <= len(text) <= MAX_TEXT_CHARS:
        raise MemoryValidationError(
            f"text must be 1 to {MAX_TEXT_CHARS} characters after trimming, "
            f"got {len(text)}; write a shorter note"
        )
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise MemoryValidationError(
            "text must not contain control characters (newline and tab included); "
            "write it as one plain line"
        )
    _check_secret_free(text, "text")
    return text


def _check_keywords(keywords: object) -> tuple[str, ...]:
    if isinstance(keywords, str) or not isinstance(keywords, Sequence):
        raise MemoryValidationError("keywords must be a list of strings")
    unique: list[str] = []
    for keyword in keywords:
        if not isinstance(keyword, str):
            raise MemoryValidationError("every keyword must be a string")
        if not 1 <= len(keyword) <= MAX_KEYWORD_CHARS:
            raise MemoryValidationError(
                f"each keyword must be 1 to {MAX_KEYWORD_CHARS} characters, "
                f"got {len(keyword)}"
            )
        if not _KEYWORD_PATTERN.fullmatch(keyword):
            raise MemoryValidationError(
                "each keyword must be lowercase English letters and digits, with "
                "single hyphens or spaces inside; fix the keyword "
                f"{keyword[:MAX_KEYWORD_CHARS]!r}"
            )
        _check_secret_free(keyword, "a keyword")
        if keyword not in unique:
            unique.append(keyword)
    if not 1 <= len(unique) <= MAX_KEYWORDS:
        raise MemoryValidationError(
            f"give 1 to {MAX_KEYWORDS} distinct keywords, got {len(unique)}"
        )
    return tuple(unique)


def validate_note(text: object, keywords: object) -> tuple[str, tuple[str, ...]]:
    """The trimmed text and the distinct keywords, or `MemoryValidationError`.

    The store's own checks, public so a caller can test a note before it saves it.
    """
    return _check_text(text), _check_keywords(keywords)


def _query_words(query: object) -> list[str]:
    """The user's meaningful words, at most `_MAX_QUERY_TERMS`; empty when there is none.

    Stop words are dropped before the cap, so a long message keeps its first
    `_MAX_QUERY_TERMS` meaningful words; a message of stop words only has no usable word.
    """
    if not isinstance(query, str):
        raise MemoryValidationError("query must be a string")
    words = [word for word in _WORD.findall(query) if word.lower() not in _STOP_WORDS]
    return words[:_MAX_QUERY_TERMS]


def _match_expression(query: object) -> str | None:
    """The words as quoted FTS5 OR terms (SQLite); None when there is none."""
    # \w never matches a double quote, so each word is a complete FTS5 string.
    return " OR ".join(f'"{word}"' for word in _query_words(query)) or None


def _websearch_expression(words: list[str]) -> str:
    """The words as quoted `websearch_to_tsquery` OR terms (PostgreSQL).

    A quoted word can never act as an operator, so `or`, `and` and `not` are safe
    (and a word cannot hold a double quote).
    """
    return " or ".join(f'"{word}"' for word in words)


def _row_to_memory(row: tuple) -> Memory:
    id_, user_id, text, keywords, created_at, updated_at, shared = row
    return Memory(
        id_,
        user_id,
        text,
        tuple(keywords.split(_KEYWORD_SEPARATOR)),
        created_at,
        updated_at,
        bool(shared),
    )


class MemoryStore:
    def __init__(
        self,
        relational: RelationalStore,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        dialect = getattr(relational, "dialect", None)
        if dialect not in _MIGRATIONS_BY_DIALECT:
            raise ValueError(
                f"no memory schema for dialect {dialect!r}; "
                f"known: {', '.join(_MIGRATIONS_BY_DIALECT)}"
            )
        self._migrations = _MIGRATIONS_BY_DIALECT[dialect]
        self._dialect = dialect
        self._relational = relational
        self._clock = clock
        self._migrate_lock = asyncio.Lock()
        self._migrated = False

    def take_notice(self) -> str | None:
        """A one-line notice for the human, once; None here (only an index raises one)."""
        return None

    async def save(
        self,
        user_id: str,
        text: str,
        keywords: Sequence[str],
        shared: bool = False,
        *,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> Memory:
        """Store a note, private unless `shared`.

        No tool passes `shared`: only the human `/memory add --shared` command does,
        after its own admin check (this method authorises nothing). The same holds for
        `created_at` and `updated_at`, which only `/memory import` passes to keep a
        file's timestamps; None means the injected clock, as for every other caller.
        """
        user_id = _check_user_id(user_id)
        text, checked = validate_note(text, keywords)
        now = self._now()
        created, updated = _timestamp_pair(created_at, updated_at, now)
        await self._ensure_migrated()
        rows = await self._query(
            "INSERT INTO memories (user_id, text, keywords, created_at, updated_at, shared) "
            f"VALUES (?, ?, ?, ?, ?, ?) RETURNING {_COLUMNS}",
            (
                user_id,
                text,
                _KEYWORD_SEPARATOR.join(checked),
                created,
                updated,
                1 if shared else 0,
            ),
        )
        return _row_to_memory(rows[0])

    def current_timestamp(self) -> str:
        """The injected clock's time in the store's format (for an export header)."""
        return self._now()

    async def has_text(self, user_id: str, text: str) -> bool:
        """True when this user's own note or any shared note has exactly this text."""
        user_id = _check_user_id(user_id)
        await self._ensure_migrated()
        rows = await self._query(
            "SELECT 1 FROM memories WHERE text = ? AND (user_id = ? OR shared = 1) LIMIT 1",
            (text, user_id),
        )
        return bool(rows)

    async def update(
        self,
        user_id: str,
        id: int,
        text: str | None = None,
        keywords: Sequence[str] | None = None,
    ) -> Memory | None:
        """Change text and/or keywords; None when this user has no such id.

        A shared note is refused for everyone, its creator included: a model tool or the
        reflection step runs as the creator and must never alter what every user reads.
        """
        user_id = _check_user_id(user_id)
        if text is None and keywords is None:
            raise MemoryValidationError("give text, keywords or both to update")
        assignments: list[str] = []
        params: list[Any] = []
        if text is not None:
            assignments.append("text = ?")
            params.append(_check_text(text))
        if keywords is not None:
            assignments.append("keywords = ?")
            params.append(_KEYWORD_SEPARATOR.join(_check_keywords(keywords)))
        await self._ensure_migrated()
        assignments.append("updated_at = ?")
        params.append(self._now())
        rows = await self._query(
            f"UPDATE memories SET {', '.join(assignments)} "
            f"WHERE id = ? AND user_id = ? AND shared = 0 RETURNING {_COLUMNS}",
            (*params, id, user_id),
        )
        return _row_to_memory(rows[0]) if rows else None

    async def forget(self, user_id: str, id: int) -> bool:
        """Hard-delete one entry; False when this user has no such id or it is shared."""
        user_id = _check_user_id(user_id)
        await self._ensure_migrated()
        rows = await self._query(
            "DELETE FROM memories WHERE id = ? AND user_id = ? AND shared = 0 RETURNING id",
            (id, user_id),
        )
        return bool(rows)

    async def forget_shared(self, id: int) -> Memory | None:
        """Hard-delete one SHARED note whoever created it; None when it is private or unknown.

        Only the human `/memory forget` command calls this, never a tool. It does no
        authorisation: the admin check lives in the command layer. `shared = 1` keeps it
        from touching a private note. The marker rows go through the delete trigger.
        """
        await self._ensure_migrated()
        rows = await self._query(
            f"DELETE FROM memories WHERE id = ? AND shared = 1 RETURNING {_COLUMNS}",
            (id,),
        )
        return _row_to_memory(rows[0]) if rows else None

    async def set_shared(self, user_id: str, id: int, shared: bool) -> Memory | None:
        """Publish or unpublish the creator's own note; None when this user has no such id.

        Only the human `/memory` commands call this, never a tool. `updated_at` stays (the
        text did not change). The markers go too: the marker triggers fire only on a text
        or keyword change, and the vector payload carries the flag, so every collection
        must embed the note again (`IndexedMemoryStore` does it for the current one).
        """
        user_id = _check_user_id(user_id)
        await self._ensure_migrated()
        rows = await self._query(
            "UPDATE memories SET shared = ? WHERE id = ? AND user_id = ? "
            f"RETURNING {_COLUMNS}",
            (1 if shared else 0, id, user_id),
        )
        if not rows:
            return None
        await self._driver_errors_as_type_only(
            self._relational.execute(
                "DELETE FROM memory_vectors WHERE memory_id = ?", (id,)
            )
        )
        return _row_to_memory(rows[0])

    async def get(self, user_id: str, id: int) -> Memory | None:
        """The user's own note or a shared one; None for another user's private note."""
        user_id = _check_user_id(user_id)
        await self._ensure_migrated()
        rows = await self._query(
            f"SELECT {_COLUMNS} FROM memories "
            "WHERE id = ? AND (user_id = ? OR shared = 1)",
            (id, user_id),
        )
        return _row_to_memory(rows[0]) if rows else None

    async def search(self, user_id: str, query: str, k: int = 5) -> "list[Memory]":
        """The best matches (bm25 on SQLite, ts_rank on PostgreSQL), at most `k` (1 to 10)."""
        user_id = _check_user_id(user_id)
        words = _query_words(query)
        if not words:
            return []
        await self._ensure_migrated()
        limit = max(1, min(MAX_SEARCH_RESULTS, k))
        if self._dialect == "postgres":
            statement, expression = _SEARCH_POSTGRES, _websearch_expression(words)
        else:
            statement, expression = _SEARCH_SQLITE, _match_expression(query)
        rows = await self._query(statement, (expression, user_id, limit))
        return [_row_to_memory(row) for row in rows]

    # Last in the class: a method named `list` hides the builtin in annotations
    # of any method defined after it.
    async def list(
        self, user_id: str, limit: int = 50, offset: int = 0
    ) -> "list[Memory]":
        """The user's own and the shared notes, newest-updated first."""
        user_id = _check_user_id(user_id)
        await self._ensure_migrated()
        rows = await self._query(
            f"SELECT {_COLUMNS} FROM memories WHERE (user_id = ? OR shared = 1) "
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, max(0, limit), max(0, offset)),
        )
        return [_row_to_memory(row) for row in rows]

    async def _query(self, statement: str, params: Sequence[Any]) -> "list[tuple]":
        return await self._driver_errors_as_type_only(
            self._relational.query(statement, params)
        )

    async def _driver_errors_as_type_only(self, call: Awaitable[_T]) -> _T:
        """Await a relational call; a PostgreSQL driver error becomes its type name only.

        SQLite messages keep their text (no host or credentials in them), as before.
        """
        try:
            return await call
        except database_error_classes() as error:
            if self._dialect != "postgres":
                raise
            raise MemoryStorageError(
                f"memory storage failed ({type(error).__name__})"
            ) from None

    def _now(self) -> str:
        return self._clock().astimezone(UTC).strftime(_TIMESTAMP_FORMAT)

    async def _ensure_migrated(self) -> None:
        async with self._migrate_lock:
            if not self._migrated:
                await self._driver_errors_as_type_only(
                    self._relational.migrate(MEMORY_NAMESPACE, self._migrations)
                )
                self._migrated = True
