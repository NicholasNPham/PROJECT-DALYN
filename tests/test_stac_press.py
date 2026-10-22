"""Tests for PressWhenUncovered and _save: a spinner over Save is waited out.

No browser. A fake driver hands back a fake button whose click raises the
way Chrome does while STAC's loading spinner covers it.
"""

import logging

import pytest
from selenium.common.exceptions import ElementClickInterceptedException, NoSuchElementException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.exceptions import ReadTimeoutError

from exceptions import SystemProblem
import stac
from stac import (
    SAVE_BUTTON_ID,
    PressWhenUncovered,
    SaveMayHaveHappened,
    StacSession,
    describe_intercepting_element,
)


# Shaped like Chrome's message in the 2026-10-13 dev traceback.
SPINNER_MESSAGE = (
    "element click intercepted: Element <button id=\"SaveImage\"> is not clickable at point (1, 2). "
    "Other element would receive the click: <div class=\"k-loading-image\"></div>"
)


class FakeButton:
    """A shown, enabled button that is covered for its first `covered` clicks."""

    def __init__(self, covered: int) -> None:
        """Remember how many clicks the spinner takes."""
        self.covered = covered
        self.clicks = 0

    def is_displayed(self) -> bool:
        """Always shown, as the Save button was in the dev traceback."""
        return True

    def is_enabled(self) -> bool:
        """Always enabled, so only the spinner stands in the way."""
        return True

    def click(self) -> None:
        """Raise while covered, then go through."""
        self.clicks += 1
        if self.clicks <= self.covered:
            raise ElementClickInterceptedException(SPINNER_MESSAGE)


class FakeDriver:
    """Finds the one button, and reads back the subtype _save waits for."""

    def __init__(self, button: FakeButton, subtype: str = "") -> None:
        """Hold the button and the subtype the form shows."""
        self.button = button
        self.subtype = subtype

    def find_element(self, by: str, value: str) -> FakeButton:
        """Every lookup is the button."""
        return self.button

    def execute_script(self, script: str) -> str:
        """The Kendo subtype read in _save."""
        return self.subtype


def test_click_goes_through_once_the_spinner_clears() -> None:
    """Two covered clicks, then the third lands."""
    button = FakeButton(covered=2)

    WebDriverWait(FakeDriver(button), 5, poll_frequency=0.01).until(PressWhenUncovered(SAVE_BUTTON_ID))

    assert button.clicks == 3


def test_uncovered_button_is_clicked_once() -> None:
    """No spinner: one click, as before the fix."""
    button = FakeButton(covered=0)

    WebDriverWait(FakeDriver(button), 5, poll_frequency=0.01).until(PressWhenUncovered(SAVE_BUTTON_ID))

    assert button.clicks == 1


def test_spinner_that_never_clears_is_a_system_problem() -> None:
    """Save never pressed, so nothing was saved: a SystemProblem, not SaveMayHaveHappened."""
    session = StacSession.__new__(StacSession)
    session.driver = FakeDriver(FakeButton(covered=10**6), subtype="ORDERS")
    session.upload_timeout = 0.1

    with pytest.raises(SystemProblem, match="Could not press Save: covered by div.k-loading-image") as raised:
        session._save("UCN", "ORDERS", "document.pdf")

    assert type(raised.value) is SystemProblem


def test_disabled_button_is_named_as_the_blocker() -> None:
    """A disabled Save is a different problem from a spinner, and the error says which."""
    button = FakeButton(covered=0)
    button.is_enabled = lambda: False
    press = PressWhenUncovered(SAVE_BUTTON_ID)

    assert press(FakeDriver(button)) is False
    assert press.blocker == "the button is disabled"


def test_intercepting_element_by_id_when_it_has_no_class() -> None:
    """An element with only an id is named by it."""
    message = 'Other element would receive the click: <div id="overlay">'

    assert describe_intercepting_element(message) == "div#overlay"


def test_unreadable_message_names_no_page_text() -> None:
    """A message in another shape gives a fixed phrase, not whatever text it holds."""
    assert describe_intercepting_element("something unexpected about a party") == "another element"


