"""Agent memory on the PostgreSQL dialect: DDL, search statement and marker upserts.

Offline: a recording fake relational object with `dialect = "postgres"` stands in for
`PostgresRelationalStore`, and `translate_sql` (the adapter's own function) checks
every statement text. What needs a real server (the tsvector, the triggers, the GIN
index) is exercised by the live tests, not here.
"""

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

import llm_harness.memory as memory_module
import llm_harness.memory_index as memory_index_module
from llm_harness.embeddings import Embedder
from llm_harness.memory import (
    MEMORY_MIGRATIONS,
    MEMORY_MIGRATIONS_POSTGRES,
    MEMORY_NAMESPACE,
    MemoryStore,
)
from llm_harness.memory_index import IndexedMemoryStore
from llm_harness.storage.postgres import translate_sql
from llm_harness.store import optional_schema_versions
from tests.fakes import ConceptEmbedder, FakeVectorStore
from tests.test_memory_index_store import model
from tests.test_store_postgres import FakeRelational

NOW = datetime(2026, 11, 3, 9, 30, 15, tzinfo=UTC)
STAMP = "2026-11-03T09:30:15Z"
HOST = "db-secret-host.example"
USER = "db-secret-user"
PASSWORD = "sk-test-SECRET-123"
ROW_TEXT = "Likes tea with oat milk"
ARBITRARY_ROW = (7, "alice", ROW_TEXT, "tea", STAMP, STAMP, 0)

# The SQLite text as it was before the PostgreSQL work: the proof that SQLite did not
# change. `git show HEAD:src/llm_harness/memory.py` already carries the same steps (the
# vector-index step is committed at HEAD too, only uncommitted in the working tree of
# earlier features), so these are copied from the file before it was edited.
OLD_SQLITE_MIGRATIONS = (
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
)
# Shared notes (memory-shared-scope): the search statements select `shared` and read
# `(user_id = ? OR shared = 1)`, so the old search text is no longer asserted.
SQLITE_SEARCH = (
    "SELECT m.id, m.user_id, m.text, m.keywords, m.created_at, m.updated_at, m.shared "
    "FROM memories_fts JOIN memories AS m ON m.id = memories_fts.rowid "
    "WHERE memories_fts MATCH ? AND (m.user_id = ? OR m.shared = 1) "
    "ORDER BY bm25(memories_fts), m.id DESC LIMIT ?"
)
SHARED_STEP = ("ALTER TABLE memories ADD COLUMN shared INTEGER NOT NULL DEFAULT 0",)
POSTGRES_SEARCH = (
    "SELECT m.id, m.user_id, m.text, m.keywords, m.created_at, m.updated_at, m.shared "
    "FROM memories AS m, websearch_to_tsquery('english', ?) AS q "
    "WHERE m.search @@ q AND (m.user_id = ? OR m.shared = 1) "
    "ORDER BY ts_rank(m.search, q) DESC, m.id DESC LIMIT ?"
)
UPSERT = (
    "INSERT INTO memory_vectors (collection, memory_id) VALUES (?, ?) "
    "ON CONFLICT (collection, memory_id) DO NOTHING"
)


class FakeMemoryRelational(FakeRelational):
    """The async slice `MemoryStore` uses; records `(sql, params)` in call order."""

    def __init__(self, dialect: str = "postgres") -> None:
        super().__init__(dialect)
        self.calls: list[tuple[str, Sequence[Any]]] = []
        self.answer: Callable[[str], list[tuple]] = lambda sql: [ARBITRARY_ROW]
        self.error: Callable[[str], Exception | None] = lambda sql: None

    async def migrate(self, namespace: str, migrations: Any, **_: Any) -> int:
        self.migrations.append((namespace, migrations, False))
        return len(migrations)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        self.calls.append((sql, params))
        failure = self.error(sql)
        if failure is not None:
            raise failure
        return self.answer(sql)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        self.calls.append((sql, params))
        failure = self.error(sql)
        if failure is not None:
            raise failure
        return 1


@pytest.fixture
def fake() -> FakeMemoryRelational:
    return FakeMemoryRelational()


def store_on(fake: FakeMemoryRelational) -> MemoryStore:
    return MemoryStore(fake, lambda: NOW)  # type: ignore[arg-type]


