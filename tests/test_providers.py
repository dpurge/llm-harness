import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from llm_harness import providers
from llm_harness.config import ConfigError, ProviderConfig
from llm_harness.providers import build_provider


def provider(**fields: Any) -> ProviderConfig:
    return TypeAdapter(ProviderConfig).validate_python(fields)


OLLAMA = {"type": "ollama", "base_url": "http://localhost:11434"}


def test_unregistered_adapter_type_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "_ADAPTER_MODULES", {})
    with pytest.raises(ConfigError, match="no adapter for provider type 'ollama' yet"):
        build_provider(provider(**OLLAMA))


def test_ollama_type_builds_an_ollama_provider() -> None:
    built = build_provider(provider(**OLLAMA))
    assert type(built).__name__ == "OllamaProvider"


def test_missing_env_variable_fails_before_adapter_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HARNESS_TEST_KEY", raising=False)
    config = provider(
        type="openai", base_url="https://api.example/v1", api_key_env="HARNESS_TEST_KEY"
    )
    with pytest.raises(ConfigError, match="HARNESS_TEST_KEY") as info:
        build_provider(config)
    assert "no adapter" not in str(info.value)


def test_adapter_module_is_imported_lazily(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    name = "harness_test_adapter"
    (tmp_path / f"{name}.py").write_text(
        "def create(config, api_key):\n    return ('built', config.type, api_key)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(providers._ADAPTER_MODULES, "ollama", name)

    assert name not in sys.modules  # registering alone imports nothing
    assert build_provider(provider(**OLLAMA)) == ("built", "ollama", None)
    assert name in sys.modules
    del sys.modules[name]
