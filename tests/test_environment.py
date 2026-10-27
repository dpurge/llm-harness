"""The shared `hidden_environment` helper: mutual exclusion between threads."""

import os
import threading

import pytest

from llm_harness.providers.environment import hidden_environment

JOIN_TIMEOUT = 5.0
EVENT_TIMEOUT = 1.0


def test_two_threads_never_interleave_the_hide_and_restore_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HIDE_ME", "1")
    first_inside = threading.Event()
    release_first = threading.Event()
    second_attempting = threading.Event()
    second_inside = threading.Event()
    observed: dict[str, object] = {}

    def first() -> None:
        with hidden_environment(names=("HIDE_ME",)):
            first_inside.set()
            release_first.wait(EVENT_TIMEOUT * 5)

    def second() -> None:
        second_attempting.set()
        with hidden_environment(names=("HIDE_ME",)):
            second_inside.set()
            # Entering before the first thread restored would have captured an
            # already-empty snapshot and lost the value for good.
            observed["restored_before_entry"] = "HIDE_ME" not in os.environ

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    assert first_inside.wait(EVENT_TIMEOUT)
    t2.start()
    assert second_attempting.wait(EVENT_TIMEOUT)
    # The second thread must stay blocked while the first holds the window.
    assert not second_inside.wait(0.3)
    release_first.set()
    t1.join(JOIN_TIMEOUT)
    t2.join(JOIN_TIMEOUT)
    assert not t1.is_alive()
    assert not t2.is_alive()
    assert second_inside.is_set()
    assert observed["restored_before_entry"] is True
    assert os.environ["HIDE_ME"] == "1"
