"""Tests for main._recheck_due: which --watch passes search waiting emails again."""

from datetime import datetime, time

from main import _recheck_due
from recheck import latest_slot

TIMES = [time(9), time(13), time(17)]


def _slot(hour: int, minute: int = 0, day: int = 12) -> datetime | None:
    """The recheck slot at that time on that October 2026 day."""
    return latest_slot(datetime(2026, 10, day, hour, minute), TIMES)


def test_first_pass_always_rechecks() -> None:
    """A restart may have missed a recheck time."""
    assert _recheck_due(_slot(7), None, rechecked=False)
    assert _recheck_due(_slot(10), None, rechecked=False)


def test_same_slot_is_not_repeated() -> None:
    """Passes every 180 seconds do not each recheck."""
    assert not _recheck_due(_slot(9, 3), _slot(9), rechecked=True)
    assert not _recheck_due(_slot(12, 59), _slot(9), rechecked=True)


def test_next_time_rechecks() -> None:
    """13:00 is new after 09:00."""
    assert _recheck_due(_slot(13, 1), _slot(9), rechecked=True)


def test_early_morning_is_not_a_recheck() -> None:
    """Before 09:00 there is no slot, which is not the same as a new one."""
    assert not _recheck_due(_slot(7, day=13), _slot(17), rechecked=True)


def test_next_morning_rechecks_at_nine() -> None:
    """09:00 tomorrow differs from 17:00 today."""
    assert _recheck_due(_slot(9, 2, day=13), _slot(17), rechecked=True)


def test_started_before_nine_then_nine_comes() -> None:
    """The first pass at 07:00 leaves no slot done, so 09:00 is new."""
    assert _recheck_due(_slot(9), None, rechecked=True)
