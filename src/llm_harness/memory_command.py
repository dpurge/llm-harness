"""The `/memory` slash command: list, show and delete saved memories by hand.

Pure and front-end neutral: the plain CLI, the terminal UI and the web GUI each
call `handle_memory_text` before a line reaches the orchestrator, so a `/memory`
line is never sent to the model and never becomes a user turn.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from llm_harness.display import escape_text, format_usage_report
from llm_harness.memory import (
    MAX_SEARCH_RESULTS,
    MAX_TEXT_CHARS,
    MemoryStorageError,
    MemoryValidationError,
    check_timestamp,
    validate_note,
)
from llm_harness.memory_add import PendingProposals, ProposedNote
from llm_harness.memory_file import TextFileError, check_text_length, read_text_file
from llm_harness.memory_index import IndexedMemoryStore
from llm_harness.memory_recall import SHARED_MARKER
from llm_harness.memory_tools import recall_store
from llm_harness.memory_transfer import (
    MAX_ENTRIES,
    MEMORIES_FILENAME,
    MemoriesFileError,
    TransferEntry,
    export_memories,
    parse_memories_file,
)
from llm_harness.state_files import (
    StateFileError,
    read_import,
    resolve_path,
    write_export,
)
from llm_harness.state_io import StateImportError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from llm_harness.memory import Memory, MemoryStore
    from llm_harness.memory_add import ProposeResult
    from llm_harness.storage import Storage
    from llm_harness.tools import ToolRegistry

COMMAND_WORD = "/memory"
MEMORY_OFF = "memory is off: set [tools] memory = true and user_id in config.toml"
VECTORS_OFF = "vector search is off: set [tools] memory_vectors = true"
USAGE = (
    "usage: /memory [list [PAGE]] | /memory show ID | /memory delete ID | /memory publish ID | /memory unpublish ID | /memory forget ID | /memory search WORDS | /memory reindex [--all] | /memory add [--shared] TEXT | /memory export [--shared] [PATH] | /memory import [--shared] PATH\n"
    "  list    one line per memory, newest first, 20 per page\n"
    "  search  the memories a search finds, best first, at most 10\n"
    "  show    the full text of one memory\n"
    "  delete  forget one memory for good\n"
    "  publish   make one of your memories shared with every user (memory admins only)\n"
    "  unpublish make a shared memory of yours private again (memory admins only)\n"
    "  forget  memory admins only: delete one shared memory for every user at once\n"
    "  reindex rebuild the vector index of your memories\n"
    "          --all re-embeds every memory into the current vector store\n"
    "  add     one model call proposes only the new facts of TEXT, or of a .txt, .md or .markdown file (/memory add [--shared] @FILE), as a preview\n"
    "          /memory add confirm saves the preview, /memory add cancel discards it; it is kept for this session only\n"
    "          --shared proposes shared memories (memory admins only)\n"
    "  export  write your memories to a YAML file (default llm-harness-memories.yaml), never overwriting; --shared adds the shared ones (memory admins only)\n"
    "  import  read a YAML file of memories, immediately: adds the entries as your own notes and skips known ones; --shared keeps shared entries shared (memory admins only)\n"
    "          these are not the state /export and /import of the terminal UI"
)
_PREVIEW_CHARS = 80
_DELETED_CHARS = 200
_LIST_ALL = 1_000_000
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")
_SHARED_FLAG = "--shared"
_ADD_WORDS = ("confirm", "cancel")  # reserved: never the text of an add
# Actions that wait on a model, a file or many saves: the GUI and the TUI run them in
# the task slot of a turn, so Stop and Escape cancel them.
SLOW_ACTIONS = ("add", "export", "import")


@dataclass(frozen=True)
class MemoryCommand:
    action: (
        str  # "list", "show", "delete", "publish", "unpublish", "forget", "search",
        # "reindex", "reindex-all", "add", "add-confirm", "add-cancel", "export",
        # "import" or "usage"
    )
    number: int = (
        1  # the page for list, the memory id for show, delete, (un)publish, forget
    )
    query: str = ""  # the words to search for; for add, the text or the path as typed;
    # for export and import, the path as typed (empty export path: the default name)
    shared: bool = False  # add: propose shared memories; export, import: shared notes
    from_file: bool = False  # add: `query` is the path of a text file


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
    if verb in ("show", "delete", "publish", "unpublish", "forget") and len(rest) == 1:
        memory_id = _positive_integer(rest[0])
        return MemoryCommand(verb, memory_id) if memory_id is not None else _usage()
    if verb == "search":
        return MemoryCommand("search", query=" ".join(rest)) if rest else _usage()
    if verb == "reindex" and not rest:
        return MemoryCommand("reindex")
    if verb == "reindex" and rest == ["--all"]:
        return MemoryCommand("reindex-all")
    if verb == "add":
        return _parse_add(text)
    if verb in ("export", "import"):
        return _parse_transfer(text, verb)
    return _usage()


def _parse_add(text: str) -> MemoryCommand:
    """The add forms; the text is the rest of the line as typed, not re-joined words."""
    parts = text.split(None, 2)  # ["/memory", "add", rest as typed]
    rest = parts[2].strip() if len(parts) > 2 else ""
    shared = rest.split(None, 1)[0] == _SHARED_FLAG if rest else False
    if shared:
        pieces = rest.split(None, 1)
        rest = pieces[1].strip() if len(pieces) > 1 else ""
    if not rest or (shared and rest in _ADD_WORDS):
        return _usage()
    if not shared and rest == "confirm":
        return MemoryCommand("add-confirm")
    if not shared and rest == "cancel":
        return MemoryCommand("add-cancel")
    if rest.startswith("@"):
        path = rest[1:].strip()
        if not path:
            return _usage()
        return MemoryCommand("add", query=path, shared=shared, from_file=True)
    return MemoryCommand("add", query=rest, shared=shared)


def _parse_transfer(text: str, verb: str) -> MemoryCommand:
    """The export and import forms; the path is the rest of the line as typed."""
    parts = text.split(None, 2)  # ["/memory", verb, rest as typed]
    rest = parts[2].strip() if len(parts) > 2 else ""
    shared = rest.split(None, 1)[0] == _SHARED_FLAG if rest else False
    if shared:
        pieces = rest.split(None, 1)
        rest = pieces[1].strip() if len(pieces) > 1 else ""
    if verb == "import" and not rest:
        return _usage()
    return MemoryCommand(verb, query=rest, shared=shared)


def _usage() -> MemoryCommand:
    return MemoryCommand("usage")


_escape = escape_text  # the body lives in `display`, so `memory_file` can use it


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _keywords(memory: Memory) -> str:
    return _escape(", ".join(memory.keywords))


def _marker(memory: Memory) -> str:
    return f"{SHARED_MARKER} " if memory.shared else ""


def _list_line(memory: Memory) -> str:
    preview = _escape(_clip(memory.text, _PREVIEW_CHARS))
    return (
        f"#{memory.id} {_marker(memory)}[{_keywords(memory)}] "
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
            f"#{memory.id} {SHARED_MARKER}" if memory.shared else f"#{memory.id}",
            f"keywords: {_keywords(memory)}",
            f"created: {_escape(memory.created_at)}",
            f"updated: {_escape(memory.updated_at)}",
            _escape(memory.text, keep_newlines=True),
        ]
    )


async def _delete(store: MemoryStore, user_id: str, memory_id: int) -> str:
    memory = await store.get(user_id, memory_id)
    if memory is None:
        return f"no memory #{memory_id}"
    if memory.shared:  # `get` finds it; `forget` would only say "no memory"
        if memory.user_id == user_id:
            return f"memory #{memory_id} is shared; unpublish it first"
        return f"memory #{memory_id} is shared and read-only"
    if not await store.forget(user_id, memory_id):
        return f"no memory #{memory_id}"
    return f"deleted memory #{memory_id}: {_escape(_clip(memory.text, _DELETED_CHARS))}"


NOT_ADMIN = "only memory admins may publish or unpublish shared memories"


async def _publish(
    store: MemoryStore,
    user_id: str,
    memory_id: int,
    admins: Sequence[str],
    *,
    shared: bool,
) -> str:
    # Admin first: a non-admin never causes a note lookup. The owner check comes
    # before anything else is said, so a private note of another user is never revealed.
    if user_id not in admins:
        return NOT_ADMIN
    memory = await store.get(user_id, memory_id)
    if memory is None or memory.user_id != user_id:
        return f"no memory #{memory_id}"
    if memory.shared == shared:
        return (
            f"memory #{memory_id} is already shared"
            if shared
            else (f"memory #{memory_id} is not shared")
        )
    if await store.set_shared(user_id, memory_id, shared) is None:
        return f"no memory #{memory_id}"
    return f"{'published' if shared else 'unpublished'} memory #{memory_id}"


NOT_ADMIN_FORGET = "only memory admins may forget shared memories"


async def _forget(
    store: MemoryStore, user_id: str, memory_id: int, admins: Sequence[str]
) -> str:
    # Admin first: a non-admin never causes a note lookup. Another user's private
    # note answers like an unknown id, so it is never revealed.
    if user_id not in admins:
        return NOT_ADMIN_FORGET
    memory = await store.get(user_id, memory_id)
    if memory is None:
        return f"no memory #{memory_id}"
    if not memory.shared:
        if memory.user_id != user_id:
            return f"no memory #{memory_id}"
        return f"memory #{memory_id} is not shared; use /memory delete {memory_id}"
    forgotten = await store.forget_shared(memory_id)
    if forgotten is None:  # another admin forgot it between the lookup and now
        return f"no memory #{memory_id}"
    return (
        f"forgot shared memory #{memory_id}: "
        f"{_escape(_clip(forgotten.text, _DELETED_CHARS))}"
    )


async def _search(store: MemoryStore, user_id: str, query: str) -> str:
    hits = await store.search(user_id, query, MAX_SEARCH_RESULTS)
    if not hits:
        return f'no memories match "{_escape(_clip(query, _PREVIEW_CHARS))}"'
    return "\n".join(_list_line(hit) for hit in hits)


async def _reindex(store: MemoryStore, user_id: str, *, rebuild: bool = False) -> str:
    if not isinstance(store, IndexedMemoryStore):
        return VECTORS_OFF
    if store.index_off_reason is not None:
        store.resume()  # a failed retry turns the index off again with its new reason
    counts = await store.reindex(user_id, rebuild=rebuild)
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
    memory_admins: Sequence[str] = (),
) -> str:
    """The plain-text answer; every action is limited to `user_id`'s memories."""
    if command.action == "list":
        return await _list(store, user_id, command.number, page_size)
    if command.action == "show":
        return await _show(store, user_id, command.number)
    if command.action == "delete":
        return await _delete(store, user_id, command.number)
    if command.action in ("publish", "unpublish"):
        return await _publish(
            store,
            user_id,
            command.number,
            memory_admins,
            shared=command.action == "publish",
        )
    if command.action == "forget":
        return await _forget(store, user_id, command.number, memory_admins)
    if command.action == "search":
        return await _search(store, user_id, command.query)
    if command.action == "reindex":
        return await _reindex(store, user_id)
    if command.action == "reindex-all":
        return await _reindex(store, user_id, rebuild=True)
    return USAGE