class FakeSpinner:
    """A Kendo spinner shown for its first `shown` checks."""

    def __init__(self, shown: int) -> None:
        """Remember how many checks it stays up for."""
        self.shown = shown
        self.checks = 0

    def is_displayed(self) -> bool:
        """Up while checks remain, then hidden, as Kendo leaves it in the page."""
        self.checks += 1
        return self.checks <= self.shown


class SpinnerDriver:
    """Finds only the spinner."""

    def __init__(self, spinner: FakeSpinner, where: list[dict] | None = None) -> None:
        """Hold the spinner, and what the page script reports about where it sits."""
        self.spinner = spinner
        self.where = where or []

    def find_elements(self, by: str, value: str) -> list[FakeSpinner]:
        """Every lookup is the spinner."""
        return [self.spinner]

    def execute_script(self, script: str, *args: str) -> list[dict]:
        """The only script run here is the one describing the spinners."""
        return self.where


def _spinner_session(spinner: FakeSpinner, timeout: float = 5, where: list[dict] | None = None) -> StacSession:
    """A session whose page holds just the spinner."""
    session = StacSession.__new__(StacSession)
    session.driver = SpinnerDriver(spinner, where)
    session.upload_timeout = timeout
    return session


def test_no_spinner_means_no_wait() -> None:
    """Checked once and gone: the step goes straight on."""
    spinner = FakeSpinner(shown=0)

    _spinner_session(spinner)._wait_out_spinner("the upload")

    assert spinner.checks == 1


def test_spinner_that_clears_is_waited_out_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The log names the step, which is the point of the check."""
    spinner = FakeSpinner(shown=3)

    with caplog.at_level(logging.INFO, logger="dalyn.stac"):
        _spinner_session(spinner)._wait_out_spinner("the upload")

    assert spinner.checks > 3
    assert "spinner cleared" in caplog.text
    assert "after the upload" in caplog.text


def test_stuck_spinner_names_the_step() -> None:
    """Still up at the timeout: a SystemProblem, so the runner retries in a fresh browser."""
    with pytest.raises(SystemProblem, match="still up 0.1s after picking COURT/ORDERS"):
        _spinner_session(FakeSpinner(shown=10**6), timeout=0.1)._wait_out_spinner("picking COURT/ORDERS")


class LateSpinner:
    """A spinner hidden for its first `hidden` checks, then up for `shown` more."""

    def __init__(self, hidden: int, shown: int) -> None:
        """Remember when it shows and for how long."""
        self.hidden = hidden
        self.shown = shown
        self.checks = 0

    def is_displayed(self) -> bool:
        """Like a preview load: not up yet, then up, then gone."""
        self.checks += 1
        return self.hidden < self.checks <= self.hidden + self.shown


def test_late_spinner_is_missed_by_a_check_alone() -> None:
    """The 13 Oct failure: checked before it showed, so the upload went in under it."""
    spinner = LateSpinner(hidden=2, shown=3)

    _spinner_session(spinner)._wait_out_spinner("opening the dropzone")

    assert spinner.checks == 1


def test_late_spinner_is_caught_and_waited_out(caplog: pytest.LogCaptureFixture) -> None:
    """With the appear window first, the spinner is seen and waited out before the upload."""
    spinner = LateSpinner(hidden=2, shown=3)
    session = _spinner_session(spinner)

    with caplog.at_level(logging.INFO, logger="dalyn.stac"):
        session._let_spinner_appear()
        session._wait_out_spinner("opening the dropzone")

    assert spinner.checks > 5
    assert "after opening the dropzone" in caplog.text


def test_no_spinner_in_the_window_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing shows: the window ends quietly and the upload goes ahead."""
    monkeypatch.setattr(stac, "SPINNER_APPEAR_SECONDS", 0.2)
    spinner = FakeSpinner(shown=0)

    _spinner_session(spinner)._let_spinner_appear()

    assert spinner.checks > 1


