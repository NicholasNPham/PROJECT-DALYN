"""Tests for PressWhenUncovered and _save: a spinner over Save is waited out.

No browser. A fake driver hands back a fake button whose click raises the
way Chrome does while STAC's loading spinner covers it.
"""

import logging

import pytest
from selenium.common.exceptions import ElementClickInterceptedException
from selenium.webdriver.support.ui import WebDriverWait

from exceptions import SystemProblem
from stac import SAVE_BUTTON_ID, PressWhenUncovered, StacSession, describe_intercepting_element


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

    def __init__(self, spinner: FakeSpinner) -> None:
        """Hold the spinner."""
        self.spinner = spinner

    def find_elements(self, by: str, value: str) -> list[FakeSpinner]:
        """Every lookup is the spinner."""
        return [self.spinner]


def _spinner_session(spinner: FakeSpinner, timeout: float = 5) -> StacSession:
    """A session whose page holds just the spinner."""
    session = StacSession.__new__(StacSession)
    session.driver = SpinnerDriver(spinner)
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
