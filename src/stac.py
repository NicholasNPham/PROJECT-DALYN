"""Drives the STAC web interface with Selenium: sign in, find the case, file the document.

A port of ftp_to_stac.py from github.com/NicholasNPham/PCSO911, which has
automated this same screen in production for months. Three things change
in the port:

    Type and Subtype come from the rules sheet per document, where PCSO911
    had 911AUDIO hardcoded in three places.

    Credentials and the instance url come from config.yaml, not key.py, so
    test and live are a config change rather than a code change.

    Failures raise DocumentProblem or SystemProblem for main.py to route,
    instead of sending an email from inside every except block.

One browser session covers a whole pass. PCSO911 opens and closes Chrome
per case, which is fine at its volume; DALYN can see 50 documents in a run
and signing in 50 times would be both slow and conspicuous in STAC's audit
log.
"""

import re
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    NoAlertPresentException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from difflib import SequenceMatcher

from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from exceptions import DocumentProblem, SystemProblem
from logger import get_logger

logger = get_logger(__name__)

# Seconds. Defaults only; config.yaml overrides both.
DEFAULT_WAIT_TIMEOUT = 10
DEFAULT_UPLOAD_TIMEOUT = 60

# Attempts per email. The second one gets a fresh browser.
DEFAULT_MAX_ATTEMPTS = 2

# Kendo rebuilds its dialogs after each interaction and will drop a click
# that lands too soon after the previous one. Found the hard way in PCSO911.
KENDO_PAUSE_SECONDS = 1

# Sign-in page
LOGIN_PROVIDER_SELECT_ID = "LoginProvider"
LOGIN_PROVIDER_LOCAL = "Local"
USERNAME_FIELD_ID = "Username"
PASSWORD_FIELD_ID = "Password"
SUBMIT_LOGIN_BUTTON_ID = "submitLogin"

# Present only once signed in, so it doubles as the proof that sign-in worked
CASES_SIDEBAR_CSS = "[data-menuid='incident']"

# Cases search
SEARCH_CRITERIA_DROPDOWN_CSS = "button[role='button'][aria-label='select']"
SEARCH_CRITERIA_UCN_XPATH = (
    "//ul[@id='incidentsSearchMainCriteria_listbox' and not(contains(@style,'display: none'))]"
    "//span[text()='UCN']"
)
SEARCH_FIELD_ID = "incidentsSearchMainSearchValue"
SEARCH_BUTTON_ID = "incidentsSearchMainButton"
NO_RECORDS_CSS = ".k-grid-norecords-template"
CASE_DEFENDANT_NAME_CSS = "td[data-original-column-name='Def_Name'] span.k-button-text"
IMAGES_TAB_ID = "incidentsTab-tab-3"

# Add image: dropzone and tiles
IMAGE_TILE_UNSELECTED_CSS = "#image-manager-listview-name .cipimage:not(.k-selected)"
DROPZONE_PANEL_CSS = ".pagesImagesIndex-upload-drop-zone-element"
PREVIEW_IFRAME_CSS = "#imageTabPageSplitterRightPane iframe.iframe-document"
FILE_INPUT_CSS = (
    "input[id^='cipFileUpload_pagesImagesIndex-upload'][multiple]:not([webkitdirectory])"
)
UPLOAD_SUCCESS_XPATH = (
    "//span[contains(@class,'k-file-validation-message') and "
    "text()='File(s) uploaded successfully.']"
)

# Type/Subtype matrix dialog
MATRIX_FIND_BUTTON_CSS = ".c-button-find-type-subtype"
MATRIX_DIALOG_CSS = "div#codeSearchDialog"
MATRIX_SEARCH_INPUT_CSS = "div#codeSearchDialog input.k-input-inner[placeholder='Search...']"
MATRIX_SELECT_BUTTON_ID = "SelectCodeAndSubCode"
SUBTYPE_INPUT_SELECTOR = "input[name='image_sub_type']"

# Finds the matrix row by both of its cell values and tells Kendo's grid
# widget that this is the selected row, then reads back what the grid now
# says is selected.
#
# Clicking the row and waiting for the k-selected class was not enough. The
# Select button reads the grid's own selection, which a click does not always
# move, so Select kept taking the first row in the list: a search for NOTICES
# lists APPEALS/NOTICES above COURT/NOTICES, and APPEALS is what got filed.
# Setting the selection through the widget and checking it here means the
# wrong row is caught before Select is pressed rather than after.
SELECT_MATRIX_ROW_JS = r"""
var wantedType = arguments[0].toUpperCase();
var wantedSub = arguments[1].toUpperCase();
var dialog = document.querySelector(arguments[2]);
if (!dialog) { return {ok: false, why: 'no-dialog'}; }

// Grouping adds a leading <td class="k-group-cell"> to every data row that
// has no counterpart in the header, so raw cell positions do not line up
// with header positions. Dropping those cells on both sides is what makes
// the indexes mean the same thing.
function isGroupCell(cell) {
  var cls = cell.className || '';
  return cls.indexOf('k-group-cell') !== -1
      || cls.indexOf('k-table-group-td') !== -1
      || cls.indexOf('k-hierarchy-cell') !== -1;
}
function cellsOf(tr) {
  var out = [];
  for (var c = 0; c < tr.cells.length; c++) {
    if (isGroupCell(tr.cells[c])) { continue; }
    out.push((tr.cells[c].innerText || tr.cells[c].textContent || '')
      .trim().toUpperCase().replace(/\s+/g, ' '));
  }
  return out;
}
function isHeaderRow(tr) {
  for (var c = 0; c < tr.cells.length; c++) {
    if (tr.cells[c].tagName === 'TH') { return true; }
  }
  return false;
}

// The dialog is four columns: Image Type, Image Type Desc, Image Sub Type,
// Image Sub Type Desc. Find the two code columns by their headers, because
// matching on "some cell equals PLS RVW" would also hit a description that
// happens to read the same as another pair's code.
var typeAt = -1, subAt = -1, seen = 0;
var headers = dialog.querySelectorAll('th');
for (var h = 0; h < headers.length; h++) {
  if (isGroupCell(headers[h])) { continue; }
  var label = (headers[h].innerText || headers[h].textContent || '')
    .trim().toUpperCase().replace(/\s+/g, ' ');
  if (label === 'IMAGE TYPE') { typeAt = seen; }
  else if (label === 'IMAGE SUB TYPE') { subAt = seen; }
  seen += 1;
}

function isMatch(tr) {
  if (!tr.cells || tr.cells.length === 0) { return false; }
  if (isHeaderRow(tr)) { return false; }
  // Hidden rows count. Kendo keeps a collapsed group's rows in the DOM, and
  // a row being out of sight does not stop the grid selecting it.
  var text = cellsOf(tr);
  if (typeAt >= 0 && subAt >= 0 && text.length > Math.max(typeAt, subAt)) {
    return text[typeAt] === wantedType && text[subAt] === wantedSub;
  }
  return text.indexOf(wantedType) !== -1 && text.indexOf(wantedSub) !== -1;
}

var matches = [];
var rows = dialog.querySelectorAll('tr');
for (var i = 0; i < rows.length; i++) {
  rows[i].removeAttribute('data-dalyn-pick');
  if (isMatch(rows[i])) { matches.push(rows[i]); }
}
if (matches.length === 0) {
  return {ok: false, why: 'no-row', scanned: rows.length,
          columns: {type: typeAt, subtype: subAt}};
}

var tr = matches[0];
// Marked so Python can click this exact row without having to guess how
// STAC wraps its cell text. The previous version looked for <span> with
// exact text, which only works while every column renders that way.
tr.setAttribute('data-dalyn-pick', '1');
var gridEl = tr.closest ? tr.closest('.k-grid') : null;
var grid = (gridEl && window.jQuery) ? window.jQuery(gridEl).data('kendoGrid') : null;
if (!grid) { return {ok: false, why: 'no-grid', matches: matches.length}; }

try { grid.clearSelection(); } catch (e) {}
grid.select(tr);
try { grid.trigger('change'); } catch (e) {}

var chosen = grid.select();
var chosenRow = (chosen && chosen.length) ? chosen[0] : null;
return {
  ok: chosenRow ? isMatch(chosenRow) : false,
  why: 'set',
  matches: matches.length,
  cells: chosenRow ? cellsOf(chosenRow) : []
};
"""

# The row the pick script marked, so a native click can land on that exact row.
MATRIX_PICKED_ROW_CSS = "tr[data-dalyn-pick='1']"

