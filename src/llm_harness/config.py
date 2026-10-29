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
    PositiveInt,
    StrictBool,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from llm_harness.redact import Secret

CONFIG_FILE = "config.toml"
MODELS_FILE = "models.yaml"
AGENTS_FILE = "agents.yaml"

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


EMBEDDING_EXCLUSIVE_TAGS = ("chat", "default", "orchestrator")


class ModelConfig(_Frozen):
    id: NonEmptyStr
    name: NonEmptyStr
    provider: NonEmptyStr
    model: NonEmptyStr
    tags: list[str]
    priority: StrictInt | StrictFloat
    price: Price = Price()
    thinking: StrictBool | None = None
    # Output size of an embedding model; None means the model's own default.
    dimensions: PositiveInt | None = None

    _finite_priority = field_validator("priority")(_require_finite)

    @model_validator(mode="after")
    def _check_embedding_tags(self) -> Self:
        # Vectors and chat replies are different products: one entry is never both.
        if "embedding" in self.tags:
            clashing = [tag for tag in EMBEDDING_EXCLUSIVE_TAGS if tag in self.tags]
            if clashing:
                names = ", ".join(repr(tag) for tag in clashing)
                raise ValueError(
                    f"model {self.id!r} is tagged 'embedding' and cannot also be "
                    f"tagged {names}"
                )
        return self


USER_ID_MAX_LENGTH = 128

ORCHESTRATOR_TAG = "orchestrator"


