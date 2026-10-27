"""Pure routing rules: fallback chain, backoff ladder and call cost. No I/O."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from llm_harness.config import ModelConfig, Price
from llm_harness.types import Usage

# Terminal error kinds that move on to the next model. `bad_request` is the
# request's fault, not the model's, so it is not here.
FALLBACK_KINDS = frozenset({"connection", "rate_limit", "not_found", "unknown", "auth"})
BACKOFF_LADDER_MINUTES = (1, 2, 3, 5, 10, 15, 30, 60)
# A longer remaining wait than the ladder's top can only come from a clock that
# was moved back, so such an entry is treated as expired.
_MAX_REMAINING = timedelta(minutes=BACKOFF_LADDER_MINUTES[-1])


@dataclass(frozen=True)
class BackoffEntry:
    model_id: str
    failures: int
    until: datetime  # timezone-aware, UTC


def fallback_chain(
    models: Sequence[ModelConfig], first: ModelConfig, required_tags: frozenset[str]
) -> list[ModelConfig]:
    """The first model, then models with all required tags, then `default` ones.

    Each group is ordered by priority, highest first; `sorted` is stable, so
    equal priorities keep their models.yaml order. Duplicates are dropped.
    """
    tagged = [m for m in models if required_tags <= set(m.tags)]
    defaults = [m for m in models if "default" in m.tags]
    chain: list[ModelConfig] = []
    seen: set[str] = set()
    for model in [
        first,
        *sorted(tagged, key=lambda m: m.priority, reverse=True),
        *sorted(defaults, key=lambda m: m.priority, reverse=True),
    ]:
        if model.id not in seen:
            seen.add(model.id)
            chain.append(model)
    return chain


def backoff_delay(failures: int) -> timedelta:
    """Wait after the n-th consecutive failure; capped at the ladder's last rung."""
    if failures < 1:
        raise ValueError(f"failures must be at least 1, got {failures}")
    rung = min(failures, len(BACKOFF_LADDER_MINUTES)) - 1
    return timedelta(minutes=BACKOFF_LADDER_MINUTES[rung])


def is_backing_off(entry: BackoffEntry | None, now: datetime) -> bool:
    if entry is None:
        return False
    remaining = entry.until - now
    return timedelta(0) < remaining <= _MAX_REMAINING


def compute_cost(usage: Usage | None, price: Price) -> float | None:
    """Cost in USD, or None when a token count is unknown (never 0)."""
    if usage is None or usage.input_tokens is None or usage.output_tokens is None:
        return None
    return (
        usage.input_tokens * price.input_per_mtok
        + usage.output_tokens * price.output_per_mtok
    ) / 1_000_000