# Puts every pair on one page before anything goes looking for a row.
#
# The dialog arrives grouped by Image Type and paged at about a hundred rows,
# listed alphabetically from APPEALS. COURT lands on page one, which is the
# only reason COURT/ORDERS ever worked. PLS does not, so PLS/RVW was not
# missing from STAC, it was missing from the page being read.
#
# Grouping goes too, because a collapsed group hides its rows and the grid
# then has nothing visible to hand the Select button.
PREPARE_MATRIX_GRID_JS = r"""
var dialog = document.querySelector(arguments[0]);
if (!dialog) { return {ok: false, why: 'no-dialog'}; }
if (!window.jQuery) { return {ok: false, why: 'no-jquery'}; }

var gridEl = dialog.querySelector('.k-grid');
var grid = gridEl ? window.jQuery(gridEl).data('kendoGrid') : null;
if (!grid || !grid.dataSource) { return {ok: false, why: 'no-grid'}; }

var ds = grid.dataSource;
var report = {
  ok: true,
  total: ds.total(),
  pageSizeWas: ds.pageSize() || null,
  grouped: false,
  unpaged: false
};

try {
  var groups = ds.group();
  if (groups && groups.length) {
    report.grouped = true;
    ds.group([]);
  }
} catch (e) { report.groupError = String(e); }

try {
  var total = ds.total();
  var size = ds.pageSize();
  if (total && size && size < total) {
    ds.pageSize(total);
    report.unpaged = true;
  }
} catch (e) { report.pageError = String(e); }

report.pageSizeNow = ds.pageSize() || null;
report.rowsRendered = gridEl.querySelectorAll('tbody tr').length;
return report;
"""

# How many of the dialog's rows to name in an error message. Enough to see
# what the search actually matched, not enough to bury the log.
MATRIX_ROW_SAMPLE = 25

# Reads back what the dialog is listing right now, for the error message when
# the wanted row is not among them.
DESCRIBE_MATRIX_ROWS_JS = r"""
var wantedType = (arguments[1] || '').toUpperCase();
var wantedSub = (arguments[2] || '').toUpperCase();
var dialog = document.querySelector(arguments[0]);
if (!dialog) { return null; }

function isGroupCell(cell) {
  var cls = cell.className || '';
  return cls.indexOf('k-group-cell') !== -1
      || cls.indexOf('k-table-group-td') !== -1
      || cls.indexOf('k-hierarchy-cell') !== -1;
}
function cellsOf(tr) {
  var out = [];
  for (var c = 0; c < tr.cells.length; c++) {
    if (isGroupCell(tr.cells[c])) { continue; }
    out.push((tr.cells[c].innerText || tr.cells[c].textContent || '')
      .trim().toUpperCase().replace(/\s+/g, ' '));
  }
  return out;
}
function isHeaderRow(tr) {
  for (var c = 0; c < tr.cells.length; c++) {
    if (tr.cells[c].tagName === 'TH') { return true; }
  }
  return false;
}

var typeAt = -1, subAt = -1, seen = 0;
var headers = dialog.querySelectorAll('th');
for (var h = 0; h < headers.length; h++) {
  if (isGroupCell(headers[h])) { continue; }
  var label = (headers[h].innerText || headers[h].textContent || '')
    .trim().toUpperCase().replace(/\s+/g, ' ');
  if (label === 'IMAGE TYPE') { typeAt = seen; }
  else if (label === 'IMAGE SUB TYPE') { subAt = seen; }
  seen += 1;
}

var out = {total: 0, types: {}, subtypesOfWantedType: [], typesOfWantedSub: [],
           nearPairs: [], columns: {type: typeAt, subtype: subAt}};
var rows = dialog.querySelectorAll('tr');
for (var i = 0; i < rows.length; i++) {
  var tr = rows[i];
  if (!tr.cells || tr.cells.length === 0) { continue; }
  if (isHeaderRow(tr)) { continue; }

  var text = cellsOf(tr);
  if (typeAt < 0 || subAt < 0 || text.length <= Math.max(typeAt, subAt)) { continue; }

  var t = text[typeAt];
  var s = text[subAt];
  if (!t && !s) { continue; }

  out.total += 1;
  out.types[t] = (out.types[t] || 0) + 1;
  if (t === wantedType) { out.subtypesOfWantedType.push(s); }
  if (s === wantedSub) { out.typesOfWantedSub.push(t); }
  // Near misses, so a pair whose real name only resembles the wanted one
  // says so instead of being reported as absent. This is what would have
  // named PLS RVW/PLS RVW the first time a search for PLS/RVW missed.
  var near = (wantedType && (t.indexOf(wantedType) !== -1 || wantedType.indexOf(t) !== -1))
          || (wantedSub && (s.indexOf(wantedSub) !== -1 || wantedSub.indexOf(s) !== -1));
  if (near && !(t === wantedType && s === wantedSub)) {
    out.nearPairs.push(t + '/' + s);
  }
}
out.typeList = Object.keys(out.types).sort();
return out;
"""

# The Type field. STAC's own naming is not certain here, so several
# spellings are tried and the first that answers wins. If none do, the type
# cannot be confirmed and the document is not saved: a search for ORDERS
# alone matches APPEALS/ORDERS, FEL APP/ORDERS and COURT/ORDERS, so checking
# only the subtype is how the wrong one gets filed.
TYPE_INPUT_SELECTORS = (
    "input[name='image_type']",
    "input[name='image_main_type']",
    "input[name='image_code']",
    "input[name='imageType']",
)

# Seconds to let an existing document's preview render before uploading.
# Short on purpose: it is a courtesy, not a requirement, and a case with no
# documents on it has nothing to show.
PREVIEW_WAIT_SECONDS = 5

# Kendo marks the chosen grid row with this class.
KENDO_SELECTED_CLASS = "k-selected"

# Save
SAVE_BUTTON_ID = "SaveImage"
SAVED_NOTIFICATION_XPATH = "//div[contains(@class,'c-notification-success')]"

# Words STAC hangs off a defendant name that are not part of the name: alert
# flags, programme codes, prosecutor initials. Carried over from PCSO911.
EXCLUDED_NAME_TOKENS = frozenset({
    "AM", "SVP", "AME", "SO", "AMSP", "JLA", "PJLA", "SP", "ALERT", "BKGRDALERT",
    "CP", "DO", "NOT", "USE", "GANG", "NCP", "NO", "CC", "OSCP", "SPCALERT",
    "TTP", "VFOSC", "HA",
})

# The caption of a Florida criminal filing, which is where the defendant's
# name lives: STATE OF FLORIDA, Plaintiff, vs. JOHN SMITH, Defendant.
# Matching starts at STATE OF FLORIDA so a name mentioned elsewhere in the
# body cannot be mistaken for the caption.
CAPTION_PATTERN = re.compile(
    r"state\s+of\s+florida\b.{0,200}?\b(?:v|vs|versus)\b[\s.,:]*(.{3,80})",
    re.IGNORECASE | re.DOTALL,
)

# Where a captured name ends. Everything from the first of these onward is
# dropped, since the caption runs straight into the next line of the page.
CAPTION_STOP_WORDS = (
    "defendant", "defendants", "case no", "case number", "uniform case",
    "division", "ucn", "accused", "respondent",
)

# A name needs at least this many usable words before it is worth comparing.
# One word is a surname on its own, which matches far too much.
MIN_NAME_TOKENS = 2

# How alike two words must be to count as the same name, once an exact match
# has failed. Scanned documents routinely lose or gain a letter: EMERICK is
# read as EMRICK (0.92), THOMPSON as THORNPSON (0.82). Tuned against both
# directions: SMITH and SMYTHE (0.73), CARTER and CARTWRIGHT (0.67), LEE and
# LEEDS (0.75) all stay apart.
#
# Known limit: ROBERT and ROBERTA score 0.92 and would pass. For that to file
# a document wrongly, the case number would have to be wrong AND the other
# case's defendant named almost identically. Rare enough to accept, where
# rejecting every OCR typo would make this check useless noise.
NAME_TOKEN_SIMILARITY = 0.82

# Highlands and Hardee, agreed 6 Oct 2026. Their documents rarely print the
# full case number, so the case is only filed when STAC's defendant is found
# by name in the email or document, and Manual Review otherwise. Nick's bar
# is 90%, stricter than the caption check above: the subject and body are
# typed by the portal, not read by OCR.
REQUIRED_NAME_COUNTY_CODES = frozenset({"25", "28"})
REQUIRED_NAME_SIMILARITY = 0.90

