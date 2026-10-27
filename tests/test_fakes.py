import pytest

from llm_harness.types import (
    Done,
    ErrorEvent,
    ModelSwitch,
    Notice,
    TextDelta,
    ThinkingDelta,
    Usage,
    UsageEvent,
    UsageReport,
)
from tests.fakes import assert_stream_contract

USAGE = UsageEvent(usage=Usage(input_tokens=1, output_tokens=2))
DONE = Done(finish_reason="stop")
SWITCH = ModelSwitch(from_model_id="a", reason="connection", to_model_id="b")
NOTICE = Notice(text="note")
REPORT = UsageReport(
    model_id="b",
    usage=None,
    cost_usd=None,
    seconds=1.0,
    session_cost_usd=0.0,
    session_cost_incomplete=True,
)


@pytest.mark.parametrize(
    "events",
    [
        [DONE],
        [ErrorEvent(kind="auth", message="boom")],
        [TextDelta(text="a"), DONE],
        [ThinkingDelta(text="t"), TextDelta(text="a"), USAGE, DONE],
        [TextDelta(text="a"), USAGE, ErrorEvent(kind="auth", message="boom")],
        [USAGE, DONE],
        [NOTICE, SWITCH, TextDelta(text="a"), REPORT, DONE],
        [SWITCH, SWITCH, REPORT, DONE],
        [NOTICE, SWITCH, ErrorEvent(kind="unknown", message="no model available")],
    ],
)
def test_contract_accepts_well_ordered_streams(events: list) -> None:
    assert_stream_contract(events)


@pytest.mark.parametrize(
    "events",
    [
        [TextDelta(text="a"), USAGE, TextDelta(text="b"), DONE],
        [USAGE, ThinkingDelta(text="t"), DONE],
        [TextDelta(text="a"), USAGE, USAGE, DONE],
        [TextDelta(text="a")],
        [DONE, TextDelta(text="a")],
        [DONE, DONE],
        [TextDelta(text="a"), DONE, USAGE],
        [TextDelta(text="a"), SWITCH, DONE],
        [TextDelta(text="a"), NOTICE, DONE],
        [TextDelta(text="a"), REPORT, REPORT, DONE],
        [REPORT, TextDelta(text="a"), DONE],
        [REPORT, SWITCH, DONE],
    ],
)
def test_contract_rejects_misordered_streams(events: list) -> None:
    with pytest.raises(AssertionError):
        assert_stream_contract(events)
