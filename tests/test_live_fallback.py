"""Live fallback check: a dead model, then the local Ollama model.

Deselected by default, run with `pytest -m live -k fallback`. It makes two tiny
Ollama calls (output capped at 32 tokens). It needs a real config with a local
Ollama model; without one it skips.
"""

import io
import json
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import pytest

from llm_harness.cli import main
from llm_harness.config import (
    AGENTS_FILE,
    CONFIG_FILE,
    MODELS_FILE,
    ConfigError,
    HarnessConfig,
    ModelConfig,
    ProviderConfig,
    default_config_dir,
    load_config,
)
from llm_harness.providers import build_provider
from llm_harness.providers.base import ChatProvider
from llm_harness.providers.ollama import OllamaProvider
from tests.test_live_ollama import CappedOutput

pytestmark = pytest.mark.live

_LOCAL_HOSTS = {None, "localhost", "127.0.0.1", "::1"}
_FIRST_CALL_SECONDS = 90
SWITCH_NOTE = "dead-model unavailable (connection); using "
BACKOFF_NOTE = "dead-model is in backoff; using "


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _local_ollama_model(config: HarnessConfig) -> ModelConfig | None:
    for model in config.models:
        provider = config.providers[model.provider]
        if provider.type == "ollama" and (
            urlparse(provider.base_url or "").hostname in _LOCAL_HOSTS
        ):
            return model
    return None


def _write_config(directory: Path, local: ModelConfig, local_url: str | None) -> None:
    # JSON strings are valid YAML and TOML basic strings for these plain values.
    dead_url = f"http://127.0.0.1:{_closed_port()}"
    toml = f'[providers.dead]\ntype = "ollama"\nbase_url = {json.dumps(dead_url)}\n'
    toml += f'\n[providers.{local.provider}]\ntype = "ollama"\n'
    if local_url is not None:
        toml += f"base_url = {json.dumps(local_url)}\n"
    models = {
        "models": [
            {
                "id": "dead-model",
                "name": "Dead",
                "provider": "dead",
                "model": "dead-id",
                "tags": ["default", "chat"],
                "priority": 200,
            },
            {
                "id": local.id,
                "name": local.name,
                "provider": local.provider,
                "model": local.model,
                "tags": ["chat", "default"],
                "priority": 100,
            },
        ]
    }
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    (directory / CONFIG_FILE).write_text(toml)
    (directory / CONFIG_FILE).chmod(0o600)
    (directory / MODELS_FILE).write_text(json.dumps(models))
    agents = {
        "agents": [
            {
                "id": "orchestrator",
                "name": "Orchestrator",
                "description": "Assembles the reply.",
                "tags": ["orchestrator"],
                "model_tags": [],
            }
        ]
    }
    (directory / AGENTS_FILE).write_text(json.dumps(agents))


def _capped_provider(config: ProviderConfig) -> ChatProvider:
    provider = build_provider(config)
    if isinstance(provider, OllamaProvider):
        provider._client = CappedOutput(provider._client)  # type: ignore[assignment]
    return provider


def _run(config_dir: Path, data_dir: Path) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    # main is synchronous (it owns the event loop), so the bound is a thread timeout.
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        main,
        ["--plain", "--no-think"],
        config_dir=config_dir,
        data_dir=data_dir,
        provider_factory=_capped_provider,
        stdin=io.StringIO("Reply with exactly one word: pong\n"),
        stdout=stdout,
        stderr=stderr,
    )
    try:
        code = future.result(timeout=_FIRST_CALL_SECONDS + 10)
    finally:
        pool.shutdown(wait=False)
    return code, stdout.getvalue(), stderr.getvalue()


def _rows(data_dir: Path, query: str) -> list[tuple]:
    with closing(sqlite3.connect(data_dir / "harness.db")) as conn:
        return conn.execute(query).fetchall()


def test_dead_model_falls_back_to_local_ollama_then_stays_in_backoff(
    tmp_path: Path,
) -> None:
    real_dir = default_config_dir()
    if not (real_dir / CONFIG_FILE).exists() or not (real_dir / MODELS_FILE).exists():
        pytest.skip(f"no {CONFIG_FILE} / {MODELS_FILE} in {real_dir}")
    try:
        real = load_config(real_dir)
    except ConfigError as exc:
        pytest.skip(f"real config is not usable: {exc}")
    local = _local_ollama_model(real)
    if local is None:
        pytest.skip("the real config has no model on a local ollama provider")

    config_dir, data_dir = tmp_path / "cfg", tmp_path / "data"
    _write_config(config_dir, local, real.providers[local.provider].base_url)

    code, out, err = _run(config_dir, data_dir)
    assert code == 0, err
    assert out.strip() != ""
    assert f"{SWITCH_NOTE}{local.id}" in err
    assert f"[{local.id} tokens " in err
    assert _rows(
        data_dir,
        "SELECT model_id, outcome, error_kind, fallback_from FROM llm_calls ORDER BY id",
    ) == [
        ("dead-model", "error", "connection", None),
        (local.id, "ok", None, "dead-model"),
    ]
    assert _rows(data_dir, "SELECT model_id, failures FROM model_backoff") == [
        ("dead-model", 1)
    ]

    code, out, err = _run(config_dir, data_dir)
    assert code == 0, err
    assert out.strip() != ""
    assert f"{BACKOFF_NOTE}{local.id}" in err
    assert _rows(
        data_dir,
        "SELECT count(*) FROM llm_calls WHERE model_id = 'dead-model'",
    ) == [(1,)]
