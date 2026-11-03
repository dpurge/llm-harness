"""The `/memory` slash command: list, show and delete saved memories by hand.

Pure and front-end neutral: the plain CLI, the terminal UI and the web GUI each
call `handle_memory_text` before a line reaches the orchestrator, so a `/memory`
line is never sent to the model and never becomes a user turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from llm_harness.display import sanitize_text
from llm_harness.memory import MemoryStorageError
from llm_harness.memory_index import IndexedMemoryStore
from llm_harness.memory_tools import recall_store

if TYPE_CHECKING:
    from llm_harness.memory import Memory, MemoryStore
    from llm_harness.storage import Storage
    from llm_harness.tools import ToolRegistry

COMMAND_WORD = "/memory"
MEMORY_OFF = "memory is off: set [tools] memory = true and user_id in config.toml"
VECTORS_OFF = "vector search is off: set [tools] memory_vectors = true"
USAGE = (
    "usage: /memory [list [PAGE]] | /memory show ID | /memory delete ID | /memory reindex\n"
    "  list    one line per memory, newest first, 20 per page\n"
    "  show    the full text of one memory\n"
    "  delete  forget one memory for good\n"
    "  reindex rebuild the vector index of your memories"
)
_PREVIEW_CHARS = 80
_DELETED_CHARS = 200
_LIST_ALL = 1_000_000
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")


@dataclass(frozen=True)
class MemoryCommand:
    action: str  # "list", "show", "delete", "reindex" or "usage"
    number: int = 1  # the page for list, the memory id for show and delete


def _positive_integer(word: str) -> int | None:
    # ASCII digits only: `str.isdigit` accepts superscripts and other scripts.
    return int(word) if _POSITIVE_INTEGER.fullmatch(word) else None


def parse_memory_command(text: str) -> MemoryCommand | None:
    """The command a line spells, or None when the line is an ordinary message."""
    words = text.split()
    if not words or words[0] != COMMAND_WORD:
        return None
    arguments = words[1:]
    if not arguments:
        return MemoryCommand("list")
    verb, rest = arguments[0], arguments[1:]
    if verb == "list" and len(rest) <= 1:
        if not rest:
            return MemoryCommand("list")
        page = _positive_integer(rest[0])
        return MemoryCommand("list", page) if page is not None else _usage()
    if verb in ("show", "delete") and len(rest) == 1:
        memory_id = _positive_integer(rest[0])
        return MemoryCommand(verb, memory_id) if memory_id is not None else _usage()
    if verb == "reindex" and not rest:
        return MemoryCommand("reindex")
    return _usage()


def _usage() -> MemoryCommand:
    return MemoryCommand("usage")


def _escape(text: str, *, keep_newlines: bool = False) -> str:
    """Show control, bidi and separator characters as `\\uXXXX`; never pass them on."""
    shown = []
    for ch in text:
        is_kept_whitespace = (keep_newlines and ch in "\n\t") or ch == " "
        if is_kept_whitespace or (ch.isprintable() and sanitize_text(ch) == ch):
            shown.append(ch)
        else:
            shown.append(
                f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}"
            )
    return "".join(shown)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _keywords(memory: Memory) -> str:
    return _escape(", ".join(memory.keywords))


def _list_line(memory: Memory) -> str:
    preview = _escape(_clip(memory.text, _PREVIEW_CHARS))
    return (
        f"#{memory.id} [{_keywords(memory)}] "
        f"(updated {_escape(memory.updated_at)}) {preview}"
    )


async def _list(store: MemoryStore, user_id: str, page: int, page_size: int) -> str:
    # The store has no count, so read the user's entries once and slice; a
    # personal memory list is small.
    entries = await store.list(user_id, limit=_LIST_ALL)
    if not entries:
        return "(no memories)"
    pages = -(-len(entries) // page_size)
    if page > pages:
        return f"no page {page}: there {'is' if pages == 1 else 'are'} {pages} page(s)"
    start = (page - 1) * page_size
    lines = [_list_line(entry) for entry in entries[start : start + page_size]]
    lines.append(f"page {page} of {pages}")
    if page < pages:
        lines.append(f"next page: /memory list {page + 1}")
    return "\n".join(lines)


async def _show(store: MemoryStore, user_id: str, memory_id: int) -> str:
    memory = await store.get(user_id, memory_id)
    if memory is None:
        return f"no memory #{memory_id}"
    return "\n".join(
        [
            f"#{memory.id}",
            f"keywords: {_keywords(memory)}",
            f"created: {_escape(memory.created_at)}",
            f"updated: {_escape(memory.updated_at)}",
            _escape(memory.text, keep_newlines=True),
        ]
    )


async def _delete(store: MemoryStore, user_id: str, memory_id: int) -> str:
    memory = await store.get(user_id, memory_id)
    if memory is None or not await store.forget(user_id, memory_id):
        return f"no memory #{memory_id}"
    return f"deleted memory #{memory_id}: {_escape(_clip(memory.text, _DELETED_CHARS))}"


async def _reindex(store: MemoryStore, user_id: str) -> str:
    if not isinstance(store, IndexedMemoryStore):
        return VECTORS_OFF
    if store.index_off_reason is not None:
        store.resume()  # a failed retry turns the index off again with its new reason
    counts = await store.reindex(user_id)
    if counts is None:
        return f"vector search is off: {_escape(store.index_off_reason or 'unknown')}"
    indexed, unindexed = counts
    return f"reindexed {indexed} of {unindexed} memories"


async def run_memory_command(
    store: MemoryStore,
    user_id: str,
    command: MemoryCommand,
    *,
    page_size: int = 20,
) -> str:
    """The plain-text answer; every action is limited to `user_id`'s memories."""
    if command.action == "list":
        return await _list(store, user_id, command.number, page_size)
    if command.action == "show":
        return await _show(store, user_id, command.number)
    if command.action == "delete":
        return await _delete(store, user_id, command.number)
    if command.action == "reindex":
        return await _reindex(store, user_id)
    return USAGE


async def handle_memory_text(
    text: str,
    tools: ToolRegistry | None,
    storage: Storage | None,
    user_id: str | None,
) -> str | None:
    """None when `text` is not a `/memory` command; else the answer to show."""
    command = parse_memory_command(text)
    if command is None:
        return None
    if command.action == "usage":
        return USAGE
    store = (
        recall_store(tools, storage)
        if tools is not None and storage is not None and user_id
        else None
    )
    if store is None or not user_id:
        return MEMORY_OFF
    try:
        answer = await run_memory_command(store, user_id, command)
    except MemoryStorageError as error:
        return str(error)  # the type name only; no front end catches an exception here
    notice = store.take_notice()
    if notice and command.action == "reindex" and "vector search is off" in answer:
        return answer  # the answer already gives the reason
    return f"{answer}\n{notice}" if notice else answer
