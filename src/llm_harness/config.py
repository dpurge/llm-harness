"""Configuration loader: providers and secrets (config.toml), models (models.yaml)."""

import math
import os
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from llm_harness.redact import Secret

CONFIG_FILE = "config.toml"
MODELS_FILE = "models.yaml"

NonEmptyStr = Annotated[str, Field(min_length=1)]


class ConfigError(Exception):
    """A configuration problem; the message never contains a secret value."""


class MissingConfigError(ConfigError):
    def __init__(self, missing: list[Path]) -> None:
        super().__init__(
            "missing configuration file(s): " + ", ".join(str(p) for p in missing)
        )
        self.missing = missing


def _is_plain_http_url(value: str) -> bool:
    """True for an http(s) URL with a host and no userinfo; never raises."""
    try:
        parts = urlsplit(value)
        has_host = bool(parts.hostname)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and has_host and "@" not in parts.netloc


def _require_finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)


class ProviderConfig(_Frozen):
    type: Literal["ollama", "openai", "vertex"]
    base_url: str | None = None
    api_key: Secret | None = None
    api_key_env: str | None = None
    project: str | None = None
    location: str | None = None
    # None means the adapter default, "openai".
    reasoning_style: Literal["openai", "openrouter"] | None = None

    @field_validator("api_key", mode="before")
    @classmethod
    def _wrap_api_key(cls, value: object) -> object:
        if isinstance(value, str):
            if not value:
                raise ValueError("api_key must not be empty")
            return Secret(value)
        return value

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        # The message must not echo base_url: it may carry a secret.
        if (
            self.type in ("ollama", "openai")
            and self.base_url is not None
            and not _is_plain_http_url(self.base_url)
        ):
            raise ValueError("base_url must be an http(s) URL without credentials")
        has_key = self.api_key is not None
        has_env = self.api_key_env is not None
        if self.reasoning_style is not None and self.type != "openai":
            raise ValueError("reasoning_style is only valid on openai providers")
        if has_key and has_env:
            raise ValueError("api_key and api_key_env must not both be set")
        if self.type == "ollama":
            if self.base_url is None:
                raise ValueError("ollama provider requires base_url")
            if has_key or has_env:
                raise ValueError(
                    "ollama provider does not accept api_key or api_key_env"
                )
        elif self.type == "openai":
            if self.base_url is None:
                raise ValueError("openai provider requires base_url")
            if not (has_key or has_env):
                raise ValueError("openai provider requires api_key or api_key_env")
        else:
            if self.project is None or self.location is None:
                raise ValueError("vertex provider requires project and location")
            # Assumption beyond the spec: Vertex uses Application Default Credentials.
            if self.base_url is not None or has_key or has_env:
                raise ValueError(
                    "vertex provider does not accept base_url, api_key or api_key_env"
                )
        return self


class Price(_Frozen):
    input_per_mtok: float = Field(default=0, ge=0)
    output_per_mtok: float = Field(default=0, ge=0)

    _finite = field_validator("input_per_mtok", "output_per_mtok")(_require_finite)


class ModelConfig(_Frozen):
    id: NonEmptyStr
    name: NonEmptyStr
    provider: NonEmptyStr
    model: NonEmptyStr
    tags: list[str]
    priority: StrictInt | StrictFloat
    price: Price = Price()
    thinking: StrictBool | None = None

    _finite_priority = field_validator("priority")(_require_finite)


class HarnessConfig(_Frozen):
    providers: dict[str, ProviderConfig]
    models: list[ModelConfig]

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        ids = [model.id for model in self.models]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate model ids: {', '.join(duplicates)}")
        for model in self.models:
            if model.provider not in self.providers:
                raise ValueError(
                    f"model {model.id!r} uses unknown provider {model.provider!r}"
                )
        return self


def default_config_dir() -> Path:
    return Path.home() / ".config" / "llm-harness"


def _format_validation_error(file_name: str, exc: ValidationError) -> ConfigError:
    # Only loc and msg: pydantic's str() echoes input_value, which could be an api_key.
    parts = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(item) for item in error["loc"]) or "(top level)"
        parts.append(f"{location}: {error['msg']}")
    return ConfigError(f"invalid {file_name}: " + "; ".join(parts))


def _unreadable(path: Path, exc: Exception) -> ConfigError:
    # Type name only: the exception text can contain file content.
    return ConfigError(f"cannot read {path}: {type(exc).__name__}")


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise _unreadable(path, exc) from None


