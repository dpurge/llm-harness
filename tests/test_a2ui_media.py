"""A2UI media: the URL policy, Image / Video / AudioPlayer drawing and the config key."""

from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing import User

from llm_harness.a2ui.media import (
    MAX_DATA_URL_CHARS,
    REASONS,
    media_url_allowed,
    media_url_refusal,
)
from llm_harness.config import (
    EXAMPLE_AGENTS_YAML,
    EXAMPLE_CONFIG_TOML,
    EXAMPLE_MODELS_YAML,
    ConfigError,
    GuiConfig,
    load_config,
)
from llm_harness.gui.a2ui_render import render_surface
from tests.a2ui_support import (
    bad_sizing,
    found,
    shown,
    validator_with,
)

HOSTS = ("example.com", "cdn.example.org")
PNG = "data:image/png;base64,iVBORw0KGgo="

# (url, kind, allowed)
POLICY: list[tuple[str, str, bool]] = [
    # same-server paths
    ("/files/a.png", "image", True),
    ("/a.mp4", "video", True),
    ("/a.mp3", "audio", True),
    ("/a/b/c.png?x=1#y", "image", True),
    ("/", "image", True),
    ("/a..b/c.png", "image", True),
    ("//evil.example/a.png", "image", False),
    ("//", "image", False),
    ("/a/../b.png", "image", False),
    ("/..", "image", False),
    ("/a/%2e%2e/b.png", "image", False),
    ("/a/%2E%2e/b.png", "image", False),
    ("/a/..%2fb.png", "image", False),
    ("/a%5cb.png", "image", False),
    ("/a\\b.png", "image", False),
    ("/a b.png", "image", False),
    ("/a\tb.png", "image", False),
    ("/a\nb.png", "image", False),
    ("/a\x00b.png", "image", False),
    ("/a\x7fb.png", "image", False),
    ("/é.png", "image", False),
    # relative and empty
    ("x.png", "image", False),
    ("./x.png", "image", False),
    ("../x.png", "image", False),
    ("", "image", False),
    ("example.com/x.png", "image", False),
    # data URLs
    (PNG, "image", True),
    ("data:image/jpeg;base64,/9j/4AAQ", "image", True),
    ("data:image/gif;base64,R0lGODlh", "image", True),
    ("data:image/webp;base64,UklGRg==", "image", True),
    ("DATA:image/png;base64,iVBOR", "image", False),
    ("data:image/svg+xml;base64,PHN2Zz4=", "image", False),
    ("data:image/svg+xml,<svg/>", "image", False),
    ("data:text/html;base64,PGI+", "image", False),
    ("data:text/html,<script>alert(1)</script>", "image", False),
    ("data:image/png,rawbytes", "image", False),
    ("data:image/png;base64,", "image", False),
    ("data:image/png;base64,AA AA", "image", False),
    ("data:image/png;base64,AA!A", "image", False),
    ("data:image/png;charset=x;base64,AAAA", "image", False),
    (PNG, "video", False),
    (PNG, "audio", False),
    ("data:video/mp4;base64,AAAA", "video", False),
    ("data:audio/mpeg;base64,AAAA", "audio", False),
    # allowed hosts
    ("https://example.com/a.png", "image", True),
    ("http://example.com/a.png", "image", True),
    ("https://example.com", "image", True),
    ("https://EXAMPLE.com/a.png", "image", True),
    ("HTTPS://example.com/a.mp4", "video", True),
    ("https://example.com:8443/a.png", "image", True),
    ("https://example.com:/a.png", "image", True),
    ("https://cdn.example.org/a.mp3", "audio", True),
    ("https://example.com/a.png?x=@evil.com", "image", True),
    # host tricks
    ("https://evil.example.com/a.png", "image", False),
    ("https://example.com.evil.io/a.png", "image", False),
    ("https://notexample.com/a.png", "image", False),
    ("https://example.org/a.png", "image", False),
    ("https://org/a.png", "image", False),
    ("https://example.com@evil.com/a.png", "image", False),
    ("https://evil.com@example.com/a.png", "image", False),
    ("https://user:pw@example.com/a.png", "image", False),
    ("https://example.com%2f@evil.com/a.png", "image", False),
    ("https://example.com#@evil.com/a.png", "image", True),
    ("https://example.com\\@evil.com/a.png", "image", False),
    ("https://example.com./a.png", "image", False),
    ("https://*.example.com/a.png", "image", False),
    ("https://xn--exmple-cua.com/a.png", "image", False),
    ("https://exаmple.com/a.png", "image", False),  # Cyrillic a
    ("https://example.K.com/a.png", "image", False),  # Kelvin sign
    ("https://example.com:99999/a.png", "image", False),
    ("https://example.com:80:80/a.png", "image", False),
    ("https:example.com/a.png", "image", False),
    ("https:///example.com/a.png", "image", False),
    ("https://", "image", False),
    ("https://[::1]/a.png", "image", False),
    ("https://127.0.0.1/a.png", "image", False),
    ("https://localhost/a.png", "image", False),
    # other schemes
    ("javascript:alert(1)", "image", False),
    ("JaVaScRiPt:alert(1)", "image", False),
    ("file:///etc/passwd", "image", False),
    ("ftp://example.com/a.png", "image", False),
    ("blob:https://example.com/uuid", "image", False),
    ("vbscript:x", "video", False),
    ("ws://example.com/a", "audio", False),
    ("about:blank", "image", False),
    ("mailto:a@example.com", "image", False),
]


