"""Config rules for `[storage.vector] backend = "pgvector"`, its secrets and guards."""

import tomllib
from pathlib import Path

import pytest

import llm_harness.cli as cli_module
import llm_harness.config as config_module
import llm_harness.secrets_file as secrets_file_module
import llm_harness.storage.postgres as postgres_module
from llm_harness.config import (
    ConfigError,
    StorageConfig,
    VectorConfig,
    load_config,
)
from llm_harness.redact import Secret
from llm_harness.storage import open_storage
from llm_harness.storage.pgvector import MIGRATIONS as PGVECTOR_MIGRATIONS
from llm_harness.storage.pgvector import PgVectorStore
from llm_harness.storage.relational import ReadOnlySchema
from llm_harness.storage.status import _schema_lines
from llm_harness.storage.vectors import VectorStoreError
from llm_harness.store import expected_schema_versions, optional_schema_versions
from tests.fake_pgvector import FakePgVectorServer
from tests.test_cli import AGENTS_YAML, CONFIG_TOML, MODELS_YAML, run_cli
from tests.test_config import (
    PROVIDER_TOML,
    load_storage,
    qdrant_toml,
    storage_error,
    write_config,
)
from tests.test_config_postgres import (
    BAD_URLS,
    DATABASE,
    FAKE_VALUES,
    HOST,
    PASSWORD,
    URL,
    USER,
    pg_toml,
)
from tests.test_secrets_file import KEY, VALID, write_secrets

PREFIX = "invalid config.toml: storage.vector: "
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
PG_SECRET = f'[postgres]\npassword = "{PASSWORD}"\n'


def pgv_toml(extra: str = f'url = "{URL}"\n') -> str:
    return f'[storage.vector]\nbackend = "pgvector"\n{extra}'


def url_toml(url: str) -> str:
    return pgv_toml(f'url = "{url}"\n')


@pytest.fixture
def driver_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_postgres_driver_installed", lambda: True)


@pytest.fixture
def client_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_qdrant_client_installed", lambda: True)


# --- the vector table ----------------------------------------------------------


def test_pgvector_with_a_valid_url_loads(tmp_path: Path, driver_installed: None):
    vector = load_storage(tmp_path, pgv_toml()).vector
    assert (vector.backend, vector.url, vector.path) == ("pgvector", URL, None)


def test_pgvector_without_url_is_rejected_with_an_example(
    tmp_path: Path, driver_installed: None
):
    message = storage_error(tmp_path, pgv_toml(""))
    assert message == (
        PREFIX + 'backend "pgvector" requires url, for example '
        'url = "postgresql://user@db.example:5432/llm_harness"'
    )


def test_pgvector_with_path_is_rejected(tmp_path: Path, driver_installed: None):
    message = storage_error(tmp_path, pgv_toml(f'url = "{URL}"\npath = "/v"\n'))
    assert message == PREFIX + 'path is only used with a local backend, not "pgvector"'


@pytest.mark.parametrize(("url", "expected"), BAD_URLS)
def test_every_relational_bad_url_is_rejected_for_pgvector(
    tmp_path: Path, driver_installed: None, url: str, expected: str
):
    message = storage_error(tmp_path, url_toml(url))
    assert message.startswith(PREFIX)
    assert expected in message


@pytest.mark.parametrize(
    "url",
    [
        f"mysql://{PASSWORD}@{FAKE_VALUES[0]}:5432/{FAKE_VALUES[3]}",
        f"postgresql://{FAKE_VALUES[1]}:{PASSWORD}@{FAKE_VALUES[0]}:5432/{FAKE_VALUES[3]}",
        f"postgresql://{FAKE_VALUES[1]}@{FAKE_VALUES[0]}/d?password={PASSWORD}",
        f"postgresql://{FAKE_VALUES[1]}@{FAKE_VALUES[0]}:99999/{FAKE_VALUES[3]}",
        f"postgresql://{FAKE_VALUES[1]}@{FAKE_VALUES[0]}:5432/{FAKE_VALUES[3]}#{PASSWORD}",
        f"postgresql://{FAKE_VALUES[1]}@{FAKE_VALUES[0]}",
    ],
)
def test_no_message_echoes_any_part_of_the_url(
    tmp_path: Path, driver_installed: None, url: str
):
    message = storage_error(tmp_path, url_toml(url))
    for value in FAKE_VALUES:
        assert value not in message


