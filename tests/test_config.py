import os
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from llm_harness.config import (
    EXAMPLE_CONFIG_TOML,
    EXAMPLE_MODELS_YAML,
    ConfigError,
    FilesConfig,
    HarnessConfig,
    MissingConfigError,
    ModelConfig,
    Price,
    ProviderConfig,
    RelationalConfig,
    StorageConfig,
    VectorConfig,
    chat_models,
    default_config_dir,
    default_data_dir,
    load_config,
    permission_warnings,
    resolve_api_key,
    resolve_think,
    select_embedding_model,
    select_model,
    select_orchestrator_model,
    write_examples,
)
from llm_harness.redact import REDACTED, Secret
from llm_harness.store import default_db_path

CANARY = "sk-CANARY-123456789"

OLLAMA = {"type": "ollama", "base_url": "http://localhost:11434"}
OPENAI = {"type": "openai", "base_url": "https://api.example/v1"}
VERTEX = {"type": "vertex", "project": "p", "location": "l"}


def provider(**fields: Any) -> ProviderConfig:
    return TypeAdapter(ProviderConfig).validate_python(fields)


def model(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "m1",
        "name": "M1",
        "provider": "local",
        "model": "x",
        "tags": ["default"],
        "priority": 1,
    }
    return base | overrides


def config_of(*models: dict[str, Any]) -> HarnessConfig:
    return HarnessConfig.model_validate(
        {"providers": {"local": OLLAMA}, "models": list(models)}
    )


def write_config(
    tmp_path: Path,
    toml: str = EXAMPLE_CONFIG_TOML,
    models_yaml: str = EXAMPLE_MODELS_YAML,
) -> None:
    (tmp_path / "config.toml").write_text(toml)
    (tmp_path / "models.yaml").write_text(models_yaml)


def models_yaml_with(body: str) -> str:
    return (
        "models:\n  - id: m1\n    name: M1\n    provider: local\n    model: x\n" + body
    )


def load_error(tmp_path: Path) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    return info.value


def test_default_config_dir_value():
    assert default_config_dir() == Path.home() / ".config" / "llm-harness"


# --- providers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        OLLAMA,
        OPENAI | {"api_key": "k"},
        OPENAI | {"api_key_env": "SOME_VAR"},
        VERTEX,
    ],
)
def test_valid_providers(fields: dict[str, Any]):
    assert provider(**fields).type == fields["type"]


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "other", "base_url": "u"},
        OPENAI | {"api_key": "k", "api_key_env": "V"},
        {"type": "ollama"},
        OLLAMA | {"api_key": "k"},
        OLLAMA | {"api_key_env": "V"},
        OPENAI,
        {"type": "openai", "api_key": "k"},
        OPENAI | {"api_key": ""},
        {"type": "vertex", "project": "p"},
        {"type": "vertex", "location": "l"},
        VERTEX | {"base_url": "u"},
        VERTEX | {"api_key": "k"},
        VERTEX | {"api_key_env": "V"},
        OLLAMA | {"unknown_key": 1},
    ],
)
def test_invalid_providers(fields: dict[str, Any]):
    with pytest.raises(ValueError):
        provider(**fields)


def test_inline_key_is_wrapped_and_resolved():
    cfg = provider(**OPENAI | {"api_key": CANARY})
    assert isinstance(cfg.api_key, Secret)
    resolved = resolve_api_key(cfg)
    assert resolved is not None and resolved.reveal() == CANARY


