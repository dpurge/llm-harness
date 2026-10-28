"""`environment_text`: the Environment lines shared by the GUI and TUI state tabs."""

from datetime import UTC, datetime

from llm_harness.inspect_state import environment_text
from llm_harness.state import Environment, OrchestratorState

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def state_with(user_id: str | None) -> OrchestratorState:
    return OrchestratorState(
        environment=Environment(
            now=NOW, user_name="u", computer_name="c", user_id=user_id
        )
    )


def test_environment_text_shows_the_user_id_after_the_computer_name() -> None:
    assert environment_text(state_with("ann"), None) == (
        f"now: {NOW.isoformat()}\nuser_name: u\ncomputer_name: c\nuser_id: ann"
    )


def test_environment_text_says_when_the_user_id_is_not_set() -> None:
    assert environment_text(state_with(None), None).endswith("\nuser_id: (not set)")


def test_environment_text_puts_the_pending_question_after_the_user_id() -> None:
    text = environment_text(state_with("ann"), "Which file?")
    assert text.endswith("\nuser_id: ann\npending question: Which file?")


def test_environment_text_sanitizes_the_user_id() -> None:
    text = environment_text(state_with("a\x1b[31mb"), None)
    assert "\x1b" not in text