def test_stuck_spinner_logs_where_it_sits(caplog: pytest.LogCaptureFixture) -> None:
    """The warning says preview pane or not, Save or not, with digits masked."""
    where = [{"chain": "div#image-4471.k-loading-mask < div#imageTabPageSplitterRightPane", "preview": True, "covers_save": False}]

    with caplog.at_level(logging.WARNING, logger="dalyn.stac"), pytest.raises(SystemProblem):
        _spinner_session(FakeSpinner(shown=10**6), timeout=0.1, where=where)._wait_out_spinner("the upload")

    assert "[in the preview pane, clear of Save] div#image-#.k-loading-mask" in caplog.text
    assert "4471" not in caplog.text


def test_spinner_gone_by_the_time_it_is_described() -> None:
    """Cleared between the timeout and the look: said plainly, not an error."""
    assert _spinner_session(FakeSpinner(shown=0))._describe_spinners() == "none shown any more"


class CrashingDriver(FakeDriver):
    """A driver whose Chrome dies at one chosen step of _save."""

    def __init__(self, button: FakeButton, crash_at: str) -> None:
        """crash_at is "subtype", "lookup", "click" or "confirm"."""
        super().__init__(button, subtype="ORDERS")
        self.crash_at = crash_at

    def find_element(self, by: str, value: str) -> FakeButton:
        """The button by id, and the saved notification by xpath once clicked."""
        if self.crash_at == "lookup" or (self.crash_at == "confirm" and by == "xpath"):
            raise WebDriverException("chrome not reachable")
        return self.button

    def execute_script(self, script: str) -> str:
        """The Kendo subtype read, or a script error in its place."""
        if self.crash_at == "subtype":
            raise WebDriverException("javascript error")
        return self.subtype


def _crashing_session(crash_at: str) -> StacSession:
    """A session whose browser fails at `crash_at`."""
    button = FakeButton(covered=0)
    if crash_at == "click":
        def click() -> None:
            """Chrome dies inside the click call."""
            raise WebDriverException("chrome not reachable")
        button.click = click
    session = StacSession.__new__(StacSession)
    session.driver = CrashingDriver(button, crash_at)
    session.upload_timeout = 0.1
    return session


@pytest.mark.parametrize("crash_at", ["subtype", "lookup"])
def test_browser_error_before_the_click_is_a_safe_retry(crash_at: str) -> None:
    """Nothing reached Save, so it is a plain SystemProblem and can be retried."""
    with pytest.raises(SystemProblem) as raised:
        _crashing_session(crash_at)._save("UCN", "ORDERS", "document.pdf")

    assert type(raised.value) is SystemProblem


@pytest.mark.parametrize("crash_at", ["click", "confirm"])
def test_browser_error_from_the_click_on_may_have_saved(crash_at: str) -> None:
    """The click may have reached STAC, so a person checks before anything is redone."""
    with pytest.raises(SaveMayHaveHappened, match="may or may not be on the case"):
        _crashing_session(crash_at)._save("UCN", "ORDERS", "document.pdf")


def test_hung_connection_inside_the_click_may_have_saved() -> None:
    """The live 16 Oct hang was urllib3's ReadTimeoutError, not a Selenium error."""
    session = _crashing_session("confirm")

    def click() -> None:
        """chromedriver stops answering mid-click."""
        raise ReadTimeoutError(None, "/session", "Read timed out.")

    session.driver.button.click = click
    session.driver.crash_at = "none"

    with pytest.raises(SaveMayHaveHappened, match="ReadTimeoutError"):
        session._save("UCN", "ORDERS", "document.pdf")


def test_browser_error_message_names_no_page_text() -> None:
    """Only the error's type reaches the message, never Selenium's own text."""
    with pytest.raises(SaveMayHaveHappened) as raised:
        _crashing_session("click")._save("UCN", "ORDERS", "document.pdf")

    assert "WebDriverException" in str(raised.value)
    assert "chrome not reachable" not in str(raised.value)


