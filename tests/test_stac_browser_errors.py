"""Tests for browser errors outside _save: a hung or dead Chrome is retried, not fatal.

No browser. The live crash of 16 Oct 2026 was urllib3's ReadTimeoutError from
a STAC page that never answered, which no handler caught. These raise the
same error from fakes.
"""

from pathlib import Path

import pytest
from selenium.common.exceptions import TimeoutException, WebDriverException
from urllib3.exceptions import ReadTimeoutError

from exceptions import ServiceUnreachable, SystemProblem
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


class RefusingDriver:
    """A driver whose page load fails with a chosen Selenium error."""

    def __init__(self, error: Exception) -> None:
        """Hold the error the load raises."""
        self.error = error

    def get(self, url: str) -> None:
        """The load fails."""
        raise self.error

    def quit(self) -> None:
        """Closes cleanly."""


def _opening_session(driver: RefusingDriver) -> StacSession:
    """A session that will try to open STAC with `driver`."""
    session = StacSession.__new__(StacSession)
    session.url = "https://stac.example.invalid"
    session.is_test_instance = True
    session.upload_enabled = False
    session.save_enabled = False
    session.wait_timeout = 1
    session.driver = None
    session.wait = None
    session._start_chrome = lambda: driver
    return session


def test_login_page_that_times_out_is_unreachable() -> None:
    """The 20 Oct outage: STAC not answering is waited out, not counted toward the stop."""
    with pytest.raises(ServiceUnreachable, match="Could not load STAC.*TimeoutException"):
        _opening_session(RefusingDriver(TimeoutException("page load"))).open()


def test_login_page_chrome_refuses_is_still_counted() -> None:
    """Chrome failing the load some other way is not fixed by waiting."""
    with pytest.raises(SystemProblem, match="WebDriverException") as raised:
        _opening_session(RefusingDriver(WebDriverException("chrome not reachable"))).open()

    assert type(raised.value) is SystemProblem


class CountingSession:
    """Stands in for StacSession: counts sign-ins, and fails the first `fail` of them."""

    opened = 0
    fail = 0

    def __init__(self, config: dict) -> None:
        """Nothing to read."""

    def open(self) -> None:
        """Count the sign-in, failing while `fail` lasts."""
        CountingSession.opened += 1
        if CountingSession.opened <= CountingSession.fail:
            raise SystemProblem("Could not load STAC: TimeoutException")

    def close(self) -> None:
        """Nothing to close."""


def _lazy_runner(monkeypatch: pytest.MonkeyPatch, fail: int = 0) -> StacRunner:
    """A runner whose sessions are CountingSession."""
    import stac

    CountingSession.opened = 0
    CountingSession.fail = fail
    monkeypatch.setattr(stac, "StacSession", CountingSession)
    runner = StacRunner.__new__(StacRunner)
    runner._config = {}
    runner.session = None
    return runner


def test_entering_the_runner_signs_in_to_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty pass never opens Chrome."""
    with _lazy_runner(monkeypatch):
        pass

    assert CountingSession.opened == 0


def test_open_signs_in_once_however_often_it_is_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """Called per mailbox with mail, it still signs in once a pass."""
    with _lazy_runner(monkeypatch) as runner:
        runner.open()
        runner.open()

    assert CountingSession.opened == 1


def test_failed_sign_in_is_tried_again_by_the_next_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed sign-in leaves no session behind to be mistaken for a working one."""
    runner = _lazy_runner(monkeypatch, fail=1)

    with pytest.raises(SystemProblem):
        runner.open()
    assert runner.session is None

    runner.open()
    assert CountingSession.opened == 2
    assert runner.session is not None
