"""Tests for the per-mailbox table at the end of a pass."""

import logging
from collections import Counter

import pytest

from main import _log_mailbox_table
from models import EmailDecision

FIRST = "first@example.com"
SECOND = "second@example.com"
EMPTY = "empty@example.com"


def _email(mailbox: str, decision: str) -> dict:
    """The first row of one email, with only the fields the table reads."""
    return {"mailbox": mailbox, "email_decision": decision}


def _table(caplog: pytest.LogCaptureFixture, *args: object) -> dict[str, list[str]]:
    """Run the table and return each mailbox's numbers, keyed by address."""
    with caplog.at_level(logging.INFO, logger="dalyn.main"):
        _log_mailbox_table(*args)
    lines = [record.getMessage().split() for record in caplog.records]
    return {line[0]: line[1:] for line in lines[1:]}


def test_counts_each_mailbox_separately(caplog: pytest.LogCaptureFixture) -> None:
    """listed, upload, review, moved and gone line up per mailbox."""
    emails = [
        _email(FIRST, EmailDecision.UPLOAD),
        _email(FIRST, EmailDecision.UPLOAD),
        _email(FIRST, EmailDecision.MANUAL_REVIEW),
        _email(SECOND, EmailDecision.GONE),
    ]
    listed = Counter({FIRST: 3, SECOND: 1})
    moved = Counter({FIRST: 2})

    table = _table(caplog, [FIRST, SECOND], emails, listed, moved)

    assert table[FIRST] == ["3", "2", "1", "2", "0"]
    assert table[SECOND] == ["1", "0", "0", "0", "1"]


def test_empty_mailbox_still_gets_a_row(caplog: pytest.LogCaptureFixture) -> None:
    """A folder with nothing in it shows as zeros, not as a missing line."""
    listed = Counter({FIRST: 1, EMPTY: 0})

    table = _table(caplog, [FIRST, EMPTY], [_email(FIRST, EmailDecision.UPLOAD)], listed, Counter())

    assert table[EMPTY] == ["0", "0", "0", "0", "0"]


def test_rows_follow_config_order(caplog: pytest.LogCaptureFixture) -> None:
    """Mailboxes print in config order, so runs compare line for line."""
    table = _table(caplog, [SECOND, FIRST], [], Counter(), Counter())

    assert list(table) == [SECOND, FIRST]