# Words either side of the first match that the rest of the name may sit in.
# Leaves room for a middle name or "vs." between the parts of the name,
# without letting a surname in one sentence pair with a first name in another.
NAME_WINDOW_SLACK = 2


class SaveMayHaveHappened(SystemProblem):
    """Save was clicked but the confirmation never came, so nobody knows.

    The one failure that must never be retried blindly: the document may
    already be on the case. A person has to look.
    """


class PartiallyEntered(DocumentProblem):
    """Some of an email's documents are in STAC and the rest are not.

    A person has to finish it, and has to know what is already filed so they
    do not enter it twice. The message carries that list.
    """


class StacSession:
    """One signed-in browser session, reused for every document in a pass.

    Opened by the caller with `with StacSession(config) as stac:` so Chrome
    is closed even when a document blows up halfway through.
    """

    def __init__(self, config: dict) -> None:
        """Read what is needed from config. Opens nothing yet.

        Args:
            config: The dict from load_config.
        """
        stac = config["stac"]
        self.url = str(stac["url"]).rstrip("/")
        self._username = stac["username"]
        self._password = stac["password"]
        self.is_test_instance = bool(stac.get("is_test_instance", True))
        self.upload_enabled = bool(stac.get("upload_enabled", False))
        self.save_enabled = bool(stac.get("save_enabled", False))
        self.wait_timeout = int(stac.get("wait_timeout", DEFAULT_WAIT_TIMEOUT))
        self.upload_timeout = int(stac.get("upload_timeout", DEFAULT_UPLOAD_TIMEOUT))
        self.max_attempts = max(1, int(stac.get("max_attempts", DEFAULT_MAX_ATTEMPTS)))
        self.action_pause = float(stac.get("action_pause", 0) or 0)
        self._chromedriver = _usable_driver_path(config["paths"].get("chromedriver"))

        self.driver: webdriver.Chrome | None = None
        self.wait: WebDriverWait | None = None
        # STAC keeps the search criteria once it is set, so the dropdown only
        # offers UCN the first time. Reset whenever a new browser opens.
        self._criteria_is_ucn = False
        self._pair = ""

    def __enter__(self) -> "StacSession":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def open(self) -> None:
        """Launch Chrome, load STAC, and sign in.

        Raises:
            SystemProblem: If Chrome will not start, STAC will not load, or
                the credentials are refused. None of these are a problem with
                any one document, so the pass should stop.
        """
        logger.info(
            "Opening STAC at %s (test instance: %s, uploading: %s, saving: %s)",
            self.url,
            self.is_test_instance,
            self.upload_enabled,
            self.save_enabled,
        )

        self.driver = self._start_chrome()
        self._criteria_is_ucn = False

        self.wait = WebDriverWait(self.driver, self.wait_timeout)

        try:
            self.driver.get(self.url)
            self.driver.maximize_window()
        except WebDriverException as error:
            self.close()
            raise SystemProblem(f"Could not load STAC at {self.url}: {error}") from error

        try:
            self._sign_in()
        except Exception:
            self.close()
            raise

    def _start_chrome(self) -> webdriver.Chrome:
        """Start Chrome, trying each way of finding chromedriver in turn.

        Order matters on a locked-down network: a path in config is used as
        given, then Selenium's own driver resolution, and only last the
        downloader, which needs to reach the internet and often cannot.

        Raises:
            SystemProblem: If none of them produce a working Chrome. The
                message lists what was tried.
        """
        attempts = []

        if self._chromedriver:
            attempts.append(("paths.chromedriver", lambda: Service(self._chromedriver)))

        # Selenium Manager, built into Selenium 4.6 and later. Uses a driver
        # already on the machine when there is one.
        attempts.append(("Selenium's own driver lookup", lambda: None))

        attempts.append(
            ("downloading chromedriver", lambda: Service(ChromeDriverManager().install()))
        )

        failures = []
        for description, build_service in attempts:
            try:
                service = build_service()
                driver = webdriver.Chrome(service=service) if service else webdriver.Chrome()
                logger.debug("Chrome started via %s", description)
                return driver
            except Exception as error:  # noqa: BLE001 - each route fails differently
                failures.append(f"{description}: {type(error).__name__}: {str(error).splitlines()[0]}")

        raise SystemProblem(
            "Could not start Chrome. Set paths.chromedriver in config.yaml to a "
            "chromedriver.exe matching your installed Chrome version. Tried: "
            + " | ".join(failures)
        )

    def _sign_in(self) -> None:
        """Fill the sign-in form and wait for the Cases sidebar to appear.

        Raises:
            SystemProblem: If any step of sign-in does not complete.
        """
        try:
            provider = self.wait.until(
                EC.presence_of_element_located((By.ID, LOGIN_PROVIDER_SELECT_ID))
            )
            Select(provider).select_by_value(LOGIN_PROVIDER_LOCAL)
        except TimeoutException as error:
            raise SystemProblem(
                f"No 'Authenticate Using' dropdown on the sign-in page at {self.url}. "
                "Either the page has changed or the url is not STAC."
            ) from error

        try:
            self.wait.until(
                EC.element_to_be_clickable((By.ID, USERNAME_FIELD_ID))
            ).send_keys(self._username)
            self.wait.until(
                EC.element_to_be_clickable((By.ID, PASSWORD_FIELD_ID))
            ).send_keys(self._password)
        except TimeoutException as error:
            raise SystemProblem("Could not find the STAC username or password field.") from error

        try:
            submit = self.wait.until(
                EC.element_to_be_clickable((By.ID, SUBMIT_LOGIN_BUTTON_ID))
            )
            # Clicked through JavaScript because the real button sits under an
            # overlay often enough that a normal click intercepts.
            self.driver.execute_script("arguments[0].click();", submit)
        except TimeoutException as error:
            raise SystemProblem("Could not find the STAC sign-in button.") from error

        try:
            self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, CASES_SIDEBAR_CSS)))
        except TimeoutException as error:
            # The sidebar is the only reliable proof of a successful sign-in:
            # STAC re-renders the same page on a bad password rather than
            # saying so in a way Selenium can read.
            raise SystemProblem(
                "Signed in but the Cases sidebar never appeared. Usually a wrong "
                "username or password in config.yaml, or an account without access "
                "to this instance."
            ) from error

        logger.info("Signed in to STAC")
        self._pause("signed in")

    def close(self) -> None:
        """Close Chrome. Safe to call twice, and never raises."""
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except WebDriverException as error:
            logger.warning("Chrome did not close cleanly: %s", error)
        finally:
            self.driver = None
            self.wait = None

    @staticmethod
    def _settle() -> None:
        """Give Kendo a beat to finish rebuilding before the next click."""
        time.sleep(KENDO_PAUSE_SECONDS)

    def _pause(self, what: str) -> None:
        """Hold after a step so a person can see it, when action_pause is set.

        Purely for watching. Nothing in STAC needs it, and production runs
        with action_pause at 0.
        """
        if self.action_pause <= 0:
            return
        logger.info("  ... %s", what)
        time.sleep(self.action_pause)

    # ---------------------------------------------------------------- search

    def find_case(
        self, ucn: str, document_text: str = "", subject: str = "", body: str = ""
    ) -> str:
        """Search STAC for a UCN, check the defendant, and open the Images tab.

        Args:
            ucn: The case number, as DALYN extracted it.
            document_text: The document's own text, used to read the defendant
                name out of its caption. Pass "" to skip the name check, for
                Polk only; Highlands and Hardee always check.
            subject: Email subject, searched for the defendant's name on
                Highlands and Hardee cases. Never logged.
            body: Email body, the same.

        Returns:
            The defendant name STAC shows for the case.

        Raises:
            DocumentProblem: No case with that number, or STAC's defendant does
                not match the document's caption. Either way this document
                needs a person, and the rest of the pass carries on.
            SystemProblem: STAC itself did not respond as expected.
        """
        self._open_case_search()
        self._pause("case search open")
        stac_name = self._search_ucn(ucn)
        self._pause(f"found {ucn}")
        if ucn[:2] in REQUIRED_NAME_COUNTY_CODES:
            self._require_defendant(ucn, stac_name, subject, body, document_text)
        else:
            self._check_defendant(ucn, stac_name, document_text)
        self._open_images_tab(ucn)
        self._pause("images tab open")
        return stac_name

    def _open_case_search(self) -> None:
        """Get to a usable Cases search box, whatever page we are on.

        Clicking the Cases sidebar is enough from most pages, but not from a
        case that has been opened: STAC leaves the search box present and
        disabled, and typing into it fails with "element is not currently
        interactable". PCSO911 never meets this because it opens a new
        browser for every case.

        So the sidebar is tried first, and if the box is not usable the page
        is loaded fresh, which always works.

        Raises:
            SystemProblem: If the search box cannot be reached either way.
        """
        if self._try_open_case_search():
            return

        logger.info("Search box was not usable; reloading STAC to clear the page")
        try:
            self.driver.get(self.url)
            self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, CASES_SIDEBAR_CSS)))
        except (TimeoutException, WebDriverException) as error:
            raise SystemProblem(f"Could not get back to STAC's Cases page: {error}") from error

        # A reload resets the search criteria, so it has to be chosen again.
        self._criteria_is_ucn = False

        if not self._try_open_case_search():
            raise SystemProblem(
                "STAC's case search box is still not usable after reloading the page."
            )

    def _try_open_case_search(self) -> bool:
        """One attempt at reaching a usable search box. False if it is not ready."""
        try:
            self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, CASES_SIDEBAR_CSS))
            ).click()
        except (TimeoutException, WebDriverException):
            return False

        if not self._criteria_is_ucn and not self._choose_ucn_criteria():
            return False

        # Present is not the same as usable. STAC leaves the box on the page
        # in a disabled state after a case is opened, and only this tells the
        # difference.
        try:
            field = self.wait.until(EC.element_to_be_clickable((By.ID, SEARCH_FIELD_ID)))
            return field.is_enabled() and field.is_displayed()
        except (TimeoutException, WebDriverException):
            return False

    def _choose_ucn_criteria(self) -> bool:
        """Set the search dropdown to UCN. False if the option is not offered."""
        try:
            dropdown = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, SEARCH_CRITERIA_DROPDOWN_CSS))
            )
            self.driver.execute_script("arguments[0].click();", dropdown)
        except (TimeoutException, WebDriverException):
            return False

        try:
            ucn_option = self.wait.until(
                EC.element_to_be_clickable((By.XPATH, SEARCH_CRITERIA_UCN_XPATH))
            )
            self.driver.execute_script("arguments[0].click();", ucn_option)
        except (TimeoutException, WebDriverException):
            self._close_dropdown()
            return False

        self._criteria_is_ucn = True
        return True

    def _close_dropdown(self) -> None:
        """Press Escape so a dropdown left open cannot block the next click."""
        try:
            self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except WebDriverException:
            pass

    def _search_ucn(self, ucn: str) -> str:
        """Run the search and return STAC's defendant name for the one result.

        Raises:
            DocumentProblem: If no case has that number.
            SystemProblem: If the results never load.
        """
        try:
            field = self.wait.until(EC.element_to_be_clickable((By.ID, SEARCH_FIELD_ID)))
            field.clear()
            field.click()
            field.send_keys(ucn)
            button = self.driver.find_element(By.ID, SEARCH_BUTTON_ID)
            self.driver.execute_script("arguments[0].click();", button)
        except (TimeoutException, WebDriverException) as error:
            raise SystemProblem(f"Could not run the UCN search in STAC: {error}") from error

        # Wait for either outcome. find_elements returns a list, so this is
        # truthy on a result row and truthy on "no records", and keeps waiting
        # while the grid is still loading.
        try:
            self.wait.until(
                lambda d: d.find_elements(By.CSS_SELECTOR, NO_RECORDS_CSS)
                or d.find_elements(By.CSS_SELECTOR, CASE_DEFENDANT_NAME_CSS)
            )
        except TimeoutException as error:
            raise SystemProblem(f"STAC's search results never loaded for {ucn}.") from error

        if self.driver.find_elements(By.CSS_SELECTOR, NO_RECORDS_CSS):
            raise DocumentProblem(
                f"No case in STAC with case number {ucn}. Either the number was "
                "read wrong or the case is not in this instance."
            )

        # Highlands and Hardee numbers can be rebuilt from a short form, so a
        # search that returns several rows is not trusted to be the case.
        # Polk is left as it was.
        if ucn[:2] in REQUIRED_NAME_COUNTY_CODES:
            rows = len(self.driver.find_elements(By.CSS_SELECTOR, CASE_DEFENDANT_NAME_CSS))
            if rows > 1:
                raise DocumentProblem(
                    f"STAC returned {rows} cases for {ucn}, expected exactly one."
                )

        try:
            return self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, CASE_DEFENDANT_NAME_CSS))
            ).text.strip()
        except TimeoutException as error:
            raise SystemProblem(
                f"Search for {ucn} returned a row with no defendant name."
            ) from error

    def _check_defendant(self, ucn: str, stac_name: str, document_text: str) -> None:
        """Compare STAC's defendant against the name in the document's caption.

        This is the second guard on the case number. A UCN can be read
        correctly and still belong to the wrong case; the names disagreeing is
        how that shows up.

        Raises:
            DocumentProblem: If both names are readable and they do not match.
        """
        if not document_text:
            return

        document_name = defendant_from_caption(document_text)
        if not document_name:
            # Plenty of real documents have no caption at all: cover letters,
            # forms, returns of service. Blocking those would send a lot of
            # good documents to a person, so the check is skipped and said so.
            logger.info("%s: no defendant caption in the document, name check skipped", ucn)
            return

        if names_match(stac_name, document_name):
            logger.info("%s: defendant matches the document caption", ucn)
            return

        # No names in the message. Defendant names are never logged or stored;
        # a person compares them in STAC.
        raise DocumentProblem(
            f"Defendant mismatch on {ucn}: STAC's defendant does not match the "
            "document's caption."
        )

    def _require_defendant(
        self, ucn: str, stac_name: str, subject: str, body: str, document_text: str
    ) -> None:
        """Find STAC's defendant by name in the subject, body or document.

        For Highlands and Hardee, where finding the name is required. Unlike
        _check_defendant, nothing is skipped: no name found anywhere is
        Manual Review.

        Raises:
            DocumentProblem: If the name is in none of the three.
        """
        for source, text in (("subject", subject), ("body", body), ("document", document_text)):
            if name_in_text(stac_name, text):
                logger.info("%s: defendant found in the %s", ucn, source)
                return

        raise DocumentProblem(
            f"Defendant check failed on {ucn}: STAC's defendant was not found in "
            "the subject, body or document."
        )

    def _open_images_tab(self, ucn: str) -> None:
        """Open the case's Images tab, where documents are added."""
        try:
            tab = self.wait.until(EC.element_to_be_clickable((By.ID, IMAGES_TAB_ID)))
            self.driver.execute_script("arguments[0].click();", tab)
        except TimeoutException as error:
            raise SystemProblem(f"Could not open the Images tab for {ucn}.") from error

    # ------------------------------------------------------------- add image

    def add_documents(self, ucn: str, document_type: str, subtype: str, paths: list) -> None:
        """Upload one group of files to the open case under one Type/Subtype.

        A STAC upload box carries a single Type and Subtype for everything in
        it, so the caller groups an email's attachments by pair and calls this
        once per group. Two notices that both file to COURT/NOTICES go up
        together; a motion and an order do not.

        Args:
            ucn: Case number, for log lines only. The case is already open.
            document_type: STAC Type, e.g. COURT.
            subtype: STAC Subtype, e.g. ORDERS.
            paths: Absolute local paths. All get this Type and Subtype.

        Raises:
            DocumentProblem: The Type/Subtype is not in STAC's matrix.
            SaveMayHaveHappened: Save was clicked and never confirmed.
            SystemProblem: Anything else in STAC misbehaved.
        """
        names = ", ".join(document_label(path) for path in paths)

        if not self.upload_enabled:
            logger.info(
                "%s: WOULD upload %s under %s/%s and press Save. Nothing sent, "
                "stac.upload_enabled is false.",
                ucn, names, document_type, subtype,
            )
            return

        self._open_dropzone()
        self._pause("dropzone open")
        self._upload(paths)
        self._pause(f"uploaded {names}")
        self._pair = f"{document_type}/{subtype}"
        self._select_type_subtype(document_type, subtype)

        if not self.save_enabled:
            self._reach_save_without_pressing(ucn, subtype, names)
            return

        self._save(ucn, subtype, names)

    def _reach_save_without_pressing(self, ucn: str, subtype: str, names: str) -> None:
        """Go as far as the Save button, prove it is ready, and leave it alone.

        Everything a real save does except the click: the Subtype has landed
        in the form, the button exists and is enabled. If this passes, turning
        save_enabled on should work.

        The upload is then thrown away by reloading the page, so the next
        document does not start on a half-filled form.
        """
        upload_wait = WebDriverWait(self.driver, self.upload_timeout)

        try:
            upload_wait.until(
                lambda d: d.execute_script(
                    f"return $({SUBTYPE_INPUT_SELECTOR!r}).data('kendoDropDownList').value()"
                )
                == subtype
            )
        except TimeoutException as error:
            raise SystemProblem(
                f"The Subtype field never showed {subtype}, so Save would have "
                "filed this under the wrong type."
            ) from error

        try:
            button = upload_wait.until(EC.element_to_be_clickable((By.ID, SAVE_BUTTON_ID)))
        except TimeoutException as error:
            raise SystemProblem("The Save button never became clickable.") from error

        self._pause("at the Save button, not pressing it")

        logger.info(
            "%s: REACHED SAVE for %s under %s. Button %r is ready and was NOT "
            "pressed, stac.save_enabled is false.",
            ucn, names, self._pair, button.text.strip() or SAVE_BUTTON_ID,
        )

        self._discard_pending_upload()

    def _discard_pending_upload(self) -> None:
        """Reload the page so an unsaved upload cannot bleed into the next one."""
        try:
            self.driver.refresh()
            try:
                alert = self.driver.switch_to.alert
                alert.accept()
            except NoAlertPresentException:
                pass
            self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, CASES_SIDEBAR_CSS)))
        except (TimeoutException, WebDriverException) as error:
            raise SystemProblem(
                f"Could not clear the unsaved upload before the next document: {error}"
            ) from error

    def _open_dropzone(self) -> None:
        """Get the Images tab into the state where files can be dropped.

        Three steps, and only the middle one is required.

        Clicking an existing image tile is what makes STAC render the
        dropzone, but a case with no documents on it yet has no tile to
        click, and the dropzone is there anyway.

        Waiting for the preview iframe is PCSO911's guard against the file
        input being swapped out mid-upload. It only applies when there is
        something to preview, so a case with no images, or one whose preview
        will not render, waits briefly and moves on rather than failing.

        Raises:
            SystemProblem: If the dropzone itself never appears.
        """
        tiles = self.driver.find_elements(By.CSS_SELECTOR, IMAGE_TILE_UNSELECTED_CSS)
        if tiles:
            try:
                self.driver.execute_script("arguments[0].click();", tiles[0])
            except WebDriverException as error:
                logger.debug("Could not click an image tile: %s", error)
        else:
            logger.info("No existing images on this case, going straight to the dropzone")

        try:
            self.wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, DROPZONE_PANEL_CSS)))
        except TimeoutException as error:
            raise SystemProblem("The upload dropzone never appeared.") from error

        self._wait_for_preview()

    def _wait_for_preview(self) -> None:
        """Let an existing document's preview settle, if there is one.

        Not required. When a preview is loading, letting it finish stops STAC
        replacing the file input underneath an upload already in progress.
        When there is nothing to preview, waiting the full timeout would stall
        every document on an empty case.
        """
        brief = WebDriverWait(self.driver, PREVIEW_WAIT_SECONDS)
        try:
            brief.until(
                lambda d: "web/viewer.html?file="
                in (d.find_element(By.CSS_SELECTOR, PREVIEW_IFRAME_CSS).get_attribute("src") or "")
            )
        except (TimeoutException, WebDriverException):
            logger.info("No document preview to wait for, continuing")

    def _upload(self, paths: list) -> None:
        """Send the files to the dropzone and wait for every one to confirm."""
        try:
            file_input = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, FILE_INPUT_CSS))
            )
            # The input is hidden behind a class; Selenium cannot type into it
            # until that is removed.
            self.driver.execute_script("arguments[0].removeAttribute('class')", file_input)
        except TimeoutException as error:
            raise SystemProblem("Could not find STAC's file input.") from error

        upload_wait = WebDriverWait(self.driver, self.upload_timeout)
        try:
            file_input.send_keys("\n".join(str(path) for path in paths))
            upload_wait.until(
                lambda d: len(d.find_elements(By.XPATH, UPLOAD_SUCCESS_XPATH)) >= len(paths)
            )
        except TimeoutException as error:
            raise SystemProblem(
                f"Only some of the {len(paths)} file(s) finished uploading within "
                f"{self.upload_timeout}s."
            ) from error

        self._remove_preview_iframe()

    def _remove_preview_iframe(self) -> None:
        """Drop the preview iframe, which otherwise intercepts the Save click."""
        self.driver.execute_script(
            f"var f=document.querySelector({PREVIEW_IFRAME_CSS!r}); if(f) f.remove();"
        )

    def _select_type_subtype(self, document_type: str, subtype: str) -> None:
        """Open the matrix dialog, select the Type/Subtype row, and check it took.

        The only genuinely new code in this port. PCSO911 had 911AUDIO written
        into the row XPath, the search box and the save check; all three now
        come from the rules sheet.

        Several tries, for two different failures. One is the dialog taking the
        wrong row, which a reopen and a double-click fix. The other is the row
        not turning up at all for a given search term: STAC's search box may be
        matching the pair's description rather than its code, so a search for
        RVW finds nothing while the PLS/RVW row is sitting right there. Each
        term gets a go before the pair is called missing.

        Raises:
            DocumentProblem: If no search term turns the row up. The message
                lists what the dialog did show, since "not in STAC" and "not
                found by this search" look identical from here and are not the
                same thing at all.
            SystemProblem: If the row was found but the form ended up holding
                something else.
        """
        terms = self._matrix_search_terms(document_type, subtype)
        last_error: SystemProblem | None = None
        last_missing: DocumentProblem | None = None

        for attempt, term in enumerate(terms, start=1):
            self._open_matrix_dialog(term)
            try:
                self._choose_matrix_row(
                    document_type, subtype, use_double_click=(attempt > 1)
                )
                self._press_matrix_select()
                self._confirm_type_subtype(document_type, subtype)
            except DocumentProblem as error:
                # Not under this search term. Another term may still find it,
                # so this is not yet grounds for calling the pair missing.
                last_missing = error
                logger.info(
                    "%s/%s not among the %s row(s) the dialog showed for search "
                    "%r. Trying the next search term.",
                    document_type, subtype, self._matrix_row_count(), term or "(cleared)",
                )
                self._close_matrix_dialog()
                continue
            except SystemProblem as error:
                last_error = error
                logger.warning(
                    "%s/%s did not take on try %s of %s (%s). Reopening the matrix.",
                    document_type, subtype, attempt, len(terms), error,
                )
                self._close_matrix_dialog()
                continue

            if attempt > 1:
                logger.info(
                    "Found %s/%s by searching the dialog for %r, not %r",
                    document_type, subtype, term, subtype,
                )
            self._pause(f"{document_type}/{subtype} selected")
            logger.debug("Selected %s/%s", document_type, subtype)
            return

        # A row that was found but would not take is the more serious failure,
        # because something is wrong with the dialog rather than with the pair.
        if last_error is not None:
            raise last_error
        raise last_missing

    @staticmethod
    def _matrix_search_terms(document_type: str, subtype: str) -> tuple[str, ...]:
        """Search terms to try in the matrix dialog, in order.

        The subtype first, since that is the narrowest and works for most
        pairs. Then the type. Then nothing at all, which clears the filter and
        leaves every pair listed for the row scan to walk.
        """
        terms = [subtype]
        if document_type and document_type != subtype:
            terms.append(document_type)
        terms.append("")
        return tuple(terms)

    def _open_matrix_dialog(self, term: str) -> None:
        """Press Find and put `term` in the dialog's search box.

        An empty term clears the box, which leaves the grid unfiltered.
        """
        try:
            find_button = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, MATRIX_FIND_BUTTON_CSS))
            )
            self.driver.execute_script("arguments[0].click();", find_button)
        except TimeoutException as error:
            raise SystemProblem("Could not open the Type/Subtype dialog.") from error

        try:
            search_box = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, MATRIX_SEARCH_INPUT_CSS))
            )
            # Real key events, not clear(). Kendo filters on keyup, and a
            # clear() that sets the value directly leaves the grid showing the
            # previous term's results.
            search_box.send_keys(Keys.CONTROL, "a")
            search_box.send_keys(Keys.DELETE)
            if term:
                search_box.send_keys(term)
                # Enter as well. Typing alone did not filter anything: the
                # unfiltered alphabetical list came back every time, which is
                # how a search for ORDERS ended up on APPEALS/ORDERS.
                search_box.send_keys(Keys.ENTER)
        except TimeoutException as error:
            raise SystemProblem(
                "Could not find the search box in the Type/Subtype dialog."
            ) from error

        # Kendo rebuilds the rows after a filter.
        self._settle()
        self._flatten_matrix_grid()

    def _flatten_matrix_grid(self) -> None:
        """Drop the dialog's grouping and paging so every pair is on the page.

        Best effort. If the grid widget cannot be reached the row scan still
        runs against whatever is rendered, which is the behaviour this had
        before, so there is nothing to gain by failing here.
        """
        try:
            report = self.driver.execute_script(
                PREPARE_MATRIX_GRID_JS, MATRIX_DIALOG_CSS
            ) or {}
        except WebDriverException as error:
            logger.debug("Could not flatten the matrix grid: %s", error)
            return

        if not report.get("ok"):
            logger.debug("Did not flatten the matrix grid: %s", report.get("why"))
            return

        if report.get("grouped") or report.get("unpaged"):
            logger.debug(
                "Matrix dialog flattened: %s pairs, page size %s -> %s, "
                "%s rows rendered",
                report.get("total"),
                report.get("pageSizeWas"),
                report.get("pageSizeNow"),
                report.get("rowsRendered"),
            )
            # Re-rendering every row takes a moment longer than a filter.
            self._settle()

    def _close_matrix_dialog(self) -> None:
        """Get the dialog off the screen so the next try starts from Find again.

        Escape is enough when it is still open, and does nothing harmful when
        Select already closed it.
        """
        self._close_dropdown()
        self._settle()

    def _choose_matrix_row(
        self, document_type: str, subtype: str, use_double_click: bool = False
    ) -> None:
        """Make the Type/Subtype row the grid's selected row.

        The row is found by its cell text, not by an XPath looking for exact
        <span> text. The XPath version could only see rows whose cells happen
        to be wrapped the way COURT/ORDERS is, and reported anything else as
        "not in STAC", which is a much more alarming thing to be wrong about.

        Raises:
            DocumentProblem: No row in the dialog holds this pair, with a list
                of what the dialog did show.
            SystemProblem: The row is there but the grid would not select it.
        """
        # Find, mark and select in one pass, so the row is located by cell
        # text rather than by markup shape.
        result = self._run_matrix_pick(document_type, subtype)

        if result.get("why") == "no-row":
            raise DocumentProblem(
                f"STAC's Type/Subtype dialog is not showing a row for "
                f"{document_type}/{subtype}. "
                f"{self._describe_matrix_rows(document_type, subtype)}"
            )

        if result.get("matches", 0) > 1:
            logger.debug(
                "The dialog showed %s rows holding %s and %s; took the first",
                result["matches"], document_type, subtype,
            )

        # Now a real click on that exact row, so STAC's own row handler runs
        # and anything it does to the form happens. Scripted clicks skip it.
        self._click_marked_row(use_double_click)

        # And again, because the click may have moved the selection.
        result = self._run_matrix_pick(document_type, subtype)
        if result.get("ok"):
            return

        why = result.get("why")
        if why == "no-grid":
            # No widget to drive, so fall back to the class on the row. Weaker,
            # but it is what the previous version relied on throughout.
            logger.warning(
                "Could not reach Kendo's grid widget in the matrix dialog; "
                "falling back to checking the row's own selected class."
            )
            try:
                self.wait.until(
                    lambda d: KENDO_SELECTED_CLASS
                    in (
                        d.find_element(By.CSS_SELECTOR, MATRIX_PICKED_ROW_CSS)
                        .get_attribute("class") or ""
                    )
                )
            except (TimeoutException, WebDriverException) as error:
                raise SystemProblem(
                    f"The {document_type}/{subtype} row never became the selected "
                    "row, so Select would have taken a different one."
                ) from error
            return

        raise SystemProblem(
            f"Could not make {document_type}/{subtype} the selected matrix row "
            f"({why}; grid reports {result.get('cells')}). Select would have taken "
            "whichever row Kendo had current, which is the first one in the list."
        )

    def _run_matrix_pick(self, document_type: str, subtype: str) -> dict:
        """Find, mark and select the matching row. Returns the script's report."""
        try:
            return self.driver.execute_script(
                SELECT_MATRIX_ROW_JS, document_type, subtype, MATRIX_DIALOG_CSS
            ) or {}
        except WebDriverException as error:
            raise SystemProblem(
                f"Could not read the Type/Subtype dialog's rows: {error}"
            ) from error

    def _click_marked_row(self, use_double_click: bool) -> None:
        """Click the row the pick script marked. Tolerant: the JS already chose it."""
        try:
            row = self.driver.find_element(By.CSS_SELECTOR, MATRIX_PICKED_ROW_CSS)
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", row
            )
            if use_double_click:
                ActionChains(self.driver).double_click(row).perform()
            else:
                row.click()
        except WebDriverException:
            # Something was over it, or it moved. The script's own selection
            # still stands, so this is not worth failing over.
            try:
                self.driver.execute_script(
                    f"var r = document.querySelector({MATRIX_PICKED_ROW_CSS!r});"
                    "if (r && r.cells.length) r.cells[0].click();"
                )
            except WebDriverException:
                pass

        self._settle()

    def _read_matrix_rows(self, document_type: str = "", subtype: str = "") -> dict:
        """What the dialog is listing, keyed to the pair being looked for."""
        try:
            return self.driver.execute_script(
                DESCRIBE_MATRIX_ROWS_JS, MATRIX_DIALOG_CSS, document_type, subtype
            ) or {}
        except WebDriverException:
            return {}

    def _matrix_row_count(self) -> int:
        """How many rows the dialog is listing. 0 if it cannot be read.

        Printed on every miss, because the row count is what says whether the
        search box filters at all: the same number every time means it does not.
        """
        return self._read_matrix_rows().get("total", 0)

    def _describe_matrix_rows(self, document_type: str, subtype: str) -> str:
        """Why the wanted row is not there, for an error a person reads.

        "No row for PLS/RVW" is the same sentence whether the pair is absent
        from STAC, the search box did not match it, or the grid is showing one
        page of many. Those need completely different fixes, so the message
        says which of them it is: whether the type exists at all, whether the
        subtype exists under some other type, and how many pairs are on the
        page to have been looked at.
        """
        report = self._read_matrix_rows(document_type, subtype)
        if not report:
            return "(could not read the dialog's rows)"

        total = report.get("total", 0)
        if not total:
            return (
                "no pairs at all. Either the search matched nothing or the grid "
                "had not loaded."
            )

        under_type = report.get("subtypesOfWantedType") or []
        over_sub = report.get("typesOfWantedSub") or []
        types = report.get("typeList") or []

        parts = [f"{total} pair(s) across {len(types)} type(s)"]

        near = report.get("nearPairs") or []
        if near:
            parts.append(
                "closest real pairs: "
                + ", ".join(sorted(set(near))[:MATRIX_ROW_SAMPLE])
            )

        if under_type:
            parts.append(
                f"{document_type} exists, with subtypes "
                f"{', '.join(sorted(set(under_type))[:MATRIX_ROW_SAMPLE])}"
            )
        else:
            parts.append(f"no type called {document_type} on the page")

        if over_sub:
            parts.append(
                f"subtype {subtype} exists under "
                f"{', '.join(sorted(set(over_sub))[:MATRIX_ROW_SAMPLE])}"
            )
        else:
            parts.append(f"no subtype called {subtype} on the page")

        if not under_type and not over_sub:
            parts.append(
                f"types listed: {', '.join(types[:MATRIX_ROW_SAMPLE])}"
                + (f" and {len(types) - MATRIX_ROW_SAMPLE} more"
                   if len(types) > MATRIX_ROW_SAMPLE else "")
            )

        return ". ".join(parts)

    def _press_matrix_select(self) -> None:
        """Press Select, after letting Kendo finish rebuilding the dialog."""
        self._settle()
        try:
            select_button = self.wait.until(
                EC.element_to_be_clickable((By.ID, MATRIX_SELECT_BUTTON_ID))
            )
            self.driver.execute_script("arguments[0].click();", select_button)
        except TimeoutException as error:
            raise SystemProblem(
                "Could not press Select in the Type/Subtype dialog."
            ) from error

    def _read_kendo_value(self, selector: str):
        """Return a Kendo dropdown's current value, or None if it is not there."""
        try:
            return self.driver.execute_script(
                "var w = $(arguments[0]).data('kendoDropDownList');"
                "return w ? w.value() : null;",
                selector,
            )
        except WebDriverException:
            return None

    def _confirm_type_subtype(self, document_type: str, subtype: str) -> None:
        """Check the form now holds the Type and Subtype that were asked for.

        The subtype alone is not enough. Searching the matrix for ORDERS
        brings back APPEALS/ORDERS, FEL APP/ORDERS and COURT/ORDERS, so a
        subtype check passes while the document is filed under the wrong
        type entirely.

        Raises:
            SystemProblem: If either field holds something else, or if the
                Type field cannot be found at all. Not knowing is treated the
                same as being wrong.
        """
        upload_wait = WebDriverWait(self.driver, self.upload_timeout)

        try:
            upload_wait.until(
                lambda d: self._read_kendo_value(SUBTYPE_INPUT_SELECTOR) == subtype
            )
        except TimeoutException as error:
            found = self._read_kendo_value(SUBTYPE_INPUT_SELECTOR)
            raise SystemProblem(
                f"Subtype never became {subtype}; the form holds {found!r}."
            ) from error

        # Read every candidate, not just the first one that answers. The field
        # names here are guesses at STAC's markup, and a guess that happens to
        # hit some other dropdown would otherwise fail a document that was
        # about to be filed correctly. Logged so the right name can be read off
        # a run instead of out of Inspect.
        found = {
            selector: self._read_kendo_value(selector)
            for selector in TYPE_INPUT_SELECTORS
        }
        answered = {s: v for s, v in found.items() if v is not None}

        if not answered:
            raise SystemProblem(
                "Could not read STAC's Type field, so there is no way to tell "
                f"{document_type}/{subtype} from another type with the same subtype. "
                f"Tried: {', '.join(TYPE_INPUT_SELECTORS)}."
            )

        logger.debug("Type candidates after Select: %s", answered)

        if document_type in answered.values():
            return

        # Nothing holds the right type. Name every candidate and its value, so
        # the next run says whether the row was wrong or the selector was.
        readings = ", ".join(f"{s} = {v!r}" for s, v in answered.items())
        raise SystemProblem(
            f"No Type field holds {document_type!r} after selecting "
            f"{document_type}/{subtype}. Found: {readings}. Either the matrix "
            f"search for {subtype!r} took the wrong row, or none of those "
            "selectors is STAC's Type box."
        )

    def _save(self, ucn: str, subtype: str, names: str) -> None:
        """Press Save and wait for STAC to confirm.

        Raises:
            SaveMayHaveHappened: If Save was clicked but no confirmation came.
                Retrying would file the document twice, so this is the one
                failure that goes straight to a person.
            SystemProblem: If Save could not be pressed at all, in which case
                nothing was saved and a retry is safe.
        """
        upload_wait = WebDriverWait(self.driver, self.upload_timeout)

        # The subtype reaches the form through Kendo, a moment after the
        # dialog closes. Saving before it lands files the document under
        # whatever was there before.
        try:
            upload_wait.until(
                lambda d: d.execute_script(
                    f"return $({SUBTYPE_INPUT_SELECTOR!r}).data('kendoDropDownList').value()"
                )
                == subtype
            )
        except TimeoutException as error:
            raise SystemProblem(
                f"The Subtype field never showed {subtype} before Save."
            ) from error

        try:
            upload_wait.until(EC.element_to_be_clickable((By.ID, SAVE_BUTTON_ID))).click()
        except TimeoutException as error:
            raise SystemProblem("Could not press Save.") from error

        # Past this line STAC may already have the document.
        try:
            upload_wait.until(
                EC.presence_of_element_located((By.XPATH, SAVED_NOTIFICATION_XPATH))
            )
        except TimeoutException as error:
            raise SaveMayHaveHappened(
                f"Pressed Save for {names} on {ucn} but STAC never confirmed. "
                "The document may or may not be on the case. Check it by hand; "
                "do not re-run this one."
            ) from error

        # Wait for the notification to clear, or it covers the next document's
        # Save button.
        try:
            upload_wait.until(
                EC.invisibility_of_element_located((By.XPATH, SAVED_NOTIFICATION_XPATH))
            )
        except TimeoutException:
            logger.warning("%s: the saved notification did not clear", ucn)

        logger.info("%s: saved %s under %s", ucn, names, self._pair)


