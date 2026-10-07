"""Tests for tagging support: the mailbox_actions config, ReviewTag, and the Graph calls.

No network and no Credential Manager. Graph is replaced by a fake session that
records each request and answers from a script.
"""

import copy
from pathlib import Path

import pytest
import yaml

from config_loader import load_config
from exceptions import MessageGone, SystemProblem
from graph_client import GraphClient
from models import ReviewTag

MAILBOX = "dalyn-test@example.com"

VALID_CONFIG = {
    "graph": {"tenant_id": "placeholder-tenant", "client_id": "placeholder-client"},
    "mailboxes": [{"address": MAILBOX, "enabled": True}],
    "source_folder": "deleteditems",
    "dry_run": True,
    "days_back": 2,
    "max_messages": 5,
    "newest_first": True,
    "stac": {"url": "https://stac-test.example.com", "is_test_instance": True},
    "mailbox_actions": {"tag_enabled": True, "move_enabled": False, "skip_tagged": False},
    "paths": {"logs": "logs", "temp": "temp", "excel": "rules.xlsx", "stac_types": "types.xlsx"},
}


def _load(tmp_path: Path, config: dict) -> dict:
    """Write a config to a temporary file and load it without credentials."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return load_config(path, with_credentials=False)


def _config_with(**actions: object) -> dict:
    """Return a copy of the valid config with mailbox_actions changed."""
    config = copy.deepcopy(VALID_CONFIG)
    config["mailbox_actions"].update(actions)
    return config


# Config


def test_valid_mailbox_actions_load(tmp_path: Path) -> None:
    """A complete section loads and comes back unchanged."""
    config = _load(tmp_path, VALID_CONFIG)

    assert config["mailbox_actions"] == VALID_CONFIG["mailbox_actions"]


def test_missing_section_is_refused(tmp_path: Path) -> None:
    """No defaults: an old config.yaml without the section stops at startup."""
    config = copy.deepcopy(VALID_CONFIG)
    del config["mailbox_actions"]

    with pytest.raises(SystemProblem, match="mailbox_actions"):
        _load(tmp_path, config)


def test_missing_switch_is_refused(tmp_path: Path) -> None:
    """Each of the three switches has to be written out."""
    config = copy.deepcopy(VALID_CONFIG)
    del config["mailbox_actions"]["skip_tagged"]

    with pytest.raises(SystemProblem, match="skip_tagged"):
        _load(tmp_path, config)


def test_non_boolean_switch_is_refused(tmp_path: Path) -> None:
    """"yes" in quotes is a string, and a string is not an answer here."""
    with pytest.raises(SystemProblem, match="tag_enabled"):
        _load(tmp_path, _config_with(tag_enabled="yes"))


def test_move_enabled_is_refused_until_moving_is_built(tmp_path: Path) -> None:
    """A switch that does nothing must not look like it worked."""
    with pytest.raises(SystemProblem, match="move_enabled"):
        _load(tmp_path, _config_with(move_enabled=True))


def test_section_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    """mailbox_actions: true is a likely typo and must not pass."""
    config = copy.deepcopy(VALID_CONFIG)
    config["mailbox_actions"] = True

    with pytest.raises(SystemProblem, match="mapping"):
        _load(tmp_path, config)


# ReviewTag


def test_every_tag_carries_the_prefix() -> None:
    """The prefix is how DALYN recognizes its own categories."""
    tags = ReviewTag.all()

    assert len(tags) == 13
    assert all(tag.startswith(ReviewTag.PREFIX) and tag != ReviewTag.PREFIX for tag in tags)
    assert len(set(tags)) == len(tags)


def test_no_tag_contains_a_category_separator() -> None:
    """Outlook splits categories on commas and semicolons, and Graph returns 400."""
    assert [tag for tag in ReviewTag.all() if "," in tag or ";" in tag] == []


def test_is_dalyn_tells_own_tags_from_staff_tags() -> None:
    """Staff categories are never mistaken for DALYN's."""
    assert ReviewTag.is_dalyn(ReviewTag.FILED)
    assert not ReviewTag.is_dalyn("Red category")
    assert not ReviewTag.is_dalyn("Follow up DALYN")


# Graph client


class FakeResponse:
    """Just enough of requests.Response for GraphClient._request."""

    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        """Build a response with a status and an optional JSON body."""
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload or {}
        self.text = str(self._payload)
        self.headers: dict = {}

    def json(self) -> dict:
        """Return the scripted JSON body."""
        return self._payload