NOT_ADMIN_ADD = "only memory admins may add shared memories"
NO_MODEL = "no working model"
SESSION_ONLY = "the preview is kept for this session only"
_REASON_TEXT = f"empty or over {MAX_TEXT_CHARS} characters"
_REASON_LINE = "not one line"
_REASON_SECRET = "looks like a secret"
_REASON_KEYWORDS = "bad keywords"


def _invalid_reason(note: ProposedNote) -> str | None:
    """A FIXED phrase for the class a note fails; never the store's message.

    The store's keyword message echoes the keyword, so no message is passed on.
    """
    try:
        validate_note(note.text, ["ok"])
    except MemoryValidationError:
        text = note.text.strip()
        if not 1 <= len(text) <= MAX_TEXT_CHARS:
            return _REASON_TEXT
        if any(unicodedata.category(char) == "Cc" for char in text):
            return _REASON_LINE
        return _REASON_SECRET
    try:
        validate_note("ok", note.keywords)
    except MemoryValidationError:
        return _REASON_KEYWORDS
    return None


def _dropped_line(dropped: Sequence[tuple[int, str]]) -> str:
    reasons = ", ".join(f"#{number} {reason}" for number, reason in dropped)
    return f"dropped {len(dropped)} invalid notes: {reasons}"


def _preview(notes: Sequence[ProposedNote], *, shared: bool) -> list[str]:
    marker = f"{SHARED_MARKER} " if shared else ""
    return [
        f"{number}. {marker}[{_escape(', '.join(note.keywords))}] {_escape(note.text)}"
        for number, note in enumerate(notes, 1)
    ]


