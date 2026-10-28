"""YAML export and import of RunState: the pure module, with hostile files."""

import datetime as dt
import time

import pytest
import yaml

from llm_harness.state import (
    Environment,
    Finding,
    OrchestratorState,
    PlanStep,
    RunState,
)
from llm_harness.state_io import (
    MAX_IMPORT_BYTES,
    StateImportError,
    export_yaml,
    import_yaml,
)

PLUS_ONE_HOUR = dt.timezone(dt.timedelta(hours=1))


def make_run_state(**state_fields) -> RunState:
    environment = Environment(
        now=dt.datetime.now(PLUS_ONE_HOUR),
        user_name="alice",
        computer_name="box",
    )
    return RunState(
        state=OrchestratorState(environment=environment, **state_fields),
        pending_question="Which file?",
    )


def minimal(**overrides) -> dict:
    """A valid dump to damage one field of."""
    data = make_run_state().model_dump(mode="json")
    data.update(overrides)
    return data


def to_bytes(data: object) -> bytes:
    return yaml.safe_dump(data).encode("utf-8")


def rejection(data: bytes) -> str:
    with pytest.raises(StateImportError) as caught:
        import_yaml(data)
    message = str(caught.value)
    assert message and "\n" not in message and "\r" not in message
    return message


# Round trip


def test_a_state_with_a_tz_aware_now_round_trips_to_an_equal_value() -> None:
    original = make_run_state(
        goal="find the port",
        findings=[Finding(text="port is 8080", source="config.yaml")],
        plan=[PlanStep(text="read config", status="doing")],
        turn=2,
        step=3,
    )
    loaded = import_yaml(export_yaml(original))
    assert loaded == original
    assert loaded.state.environment.now.utcoffset() == dt.timedelta(hours=1)


def test_a_file_exported_before_user_id_existed_still_imports_with_none() -> None:
    data = minimal()
    del data["state"]["environment"]["user_id"]
    loaded = import_yaml(to_bytes(data))
    assert loaded.state.environment.user_id is None


def test_a_user_id_round_trips() -> None:
    original = make_run_state()
    environment = original.state.environment.model_copy(update={"user_id": "ann"})
    original = original.model_copy(
        update={"state": original.state.model_copy(update={"environment": environment})}
    )
    assert import_yaml(export_yaml(original)) == original


def test_the_export_is_plain_yaml_without_python_tags() -> None:
    text = export_yaml(make_run_state(goal="g")).decode("utf-8")
    assert "!!" not in text
    assert "python" not in text
    assert yaml.safe_load(text)["pending_question"] == "Which file?"


def test_non_ascii_text_round_trips() -> None:
    original = make_run_state(goal="Zażółć gęślą jaźń ☃")
    assert import_yaml(export_yaml(original)) == original


# Safe loading


def test_a_python_object_tag_is_rejected_and_nothing_is_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: calls.append(args))
    payload = b"!!python/object/apply:builtins.print ['SENTINEL']\n"
    message = rejection(payload)
    assert calls == []
    assert "SENTINEL" not in message


def test_a_python_tag_inside_the_state_is_rejected_too() -> None:
    payload = (
        b"state:\n  goal: !!python/object/apply:os.getcwd []\npending_question: null\n"
    )
    rejection(payload)


def test_yaml_aliases_are_rejected_before_they_can_expand() -> None:
    lines = ["a: &a [x,x,x,x,x,x,x,x,x]"]
    previous = "a"
    for name in "bcdefghijkl":  # 9**11 items if it were expanded
        lines.append(f"{name}: &{name} [{','.join('*' + previous for _ in range(9))}]")
        previous = name
    lines.append("state: {findings: *l}")
    started = time.monotonic()
    assert rejection("\n".join(lines).encode()) == "YAML aliases are not supported"
    assert time.monotonic() - started < 1


def test_very_deep_nesting_is_one_line_not_a_crash() -> None:
    depth = 100_000
    rejection(b"[" * depth + b"]" * depth)


# Size


def test_a_file_one_byte_over_the_cap_is_rejected_by_size_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("parsed a file over the cap")

    monkeypatch.setattr(yaml, "load", forbidden)
    assert "larger than 1000000 bytes" in rejection(b" " * (MAX_IMPORT_BYTES + 1))


def test_a_file_of_exactly_the_cap_is_judged_by_its_content_not_its_size() -> None:
    padded = export_yaml(make_run_state()) + b"#" * MAX_IMPORT_BYTES
    padded = padded[:MAX_IMPORT_BYTES]
    assert len(padded) == MAX_IMPORT_BYTES
    assert import_yaml(padded).pending_question == "Which file?"
    # The same size of junk fails on the schema, not on size.
    assert "larger" not in rejection(b"x" * MAX_IMPORT_BYTES)


# One safe line per rejection


def test_an_unknown_key_is_rejected_with_its_path() -> None:
    data = minimal()
    data["state"]["surprise"] = 1
    assert rejection(to_bytes(data)) == "state.surprise: Extra inputs are not permitted"


def test_a_wrong_type_is_rejected_without_echoing_the_value() -> None:
    data = minimal()
    data["state"]["turn"] = "SECRET-sk-CANARY-123456789"
    message = rejection(to_bytes(data))
    assert message.startswith("state.turn: ")
    assert "CANARY" not in message


def test_a_naive_now_is_rejected() -> None:
    data = minimal()
    data["state"]["environment"]["now"] = "2026-10-28T13:42:13"
    assert rejection(to_bytes(data)).startswith("state.environment.now: ")


def test_a_blank_finding_source_is_rejected() -> None:
    data = minimal()
    data["state"]["findings"] = [{"text": "t", "source": "   "}]
    assert rejection(to_bytes(data)).startswith("state.findings.0.source: ")


def test_a_file_that_is_not_utf8_is_rejected() -> None:
    assert rejection(b"\xff\xfe\x00bad") == "file is not UTF-8 text"


def test_a_non_mapping_root_and_an_empty_file_are_rejected() -> None:
    assert rejection(b"- 1\n- 2\n") == "top level must be a mapping"
    assert rejection(b"just text") == "top level must be a mapping"
    assert rejection(b"") == "top level must be a mapping"


def test_broken_yaml_does_not_quote_the_file() -> None:
    message = rejection(b"a: [unclosed SECRET-LINE\nb: }\n")
    assert "SECRET-LINE" not in message


def test_many_errors_are_cut_to_one_short_line() -> None:
    data = minimal()
    for index in range(50):
        data["state"][f"extra{index}"] = index
    message = rejection(to_bytes(data))
    assert message.endswith("47 more")
    assert len(message) < 400


def test_a_hostile_key_name_is_one_sanitised_line_with_a_secret_masked() -> None:
    data = minimal()
    bidi = chr(0x202E)
    data["state"][f"evil\n\x1b[31m{bidi} key sk-CANARY-1234567890abcdef"] = 1
    message = rejection(to_bytes(data))
    assert "\x1b" not in message and bidi not in message
    assert len(message) <= 260
