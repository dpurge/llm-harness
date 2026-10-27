"""Text helpers shared by the front ends."""

from llm_harness.types import ModelSwitch, Usage, UsageReport

# Format characters that reorder or relocate text on screen. Other format
# characters stay: ZWJ/ZWNJ (Persian, Arabic, emoji sequences), LRM/RLM/ALM and
# variation selectors are needed to render real text.
_SPOOFING_CHARS = frozenset(
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u2028\u2029"
)


def sanitize_text(text: str) -> str:
    """Drop characters that let model output drive or spoof the terminal.

    Removes ESC, C0 (except newline and tab; CR would overwrite the current
    line), DEL, C1, bidi overrides/embeddings/isolates and line/paragraph
    separators.
    """
    return "".join(
        ch
        for ch in text
        if ch in "\n\t"
        or not (ch < " " or "\x7f" <= ch <= "\x9f" or ch in _SPOOFING_CHARS)
    )


def _format_cost(cost: float) -> str:
    if cost == 0 or cost >= 0.01:
        return f"${cost:.2f}"
    return f"${cost:.4f}"


def _format_tokens(usage: Usage | None) -> str:
    if usage is None or (usage.input_tokens is None and usage.output_tokens is None):
        return "tokens ?"
    fields = [
        ("in", usage.input_tokens),
        ("out", usage.output_tokens),
        ("reasoning", usage.reasoning_tokens),
    ]
    return "tokens " + " ".join(
        f"{name}={value}" for name, value in fields if value is not None
    )


def format_usage_report(report: UsageReport) -> str:
    cost = "$?" if report.cost_usd is None else _format_cost(report.cost_usd)
    total = _format_cost(report.session_cost_usd)
    if report.session_cost_incomplete:
        total += "+?"
    line = (
        f"[{report.model_id} {_format_tokens(report.usage)} {cost} "
        f"{report.seconds:.1f}s | session {total}]"
    )
    # The model id comes from the config file, so it is not trusted terminal text.
    return sanitize_text(line)


def format_switch(switch: ModelSwitch) -> str:
    source, target = switch.from_model_id, switch.to_model_id
    if switch.reason == "backoff":
        line = f"{source} is in backoff; using {target}"
    elif switch.reason == "config":
        line = f"{source} is not available (configuration); using {target}"
    else:
        line = f"{source} unavailable ({switch.reason}); using {target}"
    return sanitize_text(line)