async def _add_text(command: MemoryCommand) -> str:
    """The text to analyse: the paste, or the file read off the event loop."""
    if command.from_file:
        text = await asyncio.to_thread(read_text_file, command.query)
    else:
        text = command.query
    check_text_length(text)
    return text


async def _add(
    command: MemoryCommand,
    user_id: str,
    admins: Sequence[str],
    propose: Callable[[str, bool], Awaitable[ProposeResult]] | None,
    pending: PendingProposals,
    no_model: str,
) -> str:
    """Propose notes for the text; keep them in `pending` when there is a preview.

    Order: the admin check, then the file, then the model. A non-admin never causes a
    file read or a model call. Any add replaces an earlier preview, so a cancelled or
    failed one leaves nothing pending.
    """
    replaced = len(pending)
    pending.clear()
    answer = await _add_answer(command, user_id, admins, propose, pending, no_model)
    if not replaced:
        return answer
    if len(pending):
        return f"replaced the pending preview of {replaced} notes\n{answer}"
    return f"{answer}\ndiscarded the pending preview of {replaced} notes"


async def _add_answer(
    command: MemoryCommand,
    user_id: str,
    admins: Sequence[str],
    propose: Callable[[str, bool], Awaitable[ProposeResult]] | None,
    pending: PendingProposals,
    no_model: str,
) -> str:
    if command.shared and user_id not in admins:
        return NOT_ADMIN_ADD
    if propose is None:
        return no_model
    try:
        text = await _add_text(command)
    except TextFileError as error:
        return str(error)  # one safe line: the path is already escaped
    result = await propose(text, command.shared)
    usage = [format_usage_report(result.report)] if result.report else []
    if result.off:
        return MEMORY_OFF
    if result.error is not None:
        return "\n".join([f"memory add failed: {_escape(result.error)}", *usage])
    if not result.notes:
        return "\n".join(["nothing new to add", *usage])
    valid: list[ProposedNote] = []
    dropped: list[tuple[int, str]] = []
    for number, note in enumerate(result.notes, 1):
        reason = _invalid_reason(note)
        if reason is not None:
            dropped.append((number, reason))
            continue
        clean_text, keywords = validate_note(note.text, note.keywords)
        valid.append(ProposedNote(clean_text, keywords))
    if not valid:
        return "\n".join(
            [
                "memory add failed: no valid note in the reply",
                _dropped_line(dropped),
                *usage,
            ]
        )
    pending.set(user_id, command.shared, valid)
    lines = _preview(valid, shared=command.shared)
    if dropped:
        lines.append(_dropped_line(dropped))
    lines.append(
        f"/memory add confirm to save {len(valid)} notes, /memory add cancel to discard"
    )
    lines.append(SESSION_ONLY)
    return "\n".join([*lines, *usage])


