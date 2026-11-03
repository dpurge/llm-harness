"""`MemoryStore`: validation, per-user isolation, search and its sync with edits.

Offline everything runs on a throwaway SQLite file under `tmp_path` and a fake clock.
Every test that takes the `store` fixture also runs on a real PostgreSQL server as the
`postgres` parameter, deselected unless `-m live` (set-up in the docstring of
`tests/test_live_memory_postgres.py`). A test that also takes `db`, the SQLite file,
reads FTS5 rows, triggers or the file itself, and stays SQLite-only: the hook
`pytest_generate_tests` below adds the `postgres` parameter only when `db` is not among
the test's fixtures. A Postgres twin of each such test is in the live module.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_harness.memory import (
    MEMORY_MIGRATIONS,
    Memory,
    MemoryStore,
    MemoryValidationError,
    _match_expression,
)
from llm_harness.redact import Secret
from llm_harness.storage.postgres import PostgresRelationalStore
from llm_harness.storage.relational import RelationalStore, SqliteRelationalStore
from tests.live_postgres import memory_target, reset_memory_schema

START = datetime(2026, 11, 2, 9, 30, 15, tzinfo=UTC)
START_TEXT = "2026-11-02T09:30:15Z"
SECRETS = ("sk-abcdefgh12345678", "Authorization: Bearer abc12345def")


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "memory.db"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "backend" not in metafunc.fixturenames:
        return
    backends: list = ["sqlite"]
    if "db" not in metafunc.fixturenames:
        backends.append(pytest.param("postgres", marks=pytest.mark.live))
    metafunc.parametrize("backend", backends, indirect=True)


@pytest.fixture
def backend(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def relational(
    backend: str, request: pytest.FixtureRequest
) -> Iterator[RelationalStore]:
    if backend == "sqlite":
        yield SqliteRelationalStore(request.getfixturevalue("db"))
        return
    url, password = memory_target()
    reset_memory_schema(url, password)  # the program must create everything itself
    try:
        yield PostgresRelationalStore(url, Secret(password) if password else None)
    finally:
        reset_memory_schema(url, password)


@pytest.fixture
def store(relational: RelationalStore, clock: FakeClock) -> MemoryStore:
    return MemoryStore(relational, clock)


def fts_hits(db: Path, word: str) -> list[tuple]:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(
            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", (f'"{word}"',)
        ).fetchall()


async def test_save_and_get_round_trip_with_exact_timestamps(store: MemoryStore):
    saved = await store.save("alice", "  Prefers metric units.  ", ["metric", "units"])
    assert saved == Memory(
        1, "alice", "Prefers metric units.", ("metric", "units"), START_TEXT, START_TEXT
    )
    assert await store.get("alice", saved.id) == saved


async def test_the_default_clock_gives_an_utc_timestamp_with_z(
    relational: RelationalStore,
):
    saved = await MemoryStore(relational).save("alice", "a fact", ["x"])
    assert datetime.strptime(saved.created_at, "%Y-%m-%dT%H:%M:%SZ")
    assert saved.updated_at == saved.created_at


async def test_a_non_utc_clock_is_converted_to_utc(relational: RelationalStore):
    plus_two = timezone(timedelta(hours=2))
    store = MemoryStore(relational, lambda: START.astimezone(plus_two))
    assert (await store.save("alice", "a fact", ["x"])).created_at == START_TEXT


async def test_update_bumps_updated_at_only(store: MemoryStore, clock: FakeClock):
    saved = await store.save("alice", "Likes tea", ["tea"])
    clock.advance(hours=1)
    updated = await store.update("alice", saved.id, text="Likes green tea")
    assert updated == Memory(
        saved.id,
        "alice",
        "Likes green tea",
        ("tea",),
        START_TEXT,
        "2026-11-02T10:30:15Z",
    )
    clock.advance(minutes=1)
    updated = await store.update("alice", saved.id, keywords=["tea", "drink"])
    assert updated is not None
    assert updated.text == "Likes green tea"
    assert updated.keywords == ("tea", "drink")
    assert updated.created_at == START_TEXT
    assert updated.updated_at == "2026-11-02T10:31:15Z"
    assert await store.get("alice", saved.id) == updated


async def test_update_needs_text_or_keywords(store: MemoryStore):
    saved = await store.save("alice", "Likes tea", ["tea"])
    with pytest.raises(MemoryValidationError, match="text, keywords or both"):
        await store.update("alice", saved.id)


async def test_update_validates_what_it_changes(store: MemoryStore):
    saved = await store.save("alice", "Likes tea", ["tea"])
    with pytest.raises(MemoryValidationError):
        await store.update("alice", saved.id, text="")
    with pytest.raises(MemoryValidationError):
        await store.update("alice", saved.id, keywords=["Tea"])
    assert (await store.get("alice", saved.id)) == saved


async def test_update_and_forget_of_a_missing_id(store: MemoryStore):
    assert await store.update("alice", 99, text="x") is None
    assert await store.forget("alice", 99) is False
    assert await store.get("alice", 99) is None


async def test_two_users_never_see_or_change_each_other(store: MemoryStore):
    mine = await store.save("alice", "Uses a kayak every weekend", ["kayak"])
    theirs = await store.save("bob", "Rides a kayak too", ["kayak"])

    assert await store.get("alice", theirs.id) is None
    assert await store.get("bob", mine.id) is None
    assert await store.list("alice") == [mine]
    assert await store.list("bob") == [theirs]
    assert await store.search("alice", "kayak") == [mine]
    assert await store.search("bob", "kayak") == [theirs]
    assert await store.update("alice", theirs.id, text="hijacked") is None
    assert await store.forget("alice", theirs.id) is False
    assert await store.get("bob", theirs.id) == theirs


async def test_consolidation_updates_one_entry_and_forgets_two(
    store: MemoryStore, clock: FakeClock
):
    first = await store.save("alice", "Likes tea", ["tea"])
    second = await store.save("alice", "Likes green tea", ["tea", "green"])
    third = await store.save("alice", "Drinks tea at night", ["tea", "night"])
    clock.advance(days=1)

    merged = await store.update(
        "alice",
        first.id,
        text="Likes green tea, drinks it at night",
        keywords=["tea", "green", "night"],
    )
    assert await store.forget("alice", second.id) is True
    assert await store.forget("alice", third.id) is True

    assert merged is not None
    assert await store.list("alice") == [merged]
    assert [m.id for m in await store.search("alice", "tea")] == [first.id]


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
async def test_empty_text_is_rejected(store: MemoryStore, bad: str):
    with pytest.raises(MemoryValidationError, match="1 to 1500"):
        await store.save("alice", bad, ["x"])


async def test_text_length_boundary(store: MemoryStore):
    assert (await store.save("alice", "a" * 1500, ["x"])).text == "a" * 1500
    with pytest.raises(MemoryValidationError, match="1 to 1500"):
        await store.save("alice", "a" * 1501, ["x"])


async def test_the_text_length_is_counted_after_trimming(store: MemoryStore):
    saved = await store.save("alice", " " + "a" * 1500 + " ", ["x"])
    assert len(saved.text) == 1500


@pytest.mark.parametrize("char", ["\n", "\t", "\x00", "\x1b", "\r"])
async def test_control_characters_in_text_are_rejected(store: MemoryStore, char: str):
    with pytest.raises(MemoryValidationError, match="control characters.*newline"):
        await store.save("alice", f"line one{char}line two", ["x"])


async def test_text_must_be_a_string(store: MemoryStore):
    with pytest.raises(MemoryValidationError, match="text must be a string"):
        await store.save("alice", 5, ["x"])  # type: ignore[arg-type]


async def test_keyword_count_boundary(store: MemoryStore):
    eight = [f"word{i}" for i in range(8)]
    assert len((await store.save("alice", "a fact", eight)).keywords) == 8
    with pytest.raises(MemoryValidationError, match="1 to 8"):
        await store.save("alice", "a fact", [f"word{i}" for i in range(9)])
    with pytest.raises(MemoryValidationError, match="1 to 8"):
        await store.save("alice", "a fact", [])


async def test_keyword_length_boundary(store: MemoryStore):
    assert (await store.save("alice", "a fact", ["a" * 30])).keywords == ("a" * 30,)
    with pytest.raises(MemoryValidationError, match="1 to 30"):
        await store.save("alice", "a fact", ["a" * 31])
    with pytest.raises(MemoryValidationError, match="1 to 30"):
        await store.save("alice", "a fact", [""])


@pytest.mark.parametrize("good", ["python3", "e-mail", "machine learning", "3d"])
async def test_valid_keyword_shapes(store: MemoryStore, good: str):
    assert (await store.save("alice", "a fact", [good])).keywords == (good,)


@pytest.mark.parametrize(
    "bad",
    ["Python", "TEA", "café", "a_b", "-a", "a-", "a--b", "a  b", " a", "a b ", "a\nb"],
)
async def test_keywords_must_be_lowercase_english_words(store: MemoryStore, bad: str):
    with pytest.raises(MemoryValidationError, match="lowercase English"):
        await store.save("alice", "a fact", [bad])


@pytest.mark.parametrize("bad", ["tea", 5, None])
async def test_keywords_must_be_a_list_of_strings(store: MemoryStore, bad: object):
    with pytest.raises(MemoryValidationError):
        await store.save("alice", "a fact", bad)  # type: ignore[arg-type]


async def test_duplicate_keywords_are_removed_keeping_order(store: MemoryStore):
    saved = await store.save("alice", "a fact", ["b", "a", "b", "c", "a"])
    assert saved.keywords == ("b", "a", "c")


async def test_duplicates_count_once_toward_the_limit(store: MemoryStore):
    keywords = [f"w{i}" for i in range(8)] * 2
    assert len((await store.save("alice", "a fact", keywords)).keywords) == 8


@pytest.mark.parametrize("bad", ["", None, 5])
async def test_user_id_must_be_a_non_empty_string(store: MemoryStore, bad: object):
    with pytest.raises(MemoryValidationError, match="user_id"):
        await store.save(bad, "a fact", ["x"])  # type: ignore[arg-type]
    with pytest.raises(MemoryValidationError, match="user_id"):
        await store.search(bad, "fact")  # type: ignore[arg-type]
    with pytest.raises(MemoryValidationError, match="user_id"):
        await store.list(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("secret", SECRETS)
async def test_a_secret_in_text_is_refused_without_echoing_it(
    store: MemoryStore, secret: str
):
    with pytest.raises(MemoryValidationError, match="secret") as caught:
        await store.save("alice", f"My key is {secret} ok", ["x"])
    assert "abc12345" not in str(caught.value)
    assert await store.list("alice") == []


async def test_a_secret_in_a_keyword_is_refused(store: MemoryStore):
    with pytest.raises(MemoryValidationError) as caught:
        await store.save("alice", "a fact", ["sk-abcdefgh12345678"])
    assert "abcdefgh" not in str(caught.value)
    assert await store.list("alice") == []


async def test_a_secret_in_an_update_is_refused(store: MemoryStore):
    saved = await store.save("alice", "a fact", ["x"])
    with pytest.raises(MemoryValidationError, match="secret"):
        await store.update("alice", saved.id, text="Authorization: Bearer abc12345def")
    assert await store.get("alice", saved.id) == saved


async def test_ordinary_prose_is_not_mistaken_for_a_secret(store: MemoryStore):
    text = "The user is a bearer of good news and likes API design."
    assert (await store.save("alice", text, ["api"])).text == text


async def test_search_returns_the_better_match_first(store: MemoryStore):
    weak = await store.save(
        "alice", "Mentions coffee once among many other things", ["misc"]
    )
    strong = await store.save("alice", "Coffee coffee", ["coffee"])
    await store.save("alice", "Unrelated", ["other"])
    assert [m.id for m in await store.search("alice", "coffee")] == [strong.id, weak.id]


async def test_search_matches_keywords_as_well_as_text(store: MemoryStore):
    saved = await store.save("alice", "Works at a bank", ["finance"])
    assert await store.search("alice", "finance") == [saved]


async def test_search_stems_words(store: MemoryStore):
    saved = await store.save("alice", "Prefers metric units", ["metric"])
    assert await store.search("alice", "unit") == [saved]
    assert await store.search("alice", "preferring") == [saved]


async def test_search_is_case_insensitive(store: MemoryStore):
    saved = await store.save("alice", "Lives in Berlin", ["city"])
    assert await store.search("alice", "BERLIN") == [saved]


async def test_search_clamps_k(store: MemoryStore):
    for i in range(12):
        await store.save("alice", f"shared note number {i}", ["note"])
    assert len(await store.search("alice", "note", k=0)) == 1
    assert len(await store.search("alice", "note", k=-3)) == 1
    assert len(await store.search("alice", "note", k=99)) == 10
    assert len(await store.search("alice", "note", k=3)) == 3
    assert len(await store.search("alice", "note")) == 5


@pytest.mark.parametrize("query", ["", "   ", "!!! ???", "*", '"', "()", "-"])
async def test_a_query_without_words_finds_nothing(store: MemoryStore, query: str):
    await store.save("alice", "a fact", ["x"])
    assert await store.search("alice", query) == []


@pytest.mark.parametrize(
    "query",
    [
        'foo" OR "bar',
        "NEAR(",
        "NEAR(a b, 2)",
        "*",
        "a:b",
        "text:fact",
        '"',
        "",
        "café 日本語 \U0001f600",
        "AND OR NOT",
        "fact^2",
        "fact*",
        "x" * 5000,
        "word " * 500,
    ],
)
async def test_hostile_queries_never_raise(store: MemoryStore, query: str):
    await store.save("alice", "a fact", ["x"])
    assert isinstance(await store.search("alice", query), list)


async def test_fts_operators_are_taken_as_plain_words(store: MemoryStore):
    await store.save("alice", "foo only", ["x"])
    await store.save("alice", "bar only", ["y"])
    both = await store.save("alice", "the word near appears here", ["z"])
    # As an operator `NEAR` would be a syntax error; as a word it matches only `both`.
    assert [m.id for m in await store.search("alice", "NEAR")] == [both.id]
    # Terms are ORed, never ANDed: either word is enough (AND itself is a stop word).
    assert len(await store.search("alice", "foo AND bar")) == 2
    assert _match_expression("foo AND bar") == '"foo" OR "bar"'
    # NOT is a stop word too: it never reaches MATCH, and so never excludes anything.
    assert [m.id for m in await store.search("alice", "NOT")] == []


@pytest.mark.parametrize(
    "query", ["the", "what is the", "AND OR NOT", "Can you? I do."]
)
async def test_a_query_of_only_stop_words_finds_nothing(store: MemoryStore, query: str):
    await store.save("alice", "the the the what is the", ["the"])
    assert _match_expression(query) is None
    assert await store.search("alice", query) == []


async def test_stop_words_are_dropped_next_to_a_meaningful_word(store: MemoryStore):
    saved = await store.save("alice", "Lives in Lisbon", ["city"])
    await store.save("alice", "Allergic to peanuts", ["food"])
    assert _match_expression("what is the capital of France") == '"capital" OR "France"'
    assert await store.search("alice", "where is the Lisbon of it") == [saved]


@pytest.mark.parametrize("word", ["ai", "go", "pi", "900", "340", "x"])
async def test_short_words_and_numbers_are_not_stop_words(
    store: MemoryStore, word: str
):
    saved = await store.save("alice", f"Notes about {word} here", ["misc"])
    assert _match_expression(f"the {word}") == f'"{word}"'
    assert await store.search("alice", f"the {word}") == [saved]


@pytest.mark.parametrize("word", ["The", "THE", "tHe"])
def test_stop_words_are_compared_case_insensitively(word: str):
    assert _match_expression(f"{word} beach") == '"beach"'
    assert _match_expression(word) is None


def test_stop_words_are_not_stemmed():
    # "being" and "has" are stop words; "beings" and "hass" are not and are kept as typed.
    assert _match_expression("being beings has hass") == '"beings" OR "hass"'


def test_the_term_cap_counts_meaningful_words_only():
    words = []
    for i in range(40):
        words.append(f"w{i}" if i % 2 == 0 else "the")  # 20 meaningful, 20 stop words
    assert _match_expression(" ".join(words)) == " OR ".join(
        f'"w{i}"' for i in range(0, 40, 2)
    )
    meaningful = [f"w{i}" for i in range(40)]
    stop_first = ["the"] * 20 + meaningful  # 60 words; the cap must skip the stop words
    assert _match_expression(" ".join(stop_first)) == " OR ".join(
        f'"{w}"' for w in meaningful[:32]
    )


async def test_stop_words_stay_in_the_saved_text_and_the_index(
    store: MemoryStore, db: Path
):
    saved = await store.save("alice", "Walks on the beach", ["the"])
    assert (await store.get("alice", saved.id)).text == "Walks on the beach"
    assert saved.keywords == ("the",)
    assert fts_hits(db, "the") == [(saved.id,)]  # indexed as written
    assert await store.search("alice", "the beach") == [saved]
    assert await store.search("alice", "beach") == [saved]
    assert await store.search("alice", "the") == []  # only the query side drops it


QUALITY_MEMORIES = [
    ("Lives in Lisbon and works for a logistics company.", ["lisbon", "work"]),
    ("Prefers metric units for distances and temperatures.", ["metric", "units"]),
    ("Is allergic to peanuts, so check the menu for every restaurant.", ["peanuts"]),
    ("Wants answers short, with the plan for the trip first.", ["style", "trip"]),
    ("Budget for holidays is about 900 euros for a week.", ["budget", "euros"]),
]


async def seed_quality_memories(store: MemoryStore) -> dict[str, int]:
    ids = {}
    for text, keywords in QUALITY_MEMORIES:
        ids[keywords[0]] = (await store.save("alice", text, keywords)).id
    return ids


async def test_an_unrelated_question_matches_no_memory(store: MemoryStore):
    await seed_quality_memories(store)
    question = "What is the capital of France and for how long has it been the capital?"
    assert await store.search("alice", question) == []


async def test_a_unit_question_ranks_the_unit_memory_first(store: MemoryStore):
    ids = await seed_quality_memories(store)
    message = (
        "Can you convert the distance for me to miles? "
        "The road is 340 km for the whole drive."
    )
    hits = await store.search("alice", message)
    assert hits[0].id == ids["metric"]


async def test_a_trip_request_finds_the_trip_and_budget_memories(store: MemoryStore):
    ids = await seed_quality_memories(store)
    message = "Please plan the trip to Porto with my budget in mind"
    hit_ids = [m.id for m in await store.search("alice", message)]
    assert set(hit_ids) == {ids["style"], ids["budget"]}
    assert ids["peanuts"] not in hit_ids and ids["lisbon"] not in hit_ids


async def test_unicode_text_is_found(store: MemoryStore):
    saved = await store.save("alice", "Lives in Zürich", ["city"])
    assert await store.search("alice", "zürich") == [saved]


async def test_fts_follows_an_update(store: MemoryStore, db: Path):
    saved = await store.save("alice", "Likes zebra stripes", ["animal"])
    assert await store.search("alice", "zebra") == [saved]
    updated = await store.update("alice", saved.id, text="Likes giraffe spots")
    assert await store.search("alice", "zebra") == []
    assert await store.search("alice", "giraffe") == [updated]
    assert fts_hits(db, "zebra") == []
    assert fts_hits(db, "giraffe") == [(saved.id,)]


async def test_fts_follows_a_keyword_update(store: MemoryStore):
    saved = await store.save("alice", "A fact", ["oldword"])
    updated = await store.update("alice", saved.id, keywords=["newword"])
    assert await store.search("alice", "oldword") == []
    assert await store.search("alice", "newword") == [updated]


async def test_fts_follows_a_forget(store: MemoryStore, db: Path):
    saved = await store.save("alice", "Likes zebra stripes", ["animal"])
    assert await store.forget("alice", saved.id) is True
    assert await store.search("alice", "zebra") == []
    assert fts_hits(db, "zebra") == []
    assert await store.get("alice", saved.id) is None


async def test_a_fresh_database_migrates_once_and_a_second_store_reuses_it(
    db: Path, clock: FakeClock
):
    first = MemoryStore(SqliteRelationalStore(db), clock)
    saved = await first.save("alice", "a fact", ["x"])
    second = MemoryStore(SqliteRelationalStore(db), clock)
    assert await second.get("alice", saved.id) == saved
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT * FROM schema_versions").fetchall() == [
            ("memory", len(MEMORY_MIGRATIONS))
        ]
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone() == (1,)


async def test_construction_and_validation_failures_touch_no_file(db: Path):
    store = MemoryStore(SqliteRelationalStore(db))
    with pytest.raises(MemoryValidationError):
        await store.save("alice", "", ["x"])
    assert not db.exists()


async def test_the_ledger_namespace_is_left_alone(db: Path):
    relational = SqliteRelationalStore(db)
    await MemoryStore(relational).save("alice", "a fact", ["x"])
    with closing(sqlite3.connect(db)) as conn:
        tables = {n for (n,) in conn.execute("SELECT name FROM sqlite_master")}
    assert "llm_calls" not in tables


async def test_list_pages_newest_updated_first(store: MemoryStore, clock: FakeClock):
    saved = []
    for i in range(5):
        saved.append(await store.save("alice", f"fact {i}", ["x"]))
        clock.advance(seconds=10)
    # Same-second entries fall back to the higher id first.
    clock.now = START
    twin_a = await store.save("alice", "twin a", ["x"])
    twin_b = await store.save("alice", "twin b", ["x"])

    everything = await store.list("alice")
    assert [m.id for m in everything] == [
        saved[4].id,
        saved[3].id,
        saved[2].id,
        saved[1].id,
        twin_b.id,
        twin_a.id,
        saved[0].id,
    ]
    assert await store.list("alice", limit=2) == everything[:2]
    assert await store.list("alice", limit=2, offset=2) == everything[2:4]
    assert await store.list("alice", limit=50, offset=6) == everything[6:]
    assert await store.list("alice", offset=7) == []


async def test_an_updated_entry_moves_to_the_front_of_the_list(
    store: MemoryStore, clock: FakeClock
):
    old = await store.save("alice", "old", ["x"])
    await store.save("alice", "newer", ["x"])
    clock.advance(minutes=1)
    touched = await store.update("alice", old.id, text="old but touched")
    assert (await store.list("alice"))[0] == touched


async def test_list_of_an_unknown_user_is_empty(store: MemoryStore):
    assert await store.list("nobody") == []


async def test_a_store_with_600_entries_still_works(store: MemoryStore, db: Path):
    with closing(
        sqlite3.connect(db.parent.mkdir(parents=True, exist_ok=True) or db)
    ) as conn:
        pass
    await store.save("alice", "seed entry", ["seed"])
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO memories (user_id, text, keywords, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "alice",
                    f"bulk note {i} about topic{i}",
                    "bulk",
                    START_TEXT,
                    START_TEXT,
                )
                for i in range(599)
            ],
        )
        conn.execute("COMMIT")
    assert len(await store.list("alice", limit=1000)) == 600
    assert [m.text for m in await store.search("alice", "topic598")] == [
        "bulk note 598 about topic598"
    ]
    extra = await store.save("alice", "one more", ["more"])
    assert extra.id == 601
    assert len(await store.search("alice", "bulk", k=99)) == 10
