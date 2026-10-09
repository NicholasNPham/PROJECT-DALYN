"""Tests for reusing the case page between upload boxes of one email.

No browser. The runner gets a fake session that records each step, so the
tests read as the sequence of things DALYN would do in STAC.
"""

from pathlib import Path

import pytest

from exceptions import SystemProblem
from stac import StacRunner

THREE_BOXES = [
    (Path("0001_1_placeholder.pdf"), "COURT", "MOTIONS"),
    (Path("0001_2_placeholder.pdf"), "COURT", "ORDERS"),
    (Path("0001_3_placeholder.pdf"), "COURT", "NOTICES"),
]


class FakeSession:
    """Records search, box and readiness checks. Fails the scripted box once."""

    def __init__(self, steps: list[str], save_enabled: bool = True, ready: bool = True, fail_box: int = 0) -> None:
        """steps is shared with the runner's restart, so the order is one list."""
        self.steps = steps
        self.save_enabled = save_enabled
        self.ready = ready
        self.fail_box = fail_box
        self.boxes = 0

    def find_case(self, *args: object) -> None:
        """Record a case search."""
        self.steps.append("search")

    def ready_for_next_box(self) -> bool:
        """Record the check and answer as scripted."""
        self.steps.append("check")
        return self.ready

    def add_documents(self, ucn: str, document_type: str, subtype: str, *args: object) -> None:
        """Record the box, failing once at fail_box."""
        self.boxes += 1
        if self.boxes == self.fail_box:
            self.steps.append(f"fail {subtype}")
            raise SystemProblem("the matrix dialog wedged")
        self.steps.append(f"box {subtype}")

    def close(self) -> None:
        """Nothing to close."""


def _runner(steps: list[str], reuse: bool = True, fresh: bool = False, **session: object) -> StacRunner:
    """A runner whose restarts are recorded and keep the same scripted session."""
    runner = StacRunner.__new__(StacRunner)
    runner.max_attempts = 2
    runner.fresh_browser = fresh
    runner.reuse_case_page = reuse
    runner.session = FakeSession(steps, **session)
    runner._restart_session = lambda: steps.append("restart")
    return runner


def test_later_boxes_of_one_email_skip_the_search() -> None:
    """One search for the email; each later box checks the page instead."""
    steps: list[str] = []

    _runner(steps).enter_email("UCN", THREE_BOXES)

    assert steps == ["search", "box MOTIONS", "check", "box ORDERS", "check", "box NOTICES"]


def test_a_page_that_is_not_ready_is_searched_again() -> None:
    """Any doubt about the page falls back to the case search, as before."""
    steps: list[str] = []

    _runner(steps, ready=False).enter_email("UCN", THREE_BOXES[:2])

    assert steps == ["search", "box MOTIONS", "check", "search", "box ORDERS"]


def test_a_page_that_is_not_ready_gets_the_old_restart_before_the_search() -> None:
    """With fresh_browser, a refused reuse costs exactly what it did before:
    restart, then search. Searching from the saved case page instead hit
    STAC's disabled search box and used up the retry (9 Oct 2026 run)."""
    steps: list[str] = []

    _runner(steps, fresh=True, ready=False).enter_email("UCN", THREE_BOXES[:2])

    assert steps == ["search", "box MOTIONS", "check", "restart", "search", "box ORDERS", "restart"]


def test_switch_off_searches_for_every_box() -> None:
    """Absent or false: exactly the behavior before the switch existed."""
    steps: list[str] = []

    _runner(steps, reuse=False).enter_email("UCN", THREE_BOXES[:2])

    assert steps == ["search", "box MOTIONS", "search", "box ORDERS"]


def test_save_off_never_reuses() -> None:
    """With Save off the upload is discarded by a reload, so no saved page is left."""
    steps: list[str] = []

    _runner(steps, save_enabled=False).enter_email("UCN", THREE_BOXES[:2])

    assert "check" not in steps
    assert steps.count("search") == 2


def test_fresh_browser_restarts_once_per_email_when_reusing() -> None:
    """Chrome restarts after the email's last box, not between its boxes."""
    steps: list[str] = []

    _runner(steps, fresh=True).enter_email("UCN", THREE_BOXES)

    assert steps == ["search", "box MOTIONS", "check", "box ORDERS", "check", "box NOTICES", "restart"]


