"""Validate model-produced A2UI v1.0 messages against the basic or the custom catalog.

Nothing may render a message before it passes `SurfaceValidator.submit`. The
returned error text is meant to be sent back to the model unchanged.
"""

import copy
import warnings
from dataclasses import dataclass, field

from a2ui.core.exceptions import A2uiError
from a2ui.core.processing import MessageProcessor, MessageProcessorOptions
from a2ui.core.resolution import DataContext
from a2ui.core.validation import STRICT_VALIDATION

from llm_harness.a2ui.catalog import GuardedBasicCatalog, custom_catalog
from llm_harness.display import sanitize_text

_PROTOCOL_VERSION = "1.0"
_ERROR_PREFIX = "A2UI message rejected: "
_MAX_ERROR_CHARS = 500
_MAX_VALUE_ERROR_CHARS = 200
_PATH_KEY = "@path"
_CALL_KEY = "@call"
OPEN_URL = "openUrl"
_SURFACE_MESSAGES = (
    "createSurface",
    "updateComponents",
    "updateDataModel",
    "deleteSurface",
)


@dataclass(frozen=True)
class ValidationOutcome:
    """Result of one `SurfaceValidator.submit` call."""

    ok: bool
    errors: tuple[str, ...]
    # Lines for the model and the user, such as a renamed surface (accepted batches only).
    notes: tuple[str, ...] = ()
    # The batch as accepted: after a rename its surface ids differ from the input's.
    # Not part of equality: it only repeats the batch the caller sent.
    messages: tuple[dict[str, object], ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class ResolvedValue:
    """Result of `SurfaceValidator.resolve`: the value, or a short error reason."""

    ok: bool
    value: object = None
    error: str = ""


class SurfaceValidator:
    """Accepts A2UI messages batch by batch and keeps the accepted history.

    A surface uses the catalog named by its `createSurface.catalogId`: the v1.0
    basic catalog or `llm-harness/custom/v1` (basic plus HtmlBlock and SvgImage).

    a2ui-core also understands v0.8 and v0.9 messages; this validator accepts
    v1.0 only. Each batch is atomic: it is replayed together with all earlier
    accepted messages into a fresh strict processor, and the accepted state
    changes only when the whole replay succeeds.
    """

    def __init__(self) -> None:
        basic = GuardedBasicCatalog()
        self._catalogs = [basic, custom_catalog(basic)]
        self._accepted: list[dict[str, object]] = []
        self._processor = self._new_processor()

    @property
    def surface_ids(self) -> tuple[str, ...]:
        return tuple(self._processor.model.surfaces_map)

    def components(self, surface_id: str) -> dict[str, dict[str, object]]:
        """The accepted components of one surface: id -> {"id", "component", ...props}.

        Returns copies; the root component has the id "root". An unknown surface
        gives an empty dict. Reads `surfaces_map` / `components_model.entries`
        of a2ui-core, which are not documented public API (verified on 0.5.0).
        """
        surface = self._processor.model.surfaces_map.get(surface_id)
        if surface is None:
            return {}
        return {
            component_id: {
                **copy.deepcopy(model.properties),
                "id": component_id,
                "component": model.type,
            }
            for component_id, model in surface.components_model.entries
        }

    def sends_data_model(self, surface_id: str) -> bool:
        """True when the surface was created with `sendDataModel: true`."""
        surface = self._processor.model.surfaces_map.get(surface_id)
        return surface is not None and surface.send_data_model is True

    def data_model(self, surface_id: str) -> object:
        """A copy of the whole CURRENT data model of a surface (None when unknown)."""
        surface = self._processor.model.surfaces_map.get(surface_id)
        if surface is None:
            return None
        return copy.deepcopy(surface.data_model.get("/"))

    def absolute_path(self, surface_id: str, scope: str, path: str) -> str:
        """`path` made absolute inside the template scope `scope` ("/" outside templates)."""
        surface = self._processor.model.surfaces_map.get(surface_id)
        if surface is None:
            return path
        return DataContext(surface, scope).resolve_path(path)

    def path_exists(self, surface_id: str, absolute_path: str) -> bool:
        """True when the data model of the surface already holds `absolute_path`."""
        surface = self._processor.model.surfaces_map.get(surface_id)
        return surface is not None and surface.data_model.has_path(absolute_path)

    def resolve(
        self, surface_id: str, value: object, scope: str = "/"
    ) -> ResolvedValue:
        """Evaluate a dynamic value against the CURRENT data model of a surface.

        `scope` is the data path of the template item a component is drawn for
        ("/" outside templates); relative `@path` values resolve against it.

        `value` is a literal, `{"@path": ...}` or `{"@call": ..., "args": ...}`
        (or a list/dict holding them). Never raises: an unknown surface, a path
        that does not exist, a failing function and `openUrl` used as a value
        (it is allowed only as a button action) give `ok=False` and a short
        reason. a2ui-core reports a failing call through the surface's
        `on_error` event and returns None; that event is read here so the
        failure is not silent.
        """
        surface = self._processor.model.surfaces_map.get(surface_id)
        if surface is None:
            return _failed("unknown surface")
        if _calls_function(value, OPEN_URL):
            return _failed("openUrl is only allowed as a button action")
        context = DataContext(surface, scope)
        missing = _missing_path(value, context)
        if missing is not None:
            return _failed(f"path {missing} not found")
        errors: list[object] = []
        subscription = surface.on_error.subscribe(errors.append)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                resolved = context.resolve_dynamic_value(copy.deepcopy(value))
        except Exception as error:  # noqa: BLE001 - library failures must not reach the page
            return _failed(str(error) or type(error).__name__)
        finally:
            subscription.unsubscribe()
        if errors:
            first = errors[0]
            message = first.get("message") if isinstance(first, dict) else None
            return _failed(str(message or "function failed"))
        return ResolvedValue(ok=True, value=resolved)

    def set_value(self, surface_id: str, path: str, value: object) -> ValidationOutcome:
        """Write a user edit to the data model of a surface as an `updateDataModel`.

        The write goes through `submit`, so it is kept in the accepted history
        and survives the replay of every later batch. `path` must be absolute.
        """
        if surface_id not in self.surface_ids:
            return _rejected(f"unknown surface {surface_id!r}.")
        if not path.startswith("/"):
            return _rejected(f"data path {path!r} must start with '/'.")
        message = {
            "version": _PROTOCOL_VERSION,
            "updateDataModel": {"surfaceId": surface_id, "path": path, "value": value},
        }
        return self.submit([message])

    def submit(self, messages: object) -> ValidationOutcome:
        problem = _shape_problem(messages)
        if problem is not None:
            return _rejected(problem)
        assert isinstance(messages, list)  # narrowed by _shape_problem
        processor = self._new_processor()
        batch = copy.deepcopy(messages)
        notes = self._rename_reused_surfaces(batch)
        candidate = [*self._accepted, *batch]
        try:
            # a2ui-core stores `value` objects in the data model and changes them
            # in place; a copy keeps the accepted history and the caller's
            # messages intact, so a rejected batch cannot alter the adopted state.
            processor.process_messages(copy.deepcopy(candidate))
        except A2uiError as error:
            return _rejected(str(error))
        self._accepted = candidate
        self._processor = processor
        return ValidationOutcome(
            ok=True, errors=(), notes=tuple(notes), messages=tuple(batch)
        )

    def _rename_reused_surfaces(self, batch: list[dict[str, object]]) -> list[str]:
        """Give a `createSurface` of a surface that exists the first free `<id>_<n>`.

        The messages are walked in order, so a surface deleted earlier in the batch
        may be created again under its own id. Later messages of the batch that
        name the reused id are rewritten to the new id (in place); the notes say
        what was renamed.
        """
        existing = set(self.surface_ids)
        renamed: dict[str, str] = {}
        notes: list[str] = []
        for message in batch:
            for key in _SURFACE_MESSAGES:
                body = message.get(key)
                if not isinstance(body, dict) or not isinstance(
                    body.get("surfaceId"), str
                ):
                    continue
                surface_id = body["surfaceId"]
                if key == "createSurface":
                    if surface_id in existing:
                        new_id = _free_surface_id(surface_id, existing)
                        renamed[surface_id] = body["surfaceId"] = new_id
                        notes.append(_rename_note(surface_id, new_id))
                    else:
                        renamed.pop(surface_id, None)
                    existing.add(body["surfaceId"])
                else:
                    surface_id = body["surfaceId"] = renamed.get(surface_id, surface_id)
                    if key == "deleteSurface":
                        existing.discard(surface_id)
        return notes

    def _new_processor(self) -> MessageProcessor:
        return MessageProcessor(
            self._catalogs,
            options=MessageProcessorOptions(validation_config=STRICT_VALIDATION),
        )


def _free_surface_id(surface_id: str, existing: set[str]) -> str:
    number = 2
    while f"{surface_id}_{number}" in existing:
        number += 1
    return f"{surface_id}_{number}"


def _rename_note(old_id: str, new_id: str) -> str:
    return (
        f'surface "{sanitize_text(old_id)}" already exists; '
        f'the new surface is "{sanitize_text(new_id)}"'
    )


def _shape_problem(messages: object) -> str | None:
    if not isinstance(messages, list):
        return "messages must be a JSON array of message objects."
    if not messages:
        return "messages must not be empty."
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return f"message {index} must be a JSON object."
        if message.get("version") != _PROTOCOL_VERSION:
            return f'message {index} has version {message.get("version")!r}; version must be "1.0".'
    return None


def _rejected(detail: str) -> ValidationOutcome:
    text = (_ERROR_PREFIX + detail)[:_MAX_ERROR_CHARS]
    return ValidationOutcome(ok=False, errors=(text,))


def _failed(reason: str) -> ResolvedValue:
    return ResolvedValue(
        ok=False, error=" ".join(reason.split())[:_MAX_VALUE_ERROR_CHARS]
    )


def _calls_function(value: object, name: str) -> bool:
    if isinstance(value, dict):
        return value.get(_CALL_KEY) == name or any(
            _calls_function(item, name) for item in value.values()
        )
    if isinstance(value, list):
        return any(_calls_function(item, name) for item in value)
    return False


def _missing_path(value: object, context: DataContext) -> str | None:
    """The first explicit `@path` binding whose path is not in the data model."""
    if isinstance(value, dict):
        path = value.get(_PATH_KEY)
        if isinstance(path, str):
            absolute = context.resolve_path(path)
            return None if context.data_model.has_path(absolute) else absolute
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            missing = _missing_path(item, context)
            if missing is not None:
                return missing
    return None
