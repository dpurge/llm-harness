import os
from pathlib import Path

import pytest

import llm_harness.config as config_module
import llm_harness.secrets_file as secrets_file_module
from llm_harness.config import (
    EXAMPLE_CONFIG_TOML,
    ConfigError,
    HarnessConfig,
    load_config,
)
from llm_harness.redact import REDACTED, Secret
from llm_harness.secrets_file import SECRETS_FILE, Secrets, load_secrets
from tests.test_config import qdrant_toml, write_config
from tests.platform_marks import posix_only

KEY = "sk-test-SECRET-123"
VALID = f'[qdrant]\napi_key = "{KEY}"\n'


def write_secrets(config_dir: Path, text: str, mode: int = 0o600) -> Path:
    path = config_dir / SECRETS_FILE
    path.write_text(text)
    os.chmod(path, mode)
    return path


def secrets_error(config_dir: Path) -> str:
    with pytest.raises(ConfigError) as info:
        load_secrets(config_dir)
    return str(info.value)


@pytest.fixture
def client_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_qdrant_client_installed", lambda: True)


def test_absent_file_gives_no_key(tmp_path: Path):
    assert load_secrets(tmp_path).qdrant_api_key is None


def test_private_file_gives_the_key_as_a_secret(tmp_path: Path):
    write_secrets(tmp_path, VALID)
    key = load_secrets(tmp_path).qdrant_api_key
    assert isinstance(key, Secret)
    assert key.reveal() == KEY


def test_every_printable_form_is_masked(tmp_path: Path):
    write_secrets(tmp_path, VALID)
    key = load_secrets(tmp_path).qdrant_api_key
    assert (str(key), repr(key), f"{key}") == (REDACTED, REDACTED, REDACTED)


@pytest.mark.parametrize("text", ["", "[qdrant]\n"])
def test_file_without_a_key_gives_none(tmp_path: Path, text: str):
    write_secrets(tmp_path, text)
    assert load_secrets(tmp_path).qdrant_api_key is None


@posix_only
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_shared_file_is_rejected_with_the_fix(tmp_path: Path, mode: int):
    path = write_secrets(tmp_path, VALID, mode)
    message = secrets_error(tmp_path)
    assert message.startswith("invalid secrets.toml: ")
    assert str(path) in message
    assert f"chmod 600 {path}" in message
    assert KEY not in message


def test_directory_named_secrets_toml_is_rejected(tmp_path: Path):
    (tmp_path / SECRETS_FILE).mkdir()
    message = secrets_error(tmp_path)
    assert "not a regular file" in message
    assert str(tmp_path / SECRETS_FILE) in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f'[qdrnt]\napi_key = "{KEY}"\n', "unknown table [qdrnt]"),
        (f'[qdrant]\napi_key = "{KEY}"\n[mysql]\n', "unknown table [mysql]"),
        (f'top = "{KEY}"\n', "unknown table [top]"),
        (f'[qdrant]\napi_key = "{KEY}"\nurl = "http://x"\n', "unknown key 'url'"),
        (f'[qdrant]\napi_kye = "{KEY}"\n', "unknown key 'api_kye'"),
    ],
)
def test_unknown_names_are_rejected_and_listed(
    tmp_path: Path, text: str, expected: str
):
    write_secrets(tmp_path, text)
    message = secrets_error(tmp_path)
    assert message.startswith("invalid secrets.toml: ")
    assert expected in message
    assert "valid " in message
    assert str(tmp_path / SECRETS_FILE) in message
    assert KEY not in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('[qdrant]\napi_key = ""\n', "is empty"),
        ('[qdrant]\napi_key = "   "\n', "is empty"),
        (f'[qdrant]\napi_key = "{KEY}\\n"\n', "control character"),
        (f'[qdrant]\napi_key = "{KEY}\\tx"\n', "control character"),
        (f'[qdrant]\napi_key = "{KEY}\\u0000"\n', "control character"),
        (f'[qdrant]\napi_key = " {KEY}"\n', "leading or trailing whitespace"),
        ("[qdrant]\napi_key = 12345\n", "must be a string, not int"),
        (f'[qdrant]\napi_key = ["{KEY}"]\n', "must be a string, not list"),
        (f'qdrant = "{KEY}"\n', "must be a table"),
    ],
)
def test_bad_api_key_is_rejected_without_echoing_it(
    tmp_path: Path, text: str, expected: str
):
    write_secrets(tmp_path, text)
    message = secrets_error(tmp_path)
    assert message.startswith("invalid secrets.toml: ")
    assert expected in message
    assert KEY not in message