def test_env_key_is_resolved(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HARNESS_TEST_KEY", CANARY)
    resolved = resolve_api_key(provider(**OPENAI | {"api_key_env": "HARNESS_TEST_KEY"}))
    assert resolved is not None and resolved.reveal() == CANARY


@pytest.mark.parametrize("value", [None, ""])
def test_missing_or_empty_env_var_names_variable(
    monkeypatch: pytest.MonkeyPatch, value: str | None
):
    if value is None:
        monkeypatch.delenv("HARNESS_TEST_KEY", raising=False)
    else:
        monkeypatch.setenv("HARNESS_TEST_KEY", value)
    with pytest.raises(ConfigError, match="HARNESS_TEST_KEY"):
        resolve_api_key(provider(**OPENAI | {"api_key_env": "HARNESS_TEST_KEY"}))


def test_no_key_resolves_to_none():
    assert resolve_api_key(provider(**OLLAMA)) is None


def test_reprs_never_contain_canary(tmp_path: Path):
    cfg = provider(**OPENAI | {"api_key": CANARY})
    harness = HarnessConfig(providers={"p": cfg}, models=[])
    for text in (repr(cfg), str(cfg), repr(harness), str(harness)):
        assert CANARY not in text
    assert REDACTED in repr(cfg)


def test_config_error_for_bad_provider_never_contains_canary(tmp_path: Path):
    # Inline key on a provider that rejects keys, plus a wrong-typed key.
    toml = f'[providers.a]\ntype = "ollama"\nbase_url = "u"\napi_key = "{CANARY}"\n'
    toml += f'[providers.b]\ntype = "openai"\nbase_url = "u"\napi_key = 5\nextra = "{CANARY}"\n'
    write_config(tmp_path, toml)
    error = load_error(tmp_path)
    assert CANARY not in str(error) and CANARY not in repr(error)
    assert "config.toml" in str(error)


# --- models.yaml -------------------------------------------------------------


def test_models_accept_int_and_float_priority():
    cfg = config_of(model(priority=3), model(id="m2", priority=2.5))
    assert [m.priority for m in cfg.models] == [3, 2.5]


@pytest.mark.parametrize("priority", [True, "5", None])
def test_models_reject_bad_priority(priority: object):
    with pytest.raises(ValueError):
        ModelConfig.model_validate(model(priority=priority))


@pytest.mark.parametrize(
    "overrides",
    [
        {"tags": "default"},
        {"tags": [1]},
        {"price": {"input_per_mtok": -1}},
        {"price": {"output_per_mtok": -0.1}},
        {"extra": 1},
        {"id": ""},
        {"name": ""},
    ],
)
def test_models_reject_invalid_fields(overrides: dict[str, Any]):
    with pytest.raises(ValueError):
        ModelConfig.model_validate(model(**overrides))


def test_thinking_defaults_to_none():
    assert ModelConfig.model_validate(model()).thinking is None


@pytest.mark.parametrize("thinking", [True, False])
def test_thinking_accepts_booleans(thinking: bool):
    assert ModelConfig.model_validate(model(thinking=thinking)).thinking is thinking


@pytest.mark.parametrize("thinking", ["false", "yes", 1, 0, []])
def test_thinking_rejects_non_booleans(thinking: object):
    with pytest.raises(ValueError):
        ModelConfig.model_validate(model(thinking=thinking))


def test_bad_thinking_in_file_names_the_field_and_not_the_value(tmp_path: Path):
    write_config(
        tmp_path,
        models_yaml=models_yaml_with(
            '    tags: [default]\n    priority: 1\n    thinking: "SECRET-VALUE"\n'
        ),
    )
    message = str(load_error(tmp_path))
    assert "models.yaml" in message
    assert "thinking" in message
    assert "SECRET-VALUE" not in message


def test_example_models_mention_thinking_and_still_load(tmp_path: Path):
    assert "thinking" in EXAMPLE_MODELS_YAML
    write_config(tmp_path)
    selected = select_model(load_config(tmp_path), None)
    assert (selected.id, selected.thinking) == ("gemma-local", None)


def test_example_files_load_with_the_real_loader(tmp_path: Path):
    write_config(tmp_path)
    cfg = load_config(tmp_path)
    assert list(cfg.providers) == ["local"]
    assert [m.id for m in cfg.models] == ["gemma-local"]
    assert select_orchestrator_model(cfg, None).id == "gemma-local"
    assert [m.id for m in chat_models(cfg)] == ["gemma-local"]


_EMBEDDING_BEGIN = "# --- embedding example: begin ---"
_EMBEDDING_END = "# --- embedding example: end ---"


def _models_yaml_with_embedding_block(*, with_dimensions: bool) -> str:
    """The shipped example with its commented embedding entry switched on."""
    out: list[str] = []
    inside = False
    for line in EXAMPLE_MODELS_YAML.splitlines():
        stripped = line.strip()
        if stripped == _EMBEDDING_BEGIN:
            inside = True
        elif stripped == _EMBEDDING_END:
            inside = False
        elif inside:
            uncommented = stripped.removeprefix("# ")
            if with_dimensions:
                uncommented = uncommented.replace("# dimensions:", "dimensions:")
            out.append(uncommented)
            continue
        out.append(line)
    assert _EMBEDDING_BEGIN in EXAMPLE_MODELS_YAML
    return "\n".join(out) + "\n"


@pytest.mark.parametrize(("with_dimensions", "expected"), [(False, None), (True, 256)])
def test_commented_embedding_example_validates_when_switched_on(
    tmp_path: Path, with_dimensions: bool, expected: int | None
):
    yaml_text = _models_yaml_with_embedding_block(with_dimensions=with_dimensions)
    write_config(tmp_path, models_yaml=yaml_text)
    cfg = load_config(tmp_path)
    embedding = select_embedding_model(cfg, None)
    assert (embedding.id, embedding.provider, embedding.model) == (
        "bge-m3-local",
        "local",
        "bge-m3",
    )
    assert embedding.tags == ["embedding"]
    assert embedding.dimensions == expected
    assert [m.id for m in chat_models(cfg)] == ["gemma-local"]
    assert select_orchestrator_model(cfg, None).id == "gemma-local"


def _documented(text: str, key: str, *, separator: str) -> bool:
    # A key counts as documented when it starts a line (active or commented out,
    # even twice),
    # as `key:` / `key =`, or is listed in the header table as `key  text`.
    pattern = rf"^\s*(#\s*)*-?\s*{re.escape(key)}(\s*{separator}|\s{{2,}}\S)"
    return re.search(pattern, text, re.MULTILINE) is not None


@pytest.mark.parametrize(
    "key", [*ModelConfig.model_fields, *Price.model_fields], ids=str
)
def test_every_models_yaml_key_is_documented_in_the_example(key: str):
    assert _documented(EXAMPLE_MODELS_YAML, key, separator=":"), key


# Fields of ProviderConfig the config.toml example intentionally leaves out.
_UNDOCUMENTED_PROVIDER_FIELDS: dict[str, str] = {}


@pytest.mark.parametrize(
    "key",
    [k for k in ProviderConfig.model_fields if k not in _UNDOCUMENTED_PROVIDER_FIELDS],
)
def test_every_provider_key_is_documented_in_the_example(key: str):
    assert _documented(EXAMPLE_CONFIG_TOML, key, separator="="), key


def test_price_defaults_to_zero():
    price = ModelConfig.model_validate(model()).price
    assert (price.input_per_mtok, price.output_per_mtok) == (0, 0)


def test_duplicate_model_ids_rejected():
    with pytest.raises(ValueError, match="duplicate model ids: m1"):
        config_of(model(), model())


def test_unknown_provider_rejected():
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        config_of(model(provider="nope"))


def test_models_key_missing(tmp_path: Path):
    write_config(tmp_path, models_yaml="other: []\n")
    assert "models.yaml" in str(load_error(tmp_path))


def test_models_top_level_not_mapping(tmp_path: Path):
    write_config(tmp_path, models_yaml="- a\n")
    assert "models.yaml" in str(load_error(tmp_path))


def test_models_extra_key_in_file_names_file(tmp_path: Path):
    write_config(
        tmp_path,
        models_yaml=models_yaml_with("    tags: []\n    priority: 1\n    x: 1\n"),
    )
    error = str(load_error(tmp_path))
    assert "models.yaml" in error and "0.x" in error


def test_unknown_provider_in_file_names_models_file(tmp_path: Path):
    body = "    tags: []\n    priority: 1\n"
    write_config(tmp_path, models_yaml=models_yaml_with(body).replace("local", "nope"))
    assert "models.yaml" in str(load_error(tmp_path))


# --- parsing and missing files -----------------------------------------------


def test_toml_syntax_error_names_file(tmp_path: Path):
    write_config(tmp_path, toml="[providers.local\n")
    error = str(load_error(tmp_path))
    assert "config.toml" in error and "line" in error


def test_yaml_syntax_error_names_file(tmp_path: Path):
    write_config(tmp_path, models_yaml="models: [unclosed\n")
    assert "models.yaml" in str(load_error(tmp_path))


def test_yaml_python_tag_is_rejected_and_not_executed(tmp_path: Path):
    sentinel = tmp_path / "pwned"
    payload = f"models: !!python/object/apply:os.system ['touch {sentinel}']\n"
    write_config(tmp_path, models_yaml=payload)
    assert "models.yaml" in str(load_error(tmp_path))
    assert not sentinel.exists()


def test_missing_lists_both_files_when_dir_empty(tmp_path: Path):
    with pytest.raises(MissingConfigError) as info:
        load_config(tmp_path)
    assert info.value.missing == [tmp_path / "config.toml", tmp_path / "models.yaml"]


@pytest.mark.parametrize("absent", ["config.toml", "models.yaml"])
def test_missing_lists_only_absent_file(tmp_path: Path, absent: str):
    write_config(tmp_path)
    (tmp_path / absent).unlink()
    with pytest.raises(MissingConfigError) as info:
        load_config(tmp_path)
    assert info.value.missing == [tmp_path / absent]
    assert str(tmp_path / absent) in str(info.value)


# --- examples ----------------------------------------------------------------


def test_write_examples_round_trip(tmp_path: Path):
    config_dir = tmp_path / "new" / "dir"
    missing = [config_dir / "config.toml", config_dir / "models.yaml"]
    assert write_examples(config_dir, missing) == missing
    assert (config_dir / "config.toml").stat().st_mode & 0o777 == 0o600
    assert (config_dir / "models.yaml").stat().st_mode & 0o777 == 0o644
    assert select_model(load_config(config_dir), None).id == "gemma-local"


def test_write_examples_only_writes_missing_and_never_overwrites(tmp_path: Path):
    existing = tmp_path / "config.toml"
    existing.write_text("keep me")
    assert write_examples(tmp_path, [tmp_path / "models.yaml"]) == [
        tmp_path / "models.yaml"
    ]
    assert existing.read_text() == "keep me"
    with pytest.raises(FileExistsError):
        write_examples(tmp_path, [existing])
    assert existing.read_text() == "keep me"


def test_write_examples_sets_mode_at_creation(tmp_path: Path):
    old = os.umask(0)
    try:
        config_dir = tmp_path / "cfg"
        write_examples(config_dir, [config_dir / "config.toml"])
    finally:
        os.umask(old)
    assert (config_dir / "config.toml").stat().st_mode & 0o777 == 0o600
    assert config_dir.stat().st_mode & 0o777 == 0o700


def test_write_examples_does_not_rely_on_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("chmod must not be needed")

    monkeypatch.setattr(os, "chmod", refuse)
    old = os.umask(0)
    try:
        config_dir = tmp_path / "cfg"
        missing = [config_dir / "config.toml", config_dir / "models.yaml"]
        write_examples(config_dir, missing)
    finally:
        os.umask(old)
    assert (config_dir / "config.toml").stat().st_mode & 0o777 == 0o600
    assert (config_dir / "models.yaml").stat().st_mode & 0o777 == 0o644


# --- permissions -------------------------------------------------------------


def make_config_dir(tmp_path: Path, file_mode: int | None, dir_mode: int) -> Path:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    if file_mode is not None:
        (config_dir / "config.toml").write_text("")
        os.chmod(config_dir / "config.toml", file_mode)
    os.chmod(config_dir, dir_mode)
    return config_dir


def test_no_warnings_for_private_file_and_dir(tmp_path: Path):
    assert permission_warnings(make_config_dir(tmp_path, 0o600, 0o700)) == []


def test_no_warning_for_missing_file(tmp_path: Path):
    assert permission_warnings(make_config_dir(tmp_path, None, 0o700)) == []


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o606])
def test_file_with_other_read_bits_includes_read_warning(tmp_path: Path, mode: int):
    config_dir = make_config_dir(tmp_path, mode, 0o700)
    path = config_dir / "config.toml"
    expected = f"warning: {path} is readable by other users; run: chmod 600 {path}"
    assert expected in permission_warnings(config_dir)