def _check_user_id(value: object) -> str | None:
    """None stays None; anything else must be a short, printable, non-blank string.

    The string is kept as given: stripping silently would make two computers
    with "ann " and "ann" look like the same user while the files differ.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        # ValueError, not TypeError: pydantic only turns ValueError into an error.
        raise ValueError(  # noqa: TRY004
            f"user_id must be a string, not {type(value).__name__}; "
            'write it as user_id = "your-name"'
        )
    if not value.strip():
        raise ValueError(
            "user_id must not be empty or blank; set a name or remove the key"
        )
    if len(value) > USER_ID_MAX_LENGTH:
        raise ValueError(
            f"user_id must be at most {USER_ID_MAX_LENGTH} characters, "
            f"got {len(value)}; shorten it"
        )
    if not value.isprintable():
        raise ValueError(
            "user_id must not contain control characters or newlines; "
            "use plain printable text"
        )
    return value


def default_data_dir() -> Path:
    # Evaluated per call so a patched HOME is honoured.
    return Path.home() / ".local" / "share" / "llm-harness"


# Backends the program can use now, and backends that are recognised but come
# with a later feature: naming them tells the user the config is not wrong, the
# feature is just not here yet.
_AVAILABLE_BACKENDS = {
    "relational": ("sqlite",),
    "vector": ("qdrant-edge", "none"),
    "files": ("local",),
}
_PLANNED_BACKENDS = {"relational": ("postgres",), "vector": ("qdrant",), "files": ()}


def _check_backend(kind: str, value: str) -> str:
    available = _AVAILABLE_BACKENDS[kind]
    if value in available:
        return value
    valid = ", ".join(available)
    # The message is built here: pydantic's template syntax has no !r.
    if value in _PLANNED_BACKENDS[kind]:
        raise PydanticCustomError(
            "backend_planned",
            f"{value!r} is not available yet (planned feature "
            f"storage-backends-remote); use one of: {valid}",
        )
    raise PydanticCustomError(
        "backend_unknown", f"unknown backend {value!r}; use one of: {valid}"
    )


def _check_storage_path(value: object) -> object:
    """Expand `~`; a relative path would depend on the working directory."""
    if not isinstance(value, str | Path):
        return value  # pydantic reports the wrong type
    if not str(value).strip():
        raise PydanticCustomError(
            "path_empty", "path must not be empty; set an absolute path or remove it"
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PydanticCustomError(
            "path_relative",
            f"path {str(value)!r} must be absolute (or start with ~); "
            "a relative path depends on the working directory",
        )
    return path


class RelationalConfig(_Frozen):
    backend: str = "sqlite"
    path: Path | None = None

    @field_validator("backend")
    @classmethod
    def _backend_rule(cls, value: str) -> str:
        return _check_backend("relational", value)

    _path_rule = field_validator("path", mode="before")(_check_storage_path)

    def resolved_path(self, data_dir: Path) -> Path:
        return self.path or data_dir / "harness.db"


class VectorConfig(_Frozen):
    backend: str = "qdrant-edge"
    path: Path | None = None

    @field_validator("backend")
    @classmethod
    def _backend_rule(cls, value: str) -> str:
        return _check_backend("vector", value)

    _path_rule = field_validator("path", mode="before")(_check_storage_path)

    def resolved_path(self, data_dir: Path) -> Path:
        return self.path or data_dir / "vectors"


class FilesConfig(_Frozen):
    backend: str = "local"
    root: Path | None = None

    @field_validator("backend")
    @classmethod
    def _backend_rule(cls, value: str) -> str:
        return _check_backend("files", value)

    _root_rule = field_validator("root", mode="before")(_check_storage_path)

    def resolved_root(self, data_dir: Path) -> Path:
        return self.root or data_dir / "files"


class StorageConfig(_Frozen):
    relational: RelationalConfig = RelationalConfig()
    vector: VectorConfig = VectorConfig()
    files: FilesConfig = FilesConfig()


def _check_tag_list(values: list[str]) -> list[str]:
    for value in values:
        if not value or value != value.lower() or any(c.isspace() for c in value):
            raise ValueError(
                f"{value!r} must be non-empty, lowercase and contain no spaces"
            )
    duplicates = sorted({v for v in values if values.count(v) > 1})
    if duplicates:
        raise ValueError(f"duplicate entries: {', '.join(duplicates)}")
    return values


class AgentConfig(_Frozen):
    id: NonEmptyStr
    name: NonEmptyStr
    description: NonEmptyStr
    tags: list[str]
    model_tags: list[str]
    examples: list[NonEmptyStr] = []
    tools: list[NonEmptyStr] = []
    deny: list[NonEmptyStr] = []

    _tag_rules = field_validator("tags", "model_tags")(_check_tag_list)

    @model_validator(mode="after")
    def _check_tools_and_deny(self) -> Self:
        both = sorted(set(self.tools) & set(self.deny))
        if both:
            raise ValueError(
                f"agent {self.id!r} lists {', '.join(both)} in both tools and deny"
            )
        return self


def _check_agents(agents: list[AgentConfig], models: list[ModelConfig]) -> None:
    ids = [agent.id for agent in agents]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate agent ids: {', '.join(duplicates)}")
    chat_tag_sets = [set(m.tags) for m in models if "embedding" not in m.tags]
    for agent in agents:
        wanted = set(agent.model_tags)
        if not any(wanted <= tags for tags in chat_tag_sets):
            raise ValueError(
                f"agent {agent.id!r}: no chat model carries all of model_tags "
                f"{agent.model_tags}"
            )
    if agents:
        orchestrators = [a.id for a in agents if ORCHESTRATOR_TAG in a.tags]
        if len(orchestrators) != 1:
            found = ", ".join(orchestrators) or "none"
            raise ValueError(
                f"exactly one agent must carry the tag {ORCHESTRATOR_TAG!r}; "
                f"found: {found}"
            )


class HarnessConfig(_Frozen):
    providers: dict[str, ProviderConfig]
    models: list[ModelConfig]
    # The same on every computer, so the orchestrator can tell whose state it is.
    user_id: str | None = None
    storage: StorageConfig = StorageConfig()
    agents: list[AgentConfig] = []

    _user_id_rule = field_validator("user_id", mode="before")(_check_user_id)

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
        _check_agents(self.agents, self.models)
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


def _load_storage(raw: object) -> StorageConfig:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"invalid {CONFIG_FILE}: storage must be a table, not "
            f"{type(raw).__name__}; write it as [storage.vector] and so on"
        )
    try:
        # Wrapped so error locations start with "storage".
        return TypeAdapter(dict[str, StorageConfig]).validate_python({"storage": raw})[
            "storage"
        ]
    except ValidationError as exc:
        raise _format_validation_error(CONFIG_FILE, exc) from None


def load_config(config_dir: Path) -> HarnessConfig:
    config_path = config_dir / CONFIG_FILE
    models_path = config_dir / MODELS_FILE
    agents_path = config_dir / AGENTS_FILE
    missing = [
        path for path in (config_path, models_path, agents_path) if not path.is_file()
    ]
    if missing:
        raise MissingConfigError(missing)

    toml_data = _read_toml(config_path)
    unknown = sorted(set(toml_data) - {"providers", "user_id", "storage"})
    if unknown:
        raise ConfigError(
            f"invalid {CONFIG_FILE}: unknown top-level keys: {', '.join(unknown)}"
        )
    for provider_table in toml_data.get("providers", {}).values():
        if isinstance(provider_table, dict) and "user_id" in provider_table:
            raise ConfigError(
                f"invalid {CONFIG_FILE}: user_id found inside [providers.<id>]; "
                "put user_id at the top of the file, before any [providers...] header"
            )
    try:
        providers = TypeAdapter(dict[str, ProviderConfig]).validate_python(
            toml_data.get("providers", {})
        )
    except ValidationError as exc:
        raise _format_validation_error(CONFIG_FILE, exc) from None
    try:
        user_id = _check_user_id(toml_data.get("user_id"))
    except ValueError as exc:
        raise ConfigError(f"invalid {CONFIG_FILE}: {exc}") from None
    storage = _load_storage(toml_data.get("storage", {}))

    models = _load_models(models_path)
    agents = _load_agents(agents_path, models)
    try:
        return HarnessConfig(
            providers=providers,
            models=models,
            user_id=user_id,
            storage=storage,
            agents=agents,
        )
    except ValidationError as exc:
        raise _format_validation_error(MODELS_FILE, exc) from None


def _load_models(path: Path) -> list[ModelConfig]:
    yaml_data = _read_yaml(path)
    if not isinstance(yaml_data, dict) or set(yaml_data) != {"models"}:
        raise ConfigError(
            f"invalid {MODELS_FILE}: top level must be a mapping with only 'models'"
        )
    try:
        return TypeAdapter(list[ModelConfig]).validate_python(yaml_data["models"])
    except ValidationError as exc:
        raise _format_validation_error(MODELS_FILE, exc) from None


def _load_agents(path: Path, models: list[ModelConfig]) -> list[AgentConfig]:
    yaml_data = _read_yaml(path)
    if not isinstance(yaml_data, dict) or set(yaml_data) != {"agents"}:
        raise ConfigError(
            f"invalid {AGENTS_FILE}: top level must be a mapping with only 'agents'"
        )
    try:
        agents = TypeAdapter(list[AgentConfig]).validate_python(yaml_data["agents"])
    except ValidationError as exc:
        raise _format_validation_error(AGENTS_FILE, exc) from None
    if not agents:
        raise ConfigError(
            f"invalid {AGENTS_FILE}: declare at least one agent, "
            f"tagged {ORCHESTRATOR_TAG!r}"
        )
    try:
        _check_agents(agents, models)
    except ValueError as exc:
        raise ConfigError(f"invalid {AGENTS_FILE}: {exc}") from None
    return agents


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


def find_orchestrator_agent(config: HarnessConfig) -> AgentConfig | None:
    """The orchestrator agent, or None for a config built without agents (tests)."""
    return next((a for a in config.agents if ORCHESTRATOR_TAG in a.tags), None)


def _lookup_model(config: HarnessConfig, model_id: str) -> ModelConfig:
    for model in config.models:
        if model.id == model_id:
            return model
    valid = ", ".join(model.id for model in config.models)
    raise ConfigError(f"unknown model {model_id!r}; valid ids: {valid}")


def _lookup_chat_model(config: HarnessConfig, model_id: str) -> ModelConfig:
    """Like `_lookup_model`, but refuses an embedding model: it cannot chat."""
    model = _lookup_model(config, model_id)
    if "embedding" in model.tags:
        raise ConfigError(
            f"model {model_id!r} is tagged 'embedding' and cannot be used for chat"
        )
    return model


def _top_priority(candidates: list[ModelConfig], label: str) -> ModelConfig:
    """The single highest-priority model; a tie at the top is an error."""
    top = max(model.priority for model in candidates)
    best = [model for model in candidates if model.priority == top]
    if len(best) > 1:
        ids = ", ".join(model.id for model in best)
        raise ConfigError(f"several {label} models share the top priority {top}: {ids}")
    return best[0]


def select_model(config: HarnessConfig, model_id: str | None) -> ModelConfig:
    if model_id is not None:
        return _lookup_chat_model(config, model_id)

    defaults = [model for model in config.models if "default" in model.tags]
    if not defaults:
        raise ConfigError(
            "no model is tagged 'default'; use --model or tag one in models.yaml"
        )
    return _top_priority(defaults, "default")


def select_orchestrator_model(
    config: HarnessConfig, model_id: str | None
) -> ModelConfig:
    """The model that drives the orchestrator; never falls back to `default`."""
    if model_id is not None:
        return _lookup_chat_model(config, model_id)

    candidates = [model for model in config.models if "orchestrator" in model.tags]
    if not candidates:
        raise ConfigError(
            "no model is tagged 'orchestrator'; tag one in models.yaml or use --model"
        )
    return _top_priority(candidates, "orchestrator")


def select_agent_model(config: HarnessConfig, agent: AgentConfig) -> ModelConfig:
    """The chat model for a subagent: top priority among those carrying all its model_tags."""
    wanted = set(agent.model_tags)
    candidates = [
        model
        for model in config.models
        if "embedding" not in model.tags and wanted <= set(model.tags)
    ]
    if not candidates:
        raise ConfigError(
            f"no chat model carries all model_tags {agent.model_tags} "
            f"of agent {agent.id!r}; tag one in models.yaml"
        )
    return _top_priority(candidates, f"agent {agent.id!r}")


def select_embedding_model(config: HarnessConfig, model_id: str | None) -> ModelConfig:
    """The model that produces embeddings; never falls back to a chat model."""
    if model_id is not None:
        model = _lookup_model(config, model_id)
        if "embedding" not in model.tags:
            raise ConfigError(f"model {model_id!r} is not tagged 'embedding'")
        return model

    candidates = [model for model in config.models if "embedding" in model.tags]
    if not candidates:
        raise ConfigError(
            "no model has the tag 'embedding'; tag one in models.yaml or pass its id"
        )
    return _top_priority(candidates, "embedding")


def chat_models(config: HarnessConfig) -> list[ModelConfig]:
    """The models a person can chat with: everything not tagged 'embedding'."""
    return [model for model in config.models if "embedding" not in model.tags]


EXAMPLE_CONFIG_TOML = """\
# llm-harness connection settings: where each provider lives and how to sign in.
# Keep this file private: chmod 600. The list of models is in models.yaml.
# Lines that start with "# " are examples; remove the "# " to switch one on.

