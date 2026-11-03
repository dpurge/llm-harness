"""The `memory_vectors` switch and its wiring (offline).

Config key, registry, orchestrator Notice, `/memory` answer, the three front ends and
the lazy embedder factory. Real SQLite on `tmp_path`, the concept embedder behind the
real `Embedder`, and the fake vector store; no network.
"""

import io
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing import User

from llm_harness.builtin_tools import (
    MEMORY_NEEDS_USER_ID,
    MEMORY_VECTORS_NEEDS_MEMORY,
    build_tool_registry,
)
from llm_harness.cli import _run_turn
from llm_harness.config import (
    ConfigError,
    HarnessConfig,
    ModelConfig,
    ProviderConfig,
    StorageConfig,
    ToolsConfig,
    VectorConfig,
)
from llm_harness.embeddings import Embedder, build_embedder_factory
from llm_harness.gui.app import register_page
from llm_harness.inspect_state import tools_text
from llm_harness.memory import MemoryStore
from llm_harness.memory_command import handle_memory_text
from llm_harness.memory_index import IndexedMemoryStore
from llm_harness.memory_tools import MemorySearchInput, recall_store
from llm_harness.orchestrator import Orchestrator
from llm_harness.storage import DisabledVectorStore, Storage, open_storage
from llm_harness.tools import ToolContext, ToolRegistry
from llm_harness.tui.app import ChatApp
from llm_harness.types import Notice
from tests.fake_steps import ScriptedSteps, reply_events
from tests.fakes import ConceptEmbedder, FakeVectorStore, router_factory_for
from tests.test_config import PROVIDER_TOML, load_error, write_config
from tests.test_gui import CONFIG as GUI_CONFIG
from tests.test_gui import MODEL_1 as GUI_MODEL
from tests.test_gui import StepFactory as GuiStepFactory
from tests.test_gui import (
    elements,
    no_leftover_pages,  # noqa: F401 - autouse fixture
    send,
    wait_for,
)
from tests.test_memory_recall import memory_section, reflection_none, step_requests
from tests.test_orchestrator import RESPOND, SET_GOAL, call, make, model_cfg, run
from tests.test_tui import CONFIG, MODEL_1, StepFactory, texts
from tests.test_tui import send as tui_send

PEANUTS = "Partner is allergic to peanuts"
QUESTION = "what can I cook for her, she has a diet"
NO_MODEL = "no embedding model for this test"
OFF_LINE = f"memory vector search is off: ConfigError: {NO_MODEL}; keyword search only"


def embedding_model(model_id: str = "bge-m3-local") -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="local",
        model="bge-m3",
        tags=["embedding"],
        priority=1,
    )


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    """Real SQLite, with the fake vector store in place of the backend."""
    opened = open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path,
        relational_path=tmp_path / "memory.db",
    )
    opened.vectors = FakeVectorStore()
    return opened


def working_factory():
    return lambda: Embedder(ConceptEmbedder(), embedding_model())


def failing_factory():
    def factory() -> Embedder:
        raise ConfigError(NO_MODEL)

    return factory


def vector_registry(factory, *, vectors: bool = True) -> ToolRegistry:
    return build_tool_registry(
        [],
        memory=True,
        user_id="alice",
        memory_vectors=vectors,
        embedder_factory=factory,
    )


def answer_scripts() -> list[list]:
    return [reply_events(RESPOND, SET_GOAL), reflection_none()]


def notices(events: list) -> list[str]:
    return [event.text for event in events if isinstance(event, Notice)]


def ctx_for(storage: Storage) -> ToolContext:
    return ToolContext(
        storage=storage,
        user_id="alice",
        agent_id="orchestrator",
        tags=frozenset({"orchestrator"}),
    )


async def seed_plain(storage: Storage, text: str = PEANUTS) -> None:
    """A row written without any index, as if saved before the flag was on."""
    await MemoryStore(storage.relational).save("alice", text, ["partner", "allergy"])


# --- config -----------------------------------------------------------------------


def test_memory_vectors_defaults_to_off() -> None:
    assert ToolsConfig().memory_vectors is False


def test_memory_vectors_true_loads(tmp_path: Path) -> None:
    from llm_harness.config import load_config

    write_config(tmp_path, PROVIDER_TOML + "[tools]\nmemory_vectors = true\n")
    assert load_config(tmp_path).tools.memory_vectors is True


@pytest.mark.parametrize("value", ['"yes"', "1", '"true"'])
def test_a_non_bool_memory_vectors_is_rejected(tmp_path: Path, value: str) -> None:
    write_config(tmp_path, PROVIDER_TOML + f"[tools]\nmemory_vectors = {value}\n")
    assert "tools.memory_vectors" in str(load_error(tmp_path))


