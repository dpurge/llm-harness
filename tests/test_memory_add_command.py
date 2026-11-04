"""`/memory add`: the parser and `handle_memory_text` with a scripted model.

Offline: a throwaway SQLite file, a real `Orchestrator` over a scripted provider (one
script per add call), the real tool registry. The front ends are tested in their own
files.
"""

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

import llm_harness.memory_command as memory_command_module
from llm_harness.builtin_tools import build_tool_registry
from llm_harness.config import StorageConfig, VectorConfig
from llm_harness.memory import MemoryStore, MemoryValidationError
from llm_harness.memory_add import PendingProposals
from llm_harness.memory_command import (
    MEMORY_OFF,
    NO_MODEL,
    NOT_ADMIN_ADD,
    USAGE,
    MemoryCommand,
    handle_memory_text,
    parse_memory_command,
)
from llm_harness.memory_tools import recall_store
from llm_harness.orchestrator import Orchestrator
from llm_harness.storage import Storage, open_storage
from llm_harness.types import (
    Done,
    ErrorEvent,
    StreamEvent,
    TextDelta,
    Usage,
    UsageReport,
)
from tests.fake_steps import ScriptedSteps
from tests.test_orchestrator import model_cfg

TWO = (
    ["Ann likes green tea", "Ann drinks tea in the morning"],
    ["tea, drink", "morning"],
)
SAVED_TWO = "saved 2 of 2 memories (#1, #2); skipped 0 already known"
CONFIRM_LINE = "/memory add confirm to save 2 notes, /memory add cancel to discard"
SECRET = "sk-abcdefgh12345678"
REPORT = UsageReport(
    model_id="m1",
    usage=Usage(input_tokens=10, output_tokens=5),
    cost_usd=None,
    seconds=0.5,
    session_cost_usd=0.0,
    session_cost_incomplete=False,
)


# --- the parser (criterion 1) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/memory add some text", MemoryCommand("add", query="some text")),
        (
            "/memory add --shared some text",
            MemoryCommand("add", query="some text", shared=True),
        ),
        ("/memory add confirm", MemoryCommand("add-confirm")),
        ("/memory add cancel", MemoryCommand("add-cancel")),
        ("/memory add confirm now", MemoryCommand("add", query="confirm now")),
        ("/memory add cancel it", MemoryCommand("add", query="cancel it")),
        ("/memory add", MemoryCommand("usage")),
        ("/memory add   ", MemoryCommand("usage")),
        ("/memory add --shared", MemoryCommand("usage")),
        ("/memory add --shared confirm", MemoryCommand("usage")),
        ("/memory add --shared cancel", MemoryCommand("usage")),
        (
            "/memory add --shared confirm now",
            MemoryCommand("add", query="confirm now", shared=True),
        ),
        # `--shared` only as the first word: later it is plain text
        (
            "/memory add see --shared",
            MemoryCommand("add", query="see --shared"),
        ),
        ("/memory add --sharedx a", MemoryCommand("add", query="--sharedx a")),
        # spacing inside the text is kept as typed
        (
            "/memory add a   b\tc  d",
            MemoryCommand("add", query="a   b\tc  d"),
        ),
        (
            "  /memory   add   --shared   x  y  ",
            MemoryCommand("add", query="x  y", shared=True),
        ),
        # the file forms
        (
            "/memory add @/tmp/a.md",
            MemoryCommand("add", query="/tmp/a.md", from_file=True),
        ),
        (
            "/memory add --shared @~/my notes.md",
            MemoryCommand("add", query="~/my notes.md", shared=True, from_file=True),
        ),
        ("/memory add @", MemoryCommand("usage")),
        ("/memory add @  ", MemoryCommand("usage")),
        ("/memory add --shared @", MemoryCommand("usage")),
        ("/memory add see @x", MemoryCommand("add", query="see @x")),
        ("/memory addx", MemoryCommand("usage")),
    ],
)
def test_parse_the_add_forms(text: str, expected: MemoryCommand) -> None:
    assert parse_memory_command(text) == expected


def test_usage_names_the_add_forms_and_the_file_form() -> None:
    lines = USAGE.splitlines()
    assert "/memory add [--shared] TEXT" in lines[0]
    (add_line,) = (line for line in lines if line.startswith("  add "))
    assert "@FILE" in add_line
    assert any("/memory add confirm" in line for line in lines)


# --- the rig -----------------------------------------------------------------------------