@pytest.mark.parametrize("mode", [0o620, 0o602])
def test_file_writable_by_others_warns(tmp_path: Path, mode: int):
    config_dir = make_config_dir(tmp_path, mode, 0o700)
    path = config_dir / "config.toml"
    assert permission_warnings(config_dir) == [
        f"warning: {path} is writable by other users; run: chmod 600 {path}"
    ]


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_read_only_exposure_gives_exactly_the_read_warning(tmp_path: Path, mode: int):
    config_dir = make_config_dir(tmp_path, mode, 0o700)
    path = config_dir / "config.toml"
    assert permission_warnings(config_dir) == [
        f"warning: {path} is readable by other users; run: chmod 600 {path}"
    ]


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o720, 0o702])
def test_dir_writable_by_others_warns(tmp_path: Path, mode: int):
    config_dir = make_config_dir(tmp_path, 0o600, mode)
    assert permission_warnings(config_dir) == [
        f"warning: {config_dir} is writable by other users; run: chmod 700 {config_dir}"
    ]


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705, 0o700])
def test_dir_readable_by_others_is_fine(tmp_path: Path, mode: int):
    assert permission_warnings(make_config_dir(tmp_path, 0o600, mode)) == []


def test_file_and_dir_problems_give_two_lines(tmp_path: Path):
    config_dir = make_config_dir(tmp_path, 0o644, 0o775)
    warnings = permission_warnings(config_dir)
    assert len(warnings) == 2
    assert "readable" in warnings[0] and "writable" in warnings[1]


