"""Decides when an email whose case STAC could not find is searched again, and for how long.

Some emails arrive before their case is built in STAC. Rather than send those
to a person at once, DALYN tags them red Waiting for STAC, leaves them in the
Inbox, and searches again at the recheck times. Past the age limit, or for a
case number that cannot be a new case, the email goes to a person.

Kept out of main.py so the dates can be tested without importing the script
that reads live mail.
"""

from datetime import datetime, time, timedelta

from models import ReviewTag
from ucn import normalize

# A case not in STAC yet is a new case, so its year is this year, or last
# year for one filed around New Year. Any other year that STAC cannot find
# was mistyped or misread: older cases are already in STAC. This is the one
# cheap way to tell a typo from a case not built yet (Nick, 12 Oct 2026).
WAIT_YEARS_BACK = 1


def is_waiting(message: dict) -> bool:
    """True when the email carries Waiting for STAC, as listed from Graph."""
    return ReviewTag.WAITING in (message.get("categories") or [])


def received_local(message: dict) -> datetime:
    """Return Graph's receivedDateTime as naive local time.

    Naive so it compares with datetime.now(), which is what the recheck
    times and working hours are written in.

    Raises:
        KeyError: The message has no receivedDateTime.
        ValueError: It is not an ISO timestamp.
    """
    return datetime.fromisoformat(message["receivedDateTime"]).astimezone().replace(tzinfo=None)


def wait_ends(received: datetime, business_days: int) -> datetime:
    """Return when an email received at `received` has waited `business_days` weekdays.

    Each business day moves forward one weekday at the same time of day, so
    Monday 10:00 plus two is Wednesday 10:00 and Friday 15:00 plus two is
    Tuesday 15:00. Mail that arrives on a weekend counts from Monday 00:00.
    Holidays are not known, so they count as business days.
    """
    start = received
    while start.weekday() >= 5:
        start = datetime.combine(start.date() + timedelta(days=1), time())

    end = start
    for _ in range(business_days):
        end += timedelta(days=1)
        while end.weekday() >= 5:
            end += timedelta(days=1)
    return end


def should_wait(ucn: str, received: datetime, now: datetime, business_days: int) -> bool:
    """True when an email whose case STAC could not find should wait for another search.

    False sends it to a person: the case number's year cannot be a new
    case, or the email has already waited business_days.

    Args:
        ucn: The case number STAC searched for, in any printed form.
        received: From received_local.
        now: Local time now.
        business_days: recheck.max_business_days from config.
    """
    # Both the Polk and the Highlands/Hardee long forms put the year at 2:6.
    year = normalize(ucn)[2:6]
    if not year.isdigit() or not now.year - WAIT_YEARS_BACK <= int(year) <= now.year:
        return False
    return now < wait_ends(received, business_days)


def latest_slot(now: datetime, times: list[time]) -> datetime | None:
    """Return the most recent recheck time today at or before now, or None before the first.

    Carries the date, so 17:00 today and 09:00 tomorrow are different slots.
    The watch compares this against the slot it last rechecked to know when
    a new one is due.
    """
    passed = [slot for slot in times if slot <= now.time()]
    return datetime.combine(now.date(), max(passed)) if passed else None