def proposals(notes: list[str], keywords: list[str]) -> list[StreamEvent]:
    return [TextDelta(text=json.dumps({"notes": notes, "keywords": keywords})), Done()]


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return open_storage(
        StorageConfig(vector=VectorConfig(backend="none")),
        tmp_path,
        relational_path=tmp_path / "memory.db",
    )


@dataclass
class Rig:
    storage: Storage
    provider: ScriptedSteps
    orchestrator: Orchestrator
    tools: object
    pending: PendingProposals

    async def say(
        self,
        text: str,
        *,
        user_id: str = "ann",
        admins: tuple[str, ...] = (),
        with_model: bool = True,
    ) -> str | None:
        return await handle_memory_text(
            text,
            self.tools,  # type: ignore[arg-type]
            self.storage,
            user_id,
            memory_admins=admins,
            propose=self.orchestrator.propose_memories if with_model else None,
            pending=self.pending,
        )

    async def listing(self, user_id: str) -> str:
        return str(await self.say("/memory", user_id=user_id))

    def requests(self) -> int:
        return len(self.provider.requests)


def rig(storage: Storage, *scripts: list[StreamEvent], user_id: str = "ann") -> Rig:
    tools = build_tool_registry([], memory=True, user_id=user_id)
    provider = ScriptedSteps(list(scripts))
    orchestrator = Orchestrator(
        provider, model_cfg(), tools=tools, storage=storage, user_id=user_id
    )
    return Rig(storage, provider, orchestrator, tools, PendingProposals())


# --- preview, confirm, cancel (14, 15, 18) -----------------------------------------------


async def test_add_shows_a_numbered_preview_and_stores_nothing(storage) -> None:
    r = rig(storage, [REPORT, *proposals(*TWO)])
    answer = await r.say("/memory add Ann likes tea")
    lines = answer.splitlines()
    assert lines[:3] == [
        "1. [tea, drink] Ann likes green tea",
        "2. [morning] Ann drinks tea in the morning",
        CONFIRM_LINE,
    ]
    assert "kept for this session only" in lines[3]
    assert lines[4].startswith("[m1 ")  # the usage line of the call
    assert await r.listing("ann") == "(no memories)"
    assert len(r.pending) == 2


async def test_confirm_saves_private_notes_and_clears_the_preview(storage) -> None:
    r = rig(storage, proposals(*TWO))
    await r.say("/memory add Ann likes tea")
    assert await r.say("/memory add confirm") == SAVED_TWO
    assert len(r.pending) == 0
    store = MemoryStore(storage.relational)
    assert [(m.text, m.shared) for m in await store.list("bob")] == []
    assert [(m.text, m.shared) for m in await store.list("ann")] == [
        ("Ann drinks tea in the morning", False),
        ("Ann likes green tea", False),
    ]
    assert await r.say("/memory add confirm") == "nothing to confirm"


async def test_cancel_discards_and_nothing_pending_answers_without_a_model(
    storage,
) -> None:
    r = rig(storage, proposals(*TWO))
    assert await r.say("/memory add cancel") == "nothing to cancel"
    assert await r.say("/memory add confirm") == "nothing to confirm"
    assert r.requests() == 0
    await r.say("/memory add Ann likes tea")
    assert await r.say("/memory add cancel") == "discarded 2 proposed memories"
    assert await r.say("/memory add confirm") == "nothing to confirm"
    assert await r.listing("ann") == "(no memories)"
    assert r.requests() == 1


async def test_confirm_and_cancel_work_without_a_model(storage) -> None:
    r = rig(storage, proposals(*TWO), proposals(*TWO))
    await r.say("/memory add Ann likes tea")
    assert await r.say("/memory add cancel", with_model=False) == (
        "discarded 2 proposed memories"
    )
    await r.say("/memory add Ann likes tea")
    assert await r.say("/memory add confirm", with_model=False) == SAVED_TWO


async def test_an_add_without_a_model_answers_the_front_ends_words(storage) -> None:
    r = rig(storage, proposals(*TWO))
    assert await r.say("/memory add x", with_model=False) == NO_MODEL
    answer = await handle_memory_text(
        "/memory add x",
        r.tools,  # type: ignore[arg-type]
        storage,
        "ann",
        no_model="no working model: pick another",
    )
    assert answer == "no working model: pick another"


# --- --shared (16, 17) ---------------------------------------------------------------------


