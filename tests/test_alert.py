"""Tests for alert.py: one email per outage, nothing sensitive in it, never raises."""

from pathlib import Path

import pytest

from alert import STATE_FILE_NAME, report_failure, report_success
from exceptions import SystemProblem


class FakeSender:
    """Records each email instead of sending it, or fails when told to."""

    def __init__(self, fail: bool = False) -> None:
        """Start with no emails sent."""
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, subject: str, body: str) -> None:
        """Record the email, or raise as Graph would."""
        if self.fail:
            raise SystemProblem("Graph refused POST with 403")
        self.sent.append((subject, body))


def _config(tmp_path: Path, enabled: bool = True) -> dict:
    """Just the keys alert.py reads."""
    return {
        "alerts": {"enabled": enabled, "send_from": "alerts@example.com", "send_to": "person@example.com"},
        "paths": {"logs": tmp_path},
    }


def test_first_failure_sends_one_email(tmp_path: Path) -> None:
    """The first stopped run emails once and records it."""
    send = FakeSender()

    report_failure(_config(tmp_path), SystemProblem("x"), send)

    assert [subject for subject, _ in send.sent] == ["DALYN stopped"]
    assert (tmp_path / STATE_FILE_NAME).exists()


def test_repeat_failures_do_not_send_again(tmp_path: Path) -> None:
    """An outage across many scheduled runs is one email, not one per run."""
    send = FakeSender()
    config = _config(tmp_path)

    for _ in range(4):
        report_failure(config, SystemProblem("x"), send)

    assert len(send.sent) == 1


def test_success_after_failure_sends_running_again_and_resets(tmp_path: Path) -> None:
    """Recovery is announced once, and the next outage alerts afresh."""
    send = FakeSender()
    config = _config(tmp_path)

    report_failure(config, SystemProblem("x"), send)
    report_success(config, send)
    report_success(config, send)
    report_failure(config, SystemProblem("y"), send)

    assert [subject for subject, _ in send.sent] == ["DALYN stopped", "DALYN running again", "DALYN stopped"]


def test_success_with_no_outage_sends_nothing(tmp_path: Path) -> None:
    """A normal run is silent."""
    send = FakeSender()

    report_success(_config(tmp_path), send)

    assert send.sent == []


def test_email_names_the_error_type_but_not_its_text(tmp_path: Path) -> None:
    """The message could carry a case number, so only the type goes out."""
    send = FakeSender()

    report_failure(_config(tmp_path), SystemProblem("Could not open the Images tab for 000000"), send)

    body = send.sent[0][1]
    assert "SystemProblem" in body
    assert "000000" not in body
    assert "Images tab" not in body


def test_failed_send_never_raises_and_is_tried_again(tmp_path: Path) -> None:
    """Mail.Send missing: logged, and nothing recorded, so the next run tries again."""
    config = _config(tmp_path)

    report_failure(config, SystemProblem("x"), FakeSender(fail=True))
    assert not (tmp_path / STATE_FILE_NAME).exists()

    send = FakeSender()
    report_failure(config, SystemProblem("x"), send)
    assert len(send.sent) == 1


def test_failed_running_again_email_keeps_the_outage_open(tmp_path: Path) -> None:
    """If recovery could not be announced, it is announced on the next good run."""
    config = _config(tmp_path)
    report_failure(config, SystemProblem("x"), FakeSender())

    report_success(config, FakeSender(fail=True))
    send = FakeSender()
    report_success(config, send)

    assert [subject for subject, _ in send.sent] == ["DALYN running again"]


@pytest.mark.parametrize("report", ["failure", "success"])
def test_alerts_off_sends_nothing_and_writes_nothing(tmp_path: Path, report: str) -> None:
    """Disabled means silent, with no state file either."""
    send = FakeSender()
    config = _config(tmp_path, enabled=False)

    if report == "failure":
        report_failure(config, SystemProblem("x"), send)
    else:
        report_success(config, send)

    assert send.sent == []
    assert not (tmp_path / STATE_FILE_NAME).exists()


def test_unreadable_state_file_counts_as_no_alert_sent(tmp_path: Path) -> None:
    """A damaged record costs one extra email rather than silencing alerts."""
    (tmp_path / STATE_FILE_NAME).write_text("not json", encoding="utf-8")
    send = FakeSender()

    report_failure(_config(tmp_path), SystemProblem("x"), send)

    assert len(send.sent) == 1
