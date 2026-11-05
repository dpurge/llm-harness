"""The v1.0 basic catalog with a time-bounded `regex`, and the custom catalog.

a2ui-core 0.5.0 runs the model-supplied pattern of `regex` through the standard
`re.search`, which has no time limit: a pattern such as `(a|aa)+$` on a long
value of `a` followed by `!` never returns. The `regex` package (a dependency
of a2ui-core) takes a `timeout`, so this catalog swaps only that one function
implementation and keeps the library's schema (`RegexApi`) for it.

`custom_catalog` derives `llm-harness/custom/v1` from that catalog: every basic
component and function plus `HtmlBlock` and `SvgImage`. Their content is
sanitized by `a2ui.sanitize` before it is drawn.
"""

from typing import Any

import regex
from a2ui.core.basic_catalog import v1_0
from a2ui.core.catalog import Catalog, create_function_implementation
from a2ui.core.exceptions import A2uiExpressionError

REGEX_TIMEOUT_SECONDS = 0.25


def _guarded_regex(
    args: dict[str, Any],
    context: Any = None,
    abort_signal: Any | None = None,
) -> dict[str, Any]:
    pattern = args.get("pattern")
    value = args.get("value")
    if not isinstance(pattern, str):
        raise A2uiExpressionError("regex pattern must be a string")
    text = "" if value is None else str(value)
    try:
        found = regex.search(pattern, text, timeout=REGEX_TIMEOUT_SECONDS)
    except TimeoutError as error:
        raise A2uiExpressionError("regex pattern is too slow, refused") from error
    except regex.error as error:
        raise A2uiExpressionError(f"invalid regex pattern: {error}") from error
    return {"valid": found is not None}


class GuardedBasicCatalog(v1_0.BasicCatalog):
    """`BasicCatalog` whose `regex` cannot hang on a catastrophic pattern."""

    def __init__(self) -> None:
        super().__init__()
        self.functions["regex"] = create_function_implementation(
            v1_0.RegexApi, _guarded_regex
        )


CUSTOM_CATALOG_ID = "llm-harness/custom/v1"
_CUSTOM_INSTRUCTIONS = (
    "HtmlBlock shows a sanitized HTML fragment (property html); "
    "SvgImage shows a sanitized inline SVG (property svg).\n"
    "Both take a string, a data binding or a function call, and an optional weight.\n"
    "HTML and SVG are sanitized: no scripts, styles, forms or external loading."
)


def _new_component(basic: Catalog[Any, Any], name: str, text_property: str) -> Any:
    """A component schema-only entry `{component, <text_property>, weight}`.

    `derive` accepts entries, not raw schemas, so the entry is loaded through a
    throwaway catalog that holds only it (the way a2ui-core's conformance
    driver builds new entries). `text_property` is a required DynamicString;
    `weight` is the basic catalog's own `Weight`, as on Divider and Text; as on
    the basic components, any other property is rejected.
    """
    holder = Catalog.from_json(
        {
            "catalogId": "llm-harness:holder",
            "protocolVersion": "1.0",
            "components": {
                name: {
                    "type": "object",
                    "properties": {
                        "component": {"const": name},
                        text_property: {
                            "$ref": "common_types.json#/$defs/DynamicString"
                        },
                        "weight": {"$ref": "#/$defs/Weight"},
                    },
                    "required": ["component", text_property],
                    "additionalProperties": False,
                }
            },
            "$defs": {"Weight": basic.to_json()["$defs"]["Weight"]},
        }
    )
    return holder.components[name]


def custom_catalog(basic: Catalog[Any, Any]) -> Catalog[Any, Any]:
    """`basic` plus `HtmlBlock` and `SvgImage`, under `CUSTOM_CATALOG_ID`."""
    return basic.derive(
        catalog_id=CUSTOM_CATALOG_ID,
        components=[
            *basic.components,
            _new_component(basic, "HtmlBlock", "html"),
            _new_component(basic, "SvgImage", "svg"),
        ],
        instructions=_CUSTOM_INSTRUCTIONS,
    )
