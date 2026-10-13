"""Tests for main._enter_in_stac when STAC has no case: wait, or go to a person."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from exceptions import DocumentProblem
from main import _enter_in_stac
from models import EmailDecision, Outcome, StacResult
from stac import CaseNotFound


class FakeRunner:
    """Stands in for StacRunner, raising whatever the test asks for."""

    def __init__(self, error: Exception) -> None:
        """Remember the error enter_email will raise."""
        self.error = error

    def enter_email(self, *args: object, **kwargs: object) -> None:
        """Fail the way STAC would."""
        raise self.error


def _rows(ucn: str) -> list[dict]:
    """One enterable attachment, with the fields _enter_in_stac reads."""
    return [
        {
            "outcome": Outcome.WOULD_ENTER,
            "attachment_name": "document.pdf",
            "_bytes": b"%PDF-",
            "_text": "",
            "classified_type": "COURT",
            "classified_subtype": "ORDERS",
            "ucn": ucn,
            "email_decision": EmailDecision.UPLOAD,
            "email_reason": "",
            "stac_result": "",
            "stac_reason": "",
        }
    ]


def _message(age: timedelta) -> dict:
    """A message received `age` ago, as Graph lists it."""
    received = datetime.now(timezone.utc) - age
    return {"receivedDateTime": received.strftime("%Y-%m-%dT%H:%M:%SZ"), "subject": "", "body": {"content": ""}}


def _this_year_ucn() -> str:
    """A placeholder case number in the current year, so the year rule lets it wait."""
    return f"53{datetime.now().year}CF000000A000XX"


def _run(tmp_path: Path, rows: list[dict], message: dict, error: Exception) -> bool:
    """Call _enter_in_stac with a runner that fails with `error`."""
    return _enter_in_stac(FakeRunner(error), rows, tmp_path, 1, message, 2)


def test_new_email_waits(tmp_path: Path) -> None:
    """Just arrived, this year's number: held red, and not counted against STAC."""
    rows = _rows(_this_year_ucn())

    assert _run(tmp_path, rows, _message(timedelta(minutes=5)), CaseNotFound("No case")) is True
    assert rows[0]["stac_result"] == StacResult.WAITING
    assert rows[0]["email_decision"] == EmailDecision.MANUAL_REVIEW


def test_old_email_goes_to_a_person(tmp_path: Path) -> None:
    """Two weeks is past any two business days."""
    rows = _rows(_this_year_ucn())

    assert _run(tmp_path, rows, _message(timedelta(days=14)), CaseNotFound("No case")) is True
    assert rows[0]["stac_result"] == StacResult.FAILED
    assert "Not waiting" in rows[0]["stac_reason"]


def test_old_year_goes_to_a_person_at_once(tmp_path: Path) -> None:
    """An old case would already be in STAC, so this is a typo."""
    rows = _rows("53-2019-CF-000000-A000-XX")

    _run(tmp_path, rows, _message(timedelta(minutes=5)), CaseNotFound("No case"))
    assert rows[0]["stac_result"] == StacResult.FAILED


def test_other_document_problems_do_not_wait(tmp_path: Path) -> None:
    """A name mismatch will not fix itself, so it goes green as before."""
    rows = _rows(_this_year_ucn())

    _run(tmp_path, rows, _message(timedelta(minutes=5)), DocumentProblem("Defendant mismatch"))
    assert rows[0]["stac_result"] == StacResult.FAILED
