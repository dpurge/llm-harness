"""Provider adapters; only adapter modules import a provider SDK."""

import importlib
from collections.abc import Callable

from llm_harness.config import ConfigError, ProviderConfig, resolve_api_key
from llm_harness.providers.base import ChatProvider

ProviderFactory = Callable[[ProviderConfig], ChatProvider]

# Provider type -> dotted module path of its adapter. Imported lazily so a
# provider SDK is only loaded when that provider is used.
_ADAPTER_MODULES: dict[str, str] = {
    "ollama": "llm_harness.providers.ollama",
    "openai": "llm_harness.providers.openai_compat",
    "vertex": "llm_harness.providers.vertex",
}


def build_provider(config: ProviderConfig) -> ChatProvider:
    # Fail fast on a missing key variable, before any adapter lookup.
    key = resolve_api_key(config)
    module_path = _ADAPTER_MODULES.get(config.type)
    if module_path is None:
        raise ConfigError(f"no adapter for provider type {config.type!r} yet")
    module = importlib.import_module(module_path)
    return module.create(config, key)