@pytest.mark.parametrize(("url", "kind", "allowed"), POLICY)
def test_the_media_policy_table(url: str, kind: str, allowed: bool) -> None:
    assert media_url_allowed(url, HOSTS, kind) is allowed  # type: ignore[arg-type]
    refusal = media_url_refusal(url, HOSTS, kind)  # type: ignore[arg-type]
    assert (refusal is None) is allowed
    assert refusal is None or refusal in REASONS


def test_the_policy_table_is_big_enough() -> None:
    assert len(POLICY) >= 40
    assert sum(1 for *_, allowed in POLICY if not allowed) >= 40


def test_no_host_is_allowed_by_default() -> None:
    assert not media_url_allowed("https://example.com/a.png", ())
    assert media_url_allowed("/a.png", ())
    assert media_url_allowed(PNG, ())


def test_a_data_image_has_a_size_cap() -> None:
    body = "A" * (MAX_DATA_URL_CHARS - len("data:image/png;base64,"))
    assert media_url_allowed("data:image/png;base64," + body, ())
    assert media_url_refusal("data:image/png;base64," + body + "A", ()) == (
        "data url too large"
    )


@pytest.mark.parametrize("value", [None, 5, 1.5, True, ["/a.png"], {"a": 1}, b"/a"])
def test_a_url_that_is_not_text_is_refused(value: object) -> None:
    assert media_url_refusal(value, HOSTS) == "url is not text"


@pytest.mark.parametrize(
    "url", ["https://evil.test/secret-token-123.png", "data:text/html,secret-token-123"]
)
def test_a_reason_never_repeats_the_url(url: str) -> None:
    assert "secret-token-123" not in str(media_url_refusal(url, HOSTS))


# --- drawing ---------------------------------------------------------------


def media_surface(*items: dict[str, object]):
    ids = [str(item["id"]) for item in items]
    return validator_with(
        [{"id": "root", "component": "Column", "children": ids}, *items]
    )


async def open_media(user: User, validator, hosts: tuple[str, ...] = HOSTS) -> None:
    @ui.page("/a2ui-test")
    def page() -> None:
        with ui.column().classes("w-full").mark("a2ui-root"):
            render_surface(validator, "s", lambda *_: None, media_hosts=hosts)

    await user.open("/a2ui-test")


def all_strings(user: User) -> list[str]:
    """Every string in the props and text of every element of the page."""
    out: list[str] = []
    with user.client:
        for element in user.client.elements.values():
            out += [str(value) for value in element.props.values()]
            out.append(str(getattr(element, "text", "")))
            out.append(str(getattr(element, "content", "")))
    return out


async def test_an_allowed_image_is_drawn_full_width_with_alt_and_fit(
    user: User,
) -> None:
    await open_media(
        user,
        media_surface(
            {
                "id": "i",
                "component": "Image",
                "url": "/files/a.png",
                "description": "A cat",
                "fit": "scaleDown",
            }
        ),
    )
    (image,) = found(user, "a2ui-image")
    assert isinstance(image, ui.image)
    assert image.props["src"] == "/files/a.png"
    assert image.props["alt"] == "A cat"
    assert image.props["fit"] == "scale-down"
    assert "w-full" in image.classes
    assert shown(user, "a2ui-media-refused") == []


async def test_a_path_that_names_a_real_server_file_is_not_served_as_a_file(
    user: User, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"x")
    url = secret.as_posix()
    if not url.startswith("/"):
        pytest.skip("needs a POSIX style absolute path")
    await open_media(
        user,
        media_surface({"id": "i", "component": "Image", "url": url}),
    )
    (image,) = found(user, "a2ui-image")
    assert image.props["src"] == url
    assert image.auto_route is None  # NiceGUI added no route that serves the file


async def test_a_data_image_and_an_allowed_host_are_drawn(user: User) -> None:
    await open_media(
        user,
        media_surface(
            {"id": "a", "component": "Image", "url": PNG},
            {"id": "b", "component": "Image", "url": "https://cdn.example.org/b.png"},
        ),
    )
    sources = [element.props["src"] for element in found(user, "a2ui-image")]
    assert sources == [PNG, "https://cdn.example.org/b.png"]


