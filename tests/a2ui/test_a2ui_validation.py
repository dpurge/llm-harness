"""Tests for the A2UI v1.0 message validator."""

from llm_harness.a2ui.validation import SurfaceValidator, ValidationOutcome

CATALOG_ID = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"


def _create(
    components: list[dict], surface_id: str = "s1", catalog_id: str = CATALOG_ID
) -> dict:
    return {
        "version": "1.0",
        "createSurface": {
            "surfaceId": surface_id,
            "catalogId": catalog_id,
            "components": components,
        },
    }


def _text(component_id: str = "root", text: str = "hi") -> dict:
    return {"id": component_id, "component": "Text", "text": text}


def _assert_rejected(outcome: ValidationOutcome) -> str:
    assert outcome.ok is False
    assert len(outcome.errors) == 1
    assert outcome.errors[0].startswith("A2UI message rejected: ")
    return outcome.errors[0]


def test_valid_create_surface_with_text_is_accepted() -> None:
    validator = SurfaceValidator()
    outcome = validator.submit([_create([_text()])])
    assert outcome == ValidationOutcome(ok=True, errors=())
    assert validator.surface_ids == ("s1",)


def test_incremental_update_adds_a_child() -> None:
    validator = SurfaceValidator()
    first = validator.submit(
        [
            _create(
                [{"id": "root", "component": "Column", "children": ["a"]}, _text("a")]
            )
        ]
    )
    assert first.ok is True
    update = {
        "version": "1.0",
        "updateComponents": {
            "surfaceId": "s1",
            "components": [
                {"id": "root", "component": "Column", "children": ["a", "b"]},
                _text("b", "second"),
            ],
        },
    }
    outcome = validator.submit([update])
    assert outcome == ValidationOutcome(ok=True, errors=())
    assert validator.surface_ids == ("s1",)


def test_unknown_component_is_rejected() -> None:
    validator = SurfaceValidator()
    text = _assert_rejected(
        validator.submit([_create([{"id": "root", "component": "Evil", "text": "x"}])])
    )
    assert "'Evil'" in text


def test_dangling_child_reference_is_rejected() -> None:
    validator = SurfaceValidator()
    text = _assert_rejected(
        validator.submit(
            [_create([{"id": "root", "component": "Column", "children": ["nope"]}])]
        )
    )
    assert "nope" in text


def test_cyclic_references_are_rejected() -> None:
    validator = SurfaceValidator()
    text = _assert_rejected(
        validator.submit(
            [
                _create(
                    [
                        {"id": "root", "component": "Column", "children": ["a"]},
                        {"id": "a", "component": "Column", "children": ["root"]},
                    ]
                )
            ]
        )
    )
    assert "Circular" in text


def test_other_protocol_version_is_rejected_by_our_own_check() -> None:
    validator = SurfaceValidator()
    message = _create([_text()])
    message["version"] = "0.9"
    text = _assert_rejected(validator.submit([message]))
    assert 'version must be "1.0"' in text
    assert "0.9" in text
    assert validator.surface_ids == ()


def test_missing_version_is_rejected() -> None:
    validator = SurfaceValidator()
    message = _create([_text()])
    del message["version"]
    text = _assert_rejected(validator.submit([message]))
    assert 'version must be "1.0"' in text


def test_non_list_input_is_rejected() -> None:
    validator = SurfaceValidator()
    text = _assert_rejected(validator.submit(_create([_text()])))
    assert "array" in text


def test_empty_list_is_rejected() -> None:
    validator = SurfaceValidator()
    text = _assert_rejected(validator.submit([]))
    assert "empty" in text


def test_list_with_non_dict_is_rejected() -> None:
    validator = SurfaceValidator()
    text = _assert_rejected(validator.submit([_create([_text()]), "oops"]))
    assert "message 1 must be a JSON object" in text
    assert validator.surface_ids == ()


def test_wrong_catalog_id_is_rejected() -> None:
    validator = SurfaceValidator()
    text = _assert_rejected(
        validator.submit(
            [_create([_text()], catalog_id="https://example.com/other.json")]
        )
    )
    assert "Catalog not found" in text
    assert validator.surface_ids == ()


def test_invalid_batch_leaves_state_unchanged() -> None:
    validator = SurfaceValidator()
    assert validator.submit([_create([_text()])]).ok is True
    bad = validator.submit(
        [
            _create([_text()], surface_id="s2"),
            {
                "version": "1.0",
                "updateComponents": {
                    "surfaceId": "s1",
                    "components": [{"id": "x", "component": "Evil"}],
                },
            },
        ]
    )
    _assert_rejected(bad)
    assert validator.surface_ids == ("s1",)
    good = validator.submit([_create([_text()], surface_id="s2")])
    assert good == ValidationOutcome(ok=True, errors=())
    assert validator.surface_ids == ("s1", "s2")


def test_rejected_create_surface_does_not_register_its_id() -> None:
    validator = SurfaceValidator()
    _assert_rejected(
        validator.submit(
            [_create([{"id": "root", "component": "Evil"}], surface_id="bad")]
        )
    )
    assert validator.surface_ids == ()


def test_error_text_is_bounded_and_has_no_traceback_or_paths() -> None:
    validator = SurfaceValidator()
    text = _assert_rejected(
        validator.submit([_create([{"id": "root", "component": "X" * 2000}])])
    )
    assert len(text) <= 520
    assert "Traceback" not in text
    assert ".py" not in text
