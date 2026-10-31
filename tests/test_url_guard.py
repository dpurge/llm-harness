"""url_guard: URL text rules and the public-address rule, offline."""

import ipaddress
from urllib.parse import urlsplit

import httpx
import pytest

from llm_harness.url_guard import (
    SafeUrl,
    UrlBlocked,
    check_addresses,
    check_url,
    is_public_address,
)

BLOCKED_ADDRESSES = [
    # IPv4
    "127.0.0.1",
    "127.1.2.3",
    "0.0.0.0",
    "10.0.0.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "169.254.169.254",
    "100.64.0.1",
    "100.100.100.200",
    "192.0.0.192",
    "192.0.2.1",
    "198.18.0.1",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    # IPv6
    "::1",
    "::",
    "fe80::1",
    "fec0::1",
    "fc00::1",
    "fd00:ec2::254",
    "ff02::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1: is_global alone says True
    "64:ff9b::808:808",  # NAT64 of a public IPv4: refused by decision
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b:1::1",  # local-use NAT64
    "64:ff9b:1::808:808",
    "::ffff:0:7f00:1",  # SIIT of 127.0.0.1
    "2002:7f00:1::1",  # 6to4 of 127.0.0.1
    "2002:808:808::1",  # 6to4 of a public IPv4
    "2001:db8::1",
    "::7f00:1",  # IPv4-compatible: is_global says True
    "::8.8.8.8",  # IPv4-compatible, even with a public IPv4
    "2001:0:4136:e378:8000:63bf:f5ff:fdd2",  # Teredo, client 10.0.2.45
    "2001:0:0a00:0001:8000:0000:0808:0808",  # Teredo, private server
    "not an address",
]

PUBLIC_ADDRESSES = [
    "8.8.8.8",
    "1.1.1.1",
    "2606:4700:4700::1111",
    "2001:4860:4860::8888",
    "::ffff:8.8.8.8",
]


@pytest.mark.parametrize("address", BLOCKED_ADDRESSES)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert is_public_address(address) is False


@pytest.mark.parametrize("address", PUBLIC_ADDRESSES)
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address) is True


def test_address_objects_are_accepted() -> None:
    assert is_public_address(ipaddress.ip_address("8.8.8.8"))
    assert not is_public_address(ipaddress.ip_address("64:ff9b::7f00:1"))


def test_is_global_alone_is_not_enough() -> None:
    # The trap this module exists for; if Python changes, re-read the module.
    assert ipaddress.ip_address("64:ff9b::7f00:1").is_global
    assert ipaddress.ip_address("::7f00:1").is_global
    assert not is_public_address("64:ff9b::7f00:1")


def test_a_host_with_one_private_address_is_refused() -> None:
    with pytest.raises(UrlBlocked) as caught:
        check_addresses("example.com", ["8.8.8.8", "10.0.0.1"])
    assert str(caught.value) == "blocked: example.com resolves to a non-public address"


def test_no_addresses_is_refused() -> None:
    with pytest.raises(UrlBlocked, match="example.com resolves to a non-public"):
        check_addresses("example.com", [])


def test_all_public_addresses_pass() -> None:
    check_addresses(
        "example.com", ["8.8.8.8", ipaddress.ip_address("2606:4700:4700::1111")]
    )


def test_an_ip_literal_host_follows_the_same_rule() -> None:
    check_addresses("8.8.8.8", ["8.8.8.8"])
    with pytest.raises(UrlBlocked):
        check_addresses("127.0.0.1", ["127.0.0.1"])


REJECTED_URLS = [
    "ftp://example.com/",
    "file:///etc/passwd",
    "gopher://example.com/",
    "data:text/plain,x",
    "javascript:alert(1)",
    "example.com/path",
    "http://example.com:22/",
    "http://example.com:8080/",
    "http://127.0.0.1:8080/",
    "http://example.com:99999/",
    "http://example.com:abc/",
    "http://example.com:/",
    "https://example.com:80443/",
    "http://u:p@example.com/",
    "http://:p@example.com/",
    "http://example.com@127.0.0.1/",
    "http://127.0.0.1\\@example.com/",
    "http://example.com\\@127.0.0.1/",
    "http://example.com/\\@127.0.0.1",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/",
    "http://[fe80::1%25eth0]/",
    "http://[::1/",
    "http://::1/",
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/",
    "http://localhost/",
    "http://LOCALHOST/",
    "http://localhost./",
    "http://a.localhost/",
    "http://a.b.localhost./",
    "http://2130706433/",
    "http://0x7f000001/",
    "http://017700000001/",
    "http://127.1/",
    "http://0177.0.0.1/",
    "http://1.2.3/",
    "http://1.2.3.4.5/",
    "http://256.1.1.1/",
    "http://01.2.3.4/",
    "http://0x7f.0.0.1/",
    "http://example.123/",
    "http://127。0。0。1/",  # ideographic full stop: httpx maps it to dots
    "http://bücher.de/",
    "http://exa%6Dple.com/",
    "http://%31%32%37.0.0.1/",
    "http:///path",
    "http://",
    "http://./",
    "http://-bad.example.com/",
    "http://exa mple.com/",
    "http://example.com/a b",
    " http://example.com/",
    "http://example.com/\n",
    "http://example.com/\t",
    "http://example.com/\x00",
    "http://example.com/\x7f",
    "http://example.com/ ",
    "",
    "http://example.com/" + "a" * 2030,
]


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_rejected_urls(url: str) -> None:
    with pytest.raises(UrlBlocked):
        check_url(url)