def placeholders(sql: str) -> int:
    return sql.count("?")


# --- SQLite is unchanged -------------------------------------------------------------


def test_the_first_two_sqlite_migration_steps_are_byte_identical_to_the_old_text():
    assert MEMORY_MIGRATIONS[:2] == OLD_SQLITE_MIGRATIONS
    assert MEMORY_MIGRATIONS[2] == SHARED_STEP


def test_the_sqlite_search_statement_is_the_planned_text():
    assert memory_module._SEARCH_SQLITE == SQLITE_SEARCH


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("tea and coffee", '"tea" OR "coffee"'),
        ("what is the capital of France", '"capital" OR "France"'),
        ("the of and", None),
        ('quote " inside', '"quote" OR "inside"'),
    ],
)
def test_the_sqlite_match_expression_is_unchanged(query: str, expected: str | None):
    assert memory_module._match_expression(query) == expected


async def test_a_sqlite_store_searches_with_its_statement_and_expression():
    fake = FakeMemoryRelational("sqlite")
    await store_on(fake).search("alice", "tea or coffee", 3)
    assert fake.calls == [(SQLITE_SEARCH, ('"tea" OR "coffee"', "alice", 3))]
    assert fake.migrations[0][1] is MEMORY_MIGRATIONS


# --- dialect selection ---------------------------------------------------------------


async def test_a_postgres_store_migrates_with_the_postgres_tuple(
    fake: FakeMemoryRelational,
):
    await store_on(fake).get("alice", 7)
    assert fake.migrations == [(MEMORY_NAMESPACE, MEMORY_MIGRATIONS_POSTGRES, False)]


@pytest.mark.parametrize("dialect", ["mysql", "", None])
def test_an_unknown_dialect_is_refused_with_the_known_ones_named(dialect: Any):
    with pytest.raises(ValueError) as info:
        store_on(FakeMemoryRelational(dialect))
    assert str(info.value) == (
        f"no memory schema for dialect {dialect!r}; known: sqlite, postgres"
    )


def test_a_relational_store_without_a_dialect_is_refused():
    class NoDialect:
        pass

    with pytest.raises(ValueError, match="known: sqlite, postgres"):
        MemoryStore(NoDialect())  # type: ignore[arg-type]


def test_every_dialect_has_the_same_number_of_steps_so_versions_stay_aligned():
    assert len(MEMORY_MIGRATIONS) == len(MEMORY_MIGRATIONS_POSTGRES) == 3
    assert {d: len(m) for d, m in memory_module._MIGRATIONS_BY_DIALECT.items()} == {
        "sqlite": 3,
        "postgres": 3,
    }


def test_the_schema_status_reports_memory_at_version_3():
    assert optional_schema_versions()[MEMORY_NAMESPACE] == 3


# --- the PostgreSQL DDL --------------------------------------------------------------


def test_the_postgres_migrations_are_exactly_the_three_planned_steps():
    first, second, third = MEMORY_MIGRATIONS_POSTGRES
    assert third == SHARED_STEP
    assert first[1:] == (
        "CREATE INDEX memories_user_id ON memories (user_id)",
        "CREATE INDEX memories_search ON memories USING gin (search)",
    )
    assert len(first) == 3
    assert normalised(first[0]) == normalised(
        """CREATE TABLE memories (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id TEXT NOT NULL, text TEXT NOT NULL, keywords TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            search tsvector GENERATED ALWAYS AS (
                to_tsvector('english', text || ' ' || keywords)
            ) STORED
        )"""
    )
    assert len(second) == 5
    assert normalised(second[0]) == normalised(
        "CREATE TABLE memory_vectors (collection TEXT NOT NULL, "
        "memory_id BIGINT NOT NULL, PRIMARY KEY (collection, memory_id))"
    )
    assert normalised(second[1]) == normalised(
        "CREATE TABLE memory_vector_meta (collection TEXT PRIMARY KEY, "
        "model_id TEXT NOT NULL, dim INTEGER NOT NULL)"
    )


