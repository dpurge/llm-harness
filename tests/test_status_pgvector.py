"""`--storage-status` for the pgvector vector backend, on the stand-in server.

No server, no socket. What only a real server can show (the session really refuses
writes, a closed port) is for the live tests of step 5.
"""

from pathlib import Path
from typing import Any

import psycopg
import pytest

import llm_harness.storage.postgres as postgres_module
from llm_harness.config import (
    ConfigError,
    HarnessConfig,
    ProviderConfig,
    StorageConfig,
    VectorConfig,
)
from llm_harness.redact import Secret
from llm_harness.storage.pgvector import PgVectorStore
from llm_harness.storage.status import storage_status
from tests.fake_pgvector import (
    AVAILABLE,
    MISSING,
    FakeDatabaseError,
    FakePgVectorServer,
)

USER = "db-secret-user"
PASSWORD = "sk-test-SECRET-123"
HOST = "db-secret-host.example"
URL = f"postgresql://{USER}@{HOST}:5432/vectors_test"
LABEL = f"{HOST}:5432/vectors_test"
PROVIDERS = {"p": ProviderConfig(type="ollama", base_url="http://localhost:11434")}
SERVER_TEXT = (
    f'connection to server at "{HOST}" (10.0.0.1), port 5432 failed: FATAL: password '
    f'{PASSWORD} for user "{USER}" DETAIL: Failing row contains (secret-row-42)'
)


@pytest.fixture
def server() -> FakePgVectorServer:
    return FakePgVectorServer()


def make_config(url: str = URL, password: str | None = PASSWORD) -> HarnessConfig:
    return HarnessConfig(
        providers=PROVIDERS,
        models=[],
        storage=StorageConfig(vector=VectorConfig(backend="pgvector", url=url)),
        postgres_password=Secret(password) if password else None,
    )


async def run_status(
    server: Any,
    data: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: HarnessConfig | None = None,
) -> list[str]:
    monkeypatch.setattr(postgres_module, "_default_connect", server)
    return await storage_status(
        config or make_config(), relational_path=data / "never-used.db", data_dir=data
    )


def vector_part(lines: list[str]) -> list[str]:
    start = next(i for i, line in enumerate(lines) if line.startswith("vector:"))
    end = next(i for i, line in enumerate(lines) if line.startswith("files:"))
    return lines[start:end]


def set_up_catalog(
    server: FakePgVectorServer, collections: dict[str, tuple[int, str]]
) -> None:
    server.tables.add("vector_collections")
    server.collections.update(collections)


# --- the status shapes ---------------------------------------------------------------