class UnconfirmedDriver(FakeDriver):
    """Save clicks fine, but the success box never shows.

    The shape of the 12 unconfirmed saves from 19 to 21 Oct 2026. Counts
    `tiles` image tiles, and answers the page script with `page`, or raises
    from it when `page` is None.
    """

    def __init__(self, tiles: int, page: dict | None) -> None:
        """Hold the tile count and what the page script reports."""
        super().__init__(FakeButton(covered=0), subtype="ORDERS")
        self.tiles = tiles
        self.page = page

    def find_element(self, by: str, value: str) -> FakeButton:
        """The Save button by id. The success box by xpath is never there."""
        if by == "xpath":
            raise NoSuchElementException("no success box")
        return self.button

    def find_elements(self, by: str, value: str) -> list[object]:
        """The image tiles on the case."""
        return [object()] * self.tiles

    def execute_script(self, script: str, *args: str) -> object:
        """The Kendo subtype read, or the page description."""
        if script is not stac.DESCRIBE_SAVE_PAGE_JS:
            return self.subtype
        if self.page is None:
            raise WebDriverException("javascript error")
        return self.page


def _unconfirmed_session(tiles: int = 0, page: dict | None = None) -> StacSession:
    """A session whose Save is pressed and never confirmed."""
    session = StacSession.__new__(StacSession)
    session.driver = UnconfirmedDriver(tiles, page)
    session.upload_timeout = 0.1
    return session


def test_unconfirmed_save_logs_the_page(caplog: pytest.LogCaptureFixture) -> None:
    """The warning names the notifications, spinners, Save state and tile counts."""
    session = _unconfirmed_session(tiles=0)
    page = {"notifications": ["c-notification.c-notification-error"], "spinners": 1, "save": "disabled", "tiles": 1}

    def describe(script: str, *args: str) -> object:
        """A tile shows up after the click, as a late save would leave it."""
        return page if script is stac.DESCRIBE_SAVE_PAGE_JS else "ORDERS"

    session.driver.execute_script = describe

    with caplog.at_level(logging.WARNING, logger="dalyn.stac"), pytest.raises(SaveMayHaveHappened, match="STAC never confirmed"):
        session._save("UCN", "ORDERS", "document.pdf")

    assert "UCN: no confirmation after Save" in caplog.text
    assert "c-notification-error" in caplog.text
    assert "spinners 1, Save disabled, image tiles 0 before and 1 now" in caplog.text


def test_unconfirmed_save_with_no_notifications_says_none(caplog: pytest.LogCaptureFixture) -> None:
    """An empty list reads as none, so the line is easy to scan."""
    page = {"notifications": [], "spinners": 0, "save": "enabled", "tiles": 2}

    with caplog.at_level(logging.WARNING, logger="dalyn.stac"), pytest.raises(SaveMayHaveHappened):
        _unconfirmed_session(tiles=2, page=page)._save("UCN", "ORDERS", "document.pdf")

    assert "Notifications none, spinners 0, Save enabled, image tiles 2 before and 2 now" in caplog.text


def test_unreadable_page_still_raises_the_unconfirmed_save(caplog: pytest.LogCaptureFixture) -> None:
    """The page script failing is logged by type and never replaces the real error."""
    with caplog.at_level(logging.WARNING, logger="dalyn.stac"), pytest.raises(SaveMayHaveHappened, match="STAC never confirmed"):
        _unconfirmed_session(page=None)._save("UCN", "ORDERS", "document.pdf")

    assert "could not read the page after the unconfirmed save (WebDriverException)" in caplog.text
    assert "javascript error" not in caplog.text


def test_browser_crash_while_confirming_does_not_describe_the_page(caplog: pytest.LogCaptureFixture) -> None:
    """Only a timeout gets the page line. A dead Chrome has no page to read."""
    with caplog.at_level(logging.WARNING, logger="dalyn.stac"), pytest.raises(SaveMayHaveHappened):
        _crashing_session("confirm")._save("UCN", "ORDERS", "document.pdf")

    assert "no confirmation after Save" not in caplog.text
    assert "could not read the page" not in caplog.text


def test_tile_count_is_none_when_the_page_will_not_say() -> None:
    """A driver that cannot count gives None, not an error that would stop the save."""
    assert _crashing_session("confirm")._count_image_tiles() is None