def test_fresh_browser_without_reuse_restarts_after_every_box() -> None:
    """Switch off keeps the old restart per box."""
    steps: list[str] = []

    _runner(steps, reuse=False, fresh=True).enter_email("UCN", THREE_BOXES[:2])

    assert steps == ["search", "box MOTIONS", "restart", "search", "box ORDERS", "restart"]


def test_retry_after_a_failure_starts_with_a_search_and_skips_saved_boxes() -> None:
    """The retry follows a fresh browser: search first, and box 1 is not filed twice."""
    steps: list[str] = []

    _runner(steps, fail_box=2).enter_email("UCN", THREE_BOXES)

    assert steps == [
        "search", "box MOTIONS", "check", "fail ORDERS",
        "restart",
        "search", "box ORDERS", "check", "box NOTICES",
    ]
    assert steps.count("box MOTIONS") == 1


@pytest.mark.parametrize("reuse", [True, False])
def test_single_box_email_is_unchanged(reuse: bool) -> None:
    """Nothing to reuse with one box: one search, one box, either way."""
    steps: list[str] = []

    _runner(steps, reuse=reuse).enter_email("UCN", THREE_BOXES[:1])

    assert steps == ["search", "box MOTIONS"]


# ready_for_next_box

CASE_URL = "https://stac.example/cases/1/images"


class FakeElement:
    """A page element that is shown or hidden."""

    def __init__(self, shown: bool = True) -> None:
        """Shown by default, like a notification still on screen."""
        self.shown = shown

    def is_displayed(self) -> bool:
        """Whether it is on screen."""
        return self.shown


class FakeDriver:
    """Just the parts of Chrome ready_for_next_box looks at."""

    def __init__(self, url: str = CASE_URL, leftover_upload: bool = False, subtype: str | None = "",
                 notification: bool = False) -> None:
        """Scripted page state: the url, the upload list, the Subtype and the notification."""
        self.current_url = url
        self.leftover_upload = leftover_upload
        self.subtype = subtype
        self.notification = notification

    def find_elements(self, by: str, selector: str) -> list:
        """Answer the upload-list and notification lookups."""
        if "uploaded successfully" in selector:
            return [FakeElement()] if self.leftover_upload else []
        if "notification" in selector:
            return [FakeElement()] if self.notification else [FakeElement(shown=False)]
        return []

    def execute_script(self, script: str, *args: object) -> str | None:
        """The Subtype dropdown's value."""
        return self.subtype


def _session_on(driver: FakeDriver, case_url: str = CASE_URL):
    """A StacSession on a fake page, with the case recorded as find_case would."""
    from stac import StacSession

    session = StacSession.__new__(StacSession)
    session.driver = driver
    session._case_url = case_url
    return session


def test_page_left_by_a_save_is_ready() -> None:
    """Same case, nothing listed, Subtype empty, notification gone."""
    assert _session_on(FakeDriver()).ready_for_next_box() is True


@pytest.mark.parametrize(
    "driver",
    [
        FakeDriver(url="https://elsewhere.example/home"),
        FakeDriver(leftover_upload=True),
        FakeDriver(subtype="ORDERS"),
        FakeDriver(notification=True),
    ],
    ids=["left the site", "upload still listed", "subtype not cleared", "notification showing"],
)
def test_any_doubt_about_the_page_means_not_ready(driver: FakeDriver) -> None:
    """Each check on its own is enough to fall back to a search."""
    assert _session_on(driver).ready_for_next_box() is False


def test_address_change_after_save_on_the_same_site_is_still_ready() -> None:
    """STAC changes the path after a Save while showing the same case (9 Oct 2026)."""
    driver = FakeDriver(url="https://stac.example/cases/1/images/after-save")

    assert _session_on(driver).ready_for_next_box() is True


def test_no_case_opened_yet_is_not_ready() -> None:
    """Before any find_case there is nothing to reuse."""
    assert _session_on(FakeDriver(), case_url="").ready_for_next_box() is False


def test_a_check_that_cannot_run_is_not_ready() -> None:
    """A browser error counts as not ready, never as an exception."""
    from selenium.common.exceptions import WebDriverException

    driver = FakeDriver()
    driver.find_elements = lambda *args: (_ for _ in ()).throw(WebDriverException("gone"))

    assert _session_on(driver).ready_for_next_box() is False