def test_world_writable_file_gives_read_and_write_warnings(tmp_path: Path):
    config_dir = make_config_dir(tmp_path, 0o666, 0o700)
    assert len(permission_warnings(config_dir)) == 2


# --- select_model ------------------------------------------------------------


def test_default_picks_highest_priority_including_float():
    cfg = config_of(
        model(id="a", priority=5),
        model(id="b", priority=5.5),
        model(id="c", priority=100, tags=["chat"]),
    )
    assert select_model(cfg, None).id == "b"


def test_explicit_id_overrides_default():
    cfg = config_of(model(id="a", priority=9), model(id="b", tags=[]))
    assert select_model(cfg, "b").id == "b"


def test_unknown_id_lists_valid_ids():
    cfg = config_of(model(id="a"), model(id="b", tags=[]))
    with pytest.raises(ConfigError, match="valid ids: a, b"):
        select_model(cfg, "zzz")


def test_no_default_tag_is_error():
    with pytest.raises(ConfigError, match="default"):
        select_model(config_of(model(tags=["chat"])), None)


def test_tie_at_top_priority_is_error():
    cfg = config_of(model(id="a", priority=2), model(id="b", priority=2))
    with pytest.raises(ConfigError, match="a, b"):
        select_model(cfg, None)


def test_tie_below_top_priority_is_fine():
    cfg = config_of(
        model(id="a", priority=1),
        model(id="b", priority=1),
        model(id="c", priority=2),
    )
    assert select_model(cfg, None).id == "c"


# --- select_orchestrator_model -------------------------------------------------


def test_orchestrator_picks_highest_priority_tagged_model_ignoring_default():
    cfg = config_of(
        model(id="d", priority=100),
        model(id="a", priority=1, tags=["orchestrator"]),
        model(id="b", priority=2.5, tags=["orchestrator", "chat"]),
    )
    assert select_orchestrator_model(cfg, None).id == "b"


def test_orchestrator_tie_at_top_priority_names_the_ids():
    cfg = config_of(
        model(id="a", priority=2, tags=["orchestrator"]),
        model(id="b", priority=2, tags=["orchestrator"]),
        model(id="c", priority=1, tags=["orchestrator"]),
    )
    with pytest.raises(ConfigError, match="a, b"):
        select_orchestrator_model(cfg, None)


def test_orchestrator_none_tagged_is_error_naming_the_tag_and_the_fix():
    cfg = config_of(model(id="a"))  # tagged default only: no silent fallback
    with pytest.raises(ConfigError, match="'orchestrator'.*models.yaml.*--model"):
        select_orchestrator_model(cfg, None)


def test_orchestrator_explicit_id_overrides_the_tag():
    cfg = config_of(model(id="a", tags=["orchestrator"]), model(id="b", tags=[]))
    assert select_orchestrator_model(cfg, "b").id == "b"


def test_orchestrator_unknown_id_lists_valid_ids():
    cfg = config_of(model(id="a"), model(id="b", tags=[]))
    with pytest.raises(ConfigError, match="valid ids: a, b"):
        select_orchestrator_model(cfg, "zzz")


