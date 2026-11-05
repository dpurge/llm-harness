"""`read_text_file`: the file of `/memory add @PATH`, every success and every refusal.

Files live under `tmp_path`; `~` is pointed at it through `HOME`. No model, no store.
"""

import os
import stat
from pathlib import Path

import pytest

from llm_harness import memory_file
from llm_harness.memory_file import (
    MAX_ADD_CHARS,
    MAX_FILE_BYTES,
    TextFileError,
    check_text_length,
    read_text_file,
)
from tests.platform_marks import posix_only


def write(path: Path, content: str | bytes) -> str:
    data = content.encode() if isinstance(content, str) else content
    path.write_bytes(data)
    return str(path)


# --- success (criterion 12) -----------------------------------------------------------


@pytest.mark.parametrize("name", ["a.txt", "a.md", "a.markdown", "NOTE.MD", "n.TxT"])
def test_each_accepted_extension_returns_the_text(tmp_path: Path, name: str):
    assert read_text_file(write(tmp_path / name, "I like tea.\n")) == "I like tea."


def test_a_path_with_spaces_is_read(tmp_path: Path):
    (tmp_path / "my notes").mkdir()
    assert read_text_file(write(tmp_path / "my notes" / "a b.md", "hi")) == "hi"


def test_a_tilde_path_is_expanded_from_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    write(tmp_path / "home.md", "from home")
    assert read_text_file("~/home.md") == "from home"


def test_crlf_becomes_lf_and_a_bom_is_stripped(tmp_path: Path):
    path = write(tmp_path / "a.md", b"\xef\xbb\xbfline one\r\nline two\r\n")
    assert read_text_file(path) == "line one\nline two"


def test_multibyte_text_is_decoded(tmp_path: Path):
    assert read_text_file(write(tmp_path / "a.txt", "Zażółć gęślą jaźń")) == (
        "Zażółć gęślą jaźń"
    )


@posix_only
def test_a_symlink_to_a_regular_file_is_followed(tmp_path: Path):
    target = write(tmp_path / "real.md", "linked")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    assert read_text_file(str(link)) == "linked"


def test_the_limits_are_inclusive(tmp_path: Path):
    exactly = write(tmp_path / "a.md", "a" * MAX_FILE_BYTES)
    with pytest.raises(TextFileError, match="text too long"):  # past the 4000 limit
        read_text_file(exactly)
    assert read_text_file(write(tmp_path / "b.md", "b" * MAX_ADD_CHARS)) == (
        "b" * MAX_ADD_CHARS
    )
    # a 4000-character text with surrounding blanks is measured after trimming
    assert len(read_text_file(write(tmp_path / "c.md", f"\n{'c' * 4000}\n  "))) == 4000


# --- refusals (criterion 13) ----------------------------------------------------------


def refusal(path: str) -> str:
    with pytest.raises(TextFileError) as caught:
        read_text_file(path)
    message = str(caught.value)
    assert isinstance(caught.value, ValueError)
    assert "\n" not in message and "\r" not in message
    return message


def test_a_wrong_extension_is_refused_before_the_file_is_looked_at(tmp_path: Path):
    message = "only .txt, .md and .markdown files can be added"
    assert refusal(write(tmp_path / "a.pdf", "x")) == message
    assert refusal(str(tmp_path / "missing.docx")) == message  # not even stat-ed
    assert refusal(write(tmp_path / "noext", "x")) == message
    assert refusal(write(tmp_path / ".md", "x")) == message  # a dotfile has no suffix


def test_a_missing_file_names_the_path(tmp_path: Path):
    path = tmp_path / "missing.md"
    assert refusal(str(path)) == f"no such file: {path}"
    assert refusal(str(tmp_path / "no_dir" / "a.md")).startswith("no such file: ")


def test_a_directory_named_like_a_file_is_not_a_regular_file(tmp_path: Path):
    path = tmp_path / "x.md"
    path.mkdir()
    assert refusal(str(path)) == f"not a regular file: {path}"


