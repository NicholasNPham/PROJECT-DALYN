"""Tests for the check right before Save: an email a person moved is not saved.

No browser. StacSession and StacRunner are built without __init__, and every
step that would touch Chrome is replaced by one that records it was called.
"""

from pathlib import Path

import pytest

from exceptions import MessageGone
from stac import PartiallyEntered, StacRunner, StacSession


def _session(calls: list[str]) -> StacSession:
    """A session with uploads and Save on, whose browser steps only record themselves."""
    session = StacSession.__new__(StacSession)
    session.upload_enabled = True
    session.save_enabled = True
    session._pair = ""
    session._pause = lambda step: None
    session._open_dropzone = lambda: calls.append("dropzone")
    session._upload = lambda paths: calls.append("upload")
    session._select_type_subtype = lambda document_type, subtype: calls.append("type")
    session._save = lambda ucn, subtype, names: calls.append("save")
    session._discard_pending_upload = lambda: calls.append("discard")
    return session


PATHS = [Path("0001_1_placeholder.pdf")]


def test_save_goes_ahead_when_the_email_is_still_there() -> None:
    """before_save says yes: the click happens after the check."""
    calls: list[str] = []

    _session(calls).add_documents("UCN", "COURT", "ORDERS", PATHS, lambda: calls.append("check") or True)

    assert calls == ["dropzone", "upload", "type", "check", "save"]


def test_moved_email_is_not_saved_and_the_upload_is_thrown_away() -> None:
    """before_save says no: no click, and the half-filled form is cleared."""
    calls: list[str] = []

    with pytest.raises(MessageGone):
        _session(calls).add_documents("UCN", "COURT", "ORDERS", PATHS, lambda: False)

    assert "save" not in calls
    assert calls[-1] == "discard"


def test_no_check_given_saves_as_before() -> None:
    """Callers that pass nothing behave exactly as they did."""
    calls: list[str] = []

    _session(calls).add_documents("UCN", "COURT", "ORDERS", PATHS)

    assert calls[-1] == "save"


def test_check_is_not_asked_when_save_is_off() -> None:
    """Nothing is saved, so there is nothing to protect."""
    calls: list[str] = []
    session = _session(calls)
    session.save_enabled = False
    session._reach_save_without_pressing = lambda ucn, subtype, names: calls.append("reach")

    session.add_documents("UCN", "COURT", "ORDERS", PATHS, lambda: pytest.fail("asked with Save off"))

    assert calls[-1] == "reach"


class FakeSession:
    """Stands in for StacSession inside StacRunner. Box N raises if listed in gone_at."""

    def __init__(self, gone_at: int) -> None:
        """gone_at: the 1-based upload box where the email turns out to be moved."""
        self.gone_at = gone_at
        self.boxes = 0

    def find_case(self, *args: object) -> None:
        """Nothing to open."""

    def add_documents(self, *args: object) -> None:
        """Count the box, and raise MessageGone at the scripted one."""
        self.boxes += 1
        if self.boxes == self.gone_at:
            raise MessageGone("a person moved the email before Save")

    def close(self) -> None:
        """Nothing to close."""


def _runner(gone_at: int) -> StacRunner:
    """A runner with retries on, whose browser restarts do nothing."""
    runner = StacRunner.__new__(StacRunner)
    runner.max_attempts = 2
    runner.fresh_browser = False
    runner.reuse_case_page = False
    runner.session = FakeSession(gone_at)
    runner._restart_session = lambda: None
    return runner


TWO_BOXES = [
    (Path("0001_1_placeholder.pdf"), "COURT", "ORDERS"),
    (Path("0001_2_placeholder.pdf"), "COURT", "NOTICES"),
]


def test_moved_before_anything_saved_is_gone_and_not_retried() -> None:
    """Nothing on the case: the email is simply the person's now."""
    runner = _runner(gone_at=1)

    with pytest.raises(MessageGone):
        runner.enter_email("UCN", TWO_BOXES, before_save=lambda: False)

    assert runner.session.boxes == 1


def test_moved_after_one_box_saved_is_partially_entered() -> None:
    """Something is on the case already; the person has to be told what."""
    runner = _runner(gone_at=2)

    with pytest.raises(PartiallyEntered, match="email 1 attachment 1 as COURT/ORDERS"):
        runner.enter_email("UCN", TWO_BOXES, before_save=lambda: False)
