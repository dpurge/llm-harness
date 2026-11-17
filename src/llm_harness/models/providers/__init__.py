"""Provider adapters; only adapter modules import a provider SDK."""

import importlib
from collections.abc import Callable

from llm_harness.config.settings import ConfigError, ProviderConfig, resolve_api_key
from llm_harness.models.providers.base import ChatProvider

ProviderFactory = Callable[[ProviderConfig], ChatProvider]

# Provider type -> dotted module path of its adapter. Imported lazily so a
# provider SDK is only loaded when that provider is used.
_ADAPTER_MODULES: dict[str, str] = {
    "ollama": "llm_harness.models.providers.ollama",
    "openai": "llm_harness.models.providers.openai_compat",
    "vertex": "llm_harness.models.providers.vertex",
}


def build_provider(config: ProviderConfig) -> ChatProvider:
    # Fail fast on a missing key variable, before any adapter lookup.
    key = resolve_api_key(config)
    module_path = _ADAPTER_MODULES.get(config.type)
    if module_path is None:
        raise ConfigError(f"no adapter for provider type {config.type!r} yet")
    module = importlib.import_module(module_path)
    return module.create(config, key)