@pytest.mark.parametrize("admins", [(), ("bob",)])
async def test_shared_add_by_a_non_admin_is_refused_before_any_model_call(
    storage, admins
) -> None:
    r = rig(storage, proposals(*TWO))
    assert await r.say("/memory add --shared Ann likes tea", admins=admins) == (
        NOT_ADMIN_ADD
    )
    assert r.requests() == 0
    assert len(r.pending) == 0


async def test_an_admin_adds_shared_notes_other_users_see(storage) -> None:
    r = rig(storage, proposals(*TWO))
    preview = await r.say("/memory add --shared Ann likes tea", admins=("ann",))
    assert preview.splitlines()[0] == "1. [shared] [tea, drink] Ann likes green tea"
    assert await r.say("/memory add confirm", admins=("ann",)) == SAVED_TWO
    assert (await r.listing("bob")).count("[shared]") == 2
    store = MemoryStore(storage.relational)
    assert all(m.shared for m in await store.list("ann"))


async def test_confirm_by_a_user_no_longer_admin_saves_nothing(storage) -> None:
    r = rig(storage, proposals(*TWO))
    await r.say("/memory add --shared Ann likes tea", admins=("ann",))
    assert await r.say("/memory add confirm", admins=()) == NOT_ADMIN_ADD
    assert await r.listing("ann") == "(no memories)"
    assert len(r.pending) == 0


async def test_confirm_by_another_user_saves_nothing(storage) -> None:
    r = rig(storage, proposals(*TWO))
    await r.say("/memory add Ann likes tea")
    answer = await r.say("/memory add confirm", user_id="bob")
    assert "another user" in answer
    assert await r.listing("ann") == "(no memories)"
    assert await r.listing("bob") == "(no memories)"


# --- replace, limits, failures (19, 20, 21) --------------------------------------------------


async def test_a_new_add_replaces_the_pending_preview(storage) -> None:
    second = proposals(["Ann owns a cat"], ["pet"])
    r = rig(storage, proposals(*TWO), second)
    await r.say("/memory add Ann likes tea")
    answer = await r.say("/memory add Ann has a cat")
    assert answer.splitlines()[0] == "replaced the pending preview of 2 notes"
    assert "1. [pet] Ann owns a cat" in answer
    assert await r.say("/memory add confirm") == (
        "saved 1 of 1 memories (#1); skipped 0 already known"
    )
    assert [m.text for m in await MemoryStore(storage.relational).list("ann")] == [
        "Ann owns a cat"
    ]


async def test_a_failed_add_discards_the_earlier_preview_and_says_so(storage) -> None:
    r = rig(storage, proposals(*TWO))
    await r.say("/memory add Ann likes tea")
    answer = await r.say("/memory add " + "x" * 4001)
    assert answer.splitlines() == [
        "text too long (4001 characters, at most 4000); add it in parts",
        "discarded the pending preview of 2 notes",
    ]
    assert await r.say("/memory add confirm") == "nothing to confirm"


async def test_text_over_4000_characters_is_refused_and_exactly_4000_is_not(
    storage,
) -> None:
    r = rig(storage, proposals(["Ann likes tea"], ["tea"]))
    answer = await r.say("/memory add " + "x" * 4001)
    assert answer == "text too long (4001 characters, at most 4000); add it in parts"
    assert r.requests() == 0 and len(r.pending) == 0
    answer = await r.say("/memory add " + "x" * 4000)
    assert answer.startswith("1. [tea] Ann likes tea")
    assert r.requests() == 1


async def test_the_model_sees_the_text_as_typed(storage) -> None:
    r = rig(storage, proposals(*TWO))
    await r.say("/memory add Ann   likes\ttea")
    content = r.provider.requests[0].messages[1].content
    assert "Ann   likes\ttea" in content


async def test_nothing_new_leaves_nothing_pending(storage) -> None:
    r = rig(storage, proposals([], []))
    answer = await r.say("/memory add Ann likes tea")
    assert answer.splitlines()[0] == "nothing new to add"
    assert len(r.pending) == 0
    assert await r.say("/memory add confirm") == "nothing to confirm"


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ([TextDelta(text="not json"), Done()], "Reply is not valid JSON"),
        (
            [ErrorEvent(kind="connection", message="down")],
            "provider error (connection): down",
        ),
    ],
)
async def test_a_bad_reply_or_a_provider_error_fails_with_nothing_pending(
    storage, script, reason
) -> None:
    r = rig(storage, script)
    answer = await r.say("/memory add Ann likes tea")
    assert answer.startswith("memory add failed: " + reason)
    assert len(r.pending) == 0