# Your name. Use the same user_id on every computer so your state follows you.
# Optional. It must stay above the first [table] header, because a key written
# after a header belongs to that table.
# user_id = "your-name"

# One [providers.<id>] table per provider. The <id> (here "local") is the name
# that "provider:" in models.yaml refers to. `type` is "ollama", "openai" or
# "vertex"; the other keys depend on the type.

# Ollama on this computer. It needs `base_url` and takes no key.
[providers.local]
type = "ollama"
base_url = "http://localhost:11434"

# An OpenAI-compatible service (OpenAI, OpenRouter, ...). It needs `base_url`
# and one key: either the name of an environment variable (api_key_env, the
# safer way) or the key itself (api_key). Never both. `reasoning_style` is
# optional: it says how the `thinking` setting of a model is sent.
# [providers.openai]
# type = "openai"
# base_url = "https://api.openai.com/v1"
# api_key_env = "OPENAI_API_KEY"   # name of an environment variable, or:
# api_key = "sk-..."               # an inline key (not both)
# reasoning_style = "openai"       # or "openrouter"; how `thinking` is sent

# Google Vertex AI. It needs `project` and `location` and takes no key, no
# base_url: it signs in with Application Default Credentials (run
# `gcloud auth application-default login` once).
# [providers.vertex]
# type = "vertex"
# project = "my-gcp-project"
# location = "us-central1"