def defendant_from_caption(document_text: str) -> str | None:
    """Pull the defendant's name out of a filing's caption, or None.

    Looks for STATE OF FLORIDA followed by v. / vs. / versus, then takes what
    comes after until the caption runs into the next part of the page. Returns
    None when nothing usable is there, which is normal for letters, forms and
    anything OCR mangled.
    """
    if not document_text:
        return None

    match = CAPTION_PATTERN.search(document_text)
    if not match:
        return None

    name = match.group(1)

    # The caption runs straight on into ", Defendant." or the case number, so
    # cut at whichever of those appears first.
    lowered = name.lower()
    cut = len(name)
    for stop in CAPTION_STOP_WORDS:
        where = lowered.find(stop)
        if where != -1:
            cut = min(cut, where)
    name = name[:cut]

    # Line breaks and commas are part of the caption's layout, not the name.
    name = re.sub(r"[\r\n,;:]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .,")

    if len(_name_tokens(name)) < MIN_NAME_TOKENS:
        return None

    return name


def _name_tokens(name: str) -> set[str]:
    """Uppercase word set for a name, minus STAC's alert flags and initials."""
    without_brackets = re.sub(r"\(.*?\)", "", name)
    return {
        word
        for word in re.findall(r"[a-zA-Z]+", without_brackets.upper())
        if word not in EXCLUDED_NAME_TOKENS and len(word) > 1
    }