def _read_yaml(path: Path) -> object:
    try:
        with path.open("rb") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ConfigError(f"cannot parse {path}{where}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise _unreadable(path, exc) from None


def load_config(config_dir: Path) -> HarnessConfig:
    config_path = config_dir / CONFIG_FILE
    models_path = config_dir / MODELS_FILE
    missing = [path for path in (config_path, models_path) if not path.is_file()]
    if missing:
        raise MissingConfigError(missing)

    toml_data = _read_toml(config_path)
    unknown = sorted(set(toml_data) - {"providers"})
    if unknown:
        raise ConfigError(
            f"invalid {CONFIG_FILE}: unknown top-level keys: {', '.join(unknown)}"
        )
    try:
        providers = TypeAdapter(dict[str, ProviderConfig]).validate_python(
            toml_data.get("providers", {})
        )
    except ValidationError as exc:
        raise _format_validation_error(CONFIG_FILE, exc) from None

    yaml_data = _read_yaml(models_path)
    if not isinstance(yaml_data, dict) or set(yaml_data) != {"models"}:
        raise ConfigError(
            f"invalid {MODELS_FILE}: top level must be a mapping with only 'models'"
        )
    try:
        models = TypeAdapter(list[ModelConfig]).validate_python(yaml_data["models"])
        return HarnessConfig(providers=providers, models=models)
    except ValidationError as exc:
        raise _format_validation_error(MODELS_FILE, exc) from None


def resolve_api_key(provider: ProviderConfig) -> Secret | None:
    if provider.api_key is not None:
        return provider.api_key
    if provider.api_key_env is None:
        return None
    value = os.environ.get(provider.api_key_env)
    if not value:
        raise ConfigError(
            f"environment variable {provider.api_key_env} is not set or empty"
        )
    return Secret(value)


def permission_warnings(config_dir: Path) -> list[str]:
    """One line per permission problem that could expose or redirect the API key."""
    warnings = []
    config_path = config_dir / CONFIG_FILE
    try:
        file_mode = config_path.stat().st_mode
    except FileNotFoundError:
        file_mode = 0
    if file_mode & 0o044:
        warnings.append(
            f"warning: {config_path} is readable by other users; "
            f"run: chmod 600 {config_path}"
        )
    # Another user could change base_url and capture the key.
    if file_mode & 0o022:
        warnings.append(
            f"warning: {config_path} is writable by other users; "
            f"run: chmod 600 {config_path}"
        )
    try:
        dir_mode = config_dir.stat().st_mode
    except FileNotFoundError:
        dir_mode = 0
    # Another user could replace config.toml.
    if dir_mode & 0o022:
        warnings.append(
            f"warning: {config_dir} is writable by other users; "
            f"run: chmod 700 {config_dir}"
        )
    return warnings


def resolve_think(flag: bool | None, model: ModelConfig) -> bool | None:
    """CLI flag > model `thinking` > None (the model's own default)."""
    return flag if flag is not None else model.thinking


def select_model(config: HarnessConfig, model_id: str | None) -> ModelConfig:
    if model_id is not None:
        for model in config.models:
            if model.id == model_id:
                return model
        valid = ", ".join(model.id for model in config.models)
        raise ConfigError(f"unknown model {model_id!r}; valid ids: {valid}")

    defaults = [model for model in config.models if "default" in model.tags]
    if not defaults:
        raise ConfigError(
            "no model is tagged 'default'; use --model or tag one in models.yaml"
        )
    top = max(model.priority for model in defaults)
    best = [model for model in defaults if model.priority == top]
    if len(best) > 1:
        ids = ", ".join(model.id for model in best)
        raise ConfigError(f"several default models share the top priority {top}: {ids}")
    return best[0]


EXAMPLE_CONFIG_TOML = """\
# llm-harness connection settings. Keep this file private: chmod 600.

[providers.local]
type = "ollama"
base_url = "http://localhost:11434"

# [providers.openai]
# type = "openai"
# base_url = "https://api.openai.com/v1"
# api_key_env = "OPENAI_API_KEY"   # name of an environment variable, or:
# api_key = "sk-..."               # an inline key (not both)
# reasoning_style = "openai"       # or "openrouter"; how `thinking` is sent

# [providers.vertex]
# type = "vertex"
# project = "my-gcp-project"
# location = "us-central1"
"""

EXAMPLE_MODELS_YAML = """\
# llm-harness model registry (no secrets here). Higher priority wins.
models:
  - id: gemma-local
    name: Gemma 4 12B (local)
    provider: local
    model: gemma4:12b
    tags: [default, chat, cheap]
    priority: 100
    # thinking: false   # optional; omitted = the model's own default
    price:
      input_per_mtok: 0
      output_per_mtok: 0
"""

_EXAMPLES = {
    CONFIG_FILE: (EXAMPLE_CONFIG_TOML, 0o600),
    MODELS_FILE: (EXAMPLE_MODELS_YAML, 0o644),
}


def write_examples(config_dir: Path, missing: list[Path]) -> list[Path]:
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    written = []
    for path in missing:
        content, mode = _EXAMPLES[path.name]
        # O_EXCL: never overwrite an existing file. The mode is applied at
        # creation so the file is never briefly more open than intended.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        written.append(path)
    return written