def test_the_missing_driver_names_the_extra_and_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(config_module, "_postgres_driver_installed", lambda: False)
    message = storage_error(tmp_path, pgv_toml())
    assert message == (
        PREFIX + 'backend "pgvector" needs the optional extra postgres, which is '
        "not installed; run: uv sync --extra postgres"
    )
    for value in FAKE_VALUES:
        assert value not in message


def test_a_bad_url_is_reported_before_the_missing_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(config_module, "_postgres_driver_installed", lambda: False)
    message = storage_error(tmp_path, url_toml("mysql://h/d"))
    assert "url must be a postgresql://" in message
    assert "uv sync" not in message


@pytest.mark.parametrize("backend", ["qdrant-edge", "none"])
def test_local_backends_never_check_for_the_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
):
    def fail() -> bool:
        raise AssertionError("the driver check must not run")

    monkeypatch.setattr(config_module, "_postgres_driver_installed", fail)
    toml = f'[storage.vector]\nbackend = "{backend}"\n'
    assert load_storage(tmp_path, toml).vector.backend == backend


@pytest.mark.parametrize("backend", ["qdrant-edge", "none"])
def test_url_with_a_local_backend_names_both_url_backends(tmp_path: Path, backend: str):
    message = storage_error(
        tmp_path, f'[storage.vector]\nbackend = "{backend}"\nurl = "{URL}"\n'
    )
    assert message == PREFIX + 'url is only used with backend = "qdrant" or "pgvector"'


def test_qdrant_keeps_its_own_url_rules_and_message(
    tmp_path: Path, client_installed: None, driver_installed: None
):
    # A postgres url is not a valid qdrant url, and the qdrant message stays.
    message = storage_error(tmp_path, qdrant_toml(f'url = "{URL}"\n'))
    assert "url must be an absolute http:// or https:// URL" in message
    assert "postgresql" not in message


def test_qdrant_missing_extra_message_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(config_module, "_qdrant_client_installed", lambda: False)
    message = storage_error(tmp_path, qdrant_toml())
    assert message.endswith(
        'backend "qdrant" needs the optional extra remote, which is not '
        "installed; run: uv sync --extra remote"
    )


def test_unknown_vector_backend_lists_pgvector_in_order(tmp_path: Path):
    message = storage_error(tmp_path, '[storage.vector]\nbackend = "x"\n')
    assert (
        "storage.vector.backend: unknown backend 'x'; "
        "use one of: qdrant-edge, qdrant, pgvector, none"
    ) in message


def test_the_example_config_documents_pgvector():
    text = config_module.EXAMPLE_CONFIG_TOML
    assert (
        '# "pgvector" (PostgreSQL with the pgvector extension) or "none" to turn'
        in text
    )
    assert (
        '# backend = "qdrant-edge"              # or "qdrant", "pgvector", or "none"'
        in text
    )
    assert 'secrets.toml under [postgres] (see the README, "pgvector").' in text


# --- secrets ---------------------------------------------------------------------


def test_pgvector_with_sqlite_loads_only_the_password(
    tmp_path: Path, driver_installed: None
):
    write_config(tmp_path, toml=config_module.EXAMPLE_CONFIG_TOML + pgv_toml())
    write_secrets(tmp_path, VALID + PG_SECRET)
    config = load_config(tmp_path)
    assert config.qdrant_api_key is None
    assert config.postgres_password is not None
    assert config.postgres_password.reveal() == PASSWORD