def names_match(stac_name: str, document_name: str) -> bool:
    """True when the two names are the same person.

    Deliberately loose, because the two sources disagree in harmless ways.
    STAC shows SMITH, JOHN A (ALERT) where the caption reads JOHN ALLEN
    SMITH, so one name's words being contained in the other's is enough:
    that accepts a missing middle name, a suffix, and STAC's extra flags.

    Then, because the caption often comes from OCR, words that are nearly
    the same count as the same. PRIDDY, EMERICK against EMRICK PRIDDY is
    one dropped letter, not a different defendant. The threshold is set so
    that genuinely different surnames stay apart.

    This is a second guard on a case number that is usually already
    authoritative, so being a little generous here costs less than
    rejecting every scanned document.
    """
    stac_tokens = _name_tokens(stac_name)
    document_tokens = _name_tokens(document_name)

    if not stac_tokens or not document_tokens:
        return False

    if stac_tokens.issubset(document_tokens) or document_tokens.issubset(stac_tokens):
        return True

    # Whichever name has fewer words has to be fully accounted for in the
    # other. Going the other way would let a one-word name match anything.
    fewer, more = sorted((stac_tokens, document_tokens), key=len)
    if _every_word_has_a_near_match(fewer, more):
        logger.info("Names matched allowing for spelling")
        return True

    return False


