"""The memories YAML file: the writer and the strict reader of `/memory export|import`.

Pure: bytes in, bytes out, no I/O. The file is untrusted, so the reader parses through
the shared no-alias `SafeLoader` (size cap, strict UTF-8, no aliases or custom tags),
checks the key sets and the structure, and caps the entries. It executes and trusts
nothing: each entry comes back raw, and the text, keywords and timestamps are
validated later, per entry, by the importer and the store. A rejected file raises
`MemoriesFileError` with ONE fixed phrase, never built from the file's values.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import yaml

from llm_harness.memory import Memory
from llm_harness.state_io import StateImportError, load_untrusted_yaml

FORMAT_VERSION = 1
MAX_ENTRIES = 200  # one embedding call per saved note when vector search is on
MEMORIES_FILENAME = "llm-harness-memories.yaml"

_TOP_KEYS = frozenset({"version", "exported", "memories"})
_ENTRY_KEYS = frozenset({"id", "text", "keywords", "created", "updated", "shared"})
_MAX_REPORTED_VERSION = 10**9  # a huge int would make `str` slow or raise


class MemoriesFileError(ValueError):
    """The file was rejected; `str(exc)` is one safe line with no newline."""


@dataclass(frozen=True)
class TransferEntry:
    """One entry as read: raw values, not yet validated (`id` is informational)."""

    text: object
    keywords: object
    created: object = None
    updated: object = None
    shared: bool = False
    id: int | None = None


def export_memories(memories: Sequence[Memory], exported_at: str) -> bytes:
    """The notes as a YAML file, ascending by id; `shared` only for a shared note.

    The file carries no `user_id`: the importer's notes become the importer's own.
    """
    entries = []
    for memory in sorted(memories, key=lambda note: note.id):
        entry: dict[str, object] = {
            "id": memory.id,
            "text": memory.text,
            "keywords": list(memory.keywords),
            "created": memory.created_at,
            "updated": memory.updated_at,
        }
        if memory.shared:
            entry["shared"] = True
        entries.append(entry)
    document = {
        "version": FORMAT_VERSION,
        "exported": exported_at,
        "memories": entries,
    }
    text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    return text.encode("utf-8")


def parse_memories_file(data: bytes) -> list[TransferEntry]:
    """The entries of an uploaded file; `MemoriesFileError` on any structural problem."""
    try:
        loaded = load_untrusted_yaml(data)
    except StateImportError as error:
        raise MemoriesFileError(str(error)) from None
    if not isinstance(loaded, dict):
        raise MemoriesFileError("top level must be a mapping")
    if not _TOP_KEYS.issuperset(loaded):
        raise MemoriesFileError("unknown key in the memories file")
    _check_version(loaded)
    entries = loaded.get("memories")
    if not isinstance(entries, list):
        raise MemoriesFileError("memories must be a list")
    if len(entries) > MAX_ENTRIES:
        raise MemoriesFileError(
            f"too many memories ({len(entries)}, at most {MAX_ENTRIES}); split the file"
        )
    return [_entry(item) for item in entries]


def _check_version(loaded: dict) -> None:
    if "version" not in loaded:
        raise MemoriesFileError("memories file has no version")
    version = loaded["version"]
    if type(version) is int and version == FORMAT_VERSION:
        return
    if type(version) is int and abs(version) < _MAX_REPORTED_VERSION:
        raise MemoriesFileError(f"unsupported memories file version {version}")
    raise MemoriesFileError("unsupported memories file version")


def _entry(item: object) -> TransferEntry:
    if not isinstance(item, dict):
        raise MemoriesFileError("every memory must be a mapping")
    if not _ENTRY_KEYS.issuperset(item):
        raise MemoriesFileError("unknown key in a memory")
    if not isinstance(item.get("text"), str):
        raise MemoriesFileError("memory text must be a string")
    if not isinstance(item.get("keywords"), list):
        raise MemoriesFileError("memory keywords must be a list")
    note_id = item.get("id")
    if note_id is not None and type(note_id) is not int:
        raise MemoriesFileError("memory id must be an integer")
    shared = item.get("shared", False)
    if not isinstance(shared, bool):
        raise MemoriesFileError("memory shared must be true or false")
    return TransferEntry(
        text=item["text"],
        keywords=item["keywords"],
        created=item.get("created"),
        updated=item.get("updated"),
        shared=shared,
        id=note_id,
    )