async def test_the_extension_with_its_version_and_an_empty_catalog(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up_catalog(server, {})
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert vector_part(lines) == [
        f"vector: pgvector  {LABEL}",
        "  extension: vector 0.8.7",
        "  no collections yet",
    ]


async def test_two_collections_are_listed_sorted_by_name(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up_catalog(server, {"zeta": (768, "dot"), "alpha": (4, "cosine")})
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert vector_part(lines) == [
        f"vector: pgvector  {LABEL}",
        "  extension: vector 0.8.7",
        "  collection alpha: 4 dimensions, cosine",
        "  collection zeta: 768 dimensions, dot",
    ]


async def test_a_missing_catalog_table_is_not_set_up_yet(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert vector_part(lines) == [
        f"vector: pgvector  {LABEL}",
        "  extension: vector 0.8.7",
        "  collections: not set up yet (it is set up on first use)",
    ]


async def test_an_available_extension_that_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakePgVectorServer(extension=AVAILABLE)
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert vector_part(lines) == [
        f"vector: pgvector  {LABEL}",
        (
            "  extension: not created in this database "
            "(an administrator runs: CREATE EXTENSION vector;)"
        ),
    ]


async def test_an_extension_that_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakePgVectorServer(extension=MISSING)
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert vector_part(lines) == [
        f"vector: pgvector  {LABEL}",
        (
            "  extension: not installed on this server "
            "(install the pgvector package, then run: CREATE EXTENSION vector;)"
        ),
    ]


async def test_a_connect_failure_is_one_line_and_the_files_lines_follow(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.refuse_connections = psycopg.OperationalError(SERVER_TEXT)
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert vector_part(lines) == [
        f"vector: pgvector  {LABEL}",
        "  cannot connect (OperationalError)",
    ]
    assert any(line.startswith("files:") for line in lines)


async def test_another_database_error_names_its_type_only(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up_catalog(server, {"a": (3, "cosine")})
    server.fail_when = lambda sql, params: (
        psycopg.errors.InsufficientPrivilege(SERVER_TEXT)
        if "vector_collections" in sql
        else None
    )
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert vector_part(lines) == [
        f"vector: pgvector  {LABEL}",
        "  not readable (InsufficientPrivilege)",
    ]
    assert server.connections[0].closes == 1


# --- what is sent and how ------------------------------------------------------------


async def test_the_connection_is_read_only_bounded_and_keeps_the_password_apart(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_status(server, tmp_path / "data", monkeypatch)
    (kwargs,) = server.calls
    assert kwargs == {
        "host": HOST,
        "dbname": "vectors_test",
        "port": 5432,
        "user": USER,
        "password": PASSWORD,
        "connect_timeout": 5,
        "autocommit": True,
        "options": "-c default_transaction_read_only=on",
    }


async def test_a_smaller_url_timeout_wins_and_a_larger_one_is_capped(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_status(
        server, tmp_path / "data", monkeypatch, make_config(f"{URL}?connect_timeout=2")
    )
    await run_status(
        server, tmp_path / "data", monkeypatch, make_config(f"{URL}?connect_timeout=30")
    )
    assert [call["connect_timeout"] for call in server.calls] == [2, 5]


async def test_no_password_means_no_password_argument(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_status(server, tmp_path / "data", monkeypatch, make_config(password=None))
    assert "password" not in server.calls[0]


async def test_only_selects_are_sent(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up_catalog(server, {"a": (3, "cosine")})
    await run_status(server, tmp_path / "data", monkeypatch)
    assert server.statements
    assert all(sql.startswith("SELECT ") for sql in server.statements)
    text = " ".join(server.statements).upper()
    for forbidden in ("BEGIN", "LOCK", "CREATE", "INSERT", "UPDATE", "DELETE"):
        assert forbidden not in text
    assert server.snapshot() == (
        {},
        {"vector_collections"},
        {"a": (3, "cosine")},
        {},
    )  # nothing was written


@pytest.mark.parametrize("extension", [AVAILABLE, MISSING])
async def test_the_connection_is_closed_without_the_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str
) -> None:
    server = FakePgVectorServer(extension=extension)
    await run_status(server, tmp_path / "data", monkeypatch)
    assert [conn.closes for conn in server.connections] == [1]


async def test_the_connection_is_closed_with_a_missing_table(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_status(server, tmp_path / "data", monkeypatch)
    assert [conn.closes for conn in server.connections] == [1]


async def test_the_connection_is_closed_with_the_catalog_read(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up_catalog(server, {})
    await run_status(server, tmp_path / "data", monkeypatch)
    assert [conn.closes for conn in server.connections] == [1]


# --- nothing leaks, nothing is created -----------------------------------------------


@pytest.mark.parametrize("failure", ["connect", "read"])
async def test_no_secret_and_no_server_text_reaches_the_output(
    tmp_path: Path,
    server: FakePgVectorServer,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "connect":
        server.refuse_connections = psycopg.OperationalError(SERVER_TEXT)
    else:
        server.fail_when = lambda sql, params: FakeDatabaseError(SERVER_TEXT, "XX000")
    text = "\n".join(await run_status(server, tmp_path / "data", monkeypatch))
    for secret in (USER, PASSWORD, "db-secret-host.example/", "10.0.0.1", "secret-row"):
        assert secret not in text
    assert HOST in text  # only inside the label host:port/dbname
    assert text.count(HOST) == 1


async def test_a_successful_report_holds_no_user_and_no_password(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_up_catalog(server, {"a": (3, "cosine")})
    text = "\n".join(await run_status(server, tmp_path / "data", monkeypatch))
    assert USER not in text and PASSWORD not in text and "password" not in text


async def test_the_report_creates_nothing_under_the_data_dir(
    tmp_path: Path, server: FakePgVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    await run_status(server, data, monkeypatch)
    assert not data.exists()


async def test_a_hand_built_config_without_a_url_raises_a_config_error(
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        providers=PROVIDERS,
        models=[],
        storage=StorageConfig.model_construct(
            vector=VectorConfig.model_construct(backend="pgvector", path=None, url=None)
        ),
    )
    with pytest.raises(ConfigError, match=r"\[storage.vector\] url"):
        await storage_status(
            config, relational_path=tmp_path / "x.db", data_dir=tmp_path
        )


def test_the_store_method_on_its_own_returns_a_status(
    server: FakePgVectorServer,
) -> None:
    set_up_catalog(server, {"a": (3, "euclid")})
    status = PgVectorStore(URL, connect_factory=server).read_status_sync()
    assert (status.extension_version, status.extension_available) == ("0.8.7", True)
    assert status.collections == [("a", 3, "euclid")]