def test_the_length_cap_is_exactly_2048() -> None:
    prefix = "http://example.com/"
    assert check_url(prefix + "a" * (2048 - len(prefix))).host == "example.com"
    with pytest.raises(UrlBlocked):
        check_url(prefix + "a" * (2049 - len(prefix)))


def test_a_2049_character_url_is_rejected() -> None:
    with pytest.raises(UrlBlocked):
        check_url("https://example.com/" + "a" * (2049 - len("https://example.com/")))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://example.com/a?b=c#frag",
            SafeUrl("https", "example.com", 443, "https://example.com/a?b=c"),
        ),
        (
            "HTTP://EXAMPLE.com:80/x",
            SafeUrl("http", "example.com", 80, "http://example.com/x"),
        ),
        (
            "https://example.com:443/",
            SafeUrl("https", "example.com", 443, "https://example.com/"),
        ),
        (
            "https://example.com:80/",
            SafeUrl("https", "example.com", 80, "https://example.com:80/"),
        ),
        (
            "http://example.com",
            SafeUrl("http", "example.com", 80, "http://example.com/"),
        ),
        (
            "http://Example.COM./p",
            SafeUrl("http", "example.com", 80, "http://example.com/p"),
        ),
        ("http://8.8.8.8/", SafeUrl("http", "8.8.8.8", 80, "http://8.8.8.8/")),
        (
            "https://[2606:4700:4700::1111]/",
            SafeUrl(
                "https",
                "2606:4700:4700::1111",
                443,
                "https://[2606:4700:4700::1111]/",
            ),
        ),
        (
            "https://[2606:4700:4700::1111]:443/q",
            SafeUrl(
                "https",
                "2606:4700:4700::1111",
                443,
                "https://[2606:4700:4700::1111]/q",
            ),
        ),
        (
            "http://my_host.example.com/",
            SafeUrl("http", "my_host.example.com", 80, "http://my_host.example.com/"),
        ),
    ],
)
def test_accepted_urls(url: str, expected: SafeUrl) -> None:
    assert check_url(url) == expected


def test_a_host_that_only_contains_localhost_is_not_blocked_by_name() -> None:
    assert check_url("http://notlocalhost.com/").host == "notlocalhost.com"
    assert check_url("http://localhost.example.com/").host == "localhost.example.com"


EXPECTED_MESSAGES = [
    (
        "ftp://example.com/",
        "blocked: only http and https URLs on ports 80 and 443 are allowed",
    ),
    (
        "http://example.com:8080/",
        "blocked: only http and https URLs on ports 80 and 443 are allowed",
    ),
    (
        "http://u:secret@example.com/",
        "blocked: URLs with a user name or password are not allowed",
    ),
    ("http://localhost/", "blocked: localhost resolves to a non-public address"),
]


@pytest.mark.parametrize(("url", "message"), EXPECTED_MESSAGES)
def test_error_messages(url: str, message: str) -> None:
    with pytest.raises(UrlBlocked) as caught:
        check_url(url)
    assert str(caught.value) == message


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_messages_hold_no_ip_address_or_userinfo(url: str) -> None:
    with pytest.raises(UrlBlocked) as caught:
        check_url(url)
    text = str(caught.value)
    for secret in ("secret", "127.0.0.1", "169.254", "10.0.0.1", "::1", "u:p"):
        assert secret not in text


def test_secret_userinfo_is_not_echoed() -> None:
    with pytest.raises(UrlBlocked) as caught:
        check_url("http://user:hunter2@example.com/")
    assert "hunter2" not in str(caught.value)
    assert "user" not in str(caught.value).replace("user name", "")


def test_a_non_public_literal_message_names_no_address() -> None:
    with pytest.raises(UrlBlocked) as caught:
        check_url("http://169.254.169.254/")
    assert str(caught.value) == "blocked: the URL points to a non-public address"


def _client_host(url: str) -> str | None:
    """The host httpx would connect to, or None when httpx refuses the URL."""
    try:
        return httpx.URL(url).host
    except httpx.InvalidURL:
        return None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a?b=c#frag",
        "HTTP://EXAMPLE.com:80/x",
        "http://8.8.8.8/",
        "https://[2606:4700:4700::1111]/",
        "http://Example.COM./p",
        "http://example.com/a@b",
        "http://example.com/?u=http://127.0.0.1@evil",
        "http://example.com/#@127.0.0.1",
    ],
)
def test_the_checked_host_is_the_host_a_client_connects_to(url: str) -> None:
    safe = check_url(url)
    assert (
        _client_host(safe.url) == safe.host or _client_host(safe.url) == safe.host + "."
    )
    assert urlsplit(safe.url).hostname == safe.host
    # httpx on the original text must agree as well, apart from the trailing dot.
    original = _client_host(url)
    assert original is not None
    assert original.removesuffix(".") == safe.host


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com@127.0.0.1/",
        "http://127.0.0.1\\@example.com/",
        "http://example.com\\@127.0.0.1/",
        "http://example.com:80@127.0.0.1/",
        "http://127。0。0。1/",
    ],
)
def test_parser_disagreements_are_rejected_not_guessed(url: str) -> None:
    # urllib and httpx read these differently (or httpx rewrites the host), so
    # the guard must refuse them rather than pick one reading.
    with pytest.raises(UrlBlocked):
        check_url(url)
