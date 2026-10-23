"""Throwaway: does STAC's "Remove from List" button reset the search in place?

Signs in once, then for each UCN given on the command line: search, open
the Images tab, press Remove from List, and check the page is back to a
usable search without a reload. Uploads nothing and saves nothing.

Usage: python try_reset.py <ucn> <ucn> [<ucn> ...]
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from selenium.common.exceptions import TimeoutException, WebDriverException  # noqa: E402
from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support import expected_conditions as EC  # noqa: E402

from config_loader import load_config  # noqa: E402
from exceptions import DocumentProblem, SystemProblem  # noqa: E402
from stac import (  # noqa: E402
    CASE_DEFENDANT_NAME_CSS,
    CaseNotFound,
    IMAGES_TAB_ID,
    SEARCH_CRITERIA_DROPDOWN_CSS,
    SEARCH_FIELD_ID,
    StacSession,
)
from ucn import normalize  # noqa: E402

REMOVE_FROM_LIST_ID = "incidentsListMainListRemoveItem"

# Extra hold after every step once the spinners clear. STAC showed many
# spinners on 14 Oct 2026 and the script read pages before they finished.
# Slow on purpose: this is for watching, not speed.
STEP_PAUSE_SECONDS = 2


def settle_page(stac: StacSession, after: str) -> None:
    """Wait for STAC to finish loading after a step, then hold a moment.

    Uses DALYN's own spinner waits, so what works here is what the real
    code will do. A spinner still up after upload_timeout raises
    SystemProblem, the same as in a real run.
    """
    stac._let_spinner_appear()
    stac._wait_out_spinner(after)
    time.sleep(STEP_PAUSE_SECONDS)


def report(step: str, ok: bool) -> bool:
    """Print one step's outcome and pass it back for the caller to act on."""
    print(f"  {'OK  ' if ok else 'FAIL'} {step}")
    return ok


def row_counts(stac: StacSession) -> str:
    """Case rows in the page's markup and how many of them are on screen."""
    rows = stac.driver.find_elements(By.CSS_SELECTOR, CASE_DEFENDANT_NAME_CSS)
    return f"{len(rows)} in markup, {sum(row.is_displayed() for row in rows)} on screen"


def report_case_row(stac: StacSession) -> None:
    """Print whether the case's row is selected, without touching it.

    The 14 Oct 2026 runs showed the row already selected when Remove from
    List did nothing, and that clicking a cell of the row redraws the page.
    """
    names = [row for row in stac.driver.find_elements(By.CSS_SELECTOR, CASE_DEFENDANT_NAME_CSS) if row.is_displayed()]
    if not names:
        print("       no case row on screen")
        return
    row = names[0].find_element(By.XPATH, "./ancestor::tr[1]")
    print(f"       row selected: {'k-selected' in (row.get_attribute('class') or '')}")


def report_dialogs(stac: StacSession) -> None:
    """Print any dialog on screen and its button labels, in case Remove asks to confirm."""
    dialogs = [d for d in stac.driver.find_elements(By.CSS_SELECTOR, ".k-dialog, .k-window") if d.is_displayed()]
    print(f"       dialogs on screen: {len(dialogs)}")
    for dialog in dialogs:
        labels = [b.text.strip() for b in dialog.find_elements(By.TAG_NAME, "button") if b.is_displayed() and b.text.strip()]
        print(f"         buttons: {labels}")


def rows_on_screen(stac: StacSession) -> int:
    """How many case rows a person would see."""
    return sum(row.is_displayed() for row in stac.driver.find_elements(By.CSS_SELECTOR, CASE_DEFENDANT_NAME_CSS))


