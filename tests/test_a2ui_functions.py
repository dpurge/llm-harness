"""The 14 basic-catalog functions, through the validator and the renderer.

Every input is fixed: a2ui-core formats with Babel and an `en_US` default, and
`formatDate` reads the fields of the ISO string it is given, so nothing here
depends on the machine locale, time zone or clock.
"""

import time

import pytest
from nicegui.testing import User

from llm_harness.a2ui.catalog import REGEX_TIMEOUT_SECONDS
from llm_harness.a2ui.validation import SurfaceValidator
from tests.a2ui_support import (
    call,
    data_model,
    found,
    open_surface,
    shown,
    text,
    text_validator,
    validator_with,
)

PATH_NAME = {"@path": "/name"}
PATH_N = {"@path": "/n"}
VALID = {"valid": True}
INVALID = {"valid": False}

# (function, expression, value returned by SurfaceValidator.resolve)
RESOLVED = [
    ("required", call("required", value=PATH_NAME), VALID),
    ("required", call("required", value={"@path": "/empty"}), INVALID),
    ("regex", call("regex", value=PATH_NAME, pattern="^A.n$"), VALID),
    ("regex", call("regex", value=PATH_NAME, pattern="^B"), INVALID),
    ("length", call("length", value=PATH_NAME, min=3, max=3), VALID),
    ("length", call("length", value=PATH_NAME, min=4), INVALID),
    ("numeric", call("numeric", value=PATH_N, min=1, max=5), VALID),
    ("numeric", call("numeric", value=PATH_N, min=4), INVALID),
    ("email", call("email", value={"@path": "/email"}), VALID),
    (
        "formatString",
        call("formatString", value="Hi ${/name}, ${/n} items"),
        "Hi Ann, 3 items",
    ),
    ("formatString", call("formatString", value="${/user/first}!"), "Zed!"),
    ("formatNumber", call("formatNumber", value=1234.567, decimals=1), "1,234.6"),
    ("formatNumber", call("formatNumber", value=1234.567, grouping=False), "1234.567"),
    (
        "formatCurrency",
        call("formatCurrency", value={"@path": "/price"}, currency="USD"),
        "$1,234.50",
    ),
    (
        "formatCurrency",
        call("formatCurrency", value=1234.5, currency="EUR"),
        "€1,234.50",
    ),
    (
        "formatDate",
        call("formatDate", value={"@path": "/when"}, format="EEEE, MMMM d, yyyy HH:mm"),
        "Thursday, January 2, 2025 03:04",
    ),
    (
        "formatDate",
        call("formatDate", value="2025-01-02T23:04:05+05:00", format="h:mm a"),
        "11:04 PM",
    ),
    ("pluralize", call("pluralize", value=1, one="item", other="items"), "item"),
    ("pluralize", call("pluralize", value=PATH_N, one="item", other="items"), "items"),
    (
        "pluralize",
        call("pluralize", value=0, zero="none", one="item", other="items"),
        "none",
    ),
    ("and", call("and", values=[call("required", value="x"), True]), True),
    ("and", call("and", values=[True, False]), False),
    ("or", call("or", values=[False, call("email", value="x@y.zz")]), True),
    ("or", call("or", values=[False, call("email", value="nope")]), False),
    ("not", call("not", value=call("required", value="")), True),
    ("not", call("not", value=True), False),
]


@pytest.mark.parametrize(("name", "expression", "expected"), RESOLVED)
def test_each_function_resolves_through_the_validator(
    name: str, expression: dict[str, object], expected: object
) -> None:
    validator = text_validator(expression)
    result = validator.resolve("s", expression)
    assert result.ok, result.error
    assert result.value == expected


def test_the_table_covers_all_fourteen_functions_but_openurl() -> None:
    covered = {name for name, _, _ in RESOLVED}
    assert covered == {
        "required",
        "regex",
        "length",
        "numeric",
        "email",
        "formatString",
        "formatNumber",
        "formatCurrency",
        "formatDate",
        "pluralize",
        "and",
        "or",
        "not",
    }


