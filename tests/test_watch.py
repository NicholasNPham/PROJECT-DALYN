"""Tests for main._watch: which failures stop the watch and which it waits out.

No mail, no browser, no waiting. run_pass is replaced by a script of
outcomes, and a KeyboardInterrupt at the end of the script stops the watch
the way Ctrl+C does.
"""

import pytest

import main
from exceptions import GraphAuthError, ServiceUnreachable, SystemProblem

CONFIG = {"recheck": {"times": []}}


class Script:
    """Plays one outcome per pass: None for a good pass, or an error to raise."""

    def __init__(self, outcomes: list[Exception | None]) -> None:
        """Hold the outcomes, then Ctrl+C once they run out."""
        self.outcomes = list(outcomes)
        self.passes = 0

    def __call__(self, config: dict, limit: int | None, recheck: bool) -> None:
        """Stand in for run_pass."""
        self.passes += 1
        if not self.outcomes:
            raise KeyboardInterrupt
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


def _watch(monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception | None]) -> tuple[int, Script, list[str]]:
    """Run the watch over `outcomes`. Returns its exit code, the script, and the alerts sent."""
    script = Script(outcomes)
    alerts: list[str] = []
    monkeypatch.setattr(main, "run_pass", script)
    monkeypatch.setattr(main, "report_failure", lambda config, error: alerts.append(f"stopped:{type(error).__name__}"))
    monkeypatch.setattr(main, "report_success", lambda config: alerts.append("running"))
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)
    return main._watch(CONFIG, limit=None, interval=180), script, alerts


def _unreachable() -> ServiceUnreachable:
    """STAC's login page timing out, as on 20 Oct 2026."""
    return ServiceUnreachable("Could not load STAC: TimeoutException")


def test_three_real_failures_still_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged: DALYN's own problems stop the watch after three in a row."""
    code, script, _ = _watch(monkeypatch, [SystemProblem("x")] * 3)

    assert code == main.EXIT_SYSTEM_PROBLEM
    assert script.passes == 3


def test_outage_is_waited_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 20 Oct evening: many unreachable passes in a row, and the watch keeps going."""
    code, script, alerts = _watch(monkeypatch, [_unreachable()] * 10)

    assert code == main.EXIT_OK
    assert script.passes == 11
    assert alerts == ["stopped:ServiceUnreachable"] * 10


def test_good_pass_after_an_outage_says_running_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The outage ends and the next pass reports success, which sends the all-clear."""
    _, _, alerts = _watch(monkeypatch, [_unreachable(), _unreachable(), None])

    assert alerts == ["stopped:ServiceUnreachable", "stopped:ServiceUnreachable", "running"]


def test_outage_does_not_clear_real_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two real failures around an outage, then a third: still three in a row."""
    outcomes = [SystemProblem("x"), _unreachable(), SystemProblem("x"), SystemProblem("x")]

    code, script, _ = _watch(monkeypatch, outcomes)

    assert code == main.EXIT_SYSTEM_PROBLEM
    assert script.passes == 4


def test_good_pass_clears_real_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged: a good pass resets the count, so two, a good one, then two more does not stop."""
    outcomes = [SystemProblem("x"), SystemProblem("x"), None, SystemProblem("x"), SystemProblem("x")]

    code, _, _ = _watch(monkeypatch, outcomes)

    assert code == main.EXIT_OK


def test_refused_credential_still_stops_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged: a bad credential is never waited out."""
    with pytest.raises(GraphAuthError):
        _watch(monkeypatch, [GraphAuthError("refused")])