def test_example_models_yaml_tags_the_example_model_orchestrator(tmp_path: Path):
    write_config(tmp_path)
    selected = select_orchestrator_model(load_config(tmp_path), None)
    assert selected.id == "gemma-local"


# --- embedding models --------------------------------------------------------


def test_dimensions_absent_is_none():
    assert ModelConfig.model_validate(model()).dimensions is None


def test_dimensions_valid_value_loads(tmp_path: Path):
    body = "    tags: [embedding]\n    priority: 1\n    dimensions: 256\n"
    write_config(tmp_path, models_yaml=models_yaml_with(body))
    assert load_config(tmp_path).models[0].dimensions == 256


@pytest.mark.parametrize("value", [0, -1, -256])
def test_dimensions_zero_or_negative_is_a_config_error(tmp_path: Path, value: int):
    body = f"    tags: [embedding]\n    priority: 1\n    dimensions: {value}\n"
    write_config(tmp_path, models_yaml=models_yaml_with(body))
    assert "dimensions" in str(load_error(tmp_path))


@pytest.mark.parametrize("other", ["chat", "default", "orchestrator"])
def test_embedding_with_a_chat_tag_is_a_config_error_naming_id_and_tag(
    tmp_path: Path, other: str
):
    body = f"    tags: [embedding, {other}]\n    priority: 1\n"
    write_config(tmp_path, models_yaml=models_yaml_with(body))
    message = str(load_error(tmp_path))
    assert "m1" in message
    assert other in message
    assert "Traceback" not in message


def test_embedding_with_several_chat_tags_names_each():
    with pytest.raises(ValueError, match="'chat', 'default'"):
        ModelConfig.model_validate(model(tags=["embedding", "chat", "default"]))


def test_embedding_tag_alone_loads(tmp_path: Path):
    body = "    tags: [embedding]\n    priority: 1\n"
    write_config(tmp_path, models_yaml=models_yaml_with(body))
    assert load_config(tmp_path).models[0].tags == ["embedding"]


def test_embedding_with_unrelated_tag_loads():
    assert ModelConfig.model_validate(model(tags=["embedding", "local"]))


# --- select_embedding_model --------------------------------------------------


def test_embedding_picks_highest_priority_tagged_model():
    cfg = config_of(
        model(id="d", priority=100),
        model(id="a", priority=1, tags=["embedding"]),
        model(id="b", priority=2.5, tags=["embedding"]),
    )
    assert select_embedding_model(cfg, None).id == "b"


def test_embedding_explicit_id_returns_that_entry():
    cfg = config_of(
        model(id="a", tags=["embedding"], priority=9),
        model(id="b", tags=["embedding"], priority=1),
    )
    assert select_embedding_model(cfg, "b").id == "b"


def test_embedding_explicit_id_without_the_tag_is_error_naming_id():
    cfg = config_of(model(id="a"), model(id="e", tags=["embedding"]))
    with pytest.raises(ConfigError, match="'a'.*'embedding'"):
        select_embedding_model(cfg, "a")


def test_embedding_unknown_id_lists_valid_ids():
    cfg = config_of(model(id="a"), model(id="e", tags=["embedding"]))
    with pytest.raises(ConfigError, match="valid ids: a, e"):
        select_embedding_model(cfg, "zzz")


def test_embedding_tie_at_top_priority_names_the_ids():
    cfg = config_of(
        model(id="a", priority=2, tags=["embedding"]),
        model(id="b", priority=2, tags=["embedding"]),
        model(id="c", priority=1, tags=["embedding"]),
    )
    with pytest.raises(ConfigError, match="a, b"):
        select_embedding_model(cfg, None)


def test_embedding_none_tagged_is_error_saying_no_model_has_the_tag():
    cfg = config_of(model(id="a"))
    with pytest.raises(ConfigError, match="no model has the tag 'embedding'"):
        select_embedding_model(cfg, None)


# --- chat_models -------------------------------------------------------------


def test_chat_models_excludes_embedding_and_keeps_file_order():
    cfg = config_of(
        model(id="c", tags=["default"]),
        model(id="e", tags=["embedding"]),
        model(id="a", tags=["orchestrator"]),
        model(id="z", tags=[]),
    )
    assert [m.id for m in chat_models(cfg)] == ["c", "a", "z"]


def test_config_without_an_embedding_entry_is_unchanged():
    cfg = config_of(model(id="a"), model(id="b", tags=["orchestrator"]))
    assert chat_models(cfg) == list(cfg.models)
    assert select_model(cfg, None).id == "a"
    assert select_orchestrator_model(cfg, None).id == "b"


# --- reasoning_style ---------------------------------------------------------


def test_reasoning_style_defaults_to_none():
    assert provider(**OPENAI | {"api_key": "k"}).reasoning_style is None


@pytest.mark.parametrize("style", ["openai", "openrouter"])
def test_reasoning_style_accepts_known_values_on_openai(style: str):
    cfg = provider(**OPENAI | {"api_key": "k", "reasoning_style": style})
    assert cfg.reasoning_style == style


@pytest.mark.parametrize("style", ["anthropic", "", "OpenAI", 1, True])
def test_reasoning_style_rejects_other_values(style: object):
    with pytest.raises(ValueError):
        provider(**OPENAI | {"api_key": "k", "reasoning_style": style})


