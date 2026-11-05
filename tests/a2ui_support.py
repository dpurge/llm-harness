"""Shared builders for the A2UI binding, function and update tests."""

import re

from nicegui import ElementFilter, ui
from nicegui.testing import User

from llm_harness.a2ui.validation import SurfaceValidator
from llm_harness.gui.a2ui_render import render_surface

CATALOG_ID = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
FORBIDDEN_CLASS = re.compile(r"(^|:)max-w-|(^|:)mx-auto|(^|:)h-(\d|\[|px)|\d+px")

DATA: dict[str, object] = {
    "name": "Ann",
    "n": 3,
    "price": 1234.5,
    "when": "2025-01-02T03:04:05Z",
    "email": "a@b.co",
    "empty": "",
    "user": {"first": "Zed"},
}


def create(surface_id: str = "s") -> dict[str, object]:
    return {
        "version": "1.0",
        "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID},
    }


def data_model(
    value: object, path: str = "/", surface_id: str = "s"
) -> dict[str, object]:
    return {
        "version": "1.0",
        "updateDataModel": {"surfaceId": surface_id, "path": path, "value": value},
    }


def components(
    items: list[dict[str, object]], surface_id: str = "s"
) -> dict[str, object]:
    return {
        "version": "1.0",
        "updateComponents": {"surfaceId": surface_id, "components": items},
    }


def text(component_id: str, value: object) -> dict[str, object]:
    return {"id": component_id, "component": "Text", "text": value}


def call(name: str, **args: object) -> dict[str, object]:
    return {"@call": name, "args": args}


def validator_with(
    items: list[dict[str, object]], data: object = DATA
) -> SurfaceValidator:
    validator = SurfaceValidator()
    outcome = validator.submit([create(), data_model(data), components(items)])
    assert outcome.ok, outcome.errors
    return validator


def text_validator(value: object, data: object = DATA) -> SurfaceValidator:
    """A surface whose root is one Text showing `value`."""
    return validator_with([text("root", value)], data)


async def open_surface(
    user: User, validator: SurfaceValidator, surface_id: str = "s"
) -> list[tuple[str, str, dict[str, object]]]:
    """Render the surface on a throwaway page; returns the recorded actions."""
    actions: list[tuple[str, str, dict[str, object]]] = []

    @ui.page("/a2ui-test")
    def page() -> None:
        with ui.column().classes("w-full"):
            render_surface(
                validator, surface_id, lambda s, a, c: actions.append((s, a, c))
            )

    await user.open("/a2ui-test")
    return actions


def found(user: User, marker: str) -> list:
    with user.client:
        return list(ElementFilter(marker=marker, local_scope=False))


def shown(user: User, marker: str = "a2ui-text") -> list[str]:
    return [element.text for element in found(user, marker)]


async def open_redrawing(
    user: User,
    validator: SurfaceValidator,
    surface_id: str = "s",
    view: dict[str, str] | None = None,
) -> list[tuple[str, str, dict[str, object]]]:
    """Like `open_surface`, but a stored user edit draws the surface again, as the chat does."""
    actions: list[tuple[str, str, dict[str, object]]] = []

    @ui.page("/a2ui-test")
    def page() -> None:
        container = ui.column().classes("w-full").mark("a2ui-root")

        def fill() -> None:
            container.clear()
            with container:
                render_surface(
                    validator,
                    surface_id,
                    lambda s, a, c: actions.append((s, a, c)),
                    on_edit=fill,
                    view=view,
                )

        fill()

    await user.open("/a2ui-test")
    return actions


def surface_elements(user: User) -> list:
    """Every element drawn inside the `a2ui-root` column (dialogs included)."""
    with user.client:
        (root,) = found(user, "a2ui-root")
        return list(root.descendants())


def bad_sizing(user: User) -> list[str]:
    """Forbidden classes and fixed sizes (classes or inline style) of every drawn element."""
    problems: list[str] = []
    for element in surface_elements(user):
        problems += [c for c in element.classes if FORBIDDEN_CLASS.search(c)]
        problems += [
            f"{key}: {value}"
            for key, value in element._style.items()
            if key in ("height", "width", "max-width", "min-height") or "px" in value
        ]
    return problems
