import pytest

from llm_harness.display import format_switch, format_usage_report, sanitize_text
from llm_harness.types import ModelSwitch, Usage, UsageReport


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("plain text", "plain text"),
        ("keeps\nnewline\ttab", "keeps\nnewline\ttab"),
        ("line\roverwritten", "lineoverwritten"),
        ("crlf\r\nnext", "crlf\nnext"),
        ("a\x1b[31mb", "a[31mb"),
        ("bell\x07 null\x00", "bell null"),
        ("del\x7f c1\x80\x9b\x9f", "del c1"),
        ("unicode é 日本  ", "unicode é 日本  "),
        ("", ""),
    ],
)
def test_sanitize_text_drops_control_characters_only(raw: str, clean: str) -> None:
    assert sanitize_text(raw) == clean


REMOVED = [
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\u2028",
    "\u2029",
    "\r",
]
KEPT = [
    "\u200d",  # ZWJ
    "\u200c",  # ZWNJ
    "\u200e",  # LRM
    "\u200f",  # RLM
    "\u061c",  # ALM
    "\ufe0f",  # variation selector-16
    "\ufe0e",  # variation selector-15
    "\U000e0100",  # variation selector supplement
]


@pytest.mark.parametrize("char", REMOVED)
def test_sanitize_text_removes_spoofing_character(char: str) -> None:
    assert sanitize_text(f"a{char}b") == "ab"


@pytest.mark.parametrize("char", KEPT)
def test_sanitize_text_keeps_legitimate_format_character(char: str) -> None:
    assert sanitize_text(f"a{char}b") == f"a{char}b"


@pytest.mark.parametrize(
    "sample",
    [
        "می\u200cخواهم",  # Persian with ZWNJ
        "مرحبا بالعالم",  # Arabic
        "שלום עולם",  # Hebrew
        "こんにちは世界",  # Japanese
        "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",  # family
        "\U0001f1e9\U0001f1ea",  # flag
        "\u2764\ufe0f",  # heart with variation selector
    ],
)
def test_sanitize_text_passes_real_scripts_unchanged(sample: str) -> None:
    assert sanitize_text(sample) == sample


def report(**overrides: object) -> UsageReport:
    fields: dict[str, object] = {
        "model_id": "gemma-local",
        "usage": Usage(input_tokens=12, output_tokens=31),
        "cost_usd": 0.0,
        "seconds": 2.14,
        "session_cost_usd": 0.0,
        "session_cost_incomplete": False,
    }
    return UsageReport(**{**fields, **overrides})  # type: ignore[arg-type]


def test_usage_report_matches_the_spec_example() -> None:
    assert (
        format_usage_report(report())
        == "[gemma-local tokens in=12 out=31 $0.00 2.1s | session $0.00]"
    )


@pytest.mark.parametrize(
    ("usage", "tokens"),
    [
        (Usage(input_tokens=3, output_tokens=7), "tokens in=3 out=7"),
        (
            Usage(input_tokens=7, output_tokens=34, reasoning_tokens=33),
            "tokens in=7 out=34 reasoning=33",
        ),
        (Usage(input_tokens=0, output_tokens=2), "tokens in=0 out=2"),
        (Usage(input_tokens=5), "tokens in=5"),
        (Usage(output_tokens=5), "tokens out=5"),
        (Usage(), "tokens ?"),
        (None, "tokens ?"),
    ],
)
def test_usage_report_tokens_skip_missing_counts_and_show_unknown(
    usage: Usage | None, tokens: str
) -> None:
    assert f" {tokens} $0.00 " in format_usage_report(report(usage=usage))


@pytest.mark.parametrize(
    ("cost", "shown"),
    [
        (0, "$0.00"),
        (0.0, "$0.00"),
        (0.01, "$0.01"),
        (0.0149, "$0.01"),
        (12.5, "$12.50"),
        (0.0003, "$0.0003"),
        (0.00994, "$0.0099"),
        (None, "$?"),
    ],
)
def test_usage_report_cost_format(cost: float | None, shown: str) -> None:
    assert f" {shown} 2.1s |" in format_usage_report(report(cost_usd=cost))


@pytest.mark.parametrize(
    ("total", "incomplete", "shown"),
    [
        (0.0, False, "session $0.00]"),
        (0.12, True, "session $0.12+?]"),
        (0.0, True, "session $0.00+?]"),
        (0.0003, False, "session $0.0003]"),
        (12.5, False, "session $12.50]"),
    ],
)
def test_usage_report_session_total_format(
    total: float, incomplete: bool, shown: str
) -> None:
    line = format_usage_report(
        report(session_cost_usd=total, session_cost_incomplete=incomplete)
    )
    assert line.endswith(f"| {shown}")


def test_usage_report_seconds_have_one_decimal() -> None:
    assert " 0.0s |" in format_usage_report(report(seconds=0.04))
    assert " 61.6s |" in format_usage_report(report(seconds=61.56))


def test_usage_report_is_sanitized() -> None:
    line = format_usage_report(report(model_id="a\x1b[31mb\u202ec\r"))
    assert line.startswith("[a[31mbc tokens")
    assert "\x1b" not in line and "\u202e" not in line and "\r" not in line


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("connection", "gemma-gpu unavailable (connection); using gemma-local"),
        ("rate_limit", "gemma-gpu unavailable (rate_limit); using gemma-local"),
        ("not_found", "gemma-gpu unavailable (not_found); using gemma-local"),
        ("auth", "gemma-gpu unavailable (auth); using gemma-local"),
        ("unknown", "gemma-gpu unavailable (unknown); using gemma-local"),
        ("backoff", "gemma-gpu is in backoff; using gemma-local"),
        ("config", "gemma-gpu is not available (configuration); using gemma-local"),
    ],
)
def test_switch_note_per_reason(reason: str, expected: str) -> None:
    switch = ModelSwitch(
        from_model_id="gemma-gpu",
        reason=reason,  # type: ignore[arg-type]
        to_model_id="gemma-local",
    )
    assert format_switch(switch) == expected


def test_switch_note_is_sanitized() -> None:
    switch = ModelSwitch(
        from_model_id="a\x1b[31m", reason="connection", to_model_id="b\u202e\x07"
    )
    assert format_switch(switch) == "a[31m unavailable (connection); using b"