@posix_only
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_a_named_pipe_is_refused_without_waiting_for_a_writer(tmp_path: Path):
    path = tmp_path / "pipe.md"
    os.mkfifo(path)
    assert refusal(str(path)) == f"not a regular file: {path}"


def test_invalid_utf8_is_refused(tmp_path: Path):
    assert refusal(write(tmp_path / "a.md", b"ok \xff\xfe bad")) == (
        "file is not valid UTF-8 text"
    )


@pytest.mark.parametrize(
    "content", [b"", b"\xef\xbb\xbf", b" \r\n\t\n", b"\xef\xbb\xbf \n"]
)
def test_an_empty_bom_only_or_blank_file_is_empty(tmp_path: Path, content: bytes):
    assert refusal(write(tmp_path / "a.md", content)) == "file is empty"


def test_a_file_over_the_cap_is_refused_from_stat_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = write(tmp_path / "big.md", b"a" * (MAX_FILE_BYTES + 1))

    def no_open(*args: object, **kwargs: object):
        raise AssertionError("the file must not be opened")

    monkeypatch.setattr(memory_file.os, "open", no_open)
    assert refusal(path) == "file too large (65537 bytes, at most 65536)"


@posix_only
def test_a_file_that_grows_after_stat_is_still_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = write(tmp_path / "grow.md", b"a" * (MAX_FILE_BYTES + 10))
    real_stat = os.stat

    def small_stat(target: object, *args: object, **kwargs: object):
        info = real_stat(target, *args, **kwargs)
        return os.stat_result((info.st_mode, *info[1:6], 10, *info[7:]))

    monkeypatch.setattr(memory_file.os, "stat", small_stat)
    # the stat said 10 bytes; the read stops one byte past the cap and the size is rechecked
    assert (
        refusal(path) == f"file too large ({MAX_FILE_BYTES + 1} bytes, at most 65536)"
    )


def test_a_decoded_text_of_4001_characters_is_too_long(tmp_path: Path):
    path = write(tmp_path / "a.md", "é" * (MAX_ADD_CHARS + 1))
    assert refusal(path) == (
        "text too long (4001 characters, at most 4000); add it in parts"
    )


def test_a_blank_path_is_refused():
    assert refusal("") == "no path given"
    assert refusal("   ") == "no path given"


def test_an_unknown_home_user_is_a_missing_file():
    assert refusal("~no_such_user_x9/a.md") == "no such file: ~no_such_user_x9/a.md"


def test_a_path_with_a_newline_an_escape_and_a_bidi_character_is_shown_escaped(
    tmp_path: Path,
):
    name = "a\nb\x1bc\u202ed.md"
    message = refusal(str(tmp_path / name))
    assert message.endswith("a\\u000ab\\u001bc\\u202ed.md")
    assert not any(ch in message for ch in "\n\x1b\u202e")
    # and the same for a directory with that name, created where the platform allows it
    try:
        (tmp_path / name).mkdir()
    except OSError:
        pytest.skip("this platform refuses such a file name")
    message = refusal(str(tmp_path / name))
    assert message.startswith("not a regular file: ")
    assert not any(ch in message for ch in "\n\x1b\u202e")


def test_a_nul_byte_in_the_path_is_a_missing_file():
    assert refusal("a\x00b.md") == "no such file: a\\u0000b.md"


@posix_only
def test_an_unreadable_file_gives_the_class_name_only(tmp_path: Path):
    path = Path(write(tmp_path / "secret.md", "x"))
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("this user can read a mode 0 file (root)")
        assert refusal(str(path)) == "cannot read file: PermissionError"
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# --- the shared limit -----------------------------------------------------------------


def test_check_text_length_is_the_one_limit_of_a_paste_and_a_file():
    check_text_length("x" * MAX_ADD_CHARS)
    with pytest.raises(TextFileError) as caught:
        check_text_length("x" * (MAX_ADD_CHARS + 1))
    assert str(caught.value) == (
        "text too long (4001 characters, at most 4000); add it in parts"
    )
