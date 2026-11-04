"""The chat commands and the answers to `/help`.

One table lists every command and the front ends (plain, tui, gui) that support
it, so `/help` shows only what the current front end can do. The pure functions
here take and return text; each front end shows the answer its own way.
"""

from __future__ import annotations

from dataclasses import dataclass

from llm_harness.memory_command import COMMAND_WORD as MEMORY_WORD
from llm_harness.memory_command import USAGE as MEMORY_USAGE
from llm_harness.memory_command import escape_text
from llm_harness.state_files import EXPORT_COMMAND, IMPORT_COMMAND

HELP_COMMAND = "/help"
PLAIN = "plain"
TUI = "tui"
GUI = "gui"
FRONT_ENDS = frozenset({PLAIN, TUI, GUI})


@dataclass(frozen=True)
class Command:
    name: str  # what is typed (a slash command) or the label shown (a button)
    arguments: str
    description: str  # one line
    front_ends: frozenset[str]
    detail: str
    topic: str  # the word `/help` looks up: lower case, no slash
    is_button: bool = False  # a GUI button rather than a slash command


@dataclass(frozen=True)
class HelpCommand:
    topic: str | None  # the first word after `/help` as typed; None for the overview


_ALL = frozenset({PLAIN, TUI, GUI})
_PLAIN_AND_TUI = frozenset({PLAIN, TUI})
_ONLY_TUI = frozenset({TUI})
_ONLY_GUI = frozenset({GUI})

COMMANDS: tuple[Command, ...] = (
    Command(
        MEMORY_WORD,
        "[list|search|show|delete|publish|unpublish|forget|reindex|add|export|import]",
        "list, search, show, delete, publish, forget, reindex, add, export and import your memories",
        _ALL,
        MEMORY_USAGE,  # the one source of the memory usage text; never copied
        "memory",
    ),
    Command(
        HELP_COMMAND,
        "[COMMAND]",
        "list the commands; /help COMMAND shows the detail of one",
        _ALL,
        "usage: /help [COMMAND]\n"
        "  /help          one line per command of this front end\n"
        "  /help COMMAND  the detail of one command, with or without the slash",
        "help",
    ),
    Command(
        "/quit",
        "",
        "leave the chat",
        _ALL,
        "usage: /quit\n  leave the chat",
        "quit",
    ),
    Command(
        "/exit",
        "",
        "leave the chat (same as /quit)",
        _PLAIN_AND_TUI,
        "usage: /exit\n  leave the chat (same as /quit)",
        "exit",
    ),
    Command(
        EXPORT_COMMAND,
        "[path]",
        "save the orchestrator state to a file",
        _ONLY_TUI,
        f"usage: {EXPORT_COMMAND} [path]\n  save the orchestrator state to a YAML file",
        "export",
    ),
    Command(
        IMPORT_COMMAND,
        "<path>",
        "load the orchestrator state from a file",
        _ONLY_TUI,
        f"usage: {IMPORT_COMMAND} <path>\n"
        "  load the orchestrator state from a YAML file",
        "import",
    ),
    Command(
        "Export state",
        "",
        "button in the State tab: save the orchestrator state",
        _ONLY_GUI,
        'In the web GUI, use the "Export state" button in the State tab.\n'
        f"There is no {EXPORT_COMMAND} slash command here.",
        "export",
        is_button=True,
    ),
    Command(
        "Import state",
        "",
        "button in the State tab: load the orchestrator state",
        _ONLY_GUI,
        'In the web GUI, use the "Import state" button in the State tab.\n'
        f"There is no {IMPORT_COMMAND} slash command here.",
        "import",
        is_button=True,
    ),
)


def parse_help(text: str) -> HelpCommand | None:
    """The help request a line spells, or None for an ordinary message.

    Only the FIRST word counts (any case); `/helpx` is a different word.
    """
    words = text.split()
    if not words or words[0].lower() != HELP_COMMAND:
        return None
    return HelpCommand(words[1] if len(words) > 1 else None)


def help_overview(front_end: str) -> str:
    """One line per command the front end supports."""
    lines = []
    for command in _commands_for(front_end):
        label = f"[button] {command.name}" if command.is_button else command.name
        head = f"{label} {command.arguments}".rstrip()
        lines.append(f"{head} - {command.description}")
    return "\n".join(lines)


def help_detail(front_end: str, name: str) -> str:
    """The detail of one command; `name` has an optional slash, in any case."""
    topic = name.strip().removeprefix("/").lower()
    for command in _commands_for(front_end):
        if command.topic == topic:
            return command.detail
    return f'no help for "{escape_text(name)}"; type {HELP_COMMAND}'


def help_answer(front_end: str, request: HelpCommand) -> str:
    """The text to show for a parsed `/help` line."""
    if request.topic is None:
        return help_overview(front_end)
    return help_detail(front_end, request.topic)


def _commands_for(front_end: str) -> tuple[Command, ...]:
    if front_end not in FRONT_ENDS:
        raise ValueError(f"unknown front end {front_end!r}")
    return tuple(c for c in COMMANDS if front_end in c.front_ends)