def _every_word_has_a_near_match(fewer: set, more: set) -> bool:
    """True when each word in `fewer` has a close enough partner in `more`."""
    for word in fewer:
        best = max(
            (SequenceMatcher(None, word, other).ratio() for other in more), default=0.0
        )
        if best < NAME_TOKEN_SIMILARITY:
            return False
    return True


def name_in_text(stac_name: str, text: str) -> bool:
    """True when STAC's defendant is named close together somewhere in text.

    Every required word of STAC's name must have a word in the text at least
    REQUIRED_NAME_SIMILARITY alike, all within a few words of each other, in
    any order. So JOHN A SMITH, SMITH JOHN and STATE VS SMITH, JOHN ALLEN all
    find SMITH, JOHN A, but a SMITH in one sentence and a JOHN three lines on
    do not.
    """
    required = _required_name_words(stac_name)
    if len(required) < MIN_NAME_TOKENS or not text:
        return False

    words = re.findall(r"[A-Z]+", text.upper())
    positions = [
        [i for i, word in enumerate(words) if _words_alike(wanted, word)]
        for wanted in required
    ]
    reach = len(required) + NAME_WINDOW_SLACK
    return any(
        all(any(abs(p - start) <= reach for p in others) for others in positions[1:])
        for start in positions[0]
    )


