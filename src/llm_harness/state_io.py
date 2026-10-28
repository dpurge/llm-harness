"""YAML export and import of the orchestrator `RunState`.

Pure: bytes in, bytes out, no I/O. The imported file is untrusted, so the order is
fixed: size check on the raw bytes, UTF-8 decode, `SafeLoader` parse (no aliases),
pydantic validation. A rejected file raises `StateImportError` with ONE short line
built only from the field path and the rule, never from the file's values.
"""

import yaml
from pydantic import ValidationError

from llm_harness.display import sanitize_text
from llm_harness.redact import redact
from llm_harness.state import RunState

MAX_IMPORT_BYTES = 1_000_000
EXPORT_FILENAME = "llm-harness-state.yaml"
EXPORT_MEDIA_TYPE = "application/yaml"

_MAX_REASON_CHARS = 200
_MAX_REPORTED_ERRORS = 3


class StateImportError(ValueError):
    """The file was rejected; `str(exc)` is one safe line with no newline."""


class _NoAliasLoader(yaml.SafeLoader):
    """`SafeLoader` that refuses YAML aliases.

    An alias shares one object, so a few hundred bytes of nested aliases expand into
    millions of items when pydantic walks them (measured: 293 bytes, 8 levels,
    3.8 s, and every extra level multiplies the time by 9).
    """

    def compose_node(self, parent: yaml.Node | None, index: object) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            raise StateImportError("YAML aliases are not supported")
        return super().compose_node(parent, index)  # type: ignore[arg-type]


def _one_line(reason: str) -> str:
    """Single line, control characters dropped, secrets masked, length capped."""
    flat = " ".join(sanitize_text(reason).split())
    return redact(flat, limit=_MAX_REASON_CHARS)


def _validation_reason(exc: ValidationError) -> str:
    # Only loc and msg: str(exc) echoes input values, which are the file's content.
    errors = exc.errors(include_input=False, include_url=False)
    parts = [
        f"{'.'.join(str(item) for item in error['loc']) or '(top level)'}: {error['msg']}"
        for error in errors[:_MAX_REPORTED_ERRORS]
    ]
    if len(errors) > _MAX_REPORTED_ERRORS:
        parts.append(f"{len(errors) - _MAX_REPORTED_ERRORS} more")
    return "; ".join(parts)


def export_yaml(run_state: RunState) -> bytes:
    """The state as YAML: JSON-mode data, so no Python-specific tags are written."""
    text = yaml.safe_dump(
        run_state.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )
    return text.encode("utf-8")


def check_size(size: int) -> None:
    """Refuse a file over the cap; callers use it to refuse before reading the bytes."""
    if size > MAX_IMPORT_BYTES:
        raise StateImportError(f"file is larger than {MAX_IMPORT_BYTES} bytes")


def import_yaml(data: bytes) -> RunState:
    """Parse and validate an uploaded file; raise `StateImportError` on any problem."""
    check_size(len(data))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise StateImportError("file is not UTF-8 text") from None
    try:
        loaded = yaml.load(text, Loader=_NoAliasLoader)  # a SafeLoader subclass
    except yaml.YAMLError:
        # The parser's message quotes the file, so it is not passed on.
        raise StateImportError("not valid YAML (or uses an unsupported tag)") from None
    except RecursionError:
        raise StateImportError("YAML is nested too deeply") from None
    if not isinstance(loaded, dict):
        raise StateImportError("top level must be a mapping")
    try:
        return RunState.model_validate(loaded)
    except ValidationError as exc:
        raise StateImportError(_one_line(_validation_reason(exc))) from None
    except RecursionError:
        raise StateImportError("YAML is nested too deeply") from None