async def test_a_video_and_an_audio_player_are_drawn_full_width(user: User) -> None:
    await open_media(
        user,
        media_surface(
            {
                "id": "v",
                "component": "Video",
                "url": "/v.mp4",
                "posterUrl": "/poster.png",
            },
            {
                "id": "a",
                "component": "AudioPlayer",
                "url": "https://example.com/a.mp3",
                "description": "Theme tune",
            },
        ),
    )
    (video,) = found(user, "a2ui-video")
    (audio,) = found(user, "a2ui-audio")
    assert isinstance(video, ui.video)
    assert isinstance(audio, ui.audio)
    assert video.props["src"] == "/v.mp4"
    assert video.props["poster"] == "/poster.png"
    assert audio.props["src"] == "https://example.com/a.mp3"
    assert "w-full" in video.classes
    assert "w-full" in audio.classes
    assert shown(user, "a2ui-audio-text") == ["Theme tune"]


REFUSED = [
    ("Image", "https://evil.test/p.png", "host not allowed"),
    ("Image", "javascript:alert(1)", "scheme not allowed"),
    ("Image", "x.png", "relative or protocol-relative url"),
    ("Image", "data:image/svg+xml;base64,PHN2Zz4=", "data url type not allowed"),
    ("Image", "//evil.test/p.png", "relative or protocol-relative url"),
    ("Video", PNG, "data url type not allowed"),
    ("AudioPlayer", PNG, "data url type not allowed"),
    ("Video", "https://evil.test/v.mp4", "host not allowed"),
    ("AudioPlayer", "file:///etc/passwd", "scheme not allowed"),
]


@pytest.mark.parametrize(("kind", "url", "reason"), REFUSED)
async def test_a_refused_url_shows_a_line_and_is_in_no_element(
    user: User, kind: str, url: str, reason: str
) -> None:
    await open_media(
        user,
        media_surface({"id": "m", "component": kind, "url": url}),
    )
    assert shown(user, "a2ui-media-refused") == [f"[A2UI media refused: {reason}]"]
    for marker in ("a2ui-image", "a2ui-video", "a2ui-audio"):
        assert found(user, marker) == []
    assert not [text for text in all_strings(user) if url and url in text]


async def test_a_refused_poster_shows_a_line_and_the_video_has_no_poster(
    user: User,
) -> None:
    await open_media(
        user,
        media_surface(
            {
                "id": "v",
                "component": "Video",
                "url": "/v.mp4",
                "posterUrl": "https://evil.test/p.png",
            }
        ),
    )
    assert shown(user, "a2ui-media-refused") == [
        "[A2UI media refused: host not allowed]"
    ]
    (video,) = found(user, "a2ui-video")
    assert "poster" not in video.props
    assert not [t for t in all_strings(user) if "evil.test" in t]


async def test_a_bound_url_goes_through_the_same_policy(user: User) -> None:
    validator = validator_with(
        [
            {"id": "root", "component": "Column", "children": ["ok", "bad"]},
            {"id": "ok", "component": "Image", "url": {"@path": "/good"}},
            {"id": "bad", "component": "Image", "url": {"@path": "/evil"}},
        ],
        data={"good": "/pics/a.png", "evil": "https://evil.test/a.png"},
    )
    await open_media(user, validator)
    assert [e.props["src"] for e in found(user, "a2ui-image")] == ["/pics/a.png"]
    assert shown(user, "a2ui-media-refused") == [
        "[A2UI media refused: host not allowed]"
    ]


async def test_a_called_url_goes_through_the_same_policy(user: User) -> None:
    validator = validator_with(
        [
            {
                "id": "root",
                "component": "Image",
                "url": {
                    "@call": "formatString",
                    "args": {"value": "https://evil.test/${/n}.png"},
                },
            }
        ]
    )
    await open_media(user, validator)
    assert found(user, "a2ui-image") == []
    assert shown(user, "a2ui-media-refused") == [
        "[A2UI media refused: host not allowed]"
    ]


async def test_a_missing_binding_is_a_value_error_not_a_media_element(
    user: User,
) -> None:
    validator = validator_with(
        [{"id": "root", "component": "Image", "url": {"@path": "/nope"}}]
    )
    await open_media(user, validator)
    assert found(user, "a2ui-image") == []
    assert shown(user, "a2ui-error") == ["[A2UI value error: path /nope not found]"]


async def test_no_host_means_only_server_paths_and_data_images(user: User) -> None:
    await open_media(
        user,
        media_surface(
            {"id": "i", "component": "Image", "url": "https://example.com/a.png"}
        ),
        hosts=(),
    )
    assert found(user, "a2ui-image") == []
    assert shown(user, "a2ui-media-refused") == [
        "[A2UI media refused: host not allowed]"
    ]


