"""Tests for recheck: the age limit, the year rule, and which recheck time is due."""

from datetime import datetime, time

import pytest

from models import ReviewTag
from recheck import is_waiting, latest_slot, received_local, should_wait, wait_ends

POLK = "53-2026-CF-000000-A000-XX"
HIGHLANDS = "282026CF000000CFAXMX"
TIMES = [time(9), time(13), time(17)]

# 12 Oct 2026 is a Monday.
MONDAY = datetime(2026, 10, 12, 10, 0)
FRIDAY = datetime(2026, 10, 16, 15, 0)
SATURDAY = datetime(2026, 10, 17, 10, 0)


def test_is_waiting_reads_the_listed_categories() -> None:
    """Only the Waiting tag counts, among staff categories or none at all."""
    assert is_waiting({"categories": ["Red category", ReviewTag.WAITING]})
    assert not is_waiting({"categories": [ReviewTag.STAC_FAILED]})
    assert not is_waiting({"categories": []})
    assert not is_waiting({})


def test_received_local_is_naive() -> None:
    """Naive, so it compares with datetime.now() without a TypeError."""
    assert received_local({"receivedDateTime": "2026-10-12T18:52:02Z"}).tzinfo is None


@pytest.mark.parametrize(
    ("received", "expected"),
    [
        (MONDAY, datetime(2026, 10, 14, 10, 0)),
        (FRIDAY, datetime(2026, 10, 20, 15, 0)),
        (SATURDAY, datetime(2026, 10, 21, 0, 0)),
        (datetime(2026, 10, 18, 23, 0), datetime(2026, 10, 21, 0, 0)),
    ],
    ids=["monday", "friday-skips-weekend", "saturday-counts-from-monday", "sunday-night"],
)
def test_wait_ends_counts_weekdays(received: datetime, expected: datetime) -> None:
    """Two business days, weekends skipped."""
    assert wait_ends(received, 2) == expected


def test_should_wait_until_the_limit() -> None:
    """Waits up to the end of the second business day, not a moment after."""
    ends = wait_ends(MONDAY, 2)
    assert should_wait(POLK, MONDAY, MONDAY, 2)
    assert should_wait(POLK, MONDAY, datetime(2026, 10, 14, 9, 59), 2)
    assert not should_wait(POLK, MONDAY, ends, 2)


def test_friday_mail_still_waits_on_monday() -> None:
    """The weekend does not use up the wait."""
    assert should_wait(POLK, FRIDAY, datetime(2026, 10, 19, 17, 0), 2)


def test_old_mail_goes_to_a_person_on_its_first_attempt() -> None:
    """A backlog email already past the limit is not held at all."""
    assert not should_wait(POLK, datetime(2026, 10, 1, 9, 0), MONDAY, 2)


@pytest.mark.parametrize(
    ("ucn", "waits"),
    [
        (POLK, True),
        ("53-2025-CF-000000-A000-XX", True),
        ("53-2019-CF-000000-A000-XX", False),
        ("53-2062-CF-000000-A000-XX", False),
        (HIGHLANDS, True),
        ("282019CF000000CFAXMX", False),
        ("532009CF00000001XXXX", False),
    ],
    ids=["this-year", "last-year", "old-year", "future-year", "highlands", "highlands-old", "old-polk-form"],
)
def test_year_rule(ucn: str, waits: bool) -> None:
    """Only this year and last year can be a case not built yet."""
    assert should_wait(ucn, MONDAY, MONDAY, 2) is waits


def test_latest_slot() -> None:
    """None before the first time, then the most recent one, with its date."""
    assert latest_slot(datetime(2026, 10, 12, 8, 59), TIMES) is None
    assert latest_slot(datetime(2026, 10, 12, 9, 0), TIMES) == datetime(2026, 10, 12, 9, 0)
    assert latest_slot(datetime(2026, 10, 12, 16, 59), TIMES) == datetime(2026, 10, 12, 13, 0)
    assert latest_slot(datetime(2026, 10, 12, 17, 20), TIMES) == datetime(2026, 10, 12, 17, 0)


def test_tomorrows_slot_differs_from_todays() -> None:
    """17:00 today and 09:00 tomorrow are separate rechecks."""
    today = latest_slot(datetime(2026, 10, 12, 17, 20), TIMES)
    tomorrow = latest_slot(datetime(2026, 10, 13, 9, 5), TIMES)
    assert today != tomorrow