def test_email_failure_carries_the_librarys_message() -> None:
    result = text_validator("x").resolve("s", call("email", value="nope"))
    assert result.value["valid"] is False  # type: ignore[index]
    assert result.value["code"] == "INVALID_EMAIL"  # type: ignore[index]


# What a Text shows: strings and booleans only, so the validators sit under and/or/not.
DISPLAYED = [
    (call("formatString", value="Hi ${/name}, ${/n} items"), "Hi Ann, 3 items"),
    (call("formatNumber", value={"@path": "/price"}, decimals=0), "1,234"),
    (call("formatCurrency", value={"@path": "/price"}, currency="USD"), "$1,234.50"),
    (call("formatDate", value={"@path": "/when"}, format="yyyy-MM-dd"), "2025-01-02"),
    (call("pluralize", value=PATH_N, one="item", other="items"), "items"),
    (call("and", values=[call("required", value=PATH_NAME), True]), "true"),
    (call("or", values=[False, call("regex", value=PATH_NAME, pattern="^A")]), "true"),
    (call("not", value=call("length", value=PATH_NAME, min=4)), "true"),
    (call("not", value=call("numeric", value=PATH_N, min=1)), "false"),
    (call("and", values=[call("email", value={"@path": "/email"}), True]), "true"),
]


@pytest.mark.parametrize(("expression", "expected"), DISPLAYED)
async def test_each_function_value_is_displayed_by_the_renderer(
    user: User, expression: dict[str, object], expected: str
) -> None:
    await open_surface(user, text_validator(expression))
    assert shown(user) == [expected]
    assert shown(user, "a2ui-error") == []


async def test_a_function_result_follows_a_data_model_update(user: User) -> None:
    validator = text_validator(call("formatString", value="n=${/n}"))
    assert validator.submit([data_model(7, "/n")]).ok
    await open_surface(user, validator)
    assert shown(user) == ["n=7"]


def test_formatting_does_not_depend_on_the_machine_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.setenv(name, "de_DE.UTF-8")
    validator = text_validator("x")
    number = validator.resolve("s", call("formatNumber", value=1234.5, decimals=2))
    assert number.value == "1,234.50"


def test_a_failing_function_is_an_error_not_a_value() -> None:
    validator = text_validator("x")
    bad_number = validator.resolve("s", call("formatNumber", value="abc"))
    assert not bad_number.ok
    assert "formatNumber" in bad_number.error
    bad_args = validator.resolve("s", call("formatNumber", value=1, decimalPlaces=1))
    assert not bad_args.ok


# regex: a2ui-core 0.5.0 runs the pattern through plain `re`, which never returns on this one.
CATASTROPHIC = {"value": "a" * 60 + "!", "pattern": "(a|aa)+$"}


def test_a_catastrophic_regex_is_refused_quickly() -> None:
    validator = text_validator("x")
    started = time.monotonic()
    result = validator.resolve("s", call("regex", **CATASTROPHIC))
    assert time.monotonic() - started < 1.0
    assert not result.ok
    assert result.error == "regex pattern is too slow, refused"
    assert REGEX_TIMEOUT_SECONDS < 1.0


def test_a_catastrophic_regex_is_refused_when_nested_in_other_calls() -> None:
    validator = text_validator("x")
    nested = call("not", value=call("regex", **CATASTROPHIC))
    started = time.monotonic()
    assert not validator.resolve("s", nested).ok
    assert time.monotonic() - started < 1.0


async def test_a_catastrophic_regex_shows_a_visible_error(user: User) -> None:
    started = time.monotonic()
    await open_surface(
        user, text_validator(call("not", value=call("regex", **CATASTROPHIC)))
    )
    assert time.monotonic() - started < 2.0
    assert shown(user, "a2ui-error") == [
        "[A2UI value error: regex pattern is too slow, refused]"
    ]


def test_an_invalid_regex_pattern_is_an_error() -> None:
    result = text_validator("x").resolve("s", call("regex", value="a", pattern="("))
    assert not result.ok
    assert result.error.startswith("invalid regex pattern")


