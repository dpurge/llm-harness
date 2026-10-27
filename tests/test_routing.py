from datetime import UTC, datetime, timedelta

import pytest

from llm_harness.config import ModelConfig, Price
from llm_harness.routing import (
    BACKOFF_LADDER_MINUTES,
    FALLBACK_KINDS,
    BackoffEntry,
    backoff_delay,
    compute_cost,
    fallback_chain,
    is_backing_off,
)
from llm_harness.types import Usage

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def model(model_id: str, priority: float, *tags: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="p",
        model=model_id,
        tags=list(tags),
        priority=priority,
    )


def ids(models: list[ModelConfig]) -> list[str]:
    return [m.id for m in models]


def chain(models, first, tags=frozenset({"chat"})) -> list[str]:
    return ids(fallback_chain(models, first, tags))


def test_chain_orders_chat_models_by_priority_then_defaults():
    low = model("low", 1, "chat")
    high = model("high", 9, "chat")
    last = model("last", 5, "default")
    last_best = model("last_best", 7, "default")
    other = model("other", 100, "vision")
    models = [low, last, other, high, last_best]
    assert chain(models, low) == ["low", "high", "last_best", "last"]


def test_chain_ties_keep_models_yaml_order():
    a, b, c = (model(name, 5, "chat") for name in "abc")
    d, e = model("d", 3, "default"), model("e", 3, "default")
    assert chain([c, a, b, e, d], a) == ["a", "c", "b", "e", "d"]


def test_chain_has_no_duplicates_for_models_in_several_groups():
    both = model("both", 5, "chat", "default")
    first = model("first", 1, "chat", "default")
    assert chain([first, both], both) == ["both", "first"]


def test_first_model_is_always_first_even_when_untagged_or_not_listed():
    untagged = model("untagged", 0)
    chat = model("chat", 9, "chat")
    assert chain([chat, untagged], untagged) == ["untagged", "chat"]
    unlisted = model("unlisted", 0)
    assert chain([chat], unlisted) == ["unlisted", "chat"]


def test_first_model_keeps_its_place_despite_lower_priority():
    first = model("first", 1, "chat")
    better = model("better", 9, "chat")
    assert chain([better, first], first) == ["first", "better"]


def test_default_tagged_models_are_the_last_resort_without_the_required_tag():
    chat = model("chat", 1, "chat")
    fallback = model("fallback", 1, "default")
    assert chain([chat, fallback], chat) == ["chat", "fallback"]


def test_required_tags_parameter_selects_the_pool():
    coder = model("coder", 5, "code", "chat")
    chat = model("chat", 9, "chat")
    both = model("both", 1, "code", "chat")
    plain = model("plain", 7, "code")
    models = [coder, chat, both, plain]
    assert chain(models, chat, frozenset({"code"})) == [
        "chat",
        "plain",
        "coder",
        "both",
    ]
    assert chain(models, chat, frozenset({"code", "chat"})) == ["chat", "coder", "both"]
    assert chain(models, chat, frozenset()) == ["chat", "plain", "coder", "both"]


def test_fallback_kinds_are_the_five_triggers():
    assert FALLBACK_KINDS == {
        "connection",
        "rate_limit",
        "not_found",
        "unknown",
        "auth",
    }
    assert "bad_request" not in FALLBACK_KINDS


@pytest.mark.parametrize(
    ("failures", "minutes"),
    [(1, 1), (2, 2), (3, 3), (4, 5), (5, 10), (6, 15), (7, 30), (8, 60), (9, 60)],
)
def test_backoff_delay_ladder(failures: int, minutes: int):
    assert backoff_delay(failures) == timedelta(minutes=minutes)


def test_backoff_delay_is_capped_at_60_minutes():
    assert backoff_delay(1000) == timedelta(minutes=60)
    assert BACKOFF_LADDER_MINUTES == (1, 2, 3, 5, 10, 15, 30, 60)


def test_backoff_delay_rejects_zero_failures():
    with pytest.raises(ValueError, match="at least 1"):
        backoff_delay(0)


def entry(until: datetime) -> BackoffEntry:
    return BackoffEntry(model_id="m", failures=1, until=until)


def test_is_backing_off_before_at_and_after_until():
    until = NOW + timedelta(minutes=5)
    assert is_backing_off(entry(until), until - timedelta(seconds=1))
    assert not is_backing_off(entry(until), until)
    assert not is_backing_off(entry(until), until + timedelta(seconds=1))


def test_no_entry_is_not_backing_off():
    assert not is_backing_off(None, NOW)


def test_clock_moved_back_expires_an_entry_more_than_60_minutes_ahead():
    assert is_backing_off(entry(NOW + timedelta(minutes=60)), NOW)
    assert not is_backing_off(entry(NOW + timedelta(minutes=60, seconds=1)), NOW)
    assert not is_backing_off(entry(NOW + timedelta(days=2)), NOW)


def test_backoff_entry_is_frozen():
    with pytest.raises(AttributeError):
        entry(NOW).failures = 2  # type: ignore[misc]


def test_cost_formula_uses_both_prices():
    usage = Usage(input_tokens=1_000_000, output_tokens=500_000)
    price = Price(input_per_mtok=3, output_per_mtok=10)
    assert compute_cost(usage, price) == pytest.approx(3 + 5)


def test_cost_with_price_zero_is_zero_not_none():
    assert compute_cost(Usage(input_tokens=5, output_tokens=7), Price()) == 0


def test_omitted_price_means_zero_cost():
    price = Price(output_per_mtok=2)
    assert compute_cost(Usage(input_tokens=10, output_tokens=500_000), price) == 1.0


def test_cost_does_not_add_reasoning_tokens_again():
    usage = Usage(input_tokens=0, output_tokens=1_000_000, reasoning_tokens=400_000)
    assert compute_cost(usage, Price(output_per_mtok=1)) == 1.0


def test_unknown_usage_gives_none_never_zero():
    assert compute_cost(None, Price()) is None
    assert compute_cost(Usage(), Price()) is None
    assert compute_cost(Usage(input_tokens=1), Price()) is None
    assert compute_cost(Usage(output_tokens=1), Price()) is None