def _required_name_words(stac_name: str) -> list[str]:
    """The words of STAC's name that must all be found.

    STAC writes LAST, FIRST MIDDLE (FLAGS). The surname and first name are
    required. Middle names and initials are not, since filings often leave
    them out. A name with no comma has every word required.
    """
    without_flags = re.sub(r"\(.*?\)", "", stac_name)
    if "," not in without_flags:
        return _name_words(without_flags)
    last, rest = without_flags.split(",", 1)
    return _name_words(last) + _name_words(rest)[:1]


def _name_words(name: str) -> list[str]:
    """Uppercase words of a name in order, minus STAC's flags and initials."""
    return [
        word
        for word in re.findall(r"[A-Z]+", name.upper())
        if word not in EXCLUDED_NAME_TOKENS and len(word) > 1
    ]


def _words_alike(wanted: str, word: str) -> bool:
    """True when two words are the same or REQUIRED_NAME_SIMILARITY alike."""
    if wanted == word:
        return True
    matcher = SequenceMatcher(None, wanted, word)
    # Cheap upper bounds first: a long document is tens of thousands of words.
    return (
        matcher.real_quick_ratio() >= REQUIRED_NAME_SIMILARITY
        and matcher.quick_ratio() >= REQUIRED_NAME_SIMILARITY
        and matcher.ratio() >= REQUIRED_NAME_SIMILARITY
    )


