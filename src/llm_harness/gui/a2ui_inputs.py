"""A2UI input components with two-way binding: the user's edit is written to the data model.

A control whose `value` is `{"@path": ...}` reads that path and writes every
accepted edit back through `SurfaceValidator.set_value` (so it survives later
replays), then asks for a redraw so other components bound to the path follow.
A literal or function value is only an initial value: the UI keeps the edit,
nothing is written. A value the user typed is untrusted: it is checked here
(type and size) before it reaches the data model, and refusals are visible.

Not drawn, on purpose and visibly documented in the report: `checks`,
ChoicePicker `displayStyle` for a single choice, DateTimeInput `min`/`max`.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time

from nicegui import ui

from llm_harness.display import sanitize_text
from llm_harness.gui.a2ui_draw import (
    PRE_WRAP,
    Draw,
    resolved,
    text_value,
    unsupported_value,
    value_error,
)

MAX_VALUE_CHARS = 10_000
MAX_CHOICES = 100
_PATH_KEY = "@path"
_TEXT_VARIANTS = ("shortText", "longText", "number", "obscured")
_NO_VALUE = object()


@dataclass
class _Bound:
    """The `value` of a control: its initial value and, for a bare path, where edits go."""

    draw: Draw
    path: str | None
    initial: object
    box: ui.column
    last: object = _NO_VALUE
    _line: ui.label | None = None

    def commit(self, value: object) -> None:
        """Store an edit; a refusal is shown under the control and nothing changes."""
        if value == self.last:
            return
        problem = edit_problem(value)
        if problem is not None:
            self.refuse(problem)
            return
        self.last = value
        self._clear_refusal()
        if self.path is None:
            return
        outcome = self.draw.validator.set_value(self.draw.surface_id, self.path, value)
        if not outcome.ok:
            self.refuse(outcome.errors[0])
        elif self.draw.on_edit is not None and not self.draw.in_modal:
            self.draw.on_edit()

    def refuse(self, reason: str) -> None:
        self._clear_refusal()
        with self.box:
            self._line = (
                ui.label(f"[A2UI input rejected: {sanitize_text(reason)}]")
                .classes(f"w-full {PRE_WRAP} text-negative")
                .mark("a2ui-error")
            )

    def _clear_refusal(self) -> None:
        if self._line is not None:
            self._line.delete()
            self._line = None


def edit_problem(value: object) -> str | None:
    """Why a user-supplied value may not reach the data model, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return (
            None
            if len(value) <= MAX_VALUE_CHARS
            else (f"text longer than {MAX_VALUE_CHARS} characters")
        )
    if isinstance(value, (int, float)):
        return None if math.isfinite(value) else "number is not finite"
    if isinstance(value, list):
        if len(value) > MAX_CHOICES:
            return f"more than {MAX_CHOICES} choices"
        if all(
            isinstance(item, str) and len(item) <= MAX_VALUE_CHARS for item in value
        ):
            return None
        return "choices must be short text"
    return "unsupported value type"


def _bind(draw: Draw, value: object, box: ui.column) -> _Bound | None:
    """Read the control's initial value; None after a visible error line."""
    path: str | None = None
    if (
        isinstance(value, dict)
        and set(value) == {_PATH_KEY}
        and isinstance(value[_PATH_KEY], str)
    ):
        path = draw.validator.absolute_path(
            draw.surface_id, draw.scope, value[_PATH_KEY]
        )
        if not draw.validator.path_exists(draw.surface_id, path):
            return _Bound(draw, path, None, box)  # not filled in yet
    result = resolved(draw, value)
    if not result.ok:
        value_error(result.error)
        return None
    return _Bound(draw, path, result.value, box)


def _field(
    draw: Draw, component: dict[str, object], build: Callable[[ui.column], None]
) -> None:
    """A full-width box holding one control; a refusal line is added at its end."""
    with ui.column().classes("w-full gap-1").mark("a2ui-field") as box:
        build(box)


def _optional_text(draw: Draw, value: object) -> str | None:
    return None if value is None else text_value(draw, value)


def render_text_field(draw: Draw, component: dict[str, object]) -> None:
    variant = component.get("variant", "shortText")
    if variant not in _TEXT_VARIANTS:
        unsupported_value()
        return

    def build(box: ui.column) -> None:
        bound = _bind(draw, component.get("value"), box)
        label = text_value(draw, component.get("label"))
        placeholder = _optional_text(draw, component.get("placeholder"))
        if (
            bound is None
            or label is None
            or (component.get("placeholder") is not None and placeholder is None)
        ):
            return
        start = _as_string(bound.initial)
        bound.last = start
        if variant == "longText":
            control: ui.input | ui.textarea = ui.textarea(
                label, placeholder=placeholder, value=start
            )
        else:
            control = ui.input(
                label,
                placeholder=placeholder,
                value=start,
                password=variant == "obscured",
            )
        control.classes("w-full").mark("a2ui-textfield")
        if variant == "number":
            control.props("inputmode=decimal")

        def commit() -> None:
            value = control.value or ""
            if variant == "number" and value and not _is_number(value):
                bound.refuse("not a number")
                return
            bound.commit(value)

        # Typing must not redraw the surface (it would drop the keyboard focus):
        # the edit is stored when the field loses focus, or on Enter (not in a textarea).
        control.on("blur", commit)
        if variant != "longText":
            control.on("keydown.enter", commit)

    _field(draw, component, build)