# Where data lives; every key is optional and the values below are what you
# get when you write nothing. Paths must be absolute (or start with ~).
# Write these tables after user_id.
# Relational data (usage ledger): "sqlite" is the only backend available now.
# [storage.relational]
# backend = "sqlite"
# path = "/absolute/path/harness.db"   # default: ~/.local/share/llm-harness/harness.db

# Vector search: "qdrant-edge" (local), or "none" to turn vector search off.
# [storage.vector]
# backend = "qdrant-edge"              # or "none" to turn vector search off
# path = "/absolute/path"              # default: ~/.local/share/llm-harness/vectors

# Files: "local" is the only backend available now.
# [storage.files]
# backend = "local"
# root = "/absolute/path"              # default: ~/.local/share/llm-harness/files
"""

EXAMPLE_MODELS_YAML = """\
# llm-harness model registry: the list of models you can use. No secrets here
# (keys go in config.toml). Lines that start with "# " are notes or examples.
#
# Every entry has these keys:
#   id        short unique name; this is what --model and the selectors use.
#   name      the label people see in menus.
#   provider  the id of a [providers.<id>] table in config.toml.
#   model     the name the provider knows the model by (for Ollama, what
#             `ollama list` shows).
#   tags      a list of labels:
#               orchestrator  the model the program starts with: the one with
#                             the highest priority. When a model fails, the
#                             other orchestrator models are tried next, by
#                             priority.
#               default       a last resort: after the orchestrator models,
#                             the models tagged default are tried, by
#                             priority.
#               chat          marks a model as a chat model; starting and
#                             falling back use orchestrator and default.
#               embedding     reserved, see the embedding example below.
#             Any other tag (like "cheap") is a free label you can use to
#             group models; it changes nothing.
#   priority  a number; higher wins. Two orchestrator models at the same top
#             priority are an error, and so are two embedding models, so give
#             each a different number.
#   thinking  optional. true or false asks the model to think (or not) before
#             it answers; leave it out to use the model's own default.
#   price     optional. Cost per million tokens, used for the usage
#             numbers: input_per_mtok and output_per_mtok. Both default
#             to 0, which is right for local models.
models:
  - id: gemma-local
    name: Gemma 4 12B (local)
    provider: local
    model: gemma4:12b
    tags: [default, chat, orchestrator, cheap]
    priority: 100
    # thinking: false   # optional; omitted = the model's own default
    price:
      input_per_mtok: 0
      output_per_mtok: 0

  # An embedding model turns text into numbers (vectors) for search. It is not
  # a chat model:
  #   - Tag it [embedding]. Never combine that tag with chat, default or
  #     orchestrator: the program refuses to start.
  #   - It never answers chat, and it is hidden from the chat selectors and
  #     from --model.
  #   - There is no fallback between embedding models: the one with the highest
  #     priority is used.
  #   - `dimensions` is optional, a positive whole number. It is sent to the
  #     provider only when you set it; leave it out to keep the model's own
  #     size. Check that your model supports the value you ask for.
  # To use it, run `ollama pull bge-m3`, then remove the "# " in front of the
  # lines between the two marker lines (the markers stay commented).
  # --- embedding example: begin ---
  #   - id: bge-m3-local
  #     name: BGE-M3 embeddings (local)
  #     provider: local
  #     model: bge-m3
  #     tags: [embedding]
  #     priority: 1
  #     # dimensions: 256   # optional; omitted = the model's own size
  #     price:
  #       input_per_mtok: 0
  #       output_per_mtok: 0
  # --- embedding example: end ---
