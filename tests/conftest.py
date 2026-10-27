import pytest

import llm_harness.store as store_module


@pytest.fixture(autouse=True)
def isolated_default_db_path(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No test may ever open the real ~/.local/share/llm-harness database."""
    path = tmp_path_factory.mktemp("default-db") / "harness.db"
    monkeypatch.setattr(store_module, "default_db_path", lambda: path)
