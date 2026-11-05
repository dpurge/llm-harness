"""The fixed A2UI v1.0 surface drawn by the GUI `/ui-demo` command."""

from a2ui.core.basic_catalog import v1_0

DEMO_SURFACE_ID = "demo"

DEMO_MESSAGES: list[dict[str, object]] = [
    {
        "version": "1.0",
        "createSurface": {
            "surfaceId": DEMO_SURFACE_ID,
            "catalogId": v1_0.BasicCatalog().catalog_id,
            "components": [
                {
                    "id": "root",
                    "component": "Column",
                    "children": ["heading", "body", "button"],
                },
                {"id": "heading", "component": "Text", "text": "A2UI demo surface"},
                {
                    "id": "body",
                    "component": "Text",
                    "text": "This surface is drawn from fixed A2UI v1.0 messages; no model was called.",
                },
                {
                    "id": "button",
                    "component": "Button",
                    "child": "button_label",
                    "action": {"event": {"name": "demo_clicked", "context": {}}},
                },
                {"id": "button_label", "component": "Text", "text": "Click me"},
            ],
        },
    }
]
