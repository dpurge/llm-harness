"""POSIX-only calls are guarded: the platform is simulated, nothing needs Windows.

Real Windows behaviour is not covered here; these tests prove that the code takes
its Windows branch without touching `O_NOFOLLOW`, `O_NONBLOCK`, `fchmod` or mode bits.
"""

import asyncio
import os
from pathlib import Path

import pytest

from llm_harness import pathing
from tests.platform_marks import posix_only
from llm_harness.config import ToolsConfig, permission_warnings
from llm_harness.secrets_file import SECRETS_FILE, load_secrets
from llm_harness.state_files import read_import, write_export
from llm_harness.storage.files import FileStoreError, LocalFileStore


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the POSIX-only names from `os` and report no mode bits."""
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "fchmod"):
        monkeypatch.delattr(os, name, raising=False)
    monkeypatch.setattr(pathing, "has_posix_modes", lambda: False)


def test_helpers_degrade_to_no_ops(windows: None) -> None:
    assert pathing.nofollow_flag() == 0
    assert pathing.nonblock_flag() == 0
    pathing.fchmod_private(0)  # must not raise: there is no os.fchmod


@posix_only
def test_helpers_use_the_real_flags_on_posix() -> None:
    assert pathing.nofollow_flag() == os.O_NOFOLLOW
    assert pathing.nonblock_flag() == os.O_NONBLOCK


def test_file_store_round_trips_without_posix_flags(
    windows: None, tmp_path: Path
) -> None:
    store = LocalFileStore(tmp_path / "files")
    asyncio.run(store.put("a/b.txt", b"x\r\ny"))
    assert asyncio.run(store.get("a/b.txt")) == b"x\r\ny"


@posix_only
def test_file_store_refuses_a_symlink_without_o_nofollow(
    windows: None, tmp_path: Path
) -> None:
    store = LocalFileStore(tmp_path / "files")
    asyncio.run(store.put("real.txt", b"secret"))
    os.symlink(tmp_path / "files" / "real.txt", tmp_path / "files" / "link.txt")
    with pytest.raises(FileStoreError, match="not a regular file"):
        asyncio.run(store.get("link.txt"))


def test_state_export_and_import_work_without_posix_flags(
    windows: None, tmp_path: Path
) -> None:
    target = tmp_path / "state.json"
    write_export(target, b"data")
    assert read_import(target) == b"data"


def test_loose_secrets_file_is_accepted_where_modes_mean_nothing(
    windows: None, tmp_path: Path
) -> None:
    path = tmp_path / SECRETS_FILE
    path.write_text('[qdrant]\napi_key = "k"\n')
    os.chmod(path, 0o666)
    assert load_secrets(tmp_path).qdrant_api_key is not None


@posix_only
def test_loose_secrets_file_is_still_refused_on_posix(tmp_path: Path) -> None:
    path = tmp_path / SECRETS_FILE
    path.write_text('[qdrant]\napi_key = "k"\n')
    os.chmod(path, 0o666)
    with pytest.raises(Exception, match="accessible by group or other users"):
        load_secrets(tmp_path)


def test_no_permission_warnings_where_modes_mean_nothing(
    windows: None, tmp_path: Path
) -> None:
    (tmp_path / "config.toml").write_text("")
    os.chmod(tmp_path / "config.toml", 0o666)
    assert permission_warnings(tmp_path) == []


@pytest.mark.parametrize("key", ["command_allow", "command_deny"])
def test_command_paths_may_contain_spaces_but_names_may_not(key: str) -> None:
    ok = ["/opt/My Tools/git"]
    assert getattr(ToolsConfig.model_validate({key: ok}), key) == ok
    for bad in (["my tool"], ["/opt/a\tb/git"], ["/opt/a\nb/git"]):
        with pytest.raises(ValueError, match="must not contain whitespace"):
            ToolsConfig.model_validate({key: bad})