def normalised(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


def test_the_marker_function_deletes_the_markers_of_the_old_row():
    function = MEMORY_MIGRATIONS_POSTGRES[1][2]
    assert normalised(function) == (
        "CREATE FUNCTION memories_clear_markers() RETURNS trigger AS $$ BEGIN "
        "DELETE FROM memory_vectors WHERE memory_id = OLD.id; RETURN NULL; "
        "END $$ LANGUAGE plpgsql"
    )
    # Several lines, as in the spike: a single-line body would hide a typo.
    assert function.count("\n") >= 4


def test_the_two_triggers_clear_markers_on_content_change_and_on_delete_only():
    stale, gone = MEMORY_MIGRATIONS_POSTGRES[1][3:]
    assert normalised(stale) == (
        "CREATE TRIGGER memories_vectors_stale AFTER UPDATE OF text, keywords "
        "ON memories FOR EACH ROW EXECUTE FUNCTION memories_clear_markers()"
    )
    assert normalised(gone) == (
        "CREATE TRIGGER memories_vectors_gone AFTER DELETE "
        "ON memories FOR EACH ROW EXECUTE FUNCTION memories_clear_markers()"
    )


def test_no_postgres_ddl_statement_has_a_placeholder_or_a_percent_sign():
    # migrate_sync runs DDL with no parameters, so translate_sql leaves it unchanged.
    for step in MEMORY_MIGRATIONS_POSTGRES:
        for statement in step:
            assert "?" not in statement
            assert "%" not in statement
            assert translate_sql(statement, None) == (statement, None)


# --- the PostgreSQL search statement -------------------------------------------------


def test_the_postgres_search_statement_is_the_planned_text():
    assert memory_module._SEARCH_POSTGRES == POSTGRES_SEARCH


@pytest.mark.parametrize(
    ("query", "expression"),
    [
        ("tea", '"tea"'),
        ("tea coffee", '"tea" or "coffee"'),
        ("what is the capital of France", '"capital" or "France"'),
        # Stop words are dropped, so the operators are never even words here ...
        ("tea or coffee and not milk", '"tea" or "coffee" or "milk"'),
        # ... and a hostile query keeps only \w words, so no quote or operator survives.
        ("tea'; DROP TABLE memories; --", '"tea" or "DROP" or "TABLE" or "memories"'),
        (
            '"quoted" -minus +plus (group) OR',
            '"quoted" or "minus" or "plus" or "group"',
        ),
        ("café crème", '"café" or "crème"'),
        ("ORACLE ANDROID NOTEBOOK", '"ORACLE" or "ANDROID" or "NOTEBOOK"'),
    ],
)
async def test_the_postgres_search_passes_quoted_words_user_and_limit(
    fake: FakeMemoryRelational, query: str, expression: str
):
    fake.answer = lambda sql: []
    assert await store_on(fake).search("alice", query, 4) == []
    assert fake.calls == [(POSTGRES_SEARCH, (expression, "alice", 4))]


@pytest.mark.parametrize("operator", ["or", "and", "not", "OR", "Not"])
async def test_a_word_that_is_an_operator_is_quoted_never_bare(
    fake: FakeMemoryRelational, operator: str
):
    # They are stop words, so a query of one finds nothing and runs no SQL ...
    assert await store_on(fake).search("alice", operator) == []
    assert fake.calls == []
    # ... but the expression builder would quote one that slipped through.
    assert memory_module._websearch_expression([operator, "tea"]) == (
        f'"{operator}" or "tea"'
    )


async def test_a_query_of_stop_words_only_runs_no_sql_and_does_not_migrate(
    fake: FakeMemoryRelational,
):
    assert await store_on(fake).search("alice", "what is the of and or not") == []
    assert await store_on(fake).search("alice", "   ") == []
    assert (fake.calls, fake.migrations) == ([], [])


async def test_a_long_query_keeps_its_first_32_meaningful_words(
    fake: FakeMemoryRelational,
):
    fake.answer = lambda sql: []
    words = [f"word{i}" for i in range(40)]
    await store_on(fake).search("alice", " ".join(words))
    expression = fake.calls[0][1][0]
    assert expression == " or ".join(f'"{word}"' for word in words[:32])
    assert expression.count(" or ") == 31


async def test_the_search_limit_is_clamped_and_the_rows_become_memories(
    fake: FakeMemoryRelational,
):
    hits = await store_on(fake).search("alice", "tea", 99)
    assert fake.calls[0][1][2] == 10
    assert [(m.id, m.text, m.keywords) for m in hits] == [(7, ROW_TEXT, ("tea",))]
    await store_on(fake).search("alice", "tea", 0)
    assert fake.calls[1][1][2] == 1


def test_a_non_string_query_is_still_a_validation_error(fake: FakeMemoryRelational):
    with pytest.raises(memory_module.MemoryValidationError, match="query must be"):
        memory_module._query_words(42)


# --- the other statements are unchanged ----------------------------------------------


async def test_save_update_forget_get_list_issue_the_same_statements_as_before(
    fake: FakeMemoryRelational,
):
    store = store_on(fake)
    await store.save("alice", ROW_TEXT, ["tea"])
    await store.update("alice", 7, text="Likes tea")
    await store.update("alice", 7, keywords=["tea"])
    await store.forget("alice", 7)
    await store.get("alice", 7)
    await store.list("alice", 5, 10)
    columns = "id, user_id, text, keywords, created_at, updated_at, shared"
    # `shared` joined the INSERT with `/memory add --shared`; the other statements stand.
    insert = (
        "INSERT INTO memories "
        "(user_id, text, keywords, created_at, updated_at, shared) "
        f"VALUES (?, ?, ?, ?, ?, ?) RETURNING {columns}"
    )
    listing = (
        f"SELECT {columns} FROM memories WHERE (user_id = ? OR shared = 1) "
        "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    returning = f"WHERE id = ? AND user_id = ? AND shared = 0 RETURNING {columns}"
    assert [sql for sql, _ in fake.calls] == [
        insert,
        f"UPDATE memories SET text = ?, updated_at = ? {returning}",
        f"UPDATE memories SET keywords = ?, updated_at = ? {returning}",
        "DELETE FROM memories WHERE id = ? AND user_id = ? AND shared = 0 RETURNING id",
        f"SELECT {columns} FROM memories WHERE id = ? AND (user_id = ? OR shared = 1)",
        listing,
    ]
    assert len(fake.migrations) == 1  # once per store, before the first statement


async def test_every_postgres_memory_statement_translates_with_matching_placeholders(
    fake: FakeMemoryRelational,
):
    store = store_on(fake)
    await store.save("alice", ROW_TEXT, ["tea"])
    await store.update("alice", 7, text="Likes tea", keywords=["tea"])
    await store.forget("alice", 7)
    await store.get("alice", 7)
    await store.list("alice")
    await store.search("alice", "tea coffee")
    await store.set_shared("alice", 7, True)
    # The statement texts of memory_index.py that run on the same adapter.
    statements = [
        (sql, params) for sql, params in fake.calls if placeholders(sql) == len(params)
    ]
    assert len(statements) == len(fake.calls) == 8  # set_shared runs two
    for sql, params in fake.calls:
        translated, bound = translate_sql(sql, params)
        assert translated.count("%s") == placeholders(sql) == len(params)
        assert "?" not in translated
        assert bound == tuple(params)


@pytest.mark.parametrize(
    ("sql", "count"),
    [
        (UPSERT, 2),
        (
            "INSERT INTO memory_vector_meta (collection, model_id, dim) VALUES (?, ?, ?)",
            3,
        ),
        ("SELECT dim FROM memory_vector_meta WHERE collection = ?", 1),
        ("SELECT model_id, dim FROM memory_vector_meta WHERE collection = ?", 1),
    ],
)
def test_the_index_statements_translate_with_matching_placeholders(
    sql: str, count: int
):
    translated, _ = translate_sql(sql, tuple(range(count)))
    assert translated.count("%s") == count
    assert "ON CONFLICT (collection, memory_id) DO NOTHING" in translated or count != 2


async def test_set_shared_flips_the_flag_with_an_int_and_clears_the_markers(
    fake: FakeMemoryRelational,
):
    columns = "id, user_id, text, keywords, created_at, updated_at, shared"
    fake.answer = lambda sql: [(7, "alice", ROW_TEXT, "tea", STAMP, STAMP, 1)]
    memory = await store_on(fake).set_shared("alice", 7, True)
    assert memory is not None
    assert memory.shared is True
    assert fake.calls == [
        (
            f"UPDATE memories SET shared = ? WHERE id = ? AND user_id = ? RETURNING {columns}",
            (1, 7, "alice"),
        ),
        ("DELETE FROM memory_vectors WHERE memory_id = ?", (7,)),
    ]


async def test_set_shared_of_a_note_that_is_not_the_users_clears_nothing(
    fake: FakeMemoryRelational,
):
    fake.answer = lambda sql: []
    assert await store_on(fake).set_shared("bob", 7, False) is None
    assert [params for _, params in fake.calls] == [(0, 7, "bob")]


async def test_forget_shared_deletes_by_id_and_shared_flag_only(
    fake: FakeMemoryRelational,
):
    columns = "id, user_id, text, keywords, created_at, updated_at, shared"
    fake.answer = lambda sql: [(7, "bob", ROW_TEXT, "tea", STAMP, STAMP, 1)]
    memory = await store_on(fake).forget_shared(7)
    assert memory is not None and memory.user_id == "bob" and memory.shared is True
    assert fake.calls == [
        (f"DELETE FROM memories WHERE id = ? AND shared = 1 RETURNING {columns}", (7,)),
    ]


async def test_forget_shared_without_a_returned_row_is_none(
    fake: FakeMemoryRelational,
):
    fake.answer = lambda sql: []
    assert await store_on(fake).forget_shared(7) is None
    assert [params for _, params in fake.calls] == [(7,)]


# --- the marker upserts --------------------------------------------------------------


def index_rig(
    fake: FakeMemoryRelational,
) -> tuple[IndexedMemoryStore, FakeVectorStore]:
    inner = fake.answer
    # No meta row yet: `_check_model` then inserts it instead of comparing.
    fake.answer = lambda sql: (
        [] if sql.startswith("SELECT model_id, dim") else inner(sql)
    )
    vectors = FakeVectorStore()
    provider = ConceptEmbedder()
    store = IndexedMemoryStore(
        fake,  # type: ignore[arg-type]
        vectors,
        lambda: Embedder(provider, model()),
        lambda: NOW,
    )
    return store, vectors


def test_no_source_file_uses_insert_or_replace():
    for module in (memory_module, memory_index_module):
        source = open(module.__file__).read()  # noqa: SIM115
        assert "INSERT OR REPLACE" not in source


def test_both_marker_upserts_are_portable_and_idempotent():
    source = open(memory_index_module.__file__).read()  # noqa: SIM115
    pattern = re.compile(
        r'"INSERT INTO memory_vectors \(collection, memory_id\) VALUES \(\?, \?\) "\s+'
        r'"ON CONFLICT \(collection, memory_id\) DO NOTHING"'
    )
    assert len(pattern.findall(source)) == 2


async def test_a_save_writes_its_marker_with_the_on_conflict_statement(
    fake: FakeMemoryRelational,
):
    store, _ = index_rig(fake)
    await store.save("alice", ROW_TEXT, ["tea"])
    upserts = [
        call for call in fake.calls if call[0].startswith("INSERT INTO memory_v")
    ]
    markers = [call for call in upserts if call[0] == UPSERT]
    assert len(markers) == 1
    assert markers[0][1][1] == 7
    assert store.index_off_reason is None


async def test_reindex_writes_its_markers_with_the_same_statement(
    fake: FakeMemoryRelational,
):
    def answer(sql: str) -> list[tuple]:
        if sql.startswith("SELECT COUNT(*)"):
            return [(1,)]
        if "NOT EXISTS" in sql:
            return [ARBITRARY_ROW]
        if sql.startswith("SELECT model_id, dim"):
            return []
        return [ARBITRARY_ROW]

    fake.answer = answer
    store, _ = index_rig(fake)
    assert await store.reindex("alice") == (1, 1)
    assert [sql for sql, _ in fake.calls].count(UPSERT) == 1
    for sql, params in fake.calls:
        assert translate_sql(sql, params)[0].count("%s") == placeholders(sql)


# --- no secret in what the memory path prints ----------------------------------------


def leaky_error() -> psycopg.Error:
    """A psycopg error whose text quotes the server, the user and a row."""
    return psycopg.OperationalError(
        f'connection to server at "{HOST}", user "{USER}" failed: password '
        f"{PASSWORD}; DETAIL: Failing row contains ({ROW_TEXT})"
    )


async def test_an_index_failure_notice_names_no_host_user_password_or_row_text(
    fake: FakeMemoryRelational,
):
    fake.error = lambda sql: leaky_error() if sql == UPSERT else None
    store, _ = index_rig(fake)
    saved = await store.save("alice", ROW_TEXT, ["tea"])
    assert saved.id == 7  # the write itself is returned whatever the index does
    notice = store.take_notice()
    assert notice is not None
    assert "OperationalError" in notice
    for secret in (HOST, USER, PASSWORD, ROW_TEXT):
        assert secret not in notice, secret
    assert store.take_notice() is None


async def test_a_non_driver_index_error_keeps_its_redacted_text(
    fake: FakeMemoryRelational,
):
    fake.error = lambda sql: ValueError("plain failure") if sql == UPSERT else None
    store, _ = index_rig(fake)
    await store.save("alice", ROW_TEXT, ["tea"])
    assert store.take_notice() == (
        "memory vector search is off: ValueError: plain failure; keyword search only"
    )


# --- save(shared=) and has_text (memory-add-command) ----------------------------------


@pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
@pytest.mark.parametrize(("shared", "flag"), [(True, 1), (False, 0)])
async def test_save_binds_the_shared_flag_as_the_last_parameter(
    dialect: str, shared: bool, flag: int
):
    fake = FakeMemoryRelational(dialect)
    await store_on(fake).save("alice", ROW_TEXT, ["tea"], shared=shared)
    ((sql, params),) = fake.calls
    assert sql.startswith("INSERT INTO memories (user_id, text, keywords, ")
    assert sql.count("?") == len(params) == 6
    assert params == ("alice", ROW_TEXT, "tea", STAMP, STAMP, flag)
    assert type(params[-1]) is int
    translated, bound = translate_sql(sql, params)
    assert translated.count("%s") == 6 and bound == tuple(params)


async def test_save_without_the_argument_binds_a_private_note(
    fake: FakeMemoryRelational,
):
    await store_on(fake).save("alice", ROW_TEXT, ["tea"])
    assert fake.calls[0][1][-1] == 0


@pytest.mark.parametrize("dialect", ["postgres", "sqlite"])
async def test_save_with_explicit_timestamps_binds_them_in_the_same_statement(
    dialect: str,
):
    created, updated = "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z"
    explicit = FakeMemoryRelational(dialect)
    await store_on(explicit).save(
        "alice", ROW_TEXT, ["tea"], created_at=created, updated_at=updated
    )
    default = FakeMemoryRelational(dialect)
    await store_on(default).save("alice", ROW_TEXT, ["tea"])
    ((sql, params),) = explicit.calls
    ((default_sql, default_params),) = default.calls
    assert sql == default_sql  # one INSERT ... RETURNING, no new statement
    assert sql.startswith(
        "INSERT INTO memories (user_id, text, keywords, created_at, updated_at, shared) "
    )
    assert sql.endswith(
        "RETURNING id, user_id, text, keywords, created_at, updated_at, shared"
    )
    assert params == ("alice", ROW_TEXT, "tea", created, updated, 0)
    assert default_params == ("alice", ROW_TEXT, "tea", STAMP, STAMP, 0)
    translated, bound = translate_sql(sql, params)
    assert translated.count("%s") == 6 and bound == tuple(params)


async def test_has_text_is_one_exact_lookup_over_own_and_shared_notes(
    fake: FakeMemoryRelational,
):
    fake.answer = lambda sql: [(1,)]
    assert await store_on(fake).has_text("alice", ROW_TEXT) is True
    fake.answer = lambda sql: []
    assert await store_on(fake).has_text("alice", ROW_TEXT) is False
    statement = (
        "SELECT 1 FROM memories WHERE text = ? AND (user_id = ? OR shared = 1) LIMIT 1"
    )
    assert fake.calls == [(statement, (ROW_TEXT, "alice"))] * 2
    translated, _ = translate_sql(statement, (ROW_TEXT, "alice"))
    assert translated.count("%s") == 2