class FakeSession:
    """Records every request and answers each one from a queue of responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        """Queue the responses, in the order the calls will be made."""
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.params: list[dict | None] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        """Record the call and return the next scripted response."""
        self.calls.append((method, url, kwargs.get("json")))
        self.params.append(kwargs.get("params"))
        return self.responses.pop(0)


def _client(responses: list[FakeResponse]) -> tuple[GraphClient, FakeSession]:
    """Build a GraphClient without MSAL, so nothing signs in or touches the network."""
    session = FakeSession(responses)
    client = GraphClient.__new__(GraphClient)
    client._session = session
    client._allowed_mailboxes = frozenset({MAILBOX})
    client._get_token = lambda: "placeholder-token"
    return client, session


def test_ensure_categories_creates_only_missing_ones_in_red() -> None:
    """Existing categories are matched ignoring case and not created again."""
    master = {"value": [{"displayName": "dalyn: filed", "color": "preset0"}]}
    client, session = _client([FakeResponse(200, master), FakeResponse(201)])

    client.ensure_categories(MAILBOX, [ReviewTag.FILED, ReviewTag.NO_UCN], ReviewTag.COLOR)

    posts = [call for call in session.calls if call[0] == "POST"]
    assert posts == [
        (
            "POST",
            f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/outlook/masterCategories",
            {"displayName": ReviewTag.NO_UCN, "color": "preset0"},
        )
    ]


def test_ensure_categories_leaves_another_color_alone() -> None:
    """A category someone recolored is not overwritten and not recreated."""
    master = {"value": [{"displayName": ReviewTag.FILED, "color": "preset7"}]}
    client, session = _client([FakeResponse(200, master)])

    client.ensure_categories(MAILBOX, [ReviewTag.FILED], ReviewTag.COLOR)

    assert [call[0] for call in session.calls] == ["GET"]


def test_set_categories_patches_exactly_the_given_list() -> None:
    """The PATCH body is the whole list, staff categories included."""
    client, session = _client([FakeResponse(200)])

    client.set_categories(MAILBOX, "message-1", ["Staff category", ReviewTag.REHEARSED])

    assert session.calls == [
        (
            "PATCH",
            f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/messages/message-1",
            {"categories": ["Staff category", ReviewTag.REHEARSED]},
        )
    ]


def test_set_categories_on_a_deleted_message_is_message_gone() -> None:
    """A message purged mid-pass is skipped, not treated as a broken system."""
    client, _ = _client([FakeResponse(404)])

    with pytest.raises(MessageGone):
        client.set_categories(MAILBOX, "message-1", [ReviewTag.FILED])


def test_403_names_the_missing_permission() -> None:
    """A refused write says which permission to check, not just 403."""
    client, _ = _client([FakeResponse(403)])

    with pytest.raises(SystemProblem, match="Mail.ReadWrite"):
        client.set_categories(MAILBOX, "message-1", [ReviewTag.FILED])


def test_write_to_mailbox_off_the_allowlist_is_refused() -> None:
    """Tagging goes through the same allowlist as reading, before any request."""
    client, session = _client([])

    with pytest.raises(SystemProblem, match="allowlist"):
        client.set_categories("someone-else@example.com", "message-1", [ReviewTag.FILED])
    with pytest.raises(SystemProblem, match="allowlist"):
        client.ensure_categories("someone-else@example.com", [ReviewTag.FILED], ReviewTag.COLOR)

    assert session.calls == []


# list_messages with keep


def _message(message_id: str, tagged: bool = False) -> dict:
    """A listed message, tagged by DALYN or not."""
    return {"id": message_id, "hasAttachments": True, "categories": [ReviewTag.FILED] if tagged else []}


def _page(messages: list[dict], more: bool) -> FakeResponse:
    """One page of a Graph listing, with a nextLink when more pages follow."""
    payload: dict = {"value": messages}
    if more:
        payload["@odata.nextLink"] = "https://graph.microsoft.com/v1.0/next-page"
    return FakeResponse(200, payload)


def _untagged(message: dict) -> bool:
    """Stand-in for the real keep check: no DALYN category."""
    return not any(ReviewTag.is_dalyn(category) for category in message["categories"])


def test_passed_over_messages_do_not_count_toward_the_limit() -> None:
    """Three tagged emails in front must not make a limit of 2 come back short."""
    client, session = _client([
        _page([_message("t1", True), _message("u1"), _message("t2", True)], more=True),
        _page([_message("u2"), _message("u3")], more=True),
    ])

    listed = client.list_messages(MAILBOX, days_back=2, max_messages=2, keep=_untagged)

    assert [message["id"] for message in listed] == ["u1", "u2"]
    assert len(session.calls) == 2


def test_paging_stops_once_enough_are_kept() -> None:
    """The next page is not fetched when the first already has enough."""
    client, session = _client([_page([_message("u1"), _message("u2"), _message("u3")], more=True)])

    listed = client.list_messages(MAILBOX, days_back=2, max_messages=2, keep=_untagged)

    assert [message["id"] for message in listed] == ["u1", "u2"]
    assert len(session.calls) == 1


def test_without_keep_every_message_counts() -> None:
    """skip_tagged off: tagged mail is listed and counted exactly as before."""
    client, _ = _client([_page([_message("t1", True), _message("u1"), _message("u2")], more=True)])

    listed = client.list_messages(MAILBOX, days_back=2, max_messages=2)

    assert [message["id"] for message in listed] == ["t1", "u1"]


def test_keep_asks_for_full_pages() -> None:
    """--limit 3 with keep must not page through handled mail three at a time."""
    client, session = _client([_page([], more=False), _page([], more=False)])

    client.list_messages(MAILBOX, days_back=2, max_messages=3, keep=_untagged)
    client.list_messages(MAILBOX, days_back=2, max_messages=3)

    assert [params["$top"] for params in session.params] == [100, 3]