def render_check_box(draw: Draw, component: dict[str, object]) -> None:
    def build(box: ui.column) -> None:
        bound = _bind(draw, component.get("value"), box)
        label = text_value(draw, component.get("label"))
        if bound is None or label is None:
            return
        bound.last = bound.initial is True
        box = ui.checkbox(label, value=bound.initial is True)
        box.classes("w-full").mark("a2ui-checkbox")
        box.on_value_change(lambda event: bound.commit(event.value is True))

    _field(draw, component, build)


def render_slider(draw: Draw, component: dict[str, object]) -> None:
    raw_low, raw_high = component.get("min", 0), component.get("max")
    if not _is_real(raw_low) or not _is_real(raw_high):
        unsupported_value()
        return
    low, high = float(raw_low), float(raw_high)  # type: ignore[arg-type]
    if not low < high:
        unsupported_value()
        return
    steps = component.get("steps")
    # Without `steps` the range is cut into 100 divisions: Quasar needs a step.
    divisions = steps if isinstance(steps, int) and steps >= 1 else 100

    def build(box: ui.column) -> None:
        bound = _bind(draw, component.get("value"), box)
        label = _optional_text(draw, component.get("label"))
        if bound is None:
            return
        start = float(bound.initial) if _is_real(bound.initial) else low  # type: ignore[arg-type]
        start = min(max(start, low), high)
        bound.last = start
        if label:
            ui.label(label).classes(f"w-full {PRE_WRAP}").mark("a2ui-slider-label")
        slider = ui.slider(
            min=low, max=high, step=(high - low) / divisions, value=start
        )
        slider.classes("w-full").mark("a2ui-slider")
        # "change" fires when the thumb is released; a redraw during the drag would end it.
        slider.on("change", lambda: bound.commit(slider.value))

    _field(draw, component, build)


def render_choice_picker(draw: Draw, component: dict[str, object]) -> None:
    variant = component.get("variant", "mutuallyExclusive")
    options = _options(draw, component.get("options"))
    if variant not in ("mutuallyExclusive", "multipleSelection") or options is None:
        unsupported_value()
        return
    filterable = component.get("filterable") is True
    multiple = variant == "multipleSelection"

    def build(box: ui.column) -> None:
        bound = _bind(draw, component.get("value"), box)
        label = _optional_text(draw, component.get("label"))
        if bound is None:
            return
        chosen = [v for v in _as_list(bound.initial) if v in options]
        bound.last = chosen
        if label:
            ui.label(label).classes(f"w-full {PRE_WRAP}").mark("a2ui-choice-label")
        if not multiple and not filterable:
            control: ui.radio | ui.select = ui.radio(
                options, value=(chosen or [None])[0]
            )
            control.on_value_change(
                lambda event: bound.commit([] if event.value is None else [event.value])
            )
        else:
            control = ui.select(
                options,
                value=chosen if multiple else (chosen or [None])[0],
                multiple=multiple,
                with_input=filterable,
            )
            if multiple and component.get("displayStyle") == "chips":
                control.props("use-chips")
            control.on_value_change(
                lambda event: bound.commit(
                    _as_list(event.value)
                    if multiple
                    else ([] if event.value is None else [event.value])
                )
            )
        control.classes("w-full").mark("a2ui-choice")

    _field(draw, component, build)


def render_date_time_input(draw: Draw, component: dict[str, object]) -> None:
    wants_date = component.get("enableDate") is True
    wants_time = component.get("enableTime") is True

    def build(box: ui.column) -> None:
        bound = _bind(draw, component.get("value"), box)
        label = _optional_text(draw, component.get("label"))
        if bound is None:
            return
        start = _as_string(bound.initial)
        bound.last = start
        field = ui.input(
            label or "", value=start, placeholder=_iso_hint(wants_date, wants_time)
        )
        field.classes("w-full").mark("a2ui-datetime")

        def commit() -> None:
            value = field.value or ""
            if value and not _is_iso(value, wants_date, wants_time):
                bound.refuse(
                    f"not an ISO 8601 value, use {_iso_hint(wants_date, wants_time)}"
                )
                return
            bound.commit(value)

        field.on("blur", commit)
        field.on("keydown.enter", commit)

    _field(draw, component, build)


def _options(draw: Draw, raw: object) -> dict[str, str] | None:
    """value -> shown label of the options, or None when they are not `[{label, value}]`."""
    if not isinstance(raw, list) or len(raw) > MAX_CHOICES:
        return None
    options: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            return None
        label = text_value(draw, item.get("label"))
        if label is None:
            return None
        options[item["value"]] = label
    return options


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _as_string(value: object) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return sanitize_text(str(value))


def _is_real(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_number(text: str) -> bool:
    try:
        return math.isfinite(float(text))
    except ValueError:
        return False


def _iso_hint(wants_date: bool, wants_time: bool) -> str:
    if wants_date and not wants_time:
        return "YYYY-MM-DD"
    if wants_time and not wants_date:
        return "HH:MM:SS"
    return "YYYY-MM-DDTHH:MM:SS"


def _is_iso(text: str, wants_date: bool, wants_time: bool) -> bool:
    try:
        if wants_date and not wants_time:
            date.fromisoformat(text)
        elif wants_time and not wants_date:
            time.fromisoformat(text)
        else:
            datetime.fromisoformat(text)
            return "T" in text.upper() or not wants_time
    except ValueError:
        return False
    return True
