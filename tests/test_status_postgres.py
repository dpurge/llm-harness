"""`--storage-status` for relational postgres, on a fake psycopg connection.

No server, no socket. What only a real server can show (the session really refuses
writes, a closed port, the table set staying identical) is in
`tests/test_live_postgres.py`.
"""

from pathlib import Path

import psycopg
import pytest

import llm_harness.storage.postgres as postgres_module
from llm_harness.config import (
    HarnessConfig,
    ProviderConfig,
    RelationalConfig,
    StorageConfig,
)
from llm_harness.redact import Secret
from llm_harness.storage.postgres import PostgresRelationalStore
from llm_harness.storage.relational import ReadOnlySchema
from llm_harness.storage.status import _schema_lines, storage_status
from llm_harness.store import expected_schema_versions, optional_schema_versions
from tests.test_storage_postgres import HOST, PASSWORD, URL, USER, FakeServer

SELECT = "SELECT namespace, version FROM schema_versions"
PROVIDERS = {"p": ProviderConfig(type="ollama", base_url="http://localhost:11434")}
LEDGER = expected_schema_versions()["ledger"]
FIRST_LINE = f"relational: postgres  {HOST}:5432/ledger_db"


class SqlstateOnly(Exception):
    """Not a psycopg class: only the attribute the status code reads."""

    sqlstate = "42P01"


class RefusingServer(FakeServer):
    """Every connect fails with a driver error whose text holds every secret."""

    def __call__(self, **kwargs: object) -> None:  # type: ignore[override]
        self.calls.append(kwargs)
        raise psycopg.OperationalError(
            f'connection to server at "{HOST}" (10.0.0.1), port 5432 failed: '
            f'FATAL: password {PASSWORD} for user "{USER}"'
        )


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def serve_versions(server: FakeServer, versions: dict[str, int]) -> None:
    server.rows = lambda sql: list(versions.items()) if sql == SELECT else []


def make_config(url: str = URL, password: str | None = PASSWORD) -> HarnessConfig:
    return HarnessConfig(
        providers=PROVIDERS,
        models=[],
        storage=StorageConfig(relational=RelationalConfig(backend="postgres", url=url)),
        postgres_password=Secret(password) if password else None,
    )


async def run_status(
    server: FakeServer,
    data: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: HarnessConfig | None = None,
) -> list[str]:
    monkeypatch.setattr(postgres_module, "_default_connect", server)
    return await storage_status(
        config or make_config(), relational_path=data / "never-used.db", data_dir=data
    )


def relational_part(lines: list[str]) -> list[str]:
    end = next(i for i, line in enumerate(lines) if line.startswith("vector:"))
    return lines[:end]


async def test_an_up_to_date_ledger_prints_the_label_and_one_schema_line(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {"ledger": LEDGER})
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert relational_part(lines) == [
        FIRST_LINE,
        f"  schema: ledger version {LEDGER} (the code expects {LEDGER})",
    ]


async def test_a_ledger_that_is_not_recorded_is_not_set_up_yet(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {})
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert relational_part(lines) == [
        FIRST_LINE,
        "  schema: ledger not set up yet (it is set up on first use)",
    ]


async def test_a_lower_ledger_version_will_be_migrated(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {"ledger": LEDGER - 1})
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert relational_part(lines)[1] == (
        f"  schema: ledger version {LEDGER - 1} (the code expects {LEDGER}), "
        "it will be migrated on the next start"
    )


async def test_a_higher_ledger_version_is_newer_than_this_program(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {"ledger": LEDGER + 1})
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert relational_part(lines)[1] == (
        f"  schema: ledger version {LEDGER + 1}, "
        f"newer than this version of llm-harness (the code expects {LEDGER})"
    )


