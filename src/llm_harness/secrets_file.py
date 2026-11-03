"""The optional secrets file, `<config_dir>/secrets.toml`.

Holds credentials only; connection settings such as the qdrant or postgres url
live in config.toml. It is read only when a backend that needs a secret is
selected.
"""

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_harness.config import ConfigError
from llm_harness.redact import Secret

SECRETS_FILE = "secrets.toml"

# Known tables and their keys.
_KNOWN_KEYS = {"qdrant": ("api_key",), "postgres": ("password",)}


@dataclass(frozen=True)
class Secrets:
    """Every secret the file can hold; None for a missing file or key."""

    qdrant_api_key: Secret | None
    postgres_password: Secret | None


def _invalid(detail: str) -> ConfigError:
    return ConfigError(f"invalid {SECRETS_FILE}: {detail}")


def _read_private_file(path: Path) -> bytes | None:
    """The file's bytes, or None when it is absent; refuses a shared file."""
    try:
        # O_NONBLOCK so a FIFO cannot hang the open; checks run on the opened
        # descriptor so the file cannot change between the check and the read.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # Type name only: never echo anything that could come from the file.
        raise ConfigError(f"cannot read {path}: {type(exc).__name__}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _invalid(f"{path} is not a regular file")
        if info.st_mode & 0o077:
            raise _invalid(
                f"{path} is accessible by group or other users; run: chmod 600 {path}"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {type(exc).__name__}") from None
    finally:
        os.close(fd)


def _check_tables(path: Path, data: dict[str, Any]) -> None:
    for table, content in data.items():
        if table not in _KNOWN_KEYS:
            raise _invalid(
                f"unknown table [{table}] in {path}; "
                f"valid tables: {', '.join(_KNOWN_KEYS)}"
            )
        if not isinstance(content, dict):
            raise _invalid(f"{table} in {path} must be a table, written as [{table}]")
        for key in content:
            if key not in _KNOWN_KEYS[table]:
                raise _invalid(
                    f"unknown key {key!r} in [{table}] of {path}; "
                    f"valid keys: {', '.join(_KNOWN_KEYS[table])}"
                )


def _check_secret(path: Path, table: str, key: str, value: object) -> Secret:
    # Messages name the key and the file, never the value.
    label = f"[{table}] {key} in {path}"
    if not isinstance(value, str):
        raise _invalid(f"{label} must be a string, not {type(value).__name__}")
    if not value.strip():
        raise _invalid(f"{label} is empty")
    if not value.isprintable():
        raise _invalid(f"{label} contains a newline or other control character")
    if value != value.strip():
        raise _invalid(f"{label} has leading or trailing whitespace")
    return Secret(value)


def _optional_secret(
    path: Path, data: dict[str, Any], table: str, key: str
) -> Secret | None:
    value = data.get(table, {}).get(key)
    return None if value is None else _check_secret(path, table, key, value)


def load_secrets(config_dir: Path) -> Secrets:
    """The secrets in the file; both None when there is no file."""
    path = config_dir / SECRETS_FILE
    raw = _read_private_file(path)
    if raw is None:
        return Secrets(qdrant_api_key=None, postgres_password=None)
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        # tomllib messages carry a line and column, not file content.
        position = str(exc) if isinstance(exc, tomllib.TOMLDecodeError) else "not UTF-8"
        raise ConfigError(f"cannot parse {path}: {position}") from None
    _check_tables(path, data)
    return Secrets(
        qdrant_api_key=_optional_secret(path, data, "qdrant", "api_key"),
        postgres_password=_optional_secret(path, data, "postgres", "password"),
    )