def rows_cleared_within(stac: StacSession, seconds: float) -> bool:
    """Poll for the rows to go, so a slow redraw is not taken for a miss."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if rows_on_screen(stac) == 0:
            return True
    return False


# Ways to press the button, most person-like first. A normal Selenium click
# did nothing on 14 Oct 2026 while a person's click on the same browser
# cleared the list, so STAC listens for something that click does not send.
PRESS_METHODS = (
    ("mouse move then click", lambda stac, button: ActionChains(stac.driver).move_to_element(button).pause(0.3).click().perform()),
    ("mouse down and up on the icon", lambda stac, button: ActionChains(stac.driver).move_to_element(
        button.find_element(By.CSS_SELECTOR, ".k-button-icon")).click_and_hold().pause(0.1).release().perform()),
    ("jQuery click event", lambda stac, button: stac.driver.execute_script("$(arguments[0]).trigger('click');", button)),
    ("Kendo button click event", lambda stac, button: stac.driver.execute_script(
        "var b = $(arguments[0]).data('kendoButton'); if (!b) { return false; } b.trigger('click'); return true;", button)),
)


def press_remove(stac: StacSession) -> bool:
    """Press Remove from List each way in turn until the case rows clear.

    Stops at the first way that works and says which, since that is the one
    DALYN would use.
    """
    report_case_row(stac)
    for name, press in PRESS_METHODS:
        try:
            # Found fresh each time: a method that redraws the toolbar would
            # leave the old reference stale.
            button = stac.wait.until(EC.presence_of_element_located((By.ID, REMOVE_FROM_LIST_ID)))
            result = press(stac, button)
        except (TimeoutException, WebDriverException) as error:
            print(f"       {name}: could not do it ({type(error).__name__})")
            continue
        if result is False:
            print(f"       {name}: not available on this button")
            continue
        if rows_cleared_within(stac, 3):
            print(f"       {name}: cleared the list")
            settle_page(stac, "Remove from List")
            return True
        print(f"       {name}: nothing happened")
    report_dialogs(stac)
    return False


def check_reset_page(stac: StacSession) -> bool:
    """Check what a fresh search needs, without reloading anything."""
    driver = stac.driver
    # On screen only. Kendo can keep hidden markup around a cleared grid.
    rows = [row for row in driver.find_elements(By.CSS_SELECTOR, CASE_DEFENDANT_NAME_CSS) if row.is_displayed()]
    ok = report("no case rows on screen", not rows)
    # Told, not required: on 14 Oct 2026 the tab strip stayed after a reset
    # that otherwise worked. What matters is which case it shows next time,
    # checked by case_on_screen after the next search.
    tabs = [tab for tab in driver.find_elements(By.ID, IMAGES_TAB_ID) if tab.is_displayed()]
    print(f"  info Images tab still on screen: {bool(tabs)}")
    fields = driver.find_elements(By.ID, SEARCH_FIELD_ID)
    usable = bool(fields) and fields[0].is_enabled() and fields[0].is_displayed()
    ok &= report("search box present and usable", usable)
    if usable:
        # Whether it is empty, not what it holds.
        report("search box empty (fine either way, it gets cleared)", not fields[0].get_attribute("value"))
    return ok


def page_has_ucn(stac: StacSession, ucn: str) -> bool:
    """True if the page's visible text shows this case number, in any dash style.

    Only a yes or no comes back. The page text carries party names, so it is
    never printed or kept.
    """
    text = stac.driver.execute_script("return document.body.innerText;") or ""
    return normalize(ucn) in normalize(text)


def case_on_screen(stac: StacSession, ucn: str, previous: str) -> bool:
    """After opening a case, check it is this one and the last one is gone.

    The risk a reset carries over a reload: STAC clears the list but leaves
    the last case's Images tab behind, and the next upload lands there.
    """
    ok = report(f"page shows {ucn}", page_has_ucn(stac, ucn))
    if previous:
        ok &= report(f"page no longer shows {previous}", not page_has_ucn(stac, previous))
    return ok


def prepare_box_after_reset(stac: StacSession) -> None:
    """Empty the search box the way a person would and say what it is set to.

    The 14 Oct 2026 run found valid cases missing straight after a reset,
    with the last case number still in the box. Either the box kept that
    text, so the new number was typed onto the end, or the reset put the
    search type back to something other than UCN. This shows which.
    """
    label = stac.driver.execute_script(
        "var b = document.querySelector(arguments[0]);"
        "var w = b && b.closest('.k-picker, .k-dropdownlist, .k-combobox');"
        "return w ? w.innerText.trim() : null;",
        SEARCH_CRITERIA_DROPDOWN_CSS,
    )
    print(f"  info search type shows: {label!r}")

    field = stac.driver.find_element(By.ID, SEARCH_FIELD_ID)
    print(f"  info box before clearing: {field.get_attribute('value')!r}")
    field.click()
    field.send_keys(Keys.CONTROL, "a")
    field.send_keys(Keys.DELETE)
    print(f"  info box after clearing: {field.get_attribute('value')!r}")


def search_and_open(stac: StacSession, ucn: str, first: bool) -> bool:
    """Search one UCN and open its Images tab, as find_case does minus the name check."""
    try:
        if first:
            stac._open_case_search()
            settle_page(stac, "opening the case search")
        else:
            prepare_box_after_reset(stac)
            settle_page(stac, "clearing the search box")
        try:
            stac._search_ucn(ucn)  # Returns the defendant name, deliberately dropped.
        except CaseNotFound:
            # The grid a reset leaves already shows "no records", which the
            # search's wait can read before the new results arrive. Look
            # again once loading is done before believing it.
            settle_page(stac, "the search")
            if not rows_on_screen(stac):
                raise
            print("  info first look said no records, second look found it (read too soon)")
        settle_page(stac, "the search")
        report(f"found {ucn}", True)
        stac._open_images_tab(ucn)
        settle_page(stac, "opening the Images tab")
        report("Images tab open", True)
        return True
    except CaseNotFound:
        raise
    except (DocumentProblem, SystemProblem) as error:
        return report(f"{ucn}: {error}", False)


def run_cases(stac: StacSession, ucns: list[str]) -> None:
    """Search, reset and check each UCN in turn. Raises SystemExit to stop."""
    resets = 0
    searched_after_reset = 0
    previous = ""
    for index, ucn in enumerate(ucns):
        print(f"\nCase {index + 1} of {len(ucns)}")
        # Only searches before any reset go through the sidebar. Every later
        # one types straight into the box the reset left, which is the claim
        # being tested.
        try:
            found = search_and_open(stac, ucn, first=resets == 0)
        except CaseNotFound as error:
            # Test STAC is a copy and lacks recent cases. A miss says nothing
            # about the reset, so move on to the next number.
            report(f"{error}", False)
            print("  Skipped, not a reset problem")
            continue
        if not found:
            when = "after a reset" if resets else "before any reset"
            sys.exit(f"Stopped: the search failed {when}.")
        if not case_on_screen(stac, ucn, previous):
            sys.exit("Stopped: the page is not showing the case just searched.")
        if resets:
            searched_after_reset += 1
        previous = ucn

        print("  Pressing Remove from List")
        if not report("list cleared by the button", press_remove(stac)):
            sys.exit("Stopped: no way of pressing the button cleared the list.")
        if not check_reset_page(stac):
            sys.exit("Stopped: the page is not ready for a new search.")
        resets += 1

    if searched_after_reset:
        print(f"\nPassed: {searched_after_reset} search(es) worked straight after a reset.")
    else:
        print("\nInconclusive: fewer than two cases were found, so no search followed a reset.")


def main() -> None:
    """Run the check over every UCN on the command line in one sign-in."""
    ucns = sys.argv[1:]
    if len(ucns) < 2:
        sys.exit("Give at least two UCNs, so a search after a reset is tested.")

    try:
        config = load_config()
    except SystemProblem as error:
        sys.exit(f"Config problem: {error}")

    with StacSession(config) as stac:
        try:
            run_cases(stac, ucns)
        except WebDriverException as error:
            # A crash closes the browser on the way out, hiding the page.
            print(f"Crashed: {type(error).__name__}: {str(error).splitlines()[0]}")
        except SystemExit as stop:
            # Printed here rather than by exit, so the browser stays open on
            # a failure, which is when it is worth looking at.
            print(stop)
        input("Look at the browser, then press Enter to close it.")


if __name__ == "__main__":
    main()
