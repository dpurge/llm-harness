"""Config rules for `[storage.relational] backend = "postgres"` and its guards."""

from pathlib import Path

import pytest

import llm_harness.cli as cli_module
import llm_harness.config as config_module
import llm_harness.storage.postgres as postgres_module
from llm_harness.config import (
    ConfigError,
    RelationalConfig,
    StorageConfig,
    load_config,
)
from llm_harness.redact import Secret
from llm_harness.storage import open_storage
from llm_harness.storage.postgres import PostgresRelationalStore
from llm_harness.store import Store
from tests.test_cli import AGENTS_YAML, CONFIG_TOML, MODELS_YAML, run_cli
from tests.test_config import (
    PROVIDER_TOML,
    load_storage,
    qdrant_toml,
    storage_error,
    write_config,
)

HOST = "db-secret-host.example"
USER = "app-user-name"
PASSWORD = "sk-test-SECRET-123"
DATABASE = "db-secret-name"
URL = f"postgresql://{USER}@{HOST}:5432/{DATABASE}"
PREFIX = "invalid config.toml: storage.relational: "
FAKE_VALUES = (HOST, USER, PASSWORD, DATABASE)


def pg_toml(extra: str = f'url = "{URL}"\n') -> str:
    return f'[storage.relational]\nbackend = "postgres"\n{extra}'


def url_toml(url: str) -> str:
    return pg_toml(f'url = "{url}"\n')


@pytest.fixture
def driver_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_postgres_driver_installed", lambda: True)


def test_postgres_with_a_valid_url_loads(tmp_path: Path, driver_installed: None):
    relational = load_storage(tmp_path, pg_toml()).relational
    assert (relational.backend, relational.url, relational.path) == (
        "postgres",
        URL,
        None,
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgres://h/d",
        "postgresql://h/d",
        "postgresql://user@h/d",
        "postgresql://h:1/d",
        "postgresql://h:65535/d",
        "postgresql://[::1]:5432/d",
        "postgresql://h/d?sslmode=verify-full",
        "postgresql://h/d?connect_timeout=120&sslmode=disable",
        "postgresql://h/d?connect_timeout=1",
    ],
)
def test_valid_urls_load(tmp_path: Path, driver_installed: None, url: str):
    assert load_storage(tmp_path, url_toml(url)).relational.url == url


def test_postgres_without_url_is_rejected_with_an_example(
    tmp_path: Path, driver_installed: None
):
    message = storage_error(tmp_path, pg_toml(""))
    assert message == (
        PREFIX + 'backend "postgres" requires url, for example '
        'url = "postgresql://user@db.example:5432/llm_harness"'
    )


def test_postgres_with_path_is_rejected(tmp_path: Path, driver_installed: None):
    message = storage_error(tmp_path, pg_toml(f'url = "{URL}"\npath = "/x.db"\n'))
    assert 'path is only used with a local backend, not "postgres"' in message
    assert message.startswith(PREFIX)


def test_sqlite_with_url_is_rejected(tmp_path: Path):
    message = storage_error(
        tmp_path, f'[storage.relational]\nbackend = "sqlite"\nurl = "{URL}"\n'
    )
    assert 'url is only used with backend = "postgres"' in message
    assert message.startswith(PREFIX)


def test_default_backend_with_url_is_rejected(tmp_path: Path):
    message = storage_error(tmp_path, f'[storage.relational]\nurl = "{URL}"\n')
    assert 'url is only used with backend = "postgres"' in message


# message fragment per case; the url carries distinctive fake values
BAD_URLS = [
    ("mysql://u@h/d", "url must be a postgresql:// or postgres:// URL"),
    ("h:5432/d", "url must be a postgresql:// or postgres:// URL"),
    ("postgresql:///d", "url must be a postgresql:// or postgres:// URL"),
    ("postgresql://:5432/d", "url must be a postgresql:// or postgres:// URL"),
    ("postgresql://u@/d?host=/var/run", "url must be a postgresql:// or postgres://"),
    ("postgresql://[::1/d", "url must be a postgresql:// or postgres:// URL"),
    ("postgresql://h1,h2/d", "url must name exactly one host"),
    ("postgresql://h1:1,h2:2/d", "url must name exactly one host"),
    ("postgresql://h", "url must end with one database name"),
    ("postgresql://h/", "url must end with one database name"),
    ("postgresql://h/a/b", "url must end with one database name"),
    ("postgresql://h:0/d", "url port must be 1 to 65535"),
    ("postgresql://h:65536/d", "url port must be 1 to 65535"),
    ("postgresql://h:port/d", "url port must be 1 to 65535"),
    ("postgresql://u:pw@h/d", "url must not contain a password"),
    ("postgresql://u:@h/d", "url must not contain a password"),
    ("postgresql://h/d?sslmode=bogus", "url sslmode must be one of: disable"),
    ("postgresql://h/d?connect_timeout=0", "url connect_timeout must be"),
    ("postgresql://h/d?connect_timeout=121", "url connect_timeout must be"),
    ("postgresql://h/d?connect_timeout=ten", "url connect_timeout must be"),
    ("postgresql://h/d?connect_timeout=", "url connect_timeout must be"),
    (
        "postgresql://h/d?sslmode=require&sslmode=disable",
        "repeats the query parameter 'sslmode'",
    ),
    ("postgresql://h/d#frag", "url must not contain a #fragment"),
    ("postgresql://h/d x", "url must not contain whitespace"),
]