@pytest.mark.parametrize("base", [OLLAMA, VERTEX])
@pytest.mark.parametrize("style", ["openai", "openrouter"])
def test_reasoning_style_is_rejected_on_ollama_and_vertex(
    base: dict[str, Any], style: str
):
    with pytest.raises(ValueError, match="reasoning_style is only valid on openai"):
        provider(**base | {"reasoning_style": style})


def test_example_config_mentions_reasoning_style_and_still_loads(tmp_path: Path):
    assert "reasoning_style" in EXAMPLE_CONFIG_TOML
    write_config(tmp_path)
    assert load_config(tmp_path).providers["local"].type == "ollama"


def test_uncommented_openai_example_loads_with_reasoning_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    lines = EXAMPLE_CONFIG_TOML.splitlines()
    start = lines.index("# [providers.openai]")
    block = [
        line.removeprefix("# ")
        for line in lines[start : start + 6]
        if "api_key =" not in line
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    toml = "\n".join(lines[:start] + block) + "\n"
    yaml = EXAMPLE_MODELS_YAML.replace("provider: local", "provider: openai")
    write_config(tmp_path, toml, yaml)
    cfg = load_config(tmp_path)
    assert cfg.providers["openai"].reasoning_style == "openai"


# --- base_url ----------------------------------------------------------------

BASE_URL_SECRET = "hunter2-base-url-secret"


@pytest.mark.parametrize("kind", [OLLAMA, OPENAI | {"api_key": "k"}])
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "https://api.example/v1",
        "HTTP://Example.com/x?y=1",
        "http://127.0.0.1:8080",
        "http://[::1]:11434",
    ],
)
def test_base_url_accepts_plain_http_urls(kind: dict[str, Any], url: str):
    assert provider(**kind | {"base_url": url}).base_url == url


@pytest.mark.parametrize("kind", [OLLAMA, OPENAI | {"api_key": "k"}])
@pytest.mark.parametrize(
    "url",
    [
        "ftp://x",
        "file:///etc/passwd",
        "localhost:11434",
        "//host/path",
        "http://",
        "https:///path",
        "http://[bad",
        "http://host:notaport",
        f"http://user:{BASE_URL_SECRET}@host",
        f"http://{BASE_URL_SECRET}@host",
        f"https://:{BASE_URL_SECRET}@host/v1",
        "",
    ],
)
def test_base_url_rejects_other_values_without_echo(kind: dict[str, Any], url: str):
    with pytest.raises(ValueError) as info:
        provider(**kind | {"base_url": url})
    assert "base_url must be an http(s) URL without credentials" in str(info.value)
    assert BASE_URL_SECRET not in str(info.value)


def test_bad_base_url_is_a_config_error_that_does_not_echo(tmp_path: Path):
    toml = f'[providers.local]\ntype = "ollama"\nbase_url = "http://u:{BASE_URL_SECRET}@h"\n'
    write_config(tmp_path, toml)
    error = load_error(tmp_path)
    assert "local:" in str(error)
    assert "http(s) URL without credentials" in str(error)
    assert BASE_URL_SECRET not in str(error)


# --- non-finite numbers ------------------------------------------------------


@pytest.mark.parametrize("literal", [".nan", ".inf", "-.inf"])
def test_non_finite_priority_is_a_config_error(tmp_path: Path, literal: str):
    write_config(
        tmp_path,
        models_yaml=models_yaml_with(f"    tags: [default]\n    priority: {literal}\n"),
    )
    error = load_error(tmp_path)
    assert "priority" in str(error)
    assert "Traceback" not in str(error)


@pytest.mark.parametrize("field", ["input_per_mtok", "output_per_mtok"])
@pytest.mark.parametrize("literal", [".nan", ".inf", "-.inf"])
def test_non_finite_price_is_a_config_error(tmp_path: Path, field: str, literal: str):
    body = (
        f"    tags: [default]\n    priority: 1\n    price:\n      {field}: {literal}\n"
    )
    write_config(tmp_path, models_yaml=models_yaml_with(body))
    assert field in str(load_error(tmp_path))


def test_float_priority_and_price_still_work(tmp_path: Path):
    body = (
        "    tags: [default]\n    priority: 2.5\n"
        "    price:\n      input_per_mtok: 0.15\n      output_per_mtok: 0\n"
    )
    write_config(tmp_path, models_yaml=models_yaml_with(body))
    cfg = load_config(tmp_path)
    assert select_model(cfg, None).priority == 2.5
    assert cfg.models[0].price.input_per_mtok == 0.15


# --- unreadable files --------------------------------------------------------


def test_invalid_utf8_toml_is_a_config_error(tmp_path: Path):
    write_config(tmp_path)
    (tmp_path / "config.toml").write_bytes(b'[providers.x]\ntype = "\xff\xfe"\n')
    error = load_error(tmp_path)
    assert str(error) == f"cannot read {tmp_path / 'config.toml'}: UnicodeDecodeError"


def test_invalid_utf8_yaml_is_a_config_error(tmp_path: Path):
    write_config(tmp_path)
    (tmp_path / "models.yaml").write_bytes(b"models: \xff\xfe\n")
    assert isinstance(load_error(tmp_path), ConfigError)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode 000 files")