async def _save_note(
    store: MemoryStore, user_id: str, note: ProposedNote, shared: bool
):
    """The saved memory, or None when its exact text is already known."""
    if await store.has_text(user_id, note.text):
        return None
    return await store.save(user_id, note.text, note.keywords, shared=shared)


async def _confirm(
    store: MemoryStore,
    user_id: str,
    admins: Sequence[str],
    pending: PendingProposals,
) -> str:
    """Save the pending notes one by one; the preview is cleared whatever the result.

    Everything is re-checked: the user, the admin list for a shared preview, and each
    note (a preview is only a proposal).
    """
    if not len(pending):
        return "nothing to confirm"
    owner, shared, notes = pending.user_id, pending.shared, pending.notes
    pending.clear()
    if owner != user_id:
        return "the pending preview belongs to another user; nothing was saved"
    if shared and user_id not in admins:
        return NOT_ADMIN_ADD
    saved: list[int] = []
    skipped = 0
    problems: list[str] = []
    for number, note in enumerate(notes, 1):
        reason = _invalid_reason(note)
        if reason is not None:
            problems.append(f"#{number} not saved: {reason}")
            continue
        try:
            memory = await _save_note(store, user_id, note, shared=shared)
        except MemoryValidationError as error:
            problems.append(
                f"#{number} not saved: {_escape(_clip(str(error), _PREVIEW_CHARS))}"
            )
            continue
        except MemoryStorageError as error:
            problems.append(f"#{number} not saved: {error}")  # the type name only
            continue
        if memory is None:
            skipped += 1
        else:
            saved.append(memory.id)
    ids = f" ({', '.join(f'#{memory_id}' for memory_id in saved)})" if saved else ""
    summary = f"saved {len(saved)} of {len(notes)} memories{ids}; skipped {skipped} already known"
    return "\n".join([summary, *problems])


