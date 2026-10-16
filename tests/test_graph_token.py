"""Tests for _get_token: a dropped connection on renewal is retried, not fatal.

No network. GraphClient is built without __init__ and given a fake MSAL app.
"""

import pytest
import requests

import graph_client
from exceptions import GraphAuthError, SystemProblem
from graph_client import TOKEN_RETRIES, GraphClient


class FakeMsal:
    """Stands in for MSAL: drops the connection `drops` times, then answers."""

    def __init__(self, drops: int, answer: dict) -> None:
        """Remember how many drops to give and what to answer after."""
        self.drops = drops
        self.answer = answer
        self.calls = 0

    def acquire_token_for_client(self, scopes: list[str]) -> dict:
        """Raise the way requests does on a dropped line, then return the answer."""
        self.calls += 1
        if self.calls <= self.drops:
            raise requests.ConnectionError("connection reset")
        return self.answer


def _client(monkeypatch: pytest.MonkeyPatch, msal_app: FakeMsal) -> GraphClient:
    """A client with the fake MSAL app and no real waiting."""
    monkeypatch.setattr(graph_client.time, "sleep", lambda seconds: None)
    client = GraphClient.__new__(GraphClient)
    client._msal_app = msal_app
    return client


def test_dropped_renewal_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """One drop, then a token: the call carries on as if nothing happened."""
    msal_app = FakeMsal(drops=1, answer={"access_token": "placeholder"})

    assert _client(monkeypatch, msal_app)._get_token() == "placeholder"
    assert msal_app.calls == 2


def test_renewal_that_never_connects_is_a_system_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    """Out of tries: a SystemProblem, which --watch survives, not a raw error."""
    msal_app = FakeMsal(drops=TOKEN_RETRIES, answer={"access_token": "placeholder"})

    with pytest.raises(SystemProblem, match="ConnectionError") as raised:
        _client(monkeypatch, msal_app)._get_token()

    assert type(raised.value) is SystemProblem
    assert msal_app.calls == TOKEN_RETRIES


def test_refused_credential_is_still_an_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real refusal is not retried and still stops the watch."""
    msal_app = FakeMsal(drops=0, answer={"error": "invalid_client", "error_description": "bad secret"})

    with pytest.raises(GraphAuthError):
        _client(monkeypatch, msal_app)._get_token()

    assert msal_app.calls == 1