@pytest.mark.parametrize("name", ["config.toml", "models.yaml"])
def test_unreadable_file_is_a_config_error_with_type_name_only(
    tmp_path: Path, name: str
):
    write_config(tmp_path)
    os.chmod(tmp_path / name, 0o000)
    try:
        error = load_error(tmp_path)
    finally:
        os.chmod(tmp_path / name, 0o600)
    assert str(error) == f"cannot read {tmp_path / name}: PermissionError"


def test_directory_named_models_yaml_is_reported_as_missing(tmp_path: Path):
    write_config(tmp_path)
    (tmp_path / "models.yaml").unlink()
    (tmp_path / "models.yaml").mkdir()
    with pytest.raises(MissingConfigError) as info:
        load_config(tmp_path)
    assert info.value.missing == [tmp_path / "models.yaml"]


def test_resolve_think_lives_in_config_and_cli_still_exports_it():
    from llm_harness import cli
    from llm_harness.providers import ProviderFactory

    assert cli.resolve_think is resolve_think
    assert cli.ProviderFactory is ProviderFactory
    model_config = ModelConfig.model_validate(model(thinking=False))
    assert resolve_think(None, model_config) is False
    assert resolve_think(True, model_config) is True


# --- user_id -----------------------------------------------------------------


def config_toml_with(extra: str) -> str:
    return extra + "\n" + EXAMPLE_CONFIG_TOML


def test_user_id_absent_is_none(tmp_path: Path):
    write_config(tmp_path)
    assert load_config(tmp_path).user_id is None


@pytest.mark.parametrize("value", ["ann", " ann ", "a" * 128, "Zoë 田中"])
def test_user_id_loads_as_given(tmp_path: Path, value: str):
    write_config(tmp_path, config_toml_with(f'user_id = "{value}"'))
    assert load_config(tmp_path).user_id == value


@pytest.mark.parametrize(
    "literal",
    ['""', '"   "', "42", "{ a = 1 }", f'"{"a" * 129}"', '"ann\\nbob"', '"a\\tb"'],
    ids=["empty", "blank", "integer", "table", "too-long", "newline", "tab"],
)
def test_bad_user_id_is_a_config_error_naming_the_key(tmp_path: Path, literal: str):
    write_config(tmp_path, config_toml_with(f"user_id = {literal}"))
    message = str(load_error(tmp_path))
    assert message.startswith("invalid config.toml: user_id ")


def test_user_id_type_error_names_the_type(tmp_path: Path):
    write_config(tmp_path, config_toml_with("user_id = 42"))
    assert "not int" in str(load_error(tmp_path))


def test_user_id_is_validated_on_direct_construction():
    with pytest.raises(ValueError, match="user_id"):
        HarnessConfig.model_validate(
            {"providers": {"local": OLLAMA}, "models": [], "user_id": " "}
        )


def test_unknown_top_level_key_is_still_rejected(tmp_path: Path):
    write_config(tmp_path, config_toml_with('user = "ann"\nuser_id = "ann"'))
    assert "unknown top-level keys: user" in str(load_error(tmp_path))


def test_example_config_mentions_user_id_and_still_loads(tmp_path: Path):
    assert "# user_id = " in EXAMPLE_CONFIG_TOML
    write_config(tmp_path)
    assert load_config(tmp_path).user_id is None


def test_user_id_under_a_provider_header_gets_a_placement_hint(tmp_path: Path):
    write_config(tmp_path, EXAMPLE_CONFIG_TOML + '\nuser_id = "secret-ann"\n')
    message = str(load_error(tmp_path))
    assert message == (
        "invalid config.toml: user_id found inside [providers.<id>]; "
        "put user_id at the top of the file, before any [providers...] header"
    )
    assert "secret-ann" not in message


def test_other_unknown_provider_keys_keep_the_pydantic_error(tmp_path: Path):
    write_config(tmp_path, EXAMPLE_CONFIG_TOML + '\nuser = "ann"\n')
    message = str(load_error(tmp_path))
    assert ".user: Extra inputs are not permitted" in message
    assert "user_id found inside" not in message


# --- [storage] ---------------------------------------------------------------

PROVIDER_TOML = '[providers.local]\ntype = "ollama"\nbase_url = "http://x"\n'


def load_storage(tmp_path: Path, storage_toml: str) -> StorageConfig:
    write_config(tmp_path, PROVIDER_TOML + storage_toml)
    return load_config(tmp_path).storage


def storage_error(tmp_path: Path, storage_toml: str) -> str:
    write_config(tmp_path, PROVIDER_TOML + storage_toml)
    return str(load_error(tmp_path))


def test_absent_storage_table_gives_all_defaults(tmp_path: Path):
    storage = load_storage(tmp_path, "")
    assert storage == StorageConfig()
    assert (storage.relational.backend, storage.relational.path) == ("sqlite", None)
    assert (storage.vector.backend, storage.vector.path) == ("qdrant-edge", None)
    assert (storage.files.backend, storage.files.root) == ("local", None)


@pytest.mark.parametrize(
    ("table", "backend"),
    [
        ("relational", "sqlite"),
        ("vector", "qdrant-edge"),
        ("vector", "none"),
        ("files", "local"),
    ],
)
def test_each_valid_backend_loads(tmp_path: Path, table: str, backend: str):
    storage = load_storage(tmp_path, f'[storage.{table}]\nbackend = "{backend}"\n')
    assert getattr(storage, table).backend == backend