async def test_a_timeout_fails_with_nothing_pending(storage, monkeypatch) -> None:
    import llm_harness.orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "REFLECTION_TIMEOUT_SECONDS", 0.05)

    class Hang:
        async def stream_chat(self, req):
            await asyncio.Event().wait()
            yield Done()

        async def aclose(self) -> None:
            return None

    r = rig(storage)
    r.orchestrator._provider = Hang()
    answer = await r.say("/memory add Ann likes tea")
    assert answer == "memory add failed: no answer within 0.05 seconds"
    assert len(r.pending) == 0


async def test_memory_off_answers_before_any_work(storage, tmp_path) -> None:
    r = rig(storage, proposals(*TWO))
    note = tmp_path / "n.md"
    note.write_text("x")
    for line in (
        "/memory add Ann likes tea",
        f"/memory add @{note}",
        "/memory add confirm",
    ):
        answer = await handle_memory_text(
            line, None, None, "ann", propose=r.orchestrator.propose_memories
        )
        assert answer == MEMORY_OFF
    assert r.requests() == 0


# --- invalid proposed notes (22) -----------------------------------------------------------------


async def test_invalid_proposed_notes_are_dropped_without_an_echo(storage) -> None:
    notes = [
        "Ann likes tea",
        f"Ann's key is {SECRET} ok",
        "two\nlines",
        "y" * 1501,
        "Ann likes jam",
    ]
    keywords = ["tea", "x", "x", "x", "Bad_Keyword_Secret"]
    r = rig(storage, proposals(notes, keywords))
    answer = await r.say("/memory add whatever")
    assert answer.splitlines()[0] == "1. [tea] Ann likes tea"
    assert answer.splitlines()[1] == (
        "dropped 4 invalid notes: #2 looks like a secret, #3 not one line, "
        "#4 empty or over 1500 characters, #5 bad keywords"
    )
    assert SECRET not in answer and "Bad_Keyword" not in answer
    assert "Ann likes jam" not in answer
    assert await r.say("/memory add confirm") == (
        "saved 1 of 1 memories (#1); skipped 0 already known"
    )
    stored = [m.text for m in await MemoryStore(storage.relational).list("ann")]
    assert stored == ["Ann likes tea"]


async def test_all_invalid_notes_fail_with_the_reasons(storage) -> None:
    r = rig(storage, proposals([f"key {SECRET}", "a\nb"], ["k", "k"]))
    answer = await r.say("/memory add whatever")
    assert answer.splitlines()[:2] == [
        "memory add failed: no valid note in the reply",
        "dropped 2 invalid notes: #1 looks like a secret, #2 not one line",
    ]
    assert SECRET not in answer
    assert len(r.pending) == 0


# --- duplicates and partial failure (23) ---------------------------------------------------------------


async def test_exact_duplicates_are_skipped_own_and_shared(storage) -> None:
    store = MemoryStore(storage.relational)
    await store.save("ann", "Ann likes green tea", ["tea"])
    shared = await store.save(
        "bob", "Ann drinks tea in the morning", ["tea"], shared=True
    )
    assert shared.shared
    r = rig(storage, proposals(*TWO))
    await r.say("/memory add Ann likes tea")
    assert await r.say("/memory add confirm") == (
        "saved 0 of 2 memories; skipped 2 already known"
    )


async def test_a_note_that_fails_to_save_is_reported_and_the_rest_saved(
    storage, monkeypatch
) -> None:
    r = rig(storage, proposals(*TWO))
    store = recall_store(r.tools, storage)  # type: ignore[arg-type]
    real_save = store.save

    async def save(user_id, text, keywords, **kwargs):
        if "morning" in text:
            raise MemoryValidationError("refused by the store")
        return await real_save(user_id, text, keywords, **kwargs)

    monkeypatch.setattr(store, "save", save)
    await r.say("/memory add Ann likes tea")
    answer = await r.say("/memory add confirm")
    assert answer.splitlines() == [
        "saved 1 of 2 memories (#1); skipped 0 already known",
        "#2 not saved: refused by the store",
    ]
    assert len(r.pending) == 0


# --- the file form (24, 25, 26) -----------------------------------------------------------------------------


def write_note(tmp_path: Path, name: str = "my notes.md") -> Path:
    path = tmp_path / name
    path.write_bytes("﻿Ann likes tea\r\nand jam\r\n".encode())
    return path