@pytest.mark.parametrize(("url", "expected"), BAD_URLS)
def test_bad_url_is_rejected_with_a_named_problem(
    tmp_path: Path, driver_installed: None, url: str, expected: str
):
    message = storage_error(tmp_path, url_toml(url))
    assert message.startswith(PREFIX)
    assert expected in message


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "passfile",
        "service",
        "host",
        "options",
        "application_name",
        "sslrootcert",
    ],
)
def test_other_query_parameters_are_rejected_by_name(
    tmp_path: Path, driver_installed: None, name: str
):
    message = storage_error(tmp_path, url_toml(f"postgresql://h/d?{name}=VALUE-xyz"))
    assert f"query parameter '{name}' that is not allowed" in message
    assert "allowed: sslmode, connect_timeout" in message
    assert "VALUE-xyz" not in message


def test_a_stray_query_word_is_not_echoed(tmp_path: Path, driver_installed: None):
    message = storage_error(tmp_path, url_toml("postgresql://h/d?s3cr3t-word!"))
    assert "a query parameter that is not allowed" in message
    assert "s3cr3t" not in message


def test_password_in_the_query_is_named_not_echoed(
    tmp_path: Path, driver_installed: None
):
    message = storage_error(
        tmp_path, url_toml(f"postgresql://{USER}@{HOST}/d?password={PASSWORD}")
    )
    assert "query parameter 'password'" in message
    assert PASSWORD not in message


def test_userinfo_password_message_names_secrets_toml(
    tmp_path: Path, driver_installed: None
):
    message = storage_error(
        tmp_path, url_toml(f"postgresql://{USER}:{PASSWORD}@{HOST}/d")
    )
    assert "credentials do not belong in config.toml" in message
    assert "secrets.toml under [postgres]" in message


@pytest.mark.parametrize(
    "url",
    [
        f"mysql://{USER}:{PASSWORD}@{HOST}:5432/{DATABASE}",
        f"postgresql://{USER}:{PASSWORD}@{HOST}:5432/{DATABASE}",
        f"postgresql://{USER}@{HOST}:5432/{DATABASE}?password={PASSWORD}",
        f"postgresql://{USER}@{HOST}:5432/{DATABASE}?{PASSWORD}",
        f"postgresql://{USER}@{HOST}:99999/{DATABASE}",
        f"postgresql://{USER}@{HOST},{HOST}:5432/{DATABASE}",
        f"postgresql://{USER}@{HOST}:5432/{DATABASE}?sslmode={PASSWORD}",
        f"postgresql://{USER}@{HOST}:5432/{DATABASE}?connect_timeout={PASSWORD}",
        f"postgresql://{USER}@{HOST}:5432/{DATABASE}/{PASSWORD}",
        f"postgresql://{USER}@{HOST}:5432/{DATABASE}?sslmode=require&sslmode={PASSWORD}",
        f"postgresql://{USER}@{HOST}:5432/{DATABASE}#{PASSWORD}",
        f"postgresql://{USER}@{HOST}:5432/{DATABASE} {PASSWORD}",
        f"postgresql://[{HOST}/{DATABASE}",
        f"postgresql://{USER}@{HOST}",
    ],
)
def test_no_message_echoes_any_part_of_the_url(
    tmp_path: Path, driver_installed: None, url: str
):
    message = storage_error(tmp_path, url_toml(url))
    for value in FAKE_VALUES:
        assert value not in message


def test_missing_url_message_contains_no_fake_value(
    tmp_path: Path, driver_installed: None
):
    message = storage_error(tmp_path, pg_toml(""))
    assert "requires url" in message
    assert USER not in message


def test_missing_driver_names_the_extra_and_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(config_module, "_postgres_driver_installed", lambda: False)
    message = storage_error(tmp_path, pg_toml())
    assert message == (
        PREFIX + 'backend "postgres" needs the optional extra postgres, which is '
        "not installed; run: uv sync --extra postgres"
    )
    for value in FAKE_VALUES:
        assert value not in message