@pytest.mark.parametrize(
    "text",
    [
        f'[qdrant]\napi_key = "{KEY}" junk\n',
        f'[qdrant]\napi_key = "{KEY}\n',
        f"[qdrant]\napi_key = {KEY}\n",
        f'[qdrant]\napi_key = "{KEY}"\n[qdrant]\n',
        f'[qdrant]\napi_key = "{KEY}"\napi_key = "{KEY}"\n',
        f'[qdrant]\napi_key = "{KEY}\\q"\n',
    ],
)
def test_broken_toml_names_the_file_and_never_echoes_content(tmp_path: Path, text: str):
    path = write_secrets(tmp_path, text)
    message = secrets_error(tmp_path)
    assert message.startswith(f"cannot parse {path}: ")
    assert KEY not in message
    assert "api_key" not in message


@posix_only
def test_file_that_is_not_utf8_is_rejected(tmp_path: Path):
    path = tmp_path / SECRETS_FILE
    path.write_bytes(b'[qdrant]\napi_key = "\xff\xfe"\n')
    os.chmod(path, 0o600)
    assert secrets_error(tmp_path) == f"cannot parse {path}: not UTF-8"


def test_load_config_end_to_end_carries_the_key(tmp_path: Path, client_installed: None):
    write_config(tmp_path, toml=EXAMPLE_CONFIG_TOML + qdrant_toml())
    write_secrets(tmp_path, VALID)
    config = load_config(tmp_path)
    assert config.qdrant_api_key is not None
    assert config.qdrant_api_key.reveal() == KEY


def test_the_whole_config_never_prints_the_key(tmp_path: Path, client_installed: None):
    write_config(tmp_path, toml=EXAMPLE_CONFIG_TOML + qdrant_toml())
    write_secrets(tmp_path, VALID)
    config = load_config(tmp_path)
    for text in (str(config), repr(config), f"{config}"):
        assert KEY not in text
    assert REDACTED in repr(config)


def test_qdrant_without_a_file_loads_with_no_key(
    tmp_path: Path, client_installed: None
):
    write_config(tmp_path, toml=EXAMPLE_CONFIG_TOML + qdrant_toml())
    assert load_config(tmp_path).qdrant_api_key is None


