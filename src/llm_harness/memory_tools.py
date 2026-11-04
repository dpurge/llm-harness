"""The four agent-memory tools: `memory_search`, `memory_save`, `memory_update`, `memory_forget`.

Memories are short English notes kept per `user_id` (see `memory.py`). The writes
declare `prompts = False`: they run without the approval prompt, and the call line
(`tool NAME {arguments}`) is their notice, so the human still sees the stored text.
A tool needs `ctx.storage` and `ctx.user_id`; without them it returns a one-line
error string. A `MemoryValidationError` is not caught: the orchestrator turns it
into the observation `tool NAME failed: <message>`, and its message is already short.
"""

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, Field

from llm_harness.embeddings import Embedder
from llm_harness.memory import (
    MAX_KEYWORD_CHARS,
    MAX_KEYWORDS,
    MAX_SEARCH_RESULTS,
    MAX_TEXT_CHARS,
    Memory,
    MemoryStore,
)
from llm_harness.memory_index import IndexedMemoryStore
from llm_harness.memory_recall import SHARED_MARKER
from llm_harness.storage import Storage
from llm_harness.tools import Tool, ToolContext, ToolRegistry

_ENGLISH = "Memories are short English notes, written in English whatever language the chat is in."
_NO_USER = "memory is off: no user_id is set"
_NO_STORAGE = "memory is unavailable: no storage"


class MemorySearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    k: int = Field(default=5, ge=1, le=MAX_SEARCH_RESULTS)


class MemorySaveInput(BaseModel):
    text: str = Field(
        description=f"One English note, 1 to {MAX_TEXT_CHARS} characters."
    )
    keywords: list[str] = Field(
        description=(
            f"1 to {MAX_KEYWORDS} lowercase English words, "
            f"each up to {MAX_KEYWORD_CHARS} characters."
        )
    )


class MemoryUpdateInput(BaseModel):
    id: int
    text: str | None = None
    keywords: list[str] | None = None


class MemoryForgetInput(BaseModel):
    id: int


def _stamps(memory: Memory) -> str:
    return f"created {memory.created_at}, updated {memory.updated_at}"


def _block(memory: Memory) -> str:
    keywords = ", ".join(memory.keywords)
    marker = f"{SHARED_MARKER} " if memory.shared else ""
    return f"#{memory.id} {marker}[{keywords}] ({memory.updated_at}) {memory.text}"


class MemoryTools:
    """Shares one `MemoryStore` per storage among the four tools.

    `clock` None means the store's own UTC-now default. With `vectors` on and an
    `embedder_factory`, the store is an `IndexedMemoryStore` (vector search with a
    keyword fallback); otherwise the plain keyword `MemoryStore`.
    The store migrates lazily on first use, once per store; building one per call
    would repeat that check, so it is cached here. The storage is only known at run
    time (`ctx.storage`), and a different one gets its own store.
    """

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        vectors: bool = False,
        embedder_factory: Callable[[], Embedder] | None = None,
    ) -> None:
        self._clock = clock
        self._vectors = vectors
        self._embedder_factory = embedder_factory
        self._store: MemoryStore | None = None
        self._storage: Storage | None = None

    def tools(self) -> list[Tool]:
        return [
            MemorySearchTool(self),
            MemorySaveTool(self),
            MemoryUpdateTool(self),
            MemoryForgetTool(self),
        ]

    def store_for(self, storage: Storage) -> MemoryStore:
        if self._store is None or self._storage is not storage:
            self._store = self._build_store(storage)
            self._storage = storage
        return self._store

    def _build_store(self, storage: Storage) -> MemoryStore:
        if self._vectors and self._embedder_factory is not None:
            return IndexedMemoryStore(
                storage.relational,
                vectors=storage.vectors,
                embedder_factory=self._embedder_factory,
                clock=self._clock,
            )
        if self._clock is None:
            return MemoryStore(storage.relational)
        return MemoryStore(storage.relational, self._clock)

    def resolve(self, ctx: ToolContext) -> tuple[MemoryStore, str] | str:
        """The store and user for this call, or the one-line error to return."""
        if ctx.user_id is None:
            return _NO_USER
        if ctx.storage is None:
            return _NO_STORAGE
        return self.store_for(ctx.storage), ctx.user_id


