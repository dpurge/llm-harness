"""Shared helpers for the tests that run against a real PostgreSQL server.

Not a test module (no `test_` prefix): `tests/test_live_postgres.py`, the `postgres`
parameter of `tests/test_storage_relational.py`, `tests/test_live_pgvector.py` and the
`pgvector` parameter of `tests/test_storage_vectors.py`, `tests/test_live_memory_postgres.py`
and the `postgres` parameter of `tests/test_memory_store.py` all import it.
"""

import os
from urllib.parse import urlsplit

import psycopg
import pytest

URL_VARIABLE = "LLM_HARNESS_LIVE_POSTGRES_URL"
PASSWORD_VARIABLE = "LLM_HARNESS_LIVE_POSTGRES_PASSWORD"
PGVECTOR_URL_VARIABLE = "LLM_HARNESS_LIVE_PGVECTOR_URL"
PGVECTOR_PLAIN_URL_VARIABLE = "LLM_HARNESS_LIVE_PGVECTOR_PLAIN_URL"
PGMEM_URL_VARIABLE = "LLM_HARNESS_LIVE_PGMEM_URL"
PGMEM_VECTOR_URL_VARIABLE = "LLM_HARNESS_LIVE_PGMEM_VECTOR_URL"
REACHABLE_CHECK_SECONDS = 3
SAFE_DATABASE_WORDS = ("live", "test")


def live_target(variable: str = URL_VARIABLE) -> tuple[str, str | None]:
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"{variable} is not set (see the module docstring)")
    password = os.environ.get(PASSWORD_VARIABLE) or None
    try:
        raw = psycopg.connect(
            url, password=password, connect_timeout=REACHABLE_CHECK_SECONDS
        )
    except psycopg.Error as exc:
        pytest.skip(f"no postgres answered within 3 s: {type(exc).__name__}")
    raw.close()
    return url, password


def scratch_target(reason: str, variable: str = URL_VARIABLE) -> tuple[str, str | None]:
    """The live target, but only when the database is clearly a scratch one.

    `reason` says what the caller would damage; it is printed with the skip.
    """
    url, password = live_target(variable)
    database = urlsplit(url).path.lstrip("/")
    if not any(word in database for word in SAFE_DATABASE_WORDS):
        pytest.skip(
            f"{reason}; the database name must contain one of {SAFE_DATABASE_WORDS}"
        )
    return url, password


def ledger_target() -> tuple[str, str | None]:
    return scratch_target(
        "the ledger tests drop the fixed tables llm_calls and model_backoff"
    )


PGVECTOR_REASON = (
    "the pgvector tests drop the fixed tables vector_points and vector_collections "
    "and drop and recreate the vector extension"
)


def pgvector_target() -> tuple[str, str | None]:
    return scratch_target(PGVECTOR_REASON, PGVECTOR_URL_VARIABLE)


def pgvector_plain_target() -> tuple[str, str | None]:
    """A server WITHOUT the pgvector package (a plain postgres), also scratch only."""
    return scratch_target(PGVECTOR_REASON, PGVECTOR_PLAIN_URL_VARIABLE)


def reset_vector_schema(url: str, password: str | None) -> None:
    """Back to a database where the store must create everything itself.

    Raw connection as the administrator: the program never creates the extension, so
    the test setup does (`IF NOT EXISTS`, so it stays created between tests).
    """
    with psycopg.connect(url, password=password, autocommit=True) as raw:
        raw.execute("CREATE EXTENSION IF NOT EXISTS vector")
        raw.execute("DROP TABLE IF EXISTS vector_points, vector_collections")
        if raw.execute("SELECT to_regclass('schema_versions')").fetchone()[0]:
            raw.execute("DELETE FROM schema_versions WHERE namespace = 'vector'")


MEMORY_REASON = (
    "the memory tests drop the fixed tables memories, memory_vectors and "
    "memory_vector_meta and the function memories_clear_markers"
)


def memory_target() -> tuple[str, str | None]:
    """A plain postgres (no pgvector package needed), scratch only."""
    return scratch_target(MEMORY_REASON, PGMEM_URL_VARIABLE)


def memory_vector_target() -> tuple[str, str | None]:
    """The pgvector image, for memory with the real `PgVectorStore`; scratch only."""
    return scratch_target(
        MEMORY_REASON + "; " + PGVECTOR_REASON, PGMEM_VECTOR_URL_VARIABLE
    )


def reset_memory_schema(url: str, password: str | None) -> None:
    """Back to a database where the program must create the memory schema itself.

    Raw connection as the administrator. `CASCADE` removes the two triggers with the
    tables; the function is dropped by name because no table owns it.
    """
    with psycopg.connect(url, password=password, autocommit=True) as raw:
        raw.execute(
            "DROP TABLE IF EXISTS memory_vectors, memory_vector_meta, memories CASCADE"
        )
        raw.execute("DROP FUNCTION IF EXISTS memories_clear_markers() CASCADE")
        if raw.execute("SELECT to_regclass('schema_versions')").fetchone()[0]:
            raw.execute("DELETE FROM schema_versions WHERE namespace = 'memory'")
