"""The helpers and the memory commands of the shared chat session."""

from pathlib import Path

from llm_harness.chat.session import (
    MemoryCommands,
    error_event_line,
    is_approval_yes,
    status_text,
    unexpected_error_line,
)
from llm_harness.config.settings import HarnessConfig
from llm_harness.core.redact import REDACTED
from llm_harness.core.types import ErrorEvent, StatusEvent
from llm_harness.memory.command import MEMORY_OFF
from tests.frontends.tui.test_tui import memory_front_end

CONFIG = HarnessConfig.model_validate(
    {
        "providers": {
            "local": {"type": "ollama", "base_url": "http://localhost:11434"}
        },
        "models": [
            {
                "id": "m1",
                "name": "Model One",
                "provider": "local",
                "model": "provider-model-1",
                "tags": ["default"],
                "priority": 1,
            }
        ],
    }
)


def test_unexpected_error_line_is_one_masked_line() -> None:
    line = unexpected_error_line(RuntimeError("kaboom sk-CANARY-123456789"))
    assert line == f"error (unexpected): RuntimeError: kaboom {REDACTED}"


def test_error_event_line_names_the_kind_and_strips_control_characters() -> None:
    event = ErrorEvent(kind="rate_limit", message="slow\x1b[31m down")
    assert error_event_line(event) == "error (rate_limit): slow[31m down"


def test_status_text_has_no_brackets() -> None:
    assert (
        status_text(StatusEvent(step=2, action_type="respond", text="reading"))
        == "step 2: reading"
    )


def test_only_y_and_yes_approve() -> None:
    assert is_approval_yes(" Y ")
    assert is_approval_yes("yes")
    assert not is_approval_yes("")
    assert not is_approval_yes("yep")
    assert not is_approval_yes("no")


async def test_memory_answer_is_none_for_a_non_memory_text() -> None:
    commands = MemoryCommands(None, None, CONFIG)
    assert await commands.answer("hello") is None


async def test_memory_off_answers_before_the_model_matters() -> None:
    commands = MemoryCommands(None, None, CONFIG)
    assert await commands.answer("/memory list") == MEMORY_OFF


async def test_memory_add_without_an_orchestrator_answers_the_no_model_text(
    tmp_path: Path,
) -> None:
    config, tools, storage = await memory_front_end(tmp_path, "alice", [])
    try:
        commands = MemoryCommands(tools, storage, config, no_model="no model here")
        assert await commands.answer("/memory add Alice likes tea") == "no model here"
        assert await commands.answer("/memory add cancel") == "nothing to cancel"
    finally:
        await storage.close()