def test_relational_postgres_with_pgvector_reads_one_file_and_sets_the_password(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    opened: list[object] = []
    real_open = secrets_file_module.os.open

    def counting_open(*args: object, **kwargs: object) -> int:
        opened.append(args[0])
        return real_open(*args, **kwargs)

    monkeypatch.setattr(secrets_file_module.os, "open", counting_open)
    write_config(
        tmp_path, toml=config_module.EXAMPLE_CONFIG_TOML + pg_toml() + pgv_toml()
    )
    write_secrets(tmp_path, VALID + PG_SECRET)
    config = load_config(tmp_path)
    assert opened == [tmp_path / "secrets.toml"]
    assert config.postgres_password is not None
    assert config.postgres_password.reveal() == PASSWORD
    assert config.qdrant_api_key is None


def test_qdrant_with_sqlite_loads_only_the_qdrant_key(
    tmp_path: Path, client_installed: None
):
    write_config(tmp_path, toml=config_module.EXAMPLE_CONFIG_TOML + qdrant_toml())
    write_secrets(tmp_path, VALID + PG_SECRET)
    config = load_config(tmp_path)
    assert config.qdrant_api_key is not None
    assert config.qdrant_api_key.reveal() == KEY
    assert config.postgres_password is None


@pytest.mark.parametrize("vector", ["qdrant-edge", "none"])
def test_sqlite_with_local_vectors_never_opens_a_garbage_file(
    tmp_path: Path, vector: str
):
    write_config(
        tmp_path,
        toml=config_module.EXAMPLE_CONFIG_TOML
        + f'[storage.vector]\nbackend = "{vector}"\n',
    )
    write_secrets(tmp_path, f"garbage {PASSWORD} [[[\n", 0o644)
    config = load_config(tmp_path)
    assert (config.qdrant_api_key, config.postgres_password) == (None, None)


def test_pgvector_with_a_shared_secrets_file_fails_load_config(
    tmp_path: Path, driver_installed: None
):
    write_config(tmp_path, toml=config_module.EXAMPLE_CONFIG_TOML + pgv_toml())
    write_secrets(tmp_path, PG_SECRET, 0o644)
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    assert PASSWORD not in str(info.value)


# --- memory refusal -------------------------------------------------------------


def test_pgvector_with_sqlite_and_memory_is_accepted(
    tmp_path: Path, driver_installed: None
):
    toml = 'user_id = "ann"\n' + PROVIDER_TOML + "[tools]\nmemory = true\n" + pgv_toml()
    write_config(tmp_path, toml)
    config = load_config(tmp_path)
    assert (config.tools.memory, config.storage.vector.backend) == (True, "pgvector")


def test_relational_postgres_with_memory_loads(tmp_path: Path, driver_installed: None):
    toml = 'user_id = "ann"\n' + PROVIDER_TOML + "[tools]\nmemory = true\n" + pg_toml()
    write_config(tmp_path, toml)
    config = load_config(tmp_path)
    assert (config.tools.memory, config.storage.relational.backend) == (
        True,
        "postgres",
    )


# --- pyproject ----------------------------------------------------------------------


def test_pyproject_has_no_new_extra_and_no_pgvector_dependency():
    data = tomllib.loads(PYPROJECT.read_text())
    assert set(data["project"]["optional-dependencies"]) == {"remote", "postgres"}
    every_dependency = list(data["project"]["dependencies"])
    for group in data["project"]["optional-dependencies"].values():
        every_dependency += group
    for group in data.get("dependency-groups", {}).values():
        every_dependency += [item for item in group if isinstance(item, str)]
    assert every_dependency
    assert not [item for item in every_dependency if "pgvector" in item.lower()]


# --- open_storage ---------------------------------------------------------------------


def pgvector_storage() -> StorageConfig:
    return StorageConfig(vector=VectorConfig(backend="pgvector", url=URL))


def forbid_connections(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    connects: list[int] = []

    def factory(**kwargs: object) -> None:
        connects.append(1)
        raise AssertionError("nothing may connect to postgres")

    monkeypatch.setattr(postgres_module, "_default_connect", factory)
    return connects


def test_open_storage_builds_the_pgvector_store_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    connects = forbid_connections(monkeypatch)
    data = tmp_path / "data"
    storage = open_storage(pgvector_storage(), data, postgres_password=Secret(PASSWORD))
    assert isinstance(storage.vectors, PgVectorStore)
    assert (
        repr(storage.vectors) == f"PgVectorStore({HOST}:5432/{DATABASE}, password=set)"
    )
    assert PASSWORD not in repr(storage.vectors)
    assert connects == []
    assert not data.exists()


def test_open_storage_for_pgvector_without_a_url_names_the_setting(tmp_path: Path):
    storage = StorageConfig.model_construct(
        vector=VectorConfig.model_construct(backend="pgvector", path=None, url=None)
    )
    with pytest.raises(VectorStoreError, match=r"\[storage.vector\] url"):
        open_storage(storage, tmp_path / "data")


# --- the cli ----------------------------------------------------------------------------


def pgvector_cli_config(config_dir: Path) -> None:
    (config_dir / "config.toml").write_text(CONFIG_TOML + pgv_toml())
    (config_dir / "config.toml").chmod(0o600)
    (config_dir / "models.yaml").write_text(MODELS_YAML)
    (config_dir / "agents.yaml").write_text(AGENTS_YAML)


def cli_dirs(tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    pgvector_cli_config(config_dir)
    return config_dir, tmp_path / "data"


def test_the_cli_normal_start_with_pgvector_connects_to_nothing(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    connects = forbid_connections(monkeypatch)
    config_dir, data = cli_dirs(tmp_path)
    result = run_cli(["--plain"], config_dir, data_dir=data)
    assert (result.code, result.out, result.err) == (0, "", "")
    assert connects == []
    assert not data.exists()


def test_the_cli_storage_status_prints_the_pgvector_lines(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    server = FakePgVectorServer()
    server.tables.add("vector_collections")
    server.collections["tools"] = (4, "cosine")
    monkeypatch.setattr(postgres_module, "_default_connect", server)
    config_dir, data = cli_dirs(tmp_path)
    result = run_cli(["--storage-status"], config_dir, data_dir=data)
    assert (result.code, result.err) == (0, "")
    lines = result.out.splitlines()
    start = lines.index(f"vector: pgvector  {HOST}:5432/{DATABASE}")
    assert lines[start : start + 3] == [
        f"vector: pgvector  {HOST}:5432/{DATABASE}",
        "  extension: vector 0.8.7",
        "  collection tools: 4 dimensions, cosine",
    ]
    assert lines[start + 3].startswith("files:")
    for fake in (USER, PASSWORD):
        assert fake not in result.out
    assert not data.exists()


def test_the_cli_storage_status_with_a_failing_connect_still_exits_0(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    server = FakePgVectorServer()
    server.refuse_connections = ConnectionRefusedError(f"{HOST} {USER} {PASSWORD}")
    monkeypatch.setattr(postgres_module, "_default_connect", server)
    config_dir, data = cli_dirs(tmp_path)
    result = run_cli(["--storage-status"], config_dir, data_dir=data)
    assert (result.code, result.err) == (0, "")
    lines = result.out.splitlines()
    start = lines.index(f"vector: pgvector  {HOST}:5432/{DATABASE}")
    assert lines[start + 1] == "  cannot connect (ConnectionRefusedError)"
    assert lines[start + 2].startswith("files:")
    assert sum("cannot connect" in line for line in lines) == 1
    for fake in (USER, PASSWORD):
        assert fake not in result.out


# --- the plain-http key warning is for qdrant only --------------------------------------


def record_warnings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []
    real = cli_module.insecure_key_warning

    def spy(url: str, key: object) -> str | None:
        calls.append((url, key))
        return real(url, key)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module, "insecure_key_warning", spy)
    return calls


def test_a_pgvector_start_never_asks_for_the_key_warning(
    tmp_path: Path, driver_installed: None, monkeypatch: pytest.MonkeyPatch
):
    calls = record_warnings(monkeypatch)
    config_dir, data = cli_dirs(tmp_path)
    result = run_cli(["--plain"], config_dir, data_dir=data)
    assert (result.code, result.err) == (0, "")
    assert calls == []


def test_a_qdrant_start_with_http_and_a_key_still_warns_once(
    tmp_path: Path, client_installed: None, monkeypatch: pytest.MonkeyPatch
):
    calls = record_warnings(monkeypatch)
    config_dir, data = cli_dirs(tmp_path)
    (config_dir / "config.toml").write_text(
        CONFIG_TOML + qdrant_toml('url = "http://qdrant.internal.test:6333"\n')
    )
    (config_dir / "config.toml").chmod(0o600)
    write_secrets(config_dir, VALID)
    result = run_cli(["--plain"], config_dir, data_dir=data)
    assert result.code == 0, result.err
    assert len(calls) == 1
    assert len([line for line in result.err.splitlines() if "unencrypted" in line]) == 1
    assert KEY not in result.err


# --- schema versions ---------------------------------------------------------------------


def test_the_vector_namespace_is_a_known_optional_one():
    assert optional_schema_versions()["vector"] == len(PGVECTOR_MIGRATIONS) == 1


def test_a_shared_database_prints_the_vector_schema_line_as_known():
    schema = ReadOnlySchema(
        versions={"ledger": expected_schema_versions()["ledger"], "vector": 1},
        user_version=0,
    )
    lines = _schema_lines(schema)
    assert "  schema: vector version 1 (the code expects 1)" in lines
    assert not [line for line in lines if "does not know" in line]
