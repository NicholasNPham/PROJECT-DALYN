"""Tests for _get_token: a dropped connection on renewal is retried, not fatal.

No network. GraphClient is built without __init__ and given a fake MSAL app.
"""

import pytest
import requests

import graph_client
from exceptions import GraphAuthError, ServiceUnreachable, SystemProblem
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


def test_renewal_that_never_connects_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Out of tries: ServiceUnreachable, which --watch keeps retrying, not a raw error or an auth error."""
    msal_app = FakeMsal(drops=TOKEN_RETRIES, answer={"access_token": "placeholder"})

    with pytest.raises(ServiceUnreachable, match="ConnectionError") as raised:
        _client(monkeypatch, msal_app)._get_token()

    assert isinstance(raised.value, SystemProblem)
    assert not isinstance(raised.value, GraphAuthError)
    assert msal_app.calls == TOKEN_RETRIES


def test_refused_credential_is_still_an_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real refusal is not retried and still stops the watch."""
    msal_app = FakeMsal(drops=0, answer={"error": "invalid_client", "error_description": "bad secret"})

    with pytest.raises(GraphAuthError):
        _client(monkeypatch, msal_app)._get_token()

    assert msal_app.calls == 1


class FakeResponse:
    """A Graph answer with only a status code."""

    def __init__(self, status_code: int) -> None:
        """Hold the status, with no Retry-After header."""
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers: dict[str, str] = {}
        self.text = ""


class FakeSession:
    """Stands in for requests.Session: every call drops, or every call gets `status`."""

    def __init__(self, status: int | None) -> None:
        """None means the connection drops every time."""
        self.status = status
        self.calls = 0

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        """Drop the line the way requests does, or answer with the status."""
        self.calls += 1
        if self.status is None:
            raise requests.ReadTimeout("read timed out")
        return FakeResponse(self.status)


def _request_client(monkeypatch: pytest.MonkeyPatch, status: int | None) -> GraphClient:
    """A client whose token always works and whose Graph calls all fail the same way."""
    client = _client(monkeypatch, FakeMsal(drops=0, answer={"access_token": "placeholder"}))
    client._session = FakeSession(status)
    return client


@pytest.mark.parametrize("status", [None, 503, 504])
def test_read_out_of_retries_is_unreachable(monkeypatch: pytest.MonkeyPatch, status: int | None) -> None:
    """The 20 Oct 5:18 PM ReadTimeout, but every try: --watch waits it out instead of stopping."""
    client = _request_client(monkeypatch, status)

    with pytest.raises(ServiceUnreachable, match="still failing after"):
        client._request("GET", "https://graph.example.invalid/x")

    assert client._session.calls == graph_client.MAX_RETRIES


def test_busy_write_out_of_retries_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 means Microsoft did nothing, so even a write is safe to call unreachable."""
    with pytest.raises(ServiceUnreachable):
        _request_client(monkeypatch, 503)._request("PATCH", "https://graph.example.invalid/x")


def test_write_that_drops_is_still_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write may have landed before the line dropped, so it stays a plain SystemProblem."""
    client = _request_client(monkeypatch, None)

    with pytest.raises(SystemProblem, match="may or may not have gone through") as raised:
        client._request("PATCH", "https://graph.example.invalid/x")

    assert type(raised.value) is SystemProblem
    assert client._session.calls == 1


def test_gateway_error_on_a_write_is_still_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 502 PATCH from 19 and 21 Oct: not retried, not unreachable."""
    with pytest.raises(SystemProblem, match="failed with 502") as raised:
        _request_client(monkeypatch, 502)._request("PATCH", "https://graph.example.invalid/x")

    assert type(raised.value) is SystemProblem
