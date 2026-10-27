import pytest

from llm_harness.redact import REDACTED, Secret, redact

VALUE = "hunter2-very-secret"


def test_secret_never_shows_value():
    secret = Secret(VALUE)
    rendered = [
        repr(secret),
        str(secret),
        f"{secret}",
        format(secret, ">40"),
        f"{secret!r}",
    ]
    assert all(VALUE not in text for text in rendered)
    assert repr(secret) == str(secret) == REDACTED == "[REDACTED:api_key]"


def test_secret_reveal_returns_value():
    assert Secret(VALUE).reveal() == VALUE


def test_secret_masked_inside_containers():
    assert VALUE not in repr([Secret(VALUE)])


def test_redact_masks_known_secret():
    assert redact(f"token={VALUE}!", [VALUE]) == f"token={REDACTED}!"


def test_redact_ignores_empty_secret():
    assert redact("plain", [""]) == "plain"


def test_redact_masks_sk_shape():
    assert redact("key sk-abc123_DEF-456 end") == f"key {REDACTED} end"


def test_redact_keeps_short_sk_prefix():
    assert redact("sk-short") == "sk-short"


def test_redact_masks_bearer_shape():
    assert (
        redact("Authorization: Bearer abc.def") == f"Authorization: Bearer {REDACTED}"
    )


def test_redact_truncates_with_ellipsis():
    result = redact("a" * 50, limit=10)
    assert result == "a" * 10 + "…"


def test_redact_short_text_untouched():
    assert redact("a" * 10, limit=10) == "a" * 10


def test_redact_before_truncating_leaks_no_fragment():
    secret = "zq9XKPLMNW42"
    limit = 20
    text = "a" * (limit - 6) + secret + "tail"
    result = redact(text, [secret], limit=limit)
    fragments = {secret[i : i + 4] for i in range(len(secret) - 3)}
    assert not any(fragment in result for fragment in fragments)
    assert result.endswith("…")


# Bearer rule: after the word (any case), 8+ token characters that contain a
# digit or run 20+ characters.
@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abc12345",
        "bearer abc12345def",
        "BEARER abc12345def",
        "got Bearer abcdefghijklmnopqrstuvwxyz ok",
    ],
)
def test_redact_masks_bearer_tokens_in_any_case(text: str):
    result = redact(text)
    assert REDACTED in result
    assert "abc12345" not in result and "abcdefghij" not in result


@pytest.mark.parametrize(
    "text",
    [
        "the bearer of bad news",
        "Bearer of bad news arrived",
        "bearer shortone",
        "bearer abcdefgh",
        "a basic sentence about basic things",
        "Basic",
    ],
)
def test_redact_leaves_benign_bearer_and_basic_prose(text: str):
    assert redact(text) == text


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: Basic "),
        ("authorization=Bearer abc.def", "authorization=Bearer "),
        ("X-API-Key: zzz-secret", "X-API-Key: "),
        ("x-api-key=zzz-secret", "x-api-key="),
        ("api-key: zzz-secret", "api-key: "),
        ('{"api-key": "zzz-secret"}', '{"api-key": "'),
    ],
)
def test_redact_masks_header_values(text: str, kept: str):
    result = redact(text)
    assert result.startswith(kept + REDACTED)
    assert "zzz-secret" not in result and "dXNlcjpwYXNz" not in result


@pytest.mark.parametrize(
    "text",
    [
        "the api is fine",
        "api-keys are rotated",
        "author: Jane",
        "authorization is hard",
    ],
)
def test_redact_leaves_header_words_without_a_value_separator(text: str):
    assert redact(text) == text


GOOGLE_KEY = "AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r"


def test_redact_masks_google_api_key():
    assert len(GOOGLE_KEY) == 39
    assert redact(f"key={GOOGLE_KEY}&x=1") == f"key={REDACTED}&x=1"


def test_redact_keeps_short_google_prefix():
    assert redact("AIza-short") == "AIza-short"


def test_redact_masks_url_credentials_and_keeps_the_rest():
    assert redact("see https://user:p4ss@example.com/path?q=1") == (
        f"see https://{REDACTED}@example.com/path?q=1"
    )


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/path",
        "http://localhost:11434/api",
        "http://host:8080/x?mail=a@b.c",
        "mail me at user@example.com",
    ],
)
def test_redact_leaves_urls_without_credentials(text: str):
    assert redact(text) == text


JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP"


def test_redact_masks_jwt_shape():
    assert redact(f"token {JWT} end") == f"token {REDACTED} end"


@pytest.mark.parametrize("text", ["eyJshort.eyJshort.abc", "a.b.c", "eyJ"])
def test_redact_leaves_non_jwt_dotted_strings(text: str):
    assert redact(text) == text


def test_redact_plain_sk_word_is_untouched():
    assert redact("the sk word and sk- alone") == "the sk word and sk- alone"


@pytest.mark.parametrize("secret", [GOOGLE_KEY, JWT, "sk-abcdefghij123456"])
def test_pattern_secret_cut_by_limit_leaks_no_fragment(secret: str):
    limit = 20
    result = redact("a" * (limit - 6) + secret + "tail", limit=limit)
    fragments = {secret[i : i + 6] for i in range(len(secret) - 5)}
    assert not any(fragment in result for fragment in fragments)