NOT_ADMIN_EXPORT = "only memory admins may export shared memories"
NOT_ADMIN_IMPORT = "only memory admins may import shared memories"
NO_MEMORIES = "no memories to export"
_EXPORT_OVER_IMPORT_CAP = f"; import takes at most {MAX_ENTRIES} per file"


def _transfer_path(query: str) -> Path | None:
    """The file to use; the default export name when empty; None for a bad `~user`."""
    if not query:
        return Path.cwd() / MEMORIES_FILENAME
    try:
        return resolve_path(query)
    except RuntimeError:  # `~user` of an unknown user
        return None


async def _export(
    store: MemoryStore, user_id: str, command: MemoryCommand, admins: Sequence[str]
) -> str:
    # Admin first: a non-admin causes no store call and no filesystem access.
    if command.shared and user_id not in admins:
        return NOT_ADMIN_EXPORT
    notes = [
        note
        for note in await store.list(user_id, limit=_LIST_ALL)
        if note.user_id == user_id or (command.shared and note.shared)
    ]
    if not notes:
        return NO_MEMORIES
    path = _transfer_path(command.query)
    if path is None:
        return "cannot write the file: unknown user in the path"
    data = export_memories(notes, store.current_timestamp())
    try:
        await asyncio.to_thread(write_export, path, data)
    except StateFileError as error:
        return str(error)  # one safe line
    answer = f"exported {len(notes)} memories to {_escape(str(path))}"
    return answer + _EXPORT_OVER_IMPORT_CAP if len(notes) > MAX_ENTRIES else answer


def _read_entries(path: Path) -> list[TransferEntry]:
    """Read and parse in one worker thread; every failure is one safe line."""
    return parse_memories_file(read_import(path))


def _timestamps(entry: TransferEntry, now: str) -> tuple[str | None, str | None, bool]:
    """(created, updated, bad): a missing one stays None; a bad pair is dropped.

    The same rules as the store (`check_timestamp`), so a bad or future pair is counted
    and replaced by the clock instead of making the store raise.
    """
    try:
        created = None if entry.created is None else check_timestamp(entry.created, now)
        updated = None if entry.updated is None else check_timestamp(entry.updated, now)
    except MemoryValidationError:
        return None, None, True
    if created is not None and updated is not None and updated < created:
        return None, None, True
    return created, updated, False