async def test_optional_and_unknown_namespaces_read_like_sqlite(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    optional = optional_schema_versions()
    versions = {"ledger": LEDGER, "mystery": 4, **optional}
    serve_versions(server, versions)
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    expected = _schema_lines(ReadOnlySchema(versions=versions, user_version=0))
    assert relational_part(lines) == [FIRST_LINE, *expected]
    assert (
        "  schema: mystery version 4 "
        "(this version of llm-harness does not know this namespace)"
    ) in lines
    assert len(expected) == 2 + len(optional)


@pytest.mark.parametrize("failure", [psycopg.errors.UndefinedTable(), SqlstateOnly()])
async def test_a_missing_schema_versions_table_is_nothing_to_read_yet(
    failure: Exception,
    tmp_path: Path,
    server: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server.fail = lambda sql: failure
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert relational_part(lines) == [FIRST_LINE, "  schema: nothing to read yet"]
    assert server.connections[0].closes == 1


async def test_a_connect_failure_is_one_line_with_the_type_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = RefusingServer()
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert relational_part(lines) == [FIRST_LINE, "  cannot connect (OperationalError)"]
    # The vector and files lines still follow.
    assert any(line.startswith("vector:") for line in lines)
    assert any(line.startswith("files:") for line in lines)
    text = "\n".join(lines)
    for secret in (USER, PASSWORD, "10.0.0.1"):
        assert secret not in text


async def test_another_database_error_names_its_type_and_not_its_text(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.fail = lambda sql: psycopg.errors.InsufficientPrivilege(
        f"permission denied for table schema_versions, row ({PASSWORD}, {USER})"
    )
    lines = await run_status(server, tmp_path / "data", monkeypatch)
    assert relational_part(lines) == [
        FIRST_LINE,
        "  schema: not readable (InsufficientPrivilege)",
    ]
    assert PASSWORD not in "\n".join(lines) and USER not in "\n".join(lines)
    assert server.connections[0].closes == 1


async def test_the_connection_is_read_only_bounded_and_keeps_the_password_apart(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {"ledger": LEDGER})
    await run_status(server, tmp_path / "data", monkeypatch)
    (kwargs,) = server.calls
    assert kwargs == {
        "host": HOST,
        "dbname": "ledger_db",
        "port": 5432,
        "user": USER,
        "password": PASSWORD,
        "connect_timeout": 5,
        "autocommit": True,
        "options": "-c default_transaction_read_only=on",
    }
    assert PASSWORD not in [v for k, v in kwargs.items() if k != "password"]


async def test_a_smaller_url_timeout_wins_over_the_status_bound(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {})
    config = make_config(f"{URL}?connect_timeout=2")
    await run_status(server, tmp_path / "data", monkeypatch, config)
    assert server.calls[0]["connect_timeout"] == 2


async def test_a_larger_url_timeout_is_capped_at_five_seconds(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {})
    config = make_config(f"{URL}?connect_timeout=30")
    await run_status(server, tmp_path / "data", monkeypatch, config)
    assert server.calls[0]["connect_timeout"] == 5


async def test_no_password_means_no_password_argument(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {})
    await run_status(server, tmp_path / "data", monkeypatch, make_config(password=None))
    assert "password" not in server.calls[0]


async def test_exactly_one_select_is_sent_and_the_connection_is_closed(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {"ledger": LEDGER})
    await run_status(server, tmp_path / "data", monkeypatch)
    (connection,) = server.connections
    assert connection.executed == [(SELECT, None)]  # no BEGIN, no lock, no DDL
    assert connection.closes == 1


async def test_the_whole_report_has_no_user_password_or_file_lines_and_creates_nothing(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_versions(server, {"ledger": LEDGER})
    data = tmp_path / "data"
    data.mkdir()
    lines = await run_status(server, data, monkeypatch)
    text = "\n".join(lines)
    assert USER not in text and PASSWORD not in text and "password" not in text
    assert "  file:" not in text and "never-used.db" not in text
    assert sum(line.startswith("vector:") for line in lines) == 1
    assert sum(line.startswith("files:") for line in lines) == 1
    assert list(data.iterdir()) == []


def test_the_store_method_on_its_own_returns_the_recorded_versions(
    server: FakeServer,
) -> None:
    serve_versions(server, {"ledger": 1, "memory": 2})
    store = PostgresRelationalStore(URL, connect_factory=server)
    assert store.read_schema_versions_sync() == {"ledger": 1, "memory": 2}