# openUrl: only as a Button action, only http and https.
def url_button(url: object) -> SurfaceValidator:
    return validator_with(
        [
            {"id": "root", "component": "Column", "children": ["go"]},
            {
                "id": "go",
                "component": "Button",
                "child": "go_label",
                "action": {"functionCall": call("openUrl", url=url)},
            },
            text("go_label", "Open"),
        ]
    )


@pytest.fixture
def navigated(user: User, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    # The `user` fixture makes `ui.navigate` its own `UserNavigate`; patch that one.
    calls: list[tuple[str, bool]] = []

    def fake_to(target: object, new_tab: bool = False) -> None:
        calls.append((str(target), new_tab))

    monkeypatch.setattr(user.navigate, "to", fake_to)
    return calls


@pytest.mark.parametrize(
    "url", ["https://example.org/a?b=1", "http://example.org", "HTTPS://Example.org"]
)
async def test_open_url_opens_a_web_link_in_a_new_tab(
    user: User, navigated: list[tuple[str, bool]], url: str
) -> None:
    await open_surface(user, url_button(url))
    user.find(marker="a2ui-button").click()
    assert navigated == [(url, True)]
    assert found(user, "a2ui-error") == []


async def test_open_url_resolves_a_bound_url(
    user: User, navigated: list[tuple[str, bool]]
) -> None:
    validator = url_button({"@path": "/site"})
    assert validator.submit([data_model("https://example.org/x", "/site")]).ok
    await open_surface(user, validator)
    user.find(marker="a2ui-button").click()
    assert navigated == [("https://example.org/x", True)]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        " javascript:alert(1)",
        "java\nscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "mailto:a@b.co",
        "tel:123",
        "vbscript:x",
        "myapp://open",
        "//example.org/x",
        "/relative",
        "https://",
        "https:///x",
        "https://exa mple.org",
        "https://example.org/\nx",
        "",
    ],
)
async def test_open_url_refuses_every_other_scheme(
    user: User, navigated: list[tuple[str, bool]], url: str
) -> None:
    validator = validator_with(
        [
            {"id": "root", "component": "Column", "children": ["go"]},
            {
                "id": "go",
                "component": "Button",
                "child": "go_label",
                "action": {"functionCall": call("openUrl", url={"@path": "/site"})},
            },
            text("go_label", "Open"),
        ],
        {"site": url},
    )
    await open_surface(user, validator)
    user.find(marker="a2ui-button").click()
    assert navigated == []
    assert shown(user, "a2ui-error") == ["[A2UI openUrl refused: scheme]"]


async def test_a_second_click_replaces_the_refusal_line(
    user: User, navigated: list[tuple[str, bool]]
) -> None:
    await open_surface(user, url_button("javascript:alert(1)"))
    user.find(marker="a2ui-button").click()
    user.find(marker="a2ui-button").click()
    assert shown(user, "a2ui-error") == ["[A2UI openUrl refused: scheme]"]


async def test_open_url_with_an_unresolvable_url_shows_the_value_error(
    user: User, navigated: list[tuple[str, bool]]
) -> None:
    await open_surface(user, url_button({"@path": "/missing"}))
    user.find(marker="a2ui-button").click()
    assert navigated == []
    assert shown(user, "a2ui-error") == ["[A2UI value error: path /missing not found]"]


async def test_another_function_call_action_stays_unsupported(
    user: User, navigated: list[tuple[str, bool]]
) -> None:
    validator = validator_with(
        [
            {"id": "root", "component": "Column", "children": ["go"]},
            {
                "id": "go",
                "component": "Button",
                "child": "go_label",
                "action": {"functionCall": call("formatString", value="x")},
            },
            text("go_label", "Go"),
        ]
    )
    await open_surface(user, validator)
    assert shown(user, "a2ui-unsupported") == ["[unsupported A2UI value]"]
    assert found(user, "a2ui-button") == []