def _usable_driver_path(configured) -> str | None:
    """Return the configured chromedriver only if it is actually a file.

    A blank or wrong path used to reach Selenium as a directory, which fails
    with a message about not obtaining a driver and sends everyone looking in
    the wrong place. Better to say so here and fall through to the other
    routes.
    """
    if not configured:
        return None

    path = Path(str(configured))
    if path.is_file():
        return str(path)

    logger.warning(
        "paths.chromedriver is set to %s, which is not a file. Ignoring it and "
        "letting Selenium find a driver instead.",
        path,
    )
    return None


def _xpath_literal(value: str) -> str:
    """Quote a string for XPath, including when it contains an apostrophe.

    XPath 1.0 has no escape character, so a value with both quote kinds has
    to be assembled with concat(). Subtypes like EXPGE/SEAL are fine, but a
    future one with an apostrophe would silently break the row match.
    """
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    joined = ', "\'", '.join(f"'{part}'" for part in parts)
    return f"concat({joined})"


def document_label(path: Path) -> str:
    """Name an uploaded file by its email and position, never its filename.

    main.py writes each file as NNNN_P_<original name>, the email number and
    the attachment's position, and senders name files after the defendant.
    Only the numbers go into log lines and error messages. A file not named
    that way is just "a document", rather than risking the name leaking.
    """
    parts = path.name.split("_", 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
        return f"email {int(parts[0])} attachment {int(parts[1])}"
    return "a document"


def group_by_type(documents: list) -> list:
    """Group an email's documents by Type/Subtype, keeping their order.

    A STAC upload box carries one Type and Subtype for everything in it, so
    documents that share a pair go up together and the rest go separately.
    Two notices filed as COURT/NOTICES become one upload; a motion and an
    order become two.

    Args:
        documents: (path, document_type, subtype) for each attachment.

    Returns:
        [((document_type, subtype), [path, ...]), ...] in first-seen order.
    """
    groups: dict = {}
    for path, document_type, subtype in documents:
        groups.setdefault((document_type, subtype), []).append(path)
    return list(groups.items())


class StacRunner:
    """Owns one signed-in session for a whole pass and files emails through it.

    Used as `with StacRunner(config) as runner:`. Chrome opens once, not once
    per email: fifty sign-ins for one batch would be slow and would fill
    STAC's audit log with noise.
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        settings = StacSession(config)
        self.max_attempts = settings.max_attempts
        # Default true: a fresh browser per upload box is how PCSO911 works
        # and it is the behaviour that does not depend on STAC leaving the
        # page tidy. Turn it off for speed once a pass is reliably clean.
        self.fresh_browser = bool(config["stac"].get("fresh_browser", True))
        self.session: StacSession | None = None

    def __enter__(self) -> "StacRunner":
        self._start_session()
        return self

    def __exit__(self, *_) -> None:
        if self.session:
            self.session.close()
            self.session = None

    def _start_session(self) -> None:
        self.session = StacSession(self._config)
        self.session.open()

    def _restart_session(self) -> None:
        """Throw the browser away and sign in again.

        A wedged Kendo dialog or a half-loaded page does not recover from
        another click. Starting over is what actually works the second time.
        """
        logger.debug("Restarting the STAC browser session")
        if self.session:
            self.session.close()
        self._start_session()

    def _leave_browser_clean(self) -> None:
        """Throw away a browser that failed, so the next email starts fresh.

        A failure leaves STAC wherever it broke: a matrix dialog open, an
        upload pending, a case page whose search box STAC has disabled. The
        next email then fails on the mess rather than on anything of its own,
        which is what "Element is not currently interactable" was: email 1's
        wreckage, charged to emails 2 and 3.

        Never allowed to raise. Whatever went wrong before this is the error
        worth reporting, not a browser that would not restart.
        """
        try:
            self._restart_session()
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not restart the browser after a failure: %s", error)

    def enter_email(
        self,
        ucn: str,
        documents: list,
        document_text: str = "",
        subject: str = "",
        body: str = "",
    ) -> list:
        """File one email's documents on one case, retrying once if STAC wedges.

        Args:
            ucn: Case number for all of these documents.
            documents: (path, document_type, subtype) per attachment.
            document_text: Text of one of the documents, for the defendant
                name check. "" skips the check for Polk.
            subject: Email subject, for the Highlands and Hardee name check.
            body: Email body, the same.

        Returns:
            The groups entered, as [((document_type, subtype), [path, ...])].

        Raises:
            DocumentProblem: This email needs a person. Nothing was entered,
                unless the error is PartiallyEntered, whose message lists what
                was.
            SaveMayHaveHappened: Save was pressed with no confirmation. Never
                retried, since that could file the same document twice.
            SystemProblem: STAC is broken and the retries are used up. The
                pass should stop.
        """
        groups = group_by_type(documents)
        entered: list = []

        for attempt in range(1, self.max_attempts + 1):
            try:
                # Only the groups not already in STAC. On a retry the earlier
                # ones are already filed, and redoing them would duplicate.
                for key, paths in groups:
                    if key in [done_key for done_key, _ in entered]:
                        continue

                    # The case is re-opened per group, not once per email. A
                    # STAC upload box holds one Type/Subtype, so a motion and
                    # an order need separate trips, and each trip ends with
                    # the page somewhere else: after a Save, or after the
                    # reload that discards an unsaved upload. Starting from
                    # the case search each time is the only state that is
                    # reliably correct.
                    self.session.find_case(ucn, document_text, subject, body)

                    document_type, subtype = key
                    self.session.add_documents(ucn, document_type, subtype, paths)
                    entered.append((key, paths))

                    if self.fresh_browser:
                        # Close Chrome and sign in again, so the next upload
                        # box, and the next email, start from a clean page
                        # rather than whatever STAC left behind.
                        self._restart_session()

                return entered

            except (DocumentProblem, SaveMayHaveHappened):
                # Both are this document's problem, and neither improves by
                # trying again. SaveMayHaveHappened especially: a retry is
                # exactly what would file it twice.
                self._leave_browser_clean()
                raise

            except SystemProblem as error:
                if attempt >= self.max_attempts:
                    self._leave_browser_clean()
                    if entered:
                        raise PartiallyEntered(
                            f"{self._already_in(entered)} Then STAC failed and did not "
                            f"recover: {error}"
                        ) from error
                    raise

                logger.warning(
                    "%s: attempt %s of %s failed (%s). Restarting the browser.",
                    ucn, attempt, self.max_attempts, error,
                )
                self._restart_session()

        # The loop either returns or raises; this is unreachable.
        raise SystemProblem(f"{ucn}: ran out of attempts without a result.")

    @staticmethod
    def _already_in(entered: list) -> str:
        """Plain sentence naming what is already filed, for a person to read."""
        filed = ", ".join(
            f"{document_label(path)} as {document_type}/{subtype}"
            for (document_type, subtype), paths in entered
            for path in paths
        )
        return f"Already in STAC: {filed}."