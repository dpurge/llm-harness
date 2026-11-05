"""Conformance check against the A2UI project's own v1.0 basic catalog examples.

Each example in `tests/data/a2ui_examples/` (copied unchanged, see NOTICE.md) is
submitted to `SurfaceValidator` and drawn through `render_surface`, the entry
point the GUI uses. The outcome of every example is recorded in
`.agent/tmp/a2ui-conformance-summary.json`; the expectations below say which
examples the port is known not to handle. A listed example that starts passing,
or an unlisted one that fails, fails the test, so the table cannot go stale.
"""

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest
from a2ui.core.basic_catalog import v1_0
from nicegui import ElementFilter, ui
from nicegui.testing import User

from llm_harness.a2ui.validation import SurfaceValidator
from llm_harness.gui.a2ui_render import render_surface

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = Path(__file__).resolve().parent / "data" / "a2ui_examples"
SUMMARY = ROOT / ".agent" / "tmp" / "a2ui-conformance-summary.json"
VALUE_ERROR_PREFIX = "[A2UI value error"

# Examples the validator rejects: file name -> exact reason.
EXPECTED_REJECTED: dict[str, str] = {
    "00_incremental.json": (
        "streams a root that references components sent in a later batch; the port"
        " validates each batch strictly (no dangling references), while a2ui-core's"
        " default processor accepts the stream"
    ),
    "31_incremental-dashboard.json": (
        "an intermediate batch leaves placeholder components unreachable from root;"
        " the port validates each batch strictly (no orphans), while a2ui-core's"
        " default processor accepts the stream"
    ),
}

# Examples that are accepted but raise while drawing: file name -> exact reason.
EXPECTED_RENDER_FAILURES: dict[str, str] = {}

NAMES = sorted(path.name for path in EXAMPLES.glob("*.json"))


@dataclass
class Result:
    example: str
    accepted: bool = False
    rendered: bool = False
    unsupported_lines: int = 0
    value_errors: int = 0
    other_error_lines: int = 0
    error: str = ""


_results: dict[str, Result] = {}


@pytest.fixture(scope="module", autouse=True)
def write_summary() -> Iterator[None]:
    yield
    rows = [asdict(_results[name]) for name in sorted(_results)]
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print("\nA2UI conformance (example, accepted, rendered, unsupported, value errors)")
    for row in rows:
        print(
            f"{row['example']:40} {row['accepted']!s:6} {row['rendered']!s:6}"
            f" {row['unsupported_lines']:3} {row['value_errors']:3}"
            f"  {row['error'][:70]}"
        )


def load_messages(name: str) -> list[dict[str, object]]:
    document = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict), name
    messages = document["messages"]
    assert isinstance(messages, list), name
    return messages


def test_the_vendored_examples_are_present_and_listed_ones_exist() -> None:
    assert len(NAMES) >= 40
    assert (EXAMPLES / "NOTICE.md").is_file()
    assert set(EXPECTED_REJECTED) <= set(NAMES)
    assert set(EXPECTED_RENDER_FAILURES) <= set(NAMES)
    assert not set(EXPECTED_REJECTED) & set(EXPECTED_RENDER_FAILURES)


@pytest.mark.parametrize("name", NAMES)
def test_an_example_uses_the_basic_catalog_id(name: str) -> None:
    catalog_id = v1_0.BasicCatalog().catalog_id
    created = [m["createSurface"] for m in load_messages(name) if "createSurface" in m]
    assert created, name
    assert {c["catalogId"] for c in created} == {catalog_id}  # type: ignore[index]


@pytest.mark.parametrize("name", NAMES)
async def test_an_example_is_accepted_and_drawn(user: User, name: str) -> None:
    result = _results.setdefault(name, Result(example=name))
    validator = SurfaceValidator()
    outcome = validator.submit(load_messages(name))
    result.accepted = outcome.ok
    result.error = " ".join(outcome.errors)[:300]

    if name in EXPECTED_REJECTED:
        assert not outcome.ok, f"{name} is now accepted; drop it from EXPECTED_REJECTED"
        return
    assert outcome.ok, f"{name} unexpectedly rejected: {outcome.errors}"

    failure: list[str] = []

    @ui.page("/a2ui-conformance")
    def page() -> None:
        with ui.column().classes("w-full"):
            for surface_id in validator.surface_ids:
                try:
                    render_surface(validator, surface_id, lambda s, a, c: None)
                except Exception as error:  # noqa: BLE001 - recorded, then judged below
                    failure.append(f"{type(error).__name__}: {error}")

    await user.open("/a2ui-conformance")
    result.rendered = not failure
    if failure:
        result.error = failure[0][:300]
    with user.client:
        result.unsupported_lines = len(
            list(ElementFilter(marker="a2ui-unsupported", local_scope=False))
        )
        errors = [
            element.text
            for element in ElementFilter(marker="a2ui-error", local_scope=False)
        ]
    result.value_errors = sum(1 for e in errors if e.startswith(VALUE_ERROR_PREFIX))
    result.other_error_lines = len(errors) - result.value_errors

    if name in EXPECTED_RENDER_FAILURES:
        assert failure, f"{name} now draws; drop it from EXPECTED_RENDER_FAILURES"
    else:
        assert not failure, f"{name} raised while drawing: {failure}"