async def test_a_file_add_previews_and_confirm_saves(storage, tmp_path) -> None:
    path = write_note(tmp_path)
    r = rig(storage, proposals(*TWO))
    answer = await r.say(f"/memory add @{path}")
    assert answer.splitlines()[0] == "1. [tea, drink] Ann likes green tea"
    content = r.provider.requests[0].messages[1].content
    assert "Ann likes tea\nand jam" in content  # CRLF and BOM are gone
    assert "﻿" not in content and "\r" not in content
    assert str(path) not in content and "my notes" not in content
    assert await r.say("/memory add confirm") == SAVED_TWO


async def test_a_second_file_add_replaces_the_preview(storage, tmp_path) -> None:
    path = write_note(tmp_path)
    r = rig(storage, proposals(*TWO), proposals(["Ann owns a cat"], ["pet"]))
    await r.say(f"/memory add @{path}")
    answer = await r.say(f"/memory add @{path}")
    assert answer.splitlines()[0] == "replaced the pending preview of 2 notes"


async def test_a_file_over_4000_characters_is_refused_without_a_model_call(
    storage, tmp_path
) -> None:
    path = tmp_path / "long.txt"
    path.write_text("x" * 4001)
    r = rig(storage, proposals(*TWO))
    answer = await r.say(f"/memory add @{path}")
    assert answer == "text too long (4001 characters, at most 4000); add it in parts"
    assert r.requests() == 0 and len(r.pending) == 0


@pytest.mark.parametrize("admins", [(), ("bob",)])
async def test_shared_file_add_by_a_non_admin_never_touches_the_file(
    storage, monkeypatch, admins
) -> None:
    def loader(path: str) -> str:
        raise AssertionError("the loader must not run for a non-admin")

    monkeypatch.setattr(memory_command_module, "read_text_file", loader)
    r = rig(storage, proposals(*TWO))
    answer = await r.say("/memory add --shared @/nonexistent/x.md", admins=admins)
    assert answer == NOT_ADMIN_ADD
    assert r.requests() == 0


async def test_an_admin_reads_the_file_for_a_shared_add(storage, tmp_path) -> None:
    path = write_note(tmp_path)
    r = rig(storage, proposals(*TWO))
    answer = await r.say(f"/memory add --shared @{path}", admins=("ann",))
    assert answer.splitlines()[0] == "1. [shared] [tea, drink] Ann likes green tea"


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("x.pdf", b"x", "only .txt, .md and .markdown files can be added"),
        ("missing.md", None, "no such file: {path}"),
        ("bad.md", b"\xff\xfe\x00", "file is not valid UTF-8 text"),
        ("empty.md", b"  \n", "file is empty"),
        ("big.md", b"x" * 65537, "file too large (65537 bytes, at most 65536)"),
    ],
)
async def test_each_file_refusal_reaches_the_user_as_one_line(
    storage, tmp_path, name, content, expected
) -> None:
    path = tmp_path / name
    if content is not None:
        path.write_bytes(content)
    r = rig(storage, proposals(*TWO))
    answer = await r.say(f"/memory add @{path}")
    assert answer == expected.format(path=path)
    assert r.requests() == 0 and len(r.pending) == 0


async def test_a_path_with_control_characters_is_shown_escaped(storage) -> None:
    r = rig(storage, proposals(*TWO))
    bidi = chr(0x202E)
    answer = await r.say(f"/memory add @/nonexistent/a\x1b[31mb{bidi}c.md")
    assert answer == "no such file: /nonexistent/a\\u001b[31mb\\u202ec.md"


async def test_the_loader_runs_in_a_worker_thread_before_the_model_call(
    storage, tmp_path, monkeypatch
) -> None:
    path = write_note(tmp_path)
    r = rig(storage, proposals(*TWO))
    real = memory_command_module.read_text_file
    events: list[tuple[str, int]] = []

    def spy(argument: str) -> str:
        events.append(("read", threading.get_ident()))
        return real(argument)

    monkeypatch.setattr(memory_command_module, "read_text_file", spy)
    real_propose = r.orchestrator.propose_memories

    async def propose(paste: str, shared: bool = False):
        events.append(("model", threading.get_ident()))
        return await real_propose(paste, shared)

    answer = await handle_memory_text(
        f"/memory add @{path}",
        r.tools,  # type: ignore[arg-type]
        storage,
        "ann",
        propose=propose,
        pending=r.pending,
    )
    assert answer is not None and answer.startswith("1. ")
    assert [name for name, _ in events] == ["read", "model"]
    assert events[0][1] != threading.get_ident()
    assert events[1][1] == threading.get_ident()