def test_the_example_config_documents_the_key_and_still_parses(tmp_path: Path) -> None:
    from llm_harness.config import EXAMPLE_CONFIG_TOML, load_config

    assert "# memory_vectors = true" in EXAMPLE_CONFIG_TOML
    uncommented = EXAMPLE_CONFIG_TOML.replace(
        "# memory_vectors = true", "memory_vectors = true"
    ).replace("# [tools]", "[tools]")
    write_config(tmp_path, uncommented)
    assert load_config(tmp_path).tools.memory_vectors is True
    write_config(tmp_path, EXAMPLE_CONFIG_TOML)
    assert load_config(tmp_path).tools.memory_vectors is False


# --- registry ---------------------------------------------------------------------


async def test_flag_off_keeps_the_plain_store(storage: Storage) -> None:
    store = recall_store(vector_registry(working_factory(), vectors=False), storage)
    assert type(store) is MemoryStore


async def test_flag_on_without_a_factory_keeps_the_plain_store(
    storage: Storage,
) -> None:
    store = recall_store(vector_registry(None), storage)
    assert type(store) is MemoryStore


async def test_flag_on_with_a_factory_gives_the_indexed_store(storage: Storage) -> None:
    store = recall_store(vector_registry(working_factory()), storage)
    assert isinstance(store, IndexedMemoryStore)


@pytest.mark.parametrize(
    ("memory", "user_id", "extra_note"),
    [(False, "alice", None), (True, None, MEMORY_NEEDS_USER_ID)],
    ids=["memory-off", "no-user-id"],
)
def test_vectors_without_memory_is_a_note_not_an_error(
    memory: bool, user_id: str | None, extra_note: str | None
) -> None:
    registry = build_tool_registry(
        [],
        memory=memory,
        user_id=user_id,
        memory_vectors=True,
        embedder_factory=working_factory(),
    )
    assert MEMORY_VECTORS_NEEDS_MEMORY in registry.notes
    if extra_note is not None:
        assert extra_note in registry.notes  # the existing note still shows
    text = tools_text(registry.all(), registry.notes)
    assert MEMORY_VECTORS_NEEDS_MEMORY in text


def test_no_note_when_vectors_have_memory() -> None:
    assert vector_registry(working_factory()).notes == []


def test_building_the_registry_never_calls_the_factory() -> None:
    calls: list[int] = []

    def factory() -> Embedder:
        calls.append(1)
        raise AssertionError("must stay lazy")

    vector_registry(factory)
    assert calls == []


# --- search through the tool and the orchestrator ---------------------------------


async def test_the_memory_search_tool_finds_the_meaning_match(storage: Storage) -> None:
    tools = vector_registry(working_factory())
    store = recall_store(tools, storage)
    await store.save("alice", PEANUTS, ["partner", "peanuts"])
    search = tools.get("memory_search")
    result = await search.run(MemorySearchInput(query=QUESTION), ctx_for(storage))
    assert PEANUTS in result


async def test_the_recall_block_has_the_meaning_match_only_with_the_flag(
    storage: Storage,
) -> None:
    tools = vector_registry(working_factory())
    await recall_store(tools, storage).save("alice", PEANUTS, ["partner", "peanuts"])
    orchestrator, provider = make(
        answer_scripts(), tools=tools, storage=storage, user_id="alice"
    )
    events = await run(orchestrator, QUESTION)
    section = memory_section(step_requests(provider)[0])
    assert section is not None and PEANUTS in section
    assert notices(events) == []

    # Same data, flag off: keyword search cannot match, so there is no block.
    plain = vector_registry(working_factory(), vectors=False)
    orchestrator, provider = make(
        answer_scripts(), tools=plain, storage=storage, user_id="alice"
    )
    await run(orchestrator, QUESTION)
    assert memory_section(step_requests(provider)[0]) is None


# --- failure: one Notice -----------------------------------------------------------


async def test_a_failing_factory_gives_keyword_recall_and_exactly_one_notice(
    storage: Storage,
) -> None:
    await seed_plain(storage)
    tools = vector_registry(failing_factory())
    orchestrator, provider = make(
        [*answer_scripts(), *answer_scripts()],
        tools=tools,
        storage=storage,
        user_id="alice",
    )
    first = await run(orchestrator, "is my partner allergic to peanuts?")
    assert notices(first) == [OFF_LINE]  # recall and reflection drains: once
    section = memory_section(step_requests(provider)[0])
    assert section is not None and PEANUTS in section  # keyword hit survives
    second = await run(orchestrator, "and what about peanuts, partner?")
    assert notices(second) == []


async def test_a_memory_tool_call_drains_the_notice_once(storage: Storage) -> None:
    tools = vector_registry(failing_factory())
    scripts = [
        reply_events(
            call("memory_save", text="Likes tea.", keywords=["tea"]), SET_GOAL
        ),
        reply_events(RESPOND),
        reflection_none(),
    ]
    orchestrator, _ = make(scripts, tools=tools, storage=storage, user_id="alice")
    events = await run(orchestrator, "remember that I like tea")
    assert notices(events) == [OFF_LINE]
    assert len(await MemoryStore(storage.relational).list("alice")) == 1


