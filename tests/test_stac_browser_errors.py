"""Tests for browser errors outside _save: a hung or dead Chrome is retried, not fatal.

No browser. The live crash of 16 Oct 2026 was urllib3's ReadTimeoutError from
a STAC page that never answered, which no handler caught. These raise the
same error from fakes.
"""

from pathlib import Path

import pytest
from selenium.common.exceptions import WebDriverException
from urllib3.exceptions import ReadTimeoutError

from exceptions import SystemProblem
from stac import PartiallyEntered, StacRunner, StacSession


def _hang() -> ReadTimeoutError:
    """The error a hung chromedriver connection raises."""
    return ReadTimeoutError(None, "/session", "Read timed out.")


class FailingSession:
    """Stands in for StacSession. Raises `error` on the upload boxes listed in fail_on."""

    def __init__(self, fail_on: set[int], error: Exception) -> None:
        """fail_on: 1-based upload calls that raise, counted across retries."""
        self.fail_on = fail_on
        self.error = error
        self.calls = 0
        self.filed: list[str] = []

    def find_case(self, *args: object) -> None:
        """Nothing to open."""

    def add_documents(self, ucn: str, document_type: str, subtype: str, *args: object) -> None:
        """Count the call, and either fail or record the box as filed."""
        self.calls += 1
        if self.calls in self.fail_on:
            raise self.error
        self.filed.append(subtype)

    def close(self) -> None:
        """Nothing to close."""


def _runner(session: FailingSession) -> StacRunner:
    """A runner with two attempts, whose browser restarts do nothing."""
    runner = StacRunner.__new__(StacRunner)
    runner.max_attempts = 2
    runner.fresh_browser = False
    runner.reuse_case_page = False
    runner.session = session
    runner._restart_session = lambda: None
    return runner


TWO_BOXES = [
    (Path("0001_1_placeholder.pdf"), "COURT", "ORDERS"),
    (Path("0001_2_placeholder.pdf"), "COURT", "NOTICES"),
]


@pytest.mark.parametrize("error", [_hang(), WebDriverException("chrome not reachable")])
def test_box_two_failing_once_is_retried_without_refiling_box_one(error: Exception) -> None:
    """Box 1 saves, box 2's browser fails, the retry files box 2 only."""
    session = FailingSession(fail_on={2}, error=error)

    _runner(session).enter_email("UCN", TWO_BOXES)

    assert session.filed == ["ORDERS", "NOTICES"]


def test_browser_failing_every_time_after_box_one_is_partially_entered() -> None:
    """Out of retries with box 1 saved: a person is told what is already in."""
    session = FailingSession(fail_on={2, 3}, error=_hang())

    with pytest.raises(PartiallyEntered, match="as COURT/ORDERS"):
        _runner(session).enter_email("UCN", TWO_BOXES)


def test_browser_failing_every_time_before_anything_saved_is_a_system_problem() -> None:
    """Nothing filed and out of retries: a SystemProblem, which the watch survives."""
    session = FailingSession(fail_on={1, 2}, error=_hang())

    with pytest.raises(SystemProblem, match="ReadTimeoutError") as raised:
        _runner(session).enter_email("UCN", TWO_BOXES)

    assert type(raised.value) is SystemProblem


class HangingDriver:
    """A driver whose first page load hangs, as STAC's sign-in page did."""

    def __init__(self) -> None:
        """Track whether quit was called."""
        self.quit_called = False

    def get(self, url: str) -> None:
        """The page never answers."""
        raise _hang()

    def quit(self) -> None:
        """Closing a hung chromedriver fails too."""
        self.quit_called = True
        raise _hang()


def test_sign_in_page_that_hangs_is_a_system_problem() -> None:
    """The 16 Oct crash: now a failed pass the watch retries, with Chrome closed."""
    driver = HangingDriver()
    session = StacSession.__new__(StacSession)
    session.url = "https://stac.example.invalid"
    session.is_test_instance = True
    session.upload_enabled = False
    session.save_enabled = False
    session.wait_timeout = 1
    session.driver = None
    session.wait = None
    session._start_chrome = lambda: driver

    with pytest.raises(SystemProblem, match="ReadTimeoutError"):
        session.open()

    assert driver.quit_called
    assert session.driver is None