class MemorySearchTool:
    name = "memory_search"
    description = (
        f"Search your saved memories about the user by words. {_ENGLISH} Check it "
        "before you plan work."
    )
    input_model = MemorySearchInput
    risk = "read"

    def __init__(self, memories: MemoryTools) -> None:
        self.tags = ["orchestrator", "memory"]
        self._memories = memories

    @property
    def memories(self) -> MemoryTools:
        return self._memories

    async def run(self, arguments: MemorySearchInput, ctx: ToolContext) -> str:
        resolved = self._memories.resolve(ctx)
        if isinstance(resolved, str):
            return resolved
        store, user_id = resolved
        hits = await store.search(user_id, arguments.query, arguments.k)
        return "\n".join(_block(hit) for hit in hits) or "no memories match"


class MemorySaveTool:
    name = "memory_save"
    description = (
        "Save a durable fact about the user or a lesson, not page content; call "
        "memory_search first and prefer memory_update to a near-duplicate. "
        f"{_ENGLISH}"
    )
    input_model = MemorySaveInput
    risk = "write"
    prompts = False

    def __init__(self, memories: MemoryTools) -> None:
        self.tags = ["orchestrator"]
        self._memories = memories

    async def run(self, arguments: MemorySaveInput, ctx: ToolContext) -> str:
        resolved = self._memories.resolve(ctx)
        if isinstance(resolved, str):
            return resolved
        store, user_id = resolved
        memory = await store.save(user_id, arguments.text, arguments.keywords)
        return f"saved memory #{memory.id} ({_stamps(memory)})"


class MemoryUpdateTool:
    name = "memory_update"
    description = (
        "Change the text and/or keywords of one saved memory by id and bump its "
        f"update time; use it to correct or merge memories. {_ENGLISH}"
    )
    input_model = MemoryUpdateInput
    risk = "write"
    prompts = False
    notify_result = True

    def __init__(self, memories: MemoryTools) -> None:
        self.tags = ["orchestrator"]
        self._memories = memories

    async def run(self, arguments: MemoryUpdateInput, ctx: ToolContext) -> str:
        resolved = self._memories.resolve(ctx)
        if isinstance(resolved, str):
            return resolved
        store, user_id = resolved
        memory = await store.update(
            user_id, arguments.id, arguments.text, arguments.keywords
        )
        if memory is None:
            return f"no memory #{arguments.id}"
        keywords = ", ".join(memory.keywords)
        return (
            f"updated memory #{memory.id}: {memory.text} "
            f"(keywords: {keywords}; {_stamps(memory)})"
        )


class MemoryForgetTool:
    name = "memory_forget"
    description = (
        "Delete one saved memory by id for good, when it is wrong, outdated or "
        f"merged into another. {_ENGLISH}"
    )
    input_model = MemoryForgetInput
    risk = "write"
    prompts = False

    def __init__(self, memories: MemoryTools) -> None:
        self.tags = ["orchestrator"]
        self._memories = memories

    async def run(self, arguments: MemoryForgetInput, ctx: ToolContext) -> str:
        resolved = self._memories.resolve(ctx)
        if isinstance(resolved, str):
            return resolved
        store, user_id = resolved
        if await store.forget(user_id, arguments.id):
            return f"forgot memory #{arguments.id}"
        return f"no memory #{arguments.id}"


def recall_store(tools: ToolRegistry, storage: Storage) -> MemoryStore | None:
    """The `MemoryStore` the registered memory tools share, or None when memory is off.

    The orchestrator's automatic recall reads through this store, so a recall and a
    `memory_search` call see the same data and the same migration state, and the
    orchestrator needs no new constructor parameter.
    """
    tool = tools.get(MemorySearchTool.name)
    if not isinstance(tool, MemorySearchTool):
        return None
    return tool.memories.store_for(storage)