def test_bad_url_is_reported_before_the_missing_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(config_module, "_postgres_driver_installed", lambda: False)
    message = storage_error(tmp_path, url_toml("mysql://h/d"))
    assert "url must be a postgresql://" in message
    assert "uv sync" not in message


def test_the_real_driver_check_uses_find_spec(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []

    def fake_find_spec(name: str) -> object | None:
        seen.append(name)
        return None

    monkeypatch.setattr(config_module.importlib.util, "find_spec", fake_find_spec)
    assert config_module._postgres_driver_installed() is False
    assert seen == ["psycopg"]


@pytest.mark.parametrize("toml", ["", '[storage.relational]\nbackend = "sqlite"\n'])
def test_sqlite_never_checks_for_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str
):
    def fail() -> bool:
        raise AssertionError("the driver check must not run")

    monkeypatch.setattr(config_module, "_postgres_driver_installed", fail)
    assert load_storage(tmp_path, toml).relational.backend == "sqlite"


def test_postgres_and_qdrant_checks_are_independent(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(config_module, "_qdrant_client_installed", lambda: True)
    storage = load_storage(tmp_path, pg_toml() + qdrant_toml())
    assert (storage.relational.backend, storage.vector.backend) == (
        "postgres",
        "qdrant",
    )


def test_unknown_backend_names_postgres_among_the_valid_ones(tmp_path: Path):
    message = storage_error(tmp_path, '[storage.relational]\nbackend = "mysql"\n')
    assert (
        "storage.relational.backend: unknown backend 'mysql'; "
        "use one of: sqlite, postgres"
    ) in message
    assert "planned" not in message


# --- memory refusal ------------------------------------------------------------


MEMORY_TOML = 'user_id = "ann"\n' + PROVIDER_TOML + "[tools]\nmemory = true\n"
PGVECTOR_URL = f"postgresql://{USER}@{HOST}:5432/{DATABASE}"
VECTOR_TOML = {
    "qdrant-edge": '[storage.vector]\nbackend = "qdrant-edge"\n',
    "none": '[storage.vector]\nbackend = "none"\n',
    "qdrant": qdrant_toml(),
    "pgvector": f'[storage.vector]\nbackend = "pgvector"\nurl = "{PGVECTOR_URL}"\n',
}


@pytest.mark.parametrize("vector_backend", list(VECTOR_TOML))
def test_memory_with_postgres_loads_with_every_vector_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vector_backend: str
):
    monkeypatch.setattr(config_module, "_postgres_driver_installed", lambda: True)
    monkeypatch.setattr(config_module, "_qdrant_client_installed", lambda: True)
    write_config(tmp_path, MEMORY_TOML + pg_toml() + VECTOR_TOML[vector_backend])
    config = load_config(tmp_path)
    assert config.tools.memory is True
    assert (config.storage.relational.backend, config.storage.vector.backend) == (
        "postgres",
        vector_backend,
    )


def test_the_old_refusal_is_gone_from_the_source():
    source_dir = Path(config_module.__file__).parent
    for path in source_dir.rglob("*.py"):
        text = path.read_text()
        assert "storage-backends-postgres-memory" not in text, path
        assert "yet (planned feature" not in text, path


def test_postgres_without_memory_loads(tmp_path: Path, driver_installed: None):
    toml = PROVIDER_TOML + "[tools]\nmemory = false\n" + pg_toml()
    write_config(tmp_path, toml)
    assert load_config(tmp_path).storage.relational.backend == "postgres"


def test_memory_with_sqlite_is_not_refused(tmp_path: Path):
    toml = 'user_id = "ann"\n' + PROVIDER_TOML + "[tools]\nmemory = true\n"
    write_config(tmp_path, toml)
    assert load_config(tmp_path).tools.memory is True


# --- the CLI on postgres -------------------------------------------------------------


def postgres_cli_config(config_dir: Path) -> None:
    (config_dir / "config.toml").write_text(CONFIG_TOML + pg_toml())
    (config_dir / "config.toml").chmod(0o600)
    (config_dir / "models.yaml").write_text(MODELS_YAML)
    (config_dir / "agents.yaml").write_text(AGENTS_YAML)


