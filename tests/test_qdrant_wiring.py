"""The remote qdrant backend through `open_storage`, the cli and `--storage-status`.

Offline: no client is ever built and nothing listens on a socket.
"""

from pathlib import Path

import pytest
import qdrant_client

from llm_harness.config import (
    AGENTS_FILE,
    CONFIG_FILE,
    MODELS_FILE,
    HarnessConfig,
    ProviderConfig,
    StorageConfig,
    VectorConfig,
)
from llm_harness.redact import Secret
from llm_harness.secrets_file import SECRETS_FILE
from llm_harness.storage import open_storage
from llm_harness.storage.qdrant_edge import QdrantEdgeVectorStore
from llm_harness.storage.qdrant_remote import (
    QdrantRemoteVectorStore,
    insecure_key_warning,
)
from llm_harness.storage.status import storage_status
from llm_harness.storage.vectors import VectorStoreError
from llm_harness.types import TextDelta
from tests.test_cli import (
    AGENTS_YAML,
    CONFIG_TOML,
    MODELS_YAML,
    reply_events,
    run_cli,
    scripted,
)

KEY = "sk-test-SECRET-123"
HTTP_URL = "http://qdrant.internal.test:6333"
HTTPS_URL = "https://qdrant.internal.test:6333"
PROVIDERS = {"p": ProviderConfig(type="ollama", base_url="http://localhost:11434")}


def qdrant_storage(url: str = HTTPS_URL) -> StorageConfig:
    return StorageConfig(vector=VectorConfig(backend="qdrant", url=url))


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cfg"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)  # independent of the umask
    for name, text in (
        (CONFIG_FILE, CONFIG_TOML),
        (MODELS_FILE, MODELS_YAML),
        (AGENTS_FILE, AGENTS_YAML),
    ):
        (directory / name).write_text(text)
    (directory / CONFIG_FILE).chmod(0o600)
    return directory


@pytest.fixture
def no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("no qdrant client may be built")

    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", forbidden)


# open_storage


def test_open_storage_builds_the_remote_store_and_creates_nothing(
    tmp_path: Path, no_client: None
) -> None:
    data = tmp_path / "data"
    storage = open_storage(qdrant_storage(), data, qdrant_api_key=Secret(KEY))
    assert isinstance(storage.vectors, QdrantRemoteVectorStore)
    assert not data.exists()
    assert KEY not in repr(storage.vectors)


def test_open_storage_without_a_key_builds_the_remote_store(
    tmp_path: Path, no_client: None
) -> None:
    storage = open_storage(qdrant_storage(), tmp_path / "data")
    assert isinstance(storage.vectors, QdrantRemoteVectorStore)


def test_open_storage_keeps_the_local_call_shape(tmp_path: Path) -> None:
    storage = open_storage(StorageConfig(), tmp_path / "data")
    assert isinstance(storage.vectors, QdrantEdgeVectorStore)


def test_open_storage_refuses_a_qdrant_config_without_url(tmp_path: Path) -> None:
    config = StorageConfig()
    broken = config.model_copy(
        update={"vector": VectorConfig.model_construct(backend="qdrant", url=None)}
    )
    with pytest.raises(VectorStoreError, match="url"):
        open_storage(broken, tmp_path)


# insecure_key_warning


def test_warning_for_http_with_a_key_names_the_host_not_the_key_or_url() -> None:
    line = insecure_key_warning(HTTP_URL + "/prefix", Secret(KEY))
    assert line is not None
    assert "\n" not in line
    assert "unencrypted" in line and "https" in line
    assert "qdrant.internal.test" in line
    assert KEY not in line
    assert "6333" not in line and "/prefix" not in line


def test_no_warning_for_https_with_a_key() -> None:
    assert insecure_key_warning(HTTPS_URL, Secret(KEY)) is None


def test_no_warning_for_http_without_a_key() -> None:
    assert insecure_key_warning(HTTP_URL, None) is None


def test_the_warning_applies_to_any_http_host() -> None:
    assert insecure_key_warning("http://localhost:6333", Secret(KEY)) is not None
    assert insecure_key_warning("HTTP://Qdrant.Example", Secret(KEY)) is not None


# cli


def write_qdrant_config(config_dir: Path, url: str, *, key: str | None) -> None:
    path = config_dir / CONFIG_FILE
    path.write_text(
        CONFIG_TOML + f'\n[storage.vector]\nbackend = "qdrant"\nurl = "{url}"\n'
    )
    path.chmod(0o600)
    if key is not None:
        secrets = config_dir / SECRETS_FILE
        secrets.write_text(f'[qdrant]\napi_key = "{key}"\n')
        secrets.chmod(0o600)


def run_plain(config_dir: Path, tmp_path: Path):
    _, factory = scripted(TextDelta(text="ok"), *reply_events())
    return run_cli(
        ["--plain"],
        config_dir,
        factory=factory,
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def qdrant_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    import llm_harness.config as config_module

    monkeypatch.setattr(config_module, "_qdrant_client_installed", lambda: True)


def test_cli_prints_one_warning_line_for_http_with_a_key(
    config_dir: Path, tmp_path: Path, qdrant_installed: None, no_client: None
) -> None:
    write_qdrant_config(config_dir, HTTP_URL, key=KEY)
    result = run_plain(config_dir, tmp_path)
    assert result.code == 0, result.err
    lines = [line for line in result.err.splitlines() if "unencrypted" in line]
    assert len(lines) == 1
    assert KEY not in result.err and KEY not in result.out


@pytest.mark.parametrize(
    ("url", "key"), [(HTTPS_URL, KEY), (HTTP_URL, None)], ids=["https", "nokey"]
)
def test_cli_prints_no_warning_otherwise(
    config_dir: Path,
    tmp_path: Path,
    qdrant_installed: None,
    no_client: None,
    url: str,
    key: str | None,
) -> None:
    write_qdrant_config(config_dir, url, key=key)
    result = run_plain(config_dir, tmp_path)
    assert result.code == 0, result.err
    assert "unencrypted" not in result.err


# --storage-status


async def test_status_for_qdrant_reports_one_remote_line_and_touches_nothing(
    tmp_path: Path, no_client: None
) -> None:
    data = tmp_path / "data"
    config = HarnessConfig(
        providers=PROVIDERS,
        models=[],
        storage=qdrant_storage(),
        qdrant_api_key=Secret(KEY),
    )
    lines = await storage_status(
        config, relational_path=data / "harness.db", data_dir=data
    )
    vector = [line for line in lines if line.startswith("vector:")]
    assert vector == [
        f"vector: qdrant  {HTTPS_URL}  (remote server, not contacted by --storage-status)"
    ]
    index = lines.index(vector[0])
    assert lines[index + 1].startswith("files:")  # no local vector detail lines
    assert KEY not in "\n".join(lines)
    assert not data.exists()


def test_cli_storage_status_for_qdrant_never_prints_the_key(
    config_dir: Path, tmp_path: Path, qdrant_installed: None, no_client: None
) -> None:
    write_qdrant_config(config_dir, HTTP_URL, key=KEY)
    data = tmp_path / "data"
    result = run_cli(["--storage-status"], config_dir, data_dir=data)
    assert result.code == 0, result.err
    assert f"vector: qdrant  {HTTP_URL}" in result.out
    assert "not contacted" in result.out
    assert KEY not in result.out and KEY not in result.err
    assert result.err == ""
    assert not (data / "vectors").exists()