@posix_only
def test_qdrant_with_a_shared_file_fails_load_config(
    tmp_path: Path, client_installed: None
):
    write_config(tmp_path, toml=EXAMPLE_CONFIG_TOML + qdrant_toml())
    write_secrets(tmp_path, VALID, 0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_config(tmp_path)


@posix_only
@pytest.mark.parametrize("backend", ["qdrant-edge", "none"])
def test_local_backends_never_read_the_file(tmp_path: Path, backend: str):
    write_config(
        tmp_path,
        toml=EXAMPLE_CONFIG_TOML + f'[storage.vector]\nbackend = "{backend}"\n',
    )
    write_secrets(tmp_path, f"garbage {KEY} [[[\n", 0o644)
    assert load_config(tmp_path).qdrant_api_key is None


def test_local_backends_do_not_even_open_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("the secrets file must not be opened")

    monkeypatch.setattr(secrets_file_module.os, "open", fail)
    write_config(tmp_path)
    assert load_config(tmp_path).qdrant_api_key is None


def test_default_config_has_no_key():
    assert (
        HarnessConfig.model_validate({"providers": {}, "models": []}).qdrant_api_key
        is None
    )


# --- [postgres] password -----------------------------------------------------

PASSWORD = "pg-SECRET-pass-456"
PG_VALID = f'[postgres]\npassword = "{PASSWORD}"\n'
PG_URL = "postgresql://app_user@db-secret-host.example:5432/llm_harness"


def pg_toml(vector: str = "") -> str:
    return (
        EXAMPLE_CONFIG_TOML
        + f'[storage.relational]\nbackend = "postgres"\nurl = "{PG_URL}"\n'
        + vector
    )


@pytest.fixture
def driver_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_postgres_driver_installed", lambda: True)


def test_postgres_password_loads_as_a_masked_secret(tmp_path: Path):
    write_secrets(tmp_path, PG_VALID)
    password = load_secrets(tmp_path).postgres_password
    assert isinstance(password, Secret)
    assert password.reveal() == PASSWORD
    assert (str(password), repr(password), f"{password}") == (REDACTED,) * 3


def test_absent_file_gives_both_none(tmp_path: Path):
    assert load_secrets(tmp_path) == Secrets(None, None)


def test_one_file_with_both_tables_loads_both(tmp_path: Path):
    write_secrets(tmp_path, VALID + PG_VALID)
    secrets = load_secrets(tmp_path)
    assert secrets.qdrant_api_key is not None
    assert secrets.qdrant_api_key.reveal() == KEY
    assert secrets.postgres_password is not None
    assert secrets.postgres_password.reveal() == PASSWORD


def test_empty_postgres_table_gives_no_password(tmp_path: Path):
    write_secrets(tmp_path, "[postgres]\n")
    assert load_secrets(tmp_path).postgres_password is None


def test_valid_names_listed_include_postgres(tmp_path: Path):
    write_secrets(tmp_path, "[mysql]\n")
    assert "valid tables: qdrant, postgres" in secrets_error(tmp_path)
    write_secrets(tmp_path, '[postgres]\nuser = "x"\n')
    message = secrets_error(tmp_path)
    assert "unknown key 'user' in [postgres]" in message
    assert "valid keys: password" in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('[postgres]\npassword = ""\n', "[postgres] password in {path} is empty"),
        ('[postgres]\npassword = "  "\n', "is empty"),
        (
            f'[postgres]\npassword = "{PASSWORD}\\n"\n',
            "contains a newline or other control character",
        ),
        (f'[postgres]\npassword = "a\\u0000{PASSWORD}"\n', "control character"),
        (
            f'[postgres]\npassword = " {PASSWORD}"\n',
            "has leading or trailing whitespace",
        ),
        (
            f'[postgres]\npassword = "{PASSWORD} "\n',
            "has leading or trailing whitespace",
        ),
        ("[postgres]\npassword = 12345\n", "must be a string, not int"),
        (f'[postgres]\npassword = ["{PASSWORD}"]\n', "must be a string, not list"),
        (f'postgres = "{PASSWORD}"\n', "must be a table"),
    ],
)
def test_bad_password_is_rejected_without_echoing_it(
    tmp_path: Path, text: str, expected: str
):
    path = write_secrets(tmp_path, text)
    message = secrets_error(tmp_path)
    assert message.startswith("invalid secrets.toml: ")
    assert expected.format(path=path) in message
    assert PASSWORD not in message


@posix_only
@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_shared_file_with_a_password_is_rejected(tmp_path: Path, mode: int):
    path = write_secrets(tmp_path, PG_VALID, mode)
    message = secrets_error(tmp_path)
    assert f"chmod 600 {path}" in message
    assert PASSWORD not in message


