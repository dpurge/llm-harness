"""What the vector backends share, so that they keep one observable contract.

Point ids, the reserved payload key, collection names and the bool-filter refusal
must behave the same on every backend; defining them once keeps them from drifting.
"""

import re
import uuid
from collections.abc import Mapping

from llm_harness.storage.vectors import VectorStoreError

# Never change this: every stored point id is derived from it, so a new value
# would orphan every point already stored (on disk or on a server).
ID_NAMESPACE = uuid.UUID("7c0b6f3e-5d1a-4e8b-9a52-3f4d8e1c6b27")
ORIGINAL_ID_KEY = "_id"
NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")


def check_name(name: str) -> None:
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise VectorStoreError(
            "a collection name is a lowercase letter then up to 63 of a-z, 0-9 and '_'"
        )


def point_id(original: str) -> str:
    """The engine-side id; engines refuse arbitrary strings (qdrant server: HTTP 400)."""
    return str(uuid.uuid5(ID_NAMESPACE, original))


def checked_filter(
    filter: Mapping[str, str | int | bool] | None,
) -> Mapping[str, str | int | bool]:
    # Verified on qdrant-edge-py 0.9.0: `MatchValue(True)` matches the integer 1
    # and never a stored boolean. The qdrant server 1.20.0 matches bools correctly,
    # but both backends refuse them so that callers see one contract.
    for key, value in (filter or {}).items():
        if isinstance(value, bool):
            raise VectorStoreError(
                f"cannot filter on {key!r} with a bool: store 0 or 1, or a string, instead"
            )
    return filter or {}
