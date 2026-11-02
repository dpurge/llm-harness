"""Agent memory: short English notes kept per user in the relational store.

Not `specs/memory.md` (the developer log of `/code`). A memory is a short text
plus 1 to 8 keywords; both are indexed by an FTS5 table that triggers keep in
step with `memories`. Every query filters by `user_id`. Timestamps come from the
injected clock only. Text or keywords that look like a secret are refused, not
stored masked. Validation errors are `MemoryValidationError` with a short message,
so a tool can return them to the model as an observation.
"""

import asyncio
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from llm_harness.redact import redact
from llm_harness.storage.relational import Migration, RelationalStore
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
)

MAX_TEXT_CHARS = 1500
MAX_KEYWORDS = 8
MAX_KEYWORD_CHARS = 30
MAX_SEARCH_RESULTS = 10
_MAX_QUERY_TERMS = 32  # a long query must not become a huge MATCH expression

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
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
_COLUMNS = "id, user_id, text, keywords, created_at, updated_at"


class MemoryValidationError(ValueError):
    """The input is not an acceptable memory; the message says what to change."""


@dataclass(frozen=True)
class Memory:
    id: int
    user_id: str
    text: str
    keywords: tuple[str, ...]
    created_at: str
    updated_at: str


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


def _match_expression(query: object) -> str | None:
    """The user's meaningful words as quoted OR terms; None when there is none.

    Stop words are dropped before the cap, so a long message keeps its first
    `_MAX_QUERY_TERMS` meaningful words; a message of stop words only has no usable word.
    """
    if not isinstance(query, str):
        raise MemoryValidationError("query must be a string")
    words = [word for word in _WORD.findall(query) if word.lower() not in _STOP_WORDS]
    words = words[:_MAX_QUERY_TERMS]
    # \w never matches a double quote, so each word is a complete FTS5 string.
    return " OR ".join(f'"{word}"' for word in words) or None


def _row_to_memory(row: tuple) -> Memory:
    id_, user_id, text, keywords, created_at, updated_at = row
    return Memory(
        id_,
        user_id,
        text,
        tuple(keywords.split(_KEYWORD_SEPARATOR)),
        created_at,
        updated_at,
    )


class MemoryStore:
    def __init__(
        self,
        relational: RelationalStore,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._relational = relational
        self._clock = clock
        self._migrate_lock = asyncio.Lock()
        self._migrated = False

    async def save(self, user_id: str, text: str, keywords: Sequence[str]) -> Memory:
        user_id = _check_user_id(user_id)
        text = _check_text(text)
        checked = _check_keywords(keywords)
        await self._ensure_migrated()
        now = self._now()
        rows = await self._relational.query(
            "INSERT INTO memories (user_id, text, keywords, created_at, updated_at) "
            f"VALUES (?, ?, ?, ?, ?) RETURNING {_COLUMNS}",
            (user_id, text, _KEYWORD_SEPARATOR.join(checked), now, now),
        )
        return _row_to_memory(rows[0])

    async def update(
        self,
        user_id: str,
        id: int,
        text: str | None = None,
        keywords: Sequence[str] | None = None,
    ) -> Memory | None:
        """Change text and/or keywords; None when this user has no such id."""
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
        rows = await self._relational.query(
            f"UPDATE memories SET {', '.join(assignments)} "
            f"WHERE id = ? AND user_id = ? RETURNING {_COLUMNS}",
            (*params, id, user_id),
        )
        return _row_to_memory(rows[0]) if rows else None

    async def forget(self, user_id: str, id: int) -> bool:
        """Hard-delete one entry; False when this user has no such id."""
        user_id = _check_user_id(user_id)
        await self._ensure_migrated()
        rows = await self._relational.query(
            "DELETE FROM memories WHERE id = ? AND user_id = ? RETURNING id",
            (id, user_id),
        )
        return bool(rows)

    async def get(self, user_id: str, id: int) -> Memory | None:
        user_id = _check_user_id(user_id)
        await self._ensure_migrated()
        rows = await self._relational.query(
            f"SELECT {_COLUMNS} FROM memories WHERE id = ? AND user_id = ?",
            (id, user_id),
        )
        return _row_to_memory(rows[0]) if rows else None

    async def search(self, user_id: str, query: str, k: int = 5) -> "list[Memory]":
        """The best matches by bm25 over text and keywords, at most `k` (1 to 10)."""
        user_id = _check_user_id(user_id)
        expression = _match_expression(query)
        if expression is None:
            return []
        await self._ensure_migrated()
        limit = max(1, min(MAX_SEARCH_RESULTS, k))
        rows = await self._relational.query(
            "SELECT m.id, m.user_id, m.text, m.keywords, m.created_at, m.updated_at "
            "FROM memories_fts JOIN memories AS m ON m.id = memories_fts.rowid "
            "WHERE memories_fts MATCH ? AND m.user_id = ? "
            "ORDER BY bm25(memories_fts), m.id DESC LIMIT ?",
            (expression, user_id, limit),
        )
        return [_row_to_memory(row) for row in rows]

    # Last in the class: a method named `list` hides the builtin in annotations
    # of any method defined after it.
    async def list(
        self, user_id: str, limit: int = 50, offset: int = 0
    ) -> "list[Memory]":
        """Newest-updated first."""
        user_id = _check_user_id(user_id)
        await self._ensure_migrated()
        rows = await self._relational.query(
            f"SELECT {_COLUMNS} FROM memories WHERE user_id = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, max(0, limit), max(0, offset)),
        )
        return [_row_to_memory(row) for row in rows]

    def _now(self) -> str:
        return self._clock().astimezone(UTC).strftime(_TIMESTAMP_FORMAT)

    async def _ensure_migrated(self) -> None:
        async with self._migrate_lock:
            if not self._migrated:
                await self._relational.migrate(MEMORY_NAMESPACE, MEMORY_MIGRATIONS)
                self._migrated = True