"""

EXAMPLE_AGENTS_YAML = """\
# llm-harness agents: who can do work. No secrets here.
# Lines that start with "# " are notes.
#
# Every entry has these keys:
#   id           short unique name.
#   name         the label people see.
#   description  what the agent is for, in one sentence.
#   tags         a list of lowercase labels without spaces. Exactly one agent
#                must carry the tag orchestrator: it reads your prompt and
#                assembles the reply.
#   model_tags   the tags a model in models.yaml must carry (all of them)
#                for this agent. At least one chat model must match.
#   examples     optional. Example requests the agent handles.
#   tools        optional. Names of tools the agent may use.
#   deny         optional. Names of tools the agent may never use.
agents:
  - id: orchestrator
    name: Orchestrator
    description: Reads the user's prompt, keeps the goal, and assembles the reply.
    tags: [orchestrator]
    model_tags: [orchestrator]

  - id: researcher
    name: Researcher
    description: Looks up facts and reports what it found with its sources.
    tags: [research]
    model_tags: [chat]
    tools: [tool_search]
    examples:
      - Find out when the Eiffel Tower was built.
      - Look up the latest stable version of Python.
"""

_EXAMPLES = {
    CONFIG_FILE: (EXAMPLE_CONFIG_TOML, 0o600),
    MODELS_FILE: (EXAMPLE_MODELS_YAML, 0o644),
    AGENTS_FILE: (EXAMPLE_AGENTS_YAML, 0o644),
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