async def test_the_disabled_backend_turns_the_index_off_with_a_clean_notice(
    tmp_path: Path,
) -> None:
    disabled = open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path,
        relational_path=tmp_path / "memory.db",
    )
    assert isinstance(disabled.vectors, DisabledVectorStore)
    store = recall_store(vector_registry(working_factory()), disabled)
    assert store is not None
    memory = await store.save("alice", PEANUTS, ["partner"])
    assert memory.id == 1  # SQLite still has it
    notice = store.take_notice()
    assert notice is not None
    assert notice.startswith("memory vector search is off: VectorStoreError: ")
    assert "the vector store is disabled" in notice
    assert notice.endswith("; keyword search only")
    assert store.take_notice() is None
    await store.save("alice", "Second note.", ["note"])
    assert store.take_notice() is None  # one notice per process


# --- /memory ----------------------------------------------------------------------


async def test_the_memory_answer_ends_with_the_notice_when_the_index_is_off(
    storage: Storage,
) -> None:
    tools = vector_registry(failing_factory())
    await recall_store(tools, storage).save("alice", "Likes tea.", ["tea"])
    answer = await handle_memory_text("/memory", tools, storage, "alice")
    assert answer is not None
    assert answer.splitlines()[-1] == OFF_LINE
    again = await handle_memory_text("/memory", tools, storage, "alice")
    assert again is not None and "keyword search only" not in again


async def test_the_memory_answer_has_no_extra_line_when_on_or_flag_off(
    storage: Storage,
) -> None:
    for tools in (
        vector_registry(working_factory()),
        vector_registry(failing_factory(), vectors=False),
    ):
        await recall_store(tools, storage).save("alice", "Likes tea.", ["tea"])
        answer = await handle_memory_text("/memory", tools, storage, "alice")
        assert answer is not None and "memory vector search" not in answer


# --- front ends -------------------------------------------------------------------


async def test_the_cli_prints_the_notice(storage: Storage) -> None:
    await seed_plain(storage)
    orchestrator = Orchestrator(
        ScriptedSteps(answer_scripts()),
        model_cfg(),
        tools=vector_registry(failing_factory()),
        storage=storage,
        user_id="alice",
    )
    out, err = io.StringIO(), io.StringIO()
    assert await _run_turn(orchestrator, "peanuts partner", out, err)
    assert err.getvalue().splitlines().count(OFF_LINE) == 1


async def test_the_tui_shows_the_notice(storage: Storage) -> None:
    await seed_plain(storage)
    config = CONFIG.model_copy(update={"user_id": "alice"})
    app = ChatApp(
        config,
        MODEL_1,
        router_factory_for(config, StepFactory(answer_scripts()), None),
        False,
        storage=storage,
        tools=vector_registry(failing_factory()),
    )
    async with app.run_test() as pilot:
        await tui_send(pilot, app, "peanuts partner")
        assert texts(app, "note").count(OFF_LINE) == 1


async def test_the_gui_shows_the_notice(user: User, storage: Storage) -> None:
    await seed_plain(storage)
    config = GUI_CONFIG.model_copy(update={"user_id": "alice"})
    register_page(
        config,
        GUI_MODEL,
        router_factory_for(config, GuiStepFactory(answer_scripts()), None),
        False,
        storage=storage,
        tools=vector_registry(failing_factory()),
    )
    await user.open("/")
    await send(user, "peanuts partner")
    await wait_for(lambda: any(e.text == OFF_LINE for e in elements(user, "note")))
    (label,) = [e for e in elements(user, "note") if e.text == OFF_LINE]
    assert isinstance(label, ui.label)


# --- the embedder factory ---------------------------------------------------------


def harness_config(models: list[ModelConfig]) -> HarnessConfig:
    return HarnessConfig(
        providers={
            "local": ProviderConfig(type="ollama", base_url="http://x"),
        },
        models=models,
    )


def test_the_factory_builds_the_embedder_lazily_from_the_embedding_model() -> None:
    config = harness_config([embedding_model()])
    seen: list[ProviderConfig] = []

    def provider_factory(provider: ProviderConfig):
        seen.append(provider)
        return ConceptEmbedder()

    factory = build_embedder_factory(config, provider_factory)
    assert seen == []  # building the factory touches nothing
    embedder = factory()
    assert isinstance(embedder, Embedder)
    assert embedder.model_id == "bge-m3-local"
    assert seen == [config.providers["local"]]


def test_the_factory_raises_a_config_error_without_an_embedding_model() -> None:
    chat = embedding_model("chat").model_copy(update={"tags": ["default"]})
    factory = build_embedder_factory(
        harness_config([chat]), lambda p: ConceptEmbedder()
    )
    with pytest.raises(ConfigError, match="embedding"):
        factory()


def test_the_factory_names_a_provider_that_cannot_embed() -> None:
    class ChatOnly:
        pass

    factory = build_embedder_factory(
        harness_config([embedding_model()]), lambda p: ChatOnly()
    )
    with pytest.raises(ConfigError, match="provider 'local' cannot embed"):
        factory()
