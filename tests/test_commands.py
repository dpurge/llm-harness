"""The command table, `/help` parsing and answers, and the table's drift check."""

from pathlib import Path

import pytest

from llm_harness.cli import _EXIT_COMMANDS
from llm_harness.commands import (
    COMMANDS,
    FRONT_ENDS,
    HELP_COMMAND,
    UI_DEMO_COMMAND,
    HelpCommand,
    help_answer,
    help_detail,
    help_overview,
    parse_help,
)
from llm_harness.gui import inspector
from llm_harness.gui.app import _QUIT_COMMAND
from llm_harness.memory_command import COMMAND_WORD, USAGE
from llm_harness.state_files import EXPORT_COMMAND, IMPORT_COMMAND
from llm_harness.tui.app import _QUIT_COMMANDS


def overview_names(front_end: str) -> list[str]:
    return [
        line.split(" - ")[0].split(" ")[0]
        for line in help_overview(front_end).splitlines()
    ]


# Parsing


@pytest.mark.parametrize("text", ["/help", "/HELP", "/Help", "  /help  ", "/help\t"])
def test_help_alone_is_the_overview(text: str) -> None:
    assert parse_help(text) == HelpCommand(None)


def test_help_takes_the_first_word_after_it() -> None:
    assert parse_help("/help memory") == HelpCommand("memory")
    assert parse_help("/help extra words") == HelpCommand("extra")


@pytest.mark.parametrize(
    "text", ["/helpx", "help", "please /help", "", "  ", "/memory"]
)
def test_other_text_is_not_help(text: str) -> None:
    assert parse_help(text) is None


# Overview per front end


def test_gui_overview_lists_the_buttons_and_no_slash_export_import_or_exit() -> None:
    lines = help_overview("gui").splitlines()
    assert sorted(overview_names("gui")) == sorted(
        ["/memory", "/help", "/ui-demo", "/quit", "[button]", "[button]"]
    )
    buttons = [line for line in lines if line.startswith("[button] ")]
    assert [b.split(" - ")[0] for b in buttons] == [
        "[button] Export state",
        "[button] Import state",
    ]
    text = help_overview("gui")
    for absent in ("/export", "/import", "/exit"):
        assert absent not in text


def test_plain_overview_has_no_export_or_import() -> None:
    text = help_overview("plain")
    assert overview_names("plain") == ["/memory", "/help", "/quit", "/exit"]
    assert "/export" not in text and "/import" not in text
    assert "button" not in text


def test_tui_overview_lists_export_and_import_as_slash_commands() -> None:
    lines = help_overview("tui").splitlines()
    assert "/export [path] - save the orchestrator state to a file" in lines
    assert "/import <path> - load the orchestrator state from a file" in lines
    assert "button" not in help_overview("tui")


def test_every_overview_line_names_arguments_and_description() -> None:
    for front_end in FRONT_ENDS:
        for line in help_overview(front_end).splitlines():
            assert " - " in line


def test_an_unknown_front_end_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown front end"):
        help_overview("web")


# Detail


@pytest.mark.parametrize("front_end", sorted(FRONT_ENDS))
@pytest.mark.parametrize("name", ["memory", "/memory", "MEMORY", "/Memory"])
def test_memory_detail_is_exactly_the_memory_usage(front_end: str, name: str) -> None:
    assert help_detail(front_end, name) == USAGE


def test_gui_export_detail_names_the_button_and_denies_the_slash_command() -> None:
    for name, label in (("export", "Export state"), ("/IMPORT", "Import state")):
        detail = help_detail("gui", name)
        assert f'"{label}" button' in detail
        assert "no /" in detail and "slash command" in detail


def test_tui_export_detail_is_the_slash_command() -> None:
    assert help_detail("tui", "/export").startswith("usage: /export")
    assert help_detail("tui", "import").startswith("usage: /import <path>")


@pytest.mark.parametrize("name", ["quit", "help"])
def test_other_commands_have_their_own_detail(name: str) -> None:
    for front_end in FRONT_ENDS:
        assert help_detail(front_end, name).startswith(f"usage: /{name}")


def test_an_unknown_name_is_reported() -> None:
    assert help_detail("gui", "nope") == 'no help for "nope"; type /help'


@pytest.mark.parametrize(
    ("front_end", "name"), [("gui", "exit"), ("plain", "export"), ("plain", "/import")]
)
def test_a_command_the_front_end_lacks_is_reported(front_end: str, name: str) -> None:
    assert help_detail(front_end, name) == f'no help for "{name}"; type /help'


def test_the_unknown_name_is_escaped() -> None:
    escape, bidi = chr(0x1B), chr(0x202E)
    answer = help_detail("gui", f"bad{escape}[31m{bidi}")
    assert escape not in answer and bidi not in answer
    assert "\\u001b" in answer and "\\u202e" in answer


def test_extra_words_give_the_detail_of_the_first_word() -> None:
    request = parse_help("/help extra words")
    assert request is not None
    assert help_answer("gui", request) == 'no help for "extra"; type /help'
    memory = parse_help("/help memory list")
    assert memory is not None
    assert help_answer("gui", memory) == USAGE


def test_help_answer_without_a_topic_is_the_overview() -> None:
    assert help_answer("tui", HelpCommand(None)) == help_overview("tui")


# Table and drift


def slash_names(front_end: str) -> set[str]:
    return {c.name for c in COMMANDS if front_end in c.front_ends and not c.is_button}


def test_the_table_agrees_with_the_words_each_front_end_dispatches_on() -> None:
    dispatched = {
        "plain": set(_EXIT_COMMANDS) | {COMMAND_WORD, HELP_COMMAND},
        "tui": set(_QUIT_COMMANDS)
        | {COMMAND_WORD, EXPORT_COMMAND, IMPORT_COMMAND, HELP_COMMAND},
        "gui": {_QUIT_COMMAND, COMMAND_WORD, HELP_COMMAND, UI_DEMO_COMMAND},
    }
    assert set(dispatched) == FRONT_ENDS
    for front_end, words in dispatched.items():
        assert slash_names(front_end) == words, front_end


def test_gui_buttons_are_the_labels_the_state_tab_uses() -> None:
    source = Path(inspector.__file__).read_text(encoding="utf-8")
    for command in COMMANDS:
        if command.is_button:
            assert f'"{command.name}"' in source


def test_memory_detail_is_not_a_copy() -> None:
    (memory,) = [c for c in COMMANDS if c.name == COMMAND_WORD]
    assert memory.detail is USAGE