async def _import(
    store: MemoryStore, user_id: str, command: MemoryCommand, admins: Sequence[str]
) -> str:
    """Save the file's entries as the caller's own notes, one by one.

    Order: the admin check, then the file, then the saves. A non-admin never causes a
    file read. Ids are ignored; a `shared` entry is shared only with `--shared`.
    """
    if command.shared and user_id not in admins:
        return NOT_ADMIN_IMPORT
    path = _transfer_path(command.query)
    if path is None:
        return "no such file"
    try:
        entries = await asyncio.to_thread(_read_entries, path)
    except (StateFileError, StateImportError, MemoriesFileError) as error:
        return str(error)  # one safe line, never the path or the file's content
    imported = known = rejected = made_personal = replaced_time = 0
    for entry in entries:
        try:
            text, keywords = validate_note(entry.text, entry.keywords)
        except MemoryValidationError:
            rejected += 1  # the message may quote the note: never shown
            continue
        if await store.has_text(user_id, text):
            known += 1
            continue
        created, updated, bad = _timestamps(entry, store.current_timestamp())
        shared = entry.shared and command.shared
        await store.save(
            user_id,
            text,
            list(keywords),
            shared=shared,
            created_at=created,
            updated_at=updated,
        )
        imported += 1
        made_personal += entry.shared and not shared
        replaced_time += bad
    answer = (
        f"imported {imported} of {len(entries)} memories; "
        f"skipped {known} already known; rejected {rejected} invalid"
    )
    if made_personal:
        answer += f"; {made_personal} shared entries imported as personal"
    if replaced_time:
        answer += (
            f"; {replaced_time} entries got the current time (bad or future timestamps)"
        )
    return answer


def _cancel(pending: PendingProposals) -> str:
    count = len(pending)
    if not count:
        return "nothing to cancel"
    pending.clear()
    return f"discarded {count} proposed memories"


async def handle_memory_text(
    text: str,
    tools: ToolRegistry | None,
    storage: Storage | None,
    user_id: str | None,
    *,
    memory_admins: Sequence[str] = (),
    propose: Callable[[str, bool], Awaitable[ProposeResult]] | None = None,
    pending: PendingProposals | None = None,
    no_model: str = NO_MODEL,
) -> str | None:
    """None when `text` is not a `/memory` command; else the answer to show.

    `memory_admins` are the `user_id` values allowed to publish and to add shared
    notes; callers pass `config.tools.memory_admins`. `propose` is the bound
    `Orchestrator.propose_memories` (None when the front end has no working model: an
    add then answers `no_model`; confirm and cancel need no model) and `pending` is the
    front end's holder of the preview that waits for `confirm`; this function keeps no
    state. Only human input reaches this function.
    """
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
    holder = pending if pending is not None else PendingProposals()
    try:
        if command.action == "add":
            answer = await _add(
                command, user_id, memory_admins, propose, holder, no_model
            )
        elif command.action == "add-confirm":
            answer = await _confirm(store, user_id, memory_admins, holder)
        elif command.action == "add-cancel":
            answer = _cancel(holder)
        elif command.action == "export":
            answer = await _export(store, user_id, command, memory_admins)
        elif command.action == "import":
            answer = await _import(store, user_id, command, memory_admins)
        else:
            answer = await run_memory_command(
                store, user_id, command, memory_admins=memory_admins
            )
    except MemoryStorageError as error:
        return str(error)  # the type name only; no front end catches an exception here
    notice = store.take_notice()
    if (
        notice
        and command.action in ("reindex", "reindex-all")
        and "vector search is off" in answer
    ):
        return answer  # the answer already gives the reason
    return f"{answer}\n{notice}" if notice else answer