@pytest.mark.parametrize(
    ("table", "backend"), [("relational", "postgres"), ("vector", "qdrant")]
)
def test_planned_backends_are_rejected_by_name(
    tmp_path: Path, table: str, backend: str
):
    message = storage_error(tmp_path, f'[storage.{table}]\nbackend = "{backend}"\n')
    assert message.startswith(f"invalid config.toml: storage.{table}.backend: ")
    assert f"'{backend}' is not available yet" in message
    assert "storage-backends-remote" in message


def test_planned_vector_backend_message_is_exact(tmp_path: Path):
    message = storage_error(tmp_path, '[storage.vector]\nbackend = "qdrant"\n')
    assert message == (
        "invalid config.toml: storage.vector.backend: 'qdrant' is not available yet "
        "(planned feature storage-backends-remote); use one of: qdrant-edge, none"
    )


@pytest.mark.parametrize(
    ("table", "valid"),
    [
        ("relational", "sqlite"),
        ("vector", "qdrant-edge, none"),
        ("files", "local"),
    ],
)
def test_unknown_backend_lists_valid_values(tmp_path: Path, table: str, valid: str):
    message = storage_error(tmp_path, f'[storage.{table}]\nbackend = "x"\n')
    assert (
        f"storage.{table}.backend: unknown backend 'x'; use one of: {valid}" in message
    )


def test_unknown_key_in_a_storage_table_is_rejected(tmp_path: Path):
    message = storage_error(tmp_path, '[storage.vector]\nbogus = "x"\n')
    assert message.startswith("invalid config.toml: storage.vector.bogus")


def test_unknown_storage_table_is_rejected(tmp_path: Path):
    message = storage_error(tmp_path, "[storage.cache]\n")
    assert message.startswith("invalid config.toml: storage.cache")


def test_tilde_in_a_path_is_expanded(tmp_path: Path):
    storage = load_storage(tmp_path, '[storage.vector]\npath = "~/vec"\n')
    assert storage.vector.path == Path.home() / "vec"


def test_absolute_paths_are_kept(tmp_path: Path):
    storage = load_storage(
        tmp_path,
        '[storage.relational]\npath = "/data/h.db"\n[storage.files]\nroot = "/data/f"\n',
    )
    assert storage.relational.path == Path("/data/h.db")
    assert storage.files.root == Path("/data/f")


@pytest.mark.parametrize(
    ("table", "key"),
    [("relational", "path"), ("vector", "path"), ("files", "root")],
)
def test_relative_path_is_rejected(tmp_path: Path, table: str, key: str):
    message = storage_error(tmp_path, f'[storage.{table}]\n{key} = "rel/dir"\n')
    assert message.startswith(f"invalid config.toml: storage.{table}.{key}: ")
    assert "must be absolute" in message


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_path_is_rejected(tmp_path: Path, value: str):
    message = storage_error(tmp_path, f'[storage.files]\nroot = "{value}"\n')
    assert "storage.files.root: path must not be empty" in message


def test_non_table_storage_is_rejected(tmp_path: Path):
    write_config(tmp_path, "storage = 5\n" + PROVIDER_TOML)
    message = str(load_error(tmp_path))
    assert message.startswith("invalid config.toml: storage must be a table")


def test_unknown_top_level_key_still_rejected_with_storage_support(tmp_path: Path):
    write_config(tmp_path, "bogus = 1\n" + PROVIDER_TOML)
    assert "unknown top-level keys: bogus" in str(load_error(tmp_path))


def test_resolvers_default_under_the_data_dir(tmp_path: Path):
    storage = StorageConfig()
    assert storage.relational.resolved_path(tmp_path) == tmp_path / "harness.db"
    assert storage.vector.resolved_path(tmp_path) == tmp_path / "vectors"
    assert storage.files.resolved_root(tmp_path) == tmp_path / "files"


def test_resolvers_return_the_custom_value_when_set(tmp_path: Path):
    storage = StorageConfig(
        relational=RelationalConfig(path=Path("/a/h.db")),
        vector=VectorConfig(path=Path("/a/v")),
        files=FilesConfig(root=Path("/a/f")),
    )
    assert storage.relational.resolved_path(tmp_path) == Path("/a/h.db")
    assert storage.vector.resolved_path(tmp_path) == Path("/a/v")
    assert storage.files.resolved_root(tmp_path) == Path("/a/f")


def test_resolvers_create_nothing(tmp_path: Path):
    data_dir = tmp_path / "data"
    storage = StorageConfig()
    storage.relational.resolved_path(data_dir)
    storage.vector.resolved_path(data_dir)
    storage.files.resolved_root(data_dir)
    assert not data_dir.exists()
    assert list(tmp_path.iterdir()) == []


def test_default_db_path_is_under_the_default_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Imported at module load, so it is the original, not the autouse patch.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_data_dir() == tmp_path / ".local/share/llm-harness"
    assert default_db_path() == tmp_path / ".local/share/llm-harness/harness.db"


def test_example_config_with_storage_examples_still_loads(tmp_path: Path):
    write_config(tmp_path)
    assert load_config(tmp_path).storage == StorageConfig()
    assert "# [storage.vector]" in EXAMPLE_CONFIG_TOML
