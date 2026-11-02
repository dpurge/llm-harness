"""Tests for the command policy: resolution, the deny and allow lists, PATH hygiene.

The fake programs are empty files with the executable bit; nothing is ever run.
"""

import os
from pathlib import Path

import pytest

from llm_harness.command_policy import (
    DEFAULT_COMMAND_DENY,
    CommandDenied,
    check_policy,
    child_path,
    resolve_program,
)

# One concrete name per built-in pattern (`mkfs*` is covered by `mkfs.ext4`).
_DENIED_NAMES = [
    name if not name.endswith("*") else name[:-1] + ".ext4"
    for name in DEFAULT_COMMAND_DENY
]
_ACCEPTED_NAMES = ["python3", "python3.13", "node", "perl", "ruby", "git", "rm"]


def _make_exe(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / name
    program.write_text("")
    program.chmod(0o755)
    return program


@pytest.fixture
def bindir(tmp_path: Path) -> Path:
    return tmp_path / "bin"


def _check(
    name: str,
    bindir: Path,
    tmp_path: Path,
    allow: tuple[str, ...] = (),
    deny: tuple[str, ...] = (),
) -> Path:
    return check_policy([name], tmp_path, str(bindir), allow, deny)


def test_the_built_in_list_has_every_documented_name() -> None:
    assert len(DEFAULT_COMMAND_DENY) == len(set(DEFAULT_COMMAND_DENY)) == 45
    assert len(_DENIED_NAMES) == 45


@pytest.mark.parametrize("name", _DENIED_NAMES)
def test_every_built_in_name_is_denied(name: str, bindir: Path, tmp_path: Path) -> None:
    _make_exe(bindir, name)
    with pytest.raises(CommandDenied, match="built-in deny list"):
        _check(name, bindir, tmp_path)


@pytest.mark.parametrize("name", _ACCEPTED_NAMES)
def test_interpreters_git_and_rm_are_accepted_by_default(
    name: str, bindir: Path, tmp_path: Path
) -> None:
    program = _make_exe(bindir, name)
    assert _check(name, bindir, tmp_path) == program


def test_a_symlink_to_a_denied_program_is_denied_and_names_the_real_path(
    bindir: Path, tmp_path: Path
) -> None:
    real = _make_exe(tmp_path / "real", "bash")
    bindir.mkdir()
    (bindir / "safe").symlink_to(real)
    with pytest.raises(CommandDenied) as raised:
        _check("safe", bindir, tmp_path)
    message = str(raised.value)
    assert "'safe'" in message
    assert str(Path(os.path.realpath(real))) in message
    assert "built-in deny list" in message


@pytest.mark.parametrize("name", ["BASH", "Bash", "Curl", "MKFS.ext4"])
def test_case_variants_of_a_denied_name_are_denied(
    name: str, bindir: Path, tmp_path: Path
) -> None:
    # One file per name, so this holds on case-sensitive and insensitive disks.
    _make_exe(bindir, name)
    with pytest.raises(CommandDenied, match="built-in deny list"):
        _check(name, bindir, tmp_path)


def test_a_deny_entry_is_case_folded_against_the_given_name(
    bindir: Path, tmp_path: Path
) -> None:
    _make_exe(bindir, "Tool")
    with pytest.raises(CommandDenied, match="command_deny"):
        _check("Tool", bindir, tmp_path, deny=("TOOL",))
    with pytest.raises(CommandDenied, match="command_deny"):
        _check("Tool", bindir, tmp_path, deny=("t?ol",))


def test_the_real_name_is_matched_even_when_the_given_name_is_harmless(
    bindir: Path, tmp_path: Path
) -> None:
    real = _make_exe(tmp_path / "real", "Secret")
    bindir.mkdir()
    (bindir / "harmless").symlink_to(real)
    with pytest.raises(CommandDenied, match="command_deny"):
        _check("harmless", bindir, tmp_path, deny=("secret",))


def test_deny_beats_allow(bindir: Path, tmp_path: Path) -> None:
    _make_exe(bindir, "tool")
    with pytest.raises(CommandDenied, match="command_deny"):
        _check("tool", bindir, tmp_path, allow=("tool",), deny=("tool",))


def test_the_built_in_list_beats_allow(bindir: Path, tmp_path: Path) -> None:
    _make_exe(bindir, "bash")
    with pytest.raises(CommandDenied, match="built-in deny list"):
        _check("bash", bindir, tmp_path, allow=("bash",))


def test_an_empty_allow_list_accepts_anything_not_denied(
    bindir: Path, tmp_path: Path
) -> None:
    program = _make_exe(bindir, "tool")
    assert _check("tool", bindir, tmp_path, deny=("other",)) == program


def test_a_non_empty_allow_list_accepts_a_match_and_refuses_the_rest(
    bindir: Path, tmp_path: Path
) -> None:
    allowed = _make_exe(bindir, "good")
    _make_exe(bindir, "bad")
    assert _check("good", bindir, tmp_path, allow=("good",)) == allowed
    with pytest.raises(CommandDenied, match="command_allow"):
        _check("bad", bindir, tmp_path, allow=("good",))


def test_an_allow_entry_is_an_fnmatch_basename_pattern(
    bindir: Path, tmp_path: Path
) -> None:
    program = _make_exe(bindir, "python3")
    _make_exe(bindir, "ruby")
    assert _check("python3", bindir, tmp_path, allow=("py*",)) == program
    with pytest.raises(CommandDenied, match="command_allow"):
        _check("ruby", bindir, tmp_path, allow=("py*",))


def test_an_allow_pattern_cannot_match_across_directories(
    bindir: Path, tmp_path: Path
) -> None:
    _make_exe(bindir, "tool")
    with pytest.raises(CommandDenied, match="command_allow"):
        _check("tool", bindir, tmp_path, allow=("bin*tool",))


def test_an_absolute_allow_entry_matches_the_resolved_path(
    bindir: Path, tmp_path: Path
) -> None:
    program = _make_exe(bindir, "tool")
    assert _check("tool", bindir, tmp_path, allow=(str(program),)) == program
    with pytest.raises(CommandDenied, match="command_allow"):
        _check("tool", bindir, tmp_path, allow=(str(program) + "x",))


def test_an_absolute_allow_entry_matches_the_real_path_of_a_symlink(
    bindir: Path, tmp_path: Path
) -> None:
    real = _make_exe(tmp_path / "real", "tool")
    bindir.mkdir()
    link = bindir / "alias"
    link.symlink_to(real)
    real_path = os.path.realpath(real)
    assert _check("alias", bindir, tmp_path, allow=(real_path,)) == link
    assert _check("alias", bindir, tmp_path, allow=(str(link),)) == link


def test_an_absolute_path_entry_is_not_a_pattern_and_not_case_folded(
    bindir: Path, tmp_path: Path
) -> None:
    program = _make_exe(bindir, "tool")
    with pytest.raises(CommandDenied, match="command_allow"):
        _check("tool", bindir, tmp_path, allow=(str(program).replace("tool", "t*"),))
    with pytest.raises(CommandDenied, match="command_allow"):
        _check("tool", bindir, tmp_path, allow=(str(program).upper(),))


def test_absolute_deny_entries_match_resolved_and_real_path(
    bindir: Path, tmp_path: Path
) -> None:
    real = _make_exe(tmp_path / "real", "tool")
    bindir.mkdir()
    link = bindir / "alias"
    link.symlink_to(real)
    with pytest.raises(CommandDenied, match="command_deny"):
        _check("alias", bindir, tmp_path, deny=(str(link),))
    with pytest.raises(CommandDenied, match="command_deny"):
        _check("alias", bindir, tmp_path, deny=(os.path.realpath(real),))


def test_only_argv0_is_examined(bindir: Path, tmp_path: Path) -> None:
    program = _make_exe(bindir, "tool")
    assert check_policy(["tool", "bash", "-c", "x"], tmp_path, str(bindir), (), ()) == (
        program
    )


def test_a_relative_name_with_a_slash_resolves_against_cwd(
    bindir: Path, tmp_path: Path
) -> None:
    program = _make_exe(tmp_path / "work" / "sub", "tool")
    work = tmp_path / "work"
    assert check_policy(["sub/tool"], work, str(bindir), (), ()) == program
    assert check_policy(["./sub/tool"], work, str(bindir), (), ()).samefile(program)


def test_dot_dot_cannot_hide_a_denied_name(bindir: Path, tmp_path: Path) -> None:
    _make_exe(tmp_path / "work", "bash")
    (tmp_path / "work" / "sub").mkdir()
    with pytest.raises(CommandDenied, match="built-in deny list"):
        check_policy(["sub/../bash"], tmp_path / "work", str(bindir), (), ())


def test_an_absolute_name_is_used_as_it_is(bindir: Path, tmp_path: Path) -> None:
    program = _make_exe(tmp_path / "elsewhere", "tool")
    assert check_policy([str(program)], tmp_path, str(bindir), (), ()) == program


def test_a_relative_path_entry_is_never_searched(
    bindir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A name no system directory holds, because the fallback PATH is searched.
    _make_exe(tmp_path, "zz-cwd-only-tool")
    monkeypatch.chdir(tmp_path)
    for raw_path in (".:", ":", ".", "", ".:" + str(bindir)):
        path = child_path(raw_path)
        with pytest.raises(CommandDenied, match="not found"):
            check_policy(["zz-cwd-only-tool"], tmp_path, path, (), ())


def test_child_path_keeps_absolute_entries_in_order_without_repeats() -> None:
    raw = os.pathsep.join(["/b", "", ".", "rel/dir", "/a", "/b", "/c"])
    assert child_path(raw) == os.pathsep.join(["/b", "/a", "/c"])


@pytest.mark.parametrize("raw", [None, "", ":", ".:rel", "rel"])
def test_child_path_falls_back_to_a_fixed_default(raw: str | None) -> None:
    assert child_path(raw) == "/usr/local/bin:/usr/bin:/bin"


def test_a_missing_program_is_refused(bindir: Path, tmp_path: Path) -> None:
    bindir.mkdir()
    with pytest.raises(CommandDenied, match="'nothing' not found"):
        _check("nothing", bindir, tmp_path)
    with pytest.raises(CommandDenied, match="not found"):
        check_policy(["./nothing"], tmp_path, str(bindir), (), ())


def test_a_directory_is_refused(bindir: Path, tmp_path: Path) -> None:
    (bindir / "adir").mkdir(parents=True)
    with pytest.raises(CommandDenied, match="not a file|not found"):
        _check("adir", bindir, tmp_path)
    with pytest.raises(CommandDenied, match="not a file"):
        check_policy([str(bindir / "adir")], tmp_path, str(bindir), (), ())


def test_a_non_executable_file_is_refused(bindir: Path, tmp_path: Path) -> None:
    bindir.mkdir()
    (bindir / "plain").write_text("")
    (bindir / "plain").chmod(0o644)
    with pytest.raises(CommandDenied, match="not executable|not found"):
        _check("plain", bindir, tmp_path)
    with pytest.raises(CommandDenied, match="not executable"):
        check_policy([str(bindir / "plain")], tmp_path, str(bindir), (), ())


def test_a_dangling_symlink_is_refused(bindir: Path, tmp_path: Path) -> None:
    bindir.mkdir()
    (bindir / "dead").symlink_to(tmp_path / "missing")
    with pytest.raises(CommandDenied):
        check_policy([str(bindir / "dead")], tmp_path, str(bindir), (), ())


def test_an_empty_argv_is_refused(bindir: Path, tmp_path: Path) -> None:
    with pytest.raises(CommandDenied, match="argv is empty"):
        check_policy([], tmp_path, str(bindir), (), ())


@pytest.mark.parametrize("name", ["", "a\0b"])
def test_an_empty_name_or_one_with_nul_is_refused(
    name: str, bindir: Path, tmp_path: Path
) -> None:
    with pytest.raises(CommandDenied, match="empty or contains a NUL"):
        check_policy([name], tmp_path, str(bindir), (), ())


def test_the_message_escapes_control_characters_and_cuts_a_long_name(
    bindir: Path, tmp_path: Path
) -> None:
    bindir.mkdir()
    with pytest.raises(CommandDenied) as raised:
        _check("evil\x1b[31m\n" + "x" * 500, bindir, tmp_path)
    message = str(raised.value)
    assert "\x1b" not in message and "\n" not in message
    assert len(message) < 300


def test_the_result_is_absolute_and_is_the_symlink_not_its_target(
    bindir: Path, tmp_path: Path
) -> None:
    real = _make_exe(tmp_path / "real", "tool")
    bindir.mkdir()
    link = bindir / "alias"
    link.symlink_to(real)
    result = _check("alias", bindir, tmp_path)
    assert result.is_absolute()
    assert result == link
    assert result != Path(os.path.realpath(real))


def test_resolve_program_returns_an_absolute_path_for_a_relative_search_path(
    bindir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_exe(bindir, "tool")
    monkeypatch.chdir(tmp_path)
    assert resolve_program("tool", tmp_path, "bin").is_absolute()