def test_open_storage_builds_the_postgres_store_and_touches_nothing(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    calls: list[int] = []

    def factory(**kwargs: object) -> None:
        calls.append(1)
        raise AssertionError("open_storage must not connect")

    monkeypatch.setattr(postgres_module, "_default_connect", factory)
    storage = open_storage(
        load_storage(tmp_path, pg_toml()),
        tmp_path / "data",
        postgres_password=Secret(PASSWORD),
    )
    relational = storage.relational
    assert isinstance(relational, PostgresRelationalStore)
    assert relational.label == f"{HOST}:5432/{DATABASE}"
    assert PASSWORD not in repr(relational)
    assert calls == []
    assert not (tmp_path / "data").exists()


def test_open_storage_without_a_url_names_the_setting(tmp_path: Path):
    storage = StorageConfig.model_construct(
        relational=RelationalConfig.model_construct(
            backend="postgres", path=None, url=None
        )
    )
    with pytest.raises(ConfigError, match=r"\[storage.relational\] url"):
        open_storage(storage, tmp_path / "data")


def test_cli_startup_builds_the_ledger_on_postgres_and_connects_to_nothing(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    # With EOF on stdin no model is called, so nothing reaches the ledger: neither the
    # migration nor a connection happens at startup.
    connects: list[int] = []

    def factory(**kwargs: object) -> None:
        connects.append(1)
        raise AssertionError("startup must not connect to postgres")

    monkeypatch.setattr(postgres_module, "_default_connect", factory)
    built: list[Store] = []

    def spy(*args: object, **kwargs: object) -> Store:
        built.append(Store(*args, **kwargs))  # type: ignore[arg-type]
        return built[-1]

    monkeypatch.setattr(cli_module, "Store", spy)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    postgres_cli_config(config_dir)
    data = tmp_path / "data"
    result = run_cli(["--plain"], config_dir, data_dir=data)
    assert (result.code, result.out) == (0, "")
    assert result.err == ""
    assert connects == []
    assert not data.exists()  # no SQLite file, no directory
    (store,) = built
    assert isinstance(store._relational, PostgresRelationalStore)
    assert store._relational.label == f"{HOST}:5432/{DATABASE}"


class StatusConnection:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, sql: str, params: object = None) -> "StatusConnection":
        return self

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        pass


def test_cli_storage_status_prints_the_postgres_lines(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        postgres_module,
        "_default_connect",
        lambda **kwargs: StatusConnection([("ledger", 1)]),
    )
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    postgres_cli_config(config_dir)
    data = tmp_path / "data"
    result = run_cli(["--storage-status"], config_dir, data_dir=data)
    assert (result.code, result.err) == (0, "")
    lines = result.out.splitlines()
    assert lines[:2] == [
        f"relational: postgres  {HOST}:5432/{DATABASE}",
        "  schema: ledger version 1 (the code expects 1)",
    ]
    assert any(line.startswith("vector:") for line in lines)
    assert any(line.startswith("files:") for line in lines)
    for fake in FAKE_VALUES[1:3]:
        assert fake not in result.out
    assert not data.exists()


def test_cli_storage_status_with_a_failing_connect_prints_one_line_and_exits_0(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    def refuse(**kwargs: object) -> None:
        raise ConnectionRefusedError(f"{HOST} {USER} {PASSWORD}")

    monkeypatch.setattr(postgres_module, "_default_connect", refuse)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    postgres_cli_config(config_dir)
    result = run_cli(["--storage-status"], config_dir, data_dir=tmp_path / "data")
    assert (result.code, result.err) == (0, "")
    lines = result.out.splitlines()
    assert lines[:2] == [
        f"relational: postgres  {HOST}:5432/{DATABASE}",
        "  cannot connect (ConnectionRefusedError)",
    ]
    assert lines[2].startswith("vector:")
    assert any(line.startswith("files:") for line in lines)
    for fake in (USER, PASSWORD):
        assert fake not in result.out


def test_cli_startup_with_sqlite_still_builds_the_ledger_from_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    kwargs_seen: list[dict[str, object]] = []

    def spy(*args: object, **kwargs: object) -> Store:
        kwargs_seen.append({"args": args, **kwargs})
        return Store(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module, "Store", spy)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    (config_dir / "config.toml").write_text(CONFIG_TOML)
    (config_dir / "models.yaml").write_text(MODELS_YAML)
    (config_dir / "agents.yaml").write_text(AGENTS_YAML)
    data = tmp_path / "data"
    result = run_cli(["--plain"], config_dir, data_dir=data)
    assert result.code == 0
    assert kwargs_seen == [{"args": (data / "harness.db",)}]


def test_cli_storage_status_for_postgres_ignores_a_sqlite_file_in_the_data_dir(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        postgres_module, "_default_connect", lambda **kwargs: StatusConnection([])
    )
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    postgres_cli_config(config_dir)
    data = tmp_path / "data"
    data.mkdir()
    (data / "harness.db").write_bytes(b"")  # an existing ledger must not be reported
    result = run_cli(["--storage-status"], config_dir, data_dir=data)
    assert (result.code, result.err) == (0, "")
    assert "sqlite" not in result.out and "harness.db" not in result.out
    assert "  schema: ledger not set up yet (it is set up on first use)" in result.out
    assert sorted(p.name for p in data.iterdir()) == ["harness.db"]