async def test_a_surface_with_all_three_media_components_has_no_fixed_sizes(
    user: User,
) -> None:
    await open_media(
        user,
        media_surface(
            {"id": "i", "component": "Image", "url": "/a.png", "variant": "header"},
            {"id": "v", "component": "Video", "url": "/v.mp4", "posterUrl": "/p.png"},
            {"id": "a", "component": "AudioPlayer", "url": "/a.mp3"},
        ),
    )
    assert bad_sizing(user) == []
    media = [
        element
        for marker in ("a2ui-image", "a2ui-video", "a2ui-audio")
        for element in found(user, marker)
    ]
    assert len(media) == 3
    assert all("w-full" in element.classes for element in media)
    for element in media:
        assert not any(
            key in element._style for key in ("height", "width", "max-width")
        )


# --- the config key --------------------------------------------------------

PROVIDER_TOML = (
    '[providers.local]\ntype = "ollama"\nbase_url = "http://localhost:11434"\n'
)


def write_config(directory: Path, toml: str) -> None:
    directory.joinpath("config.toml").write_text(toml, encoding="utf-8")
    directory.joinpath("models.yaml").write_text(EXAMPLE_MODELS_YAML, encoding="utf-8")
    directory.joinpath("agents.yaml").write_text(EXAMPLE_AGENTS_YAML, encoding="utf-8")


def test_the_media_hosts_default_is_empty() -> None:
    assert GuiConfig().a2ui_media_hosts == []


def test_a_loaded_config_has_no_media_host_by_default(tmp_path: Path) -> None:
    write_config(tmp_path, PROVIDER_TOML)
    assert load_config(tmp_path).gui.a2ui_media_hosts == []


def test_valid_media_hosts_load_as_written(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        PROVIDER_TOML
        + '[gui]\na2ui_media_hosts = ["images.example.com", "a-b.io", "localhost", "xn--exmple-cua.com"]\n',
    )
    assert load_config(tmp_path).gui.a2ui_media_hosts == [
        "images.example.com",
        "a-b.io",
        "localhost",
        "xn--exmple-cua.com",
    ]


@pytest.mark.parametrize(
    "entry",
    [
        "https://example.com",
        "example.com/path",
        "example.com:8080",
        "*.example.com",
        "Example.com",
        "",
        " example.com",
        "example..com",
        ".example.com",
        "example.com.",
        "-a.com",
        "a-.com",
        "user@example.com",
        "exаmple.com",
        "a_b.com",
        "a" * 64 + ".com",
    ],
)
def test_a_bad_media_host_is_a_clear_config_error(tmp_path: Path, entry: str) -> None:
    write_config(tmp_path, PROVIDER_TOML + f'[gui]\na2ui_media_hosts = ["{entry}"]\n')
    with pytest.raises(
        ConfigError, match=r"gui\.a2ui_media_hosts.*lowercase host name"
    ):
        load_config(tmp_path)


def test_a_duplicate_media_host_and_a_wrong_type_are_errors(tmp_path: Path) -> None:
    write_config(
        tmp_path, PROVIDER_TOML + '[gui]\na2ui_media_hosts = ["a.com", "a.com"]\n'
    )
    with pytest.raises(ConfigError, match="duplicate entries: a.com"):
        load_config(tmp_path)
    write_config(tmp_path, PROVIDER_TOML + '[gui]\na2ui_media_hosts = "a.com"\n')
    with pytest.raises(ConfigError, match="a2ui_media_hosts"):
        load_config(tmp_path)
    write_config(tmp_path, 'gui = "x"\n' + PROVIDER_TOML)
    with pytest.raises(ConfigError, match="gui must be a table"):
        load_config(tmp_path)
    write_config(tmp_path, PROVIDER_TOML + "[gui]\nbogus = 1\n")
    with pytest.raises(ConfigError, match="bogus"):
        load_config(tmp_path)


def test_the_example_config_documents_the_key_and_loads_when_switched_on(
    tmp_path: Path,
) -> None:
    assert '# a2ui_media_hosts = ["images.example.com"]' in EXAMPLE_CONFIG_TOML
    lines = EXAMPLE_CONFIG_TOML.splitlines()
    start = lines.index("# [gui]")
    block = [line.removeprefix("# ") for line in lines[start : start + 2]]
    write_config(tmp_path, EXAMPLE_CONFIG_TOML + "\n" + "\n".join(block) + "\n")
    # The example already holds the commented table; an uncommented copy must load.
    assert load_config(tmp_path).gui.a2ui_media_hosts == ["images.example.com"]