def test_load_config_postgres_carries_the_password(
    tmp_path: Path, driver_installed: None
):
    write_config(tmp_path, toml=pg_toml())
    write_secrets(tmp_path, PG_VALID)
    config = load_config(tmp_path)
    assert config.postgres_password is not None
    assert config.postgres_password.reveal() == PASSWORD
    assert config.qdrant_api_key is None


def test_the_whole_config_never_prints_the_password(
    tmp_path: Path, driver_installed: None
):
    write_config(tmp_path, toml=pg_toml())
    write_secrets(tmp_path, PG_VALID)
    config = load_config(tmp_path)
    for text in (str(config), repr(config), f"{config}"):
        assert PASSWORD not in text
    assert REDACTED in repr(config)


def test_postgres_without_a_file_loads_with_no_password(
    tmp_path: Path, driver_installed: None
):
    write_config(tmp_path, toml=pg_toml())
    assert load_config(tmp_path).postgres_password is None


@posix_only
def test_postgres_with_a_shared_file_fails_load_config(
    tmp_path: Path, driver_installed: None
):
    write_config(tmp_path, toml=pg_toml())
    write_secrets(tmp_path, PG_VALID, 0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_config(tmp_path)


def test_sqlite_and_qdrant_loads_only_the_qdrant_key(
    tmp_path: Path, client_installed: None
):
    write_config(tmp_path, toml=EXAMPLE_CONFIG_TOML + qdrant_toml())
    write_secrets(tmp_path, VALID + PG_VALID)
    config = load_config(tmp_path)
    assert config.qdrant_api_key is not None
    assert config.qdrant_api_key.reveal() == KEY
    assert config.postgres_password is None


def test_postgres_and_qdrant_edge_loads_only_the_password(
    tmp_path: Path, driver_installed: None
):
    write_config(tmp_path, toml=pg_toml('[storage.vector]\nbackend = "qdrant-edge"\n'))
    write_secrets(tmp_path, VALID + PG_VALID)
    config = load_config(tmp_path)
    assert config.qdrant_api_key is None
    assert config.postgres_password is not None
    assert config.postgres_password.reveal() == PASSWORD


def test_postgres_and_qdrant_load_both_from_one_read(
    tmp_path: Path, driver_installed: None, client_installed: None
):
    write_config(tmp_path, toml=pg_toml(qdrant_toml()))
    write_secrets(tmp_path, VALID + PG_VALID)
    config = load_config(tmp_path)
    assert config.qdrant_api_key is not None
    assert config.postgres_password is not None


def test_one_read_and_one_parse_serve_both_secrets(
    tmp_path: Path,
    driver_installed: None,
    client_installed: None,
    monkeypatch: pytest.MonkeyPatch,
):
    opened: list[object] = []
    real_open = secrets_file_module.os.open

    def counting_open(*args: object, **kwargs: object) -> int:
        opened.append(args[0])
        return real_open(*args, **kwargs)

    monkeypatch.setattr(secrets_file_module.os, "open", counting_open)
    write_config(tmp_path, toml=pg_toml(qdrant_toml()))
    write_secrets(tmp_path, VALID + PG_VALID)
    load_config(tmp_path)
    assert opened == [tmp_path / SECRETS_FILE]


@posix_only
@pytest.mark.parametrize("vector", ["qdrant-edge", "none"])
def test_sqlite_with_local_vectors_never_reads_a_garbage_file(
    tmp_path: Path, vector: str
):
    write_config(
        tmp_path,
        toml=EXAMPLE_CONFIG_TOML + f'[storage.vector]\nbackend = "{vector}"\n',
    )
    write_secrets(tmp_path, f"garbage {PASSWORD} [[[\n", 0o644)
    config = load_config(tmp_path)
    assert (config.qdrant_api_key, config.postgres_password) == (None, None)


def test_default_config_has_no_password():
    config = HarnessConfig.model_validate({"providers": {}, "models": []})
    assert config.postgres_password is None
