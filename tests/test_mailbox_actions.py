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
from graph_client import ALLOWED_FOLDERS, GraphClient
from models import ReviewTag

MAILBOX = "dalyn-test@example.com"
ALERT_SENDER = "alerts@example.com"

VALID_CONFIG = {
    "graph": {"tenant_id": "placeholder-tenant", "client_id": "placeholder-client"},
    "mailboxes": [{"address": MAILBOX, "enabled": True}],
    "source_folder": "deleteditems",
    "days_back": 2,
    "max_messages_per_mailbox": 5,
    "newest_first": True,
    "stac": {"url": "https://stac-test.example.com", "is_test_instance": True},
    "mailbox_actions": {
        "tag_enabled": True,
        "move_enabled": False,
        "skip_tagged": False,
        "done_folder": "deleteditems/DALYN Completed",
    },
    "alerts": {"enabled": False, "send_from": "", "send_to": ""},
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


def test_old_max_messages_is_refused_with_new_name(tmp_path: Path) -> None:
    """The old shared budget is named in the error along with what replaced it."""
    config = copy.deepcopy(VALID_CONFIG)
    config["max_messages"] = config.pop("max_messages_per_mailbox")

    with pytest.raises(SystemProblem, match="max_messages_per_mailbox"):
        _load(tmp_path, config)


def test_old_and_new_budget_together_are_refused(tmp_path: Path) -> None:
    """Leaving the old key beside the new one is refused, not quietly ignored."""
    config = copy.deepcopy(VALID_CONFIG)
    config["max_messages"] = 50

    with pytest.raises(SystemProblem, match="'max_messages'"):
        _load(tmp_path, config)


def test_non_boolean_switch_is_refused(tmp_path: Path) -> None:
    """"yes" in quotes is a string, and a string is not an answer here."""
    with pytest.raises(SystemProblem, match="tag_enabled"):
        _load(tmp_path, _config_with(tag_enabled="yes"))


def test_move_enabled_with_tagging_is_accepted(tmp_path: Path) -> None:
    """Moving is built now, and allowed once tagging is on."""
    config = _load(tmp_path, _config_with(move_enabled=True))

    assert config["mailbox_actions"]["move_enabled"] is True


def test_move_without_tagging_is_refused(tmp_path: Path) -> None:
    """A moved email's tag is the only record of what happened to it."""
    with pytest.raises(SystemProblem, match="tag_enabled"):
        _load(tmp_path, _config_with(move_enabled=True, tag_enabled=False))


def test_missing_folder_is_refused_even_with_moving_off(tmp_path: Path) -> None:
    """Turning moving on later must be one switch, not a missing setting."""
    config = copy.deepcopy(VALID_CONFIG)
    del config["mailbox_actions"]["done_folder"]

    with pytest.raises(SystemProblem, match="done_folder"):
        _load(tmp_path, config)


@pytest.mark.parametrize("folder", ["inbox", "Archive", "Manual Review", "deleteditems/Other", "", 5])
def test_folder_off_the_move_list_is_refused(tmp_path: Path, folder: object) -> None:
    """Only Deleted Items and the test Completed folder may receive moved mail."""
    with pytest.raises(SystemProblem, match="done_folder"):
        _load(tmp_path, _config_with(done_folder=folder))


def test_folder_names_match_ignoring_case(tmp_path: Path) -> None:
    """DeletedItems/dalyn completed and the listed spelling are the same folder."""
    config = _load(tmp_path, _config_with(done_folder="DeletedItems/dalyn completed"))

    assert config["mailbox_actions"]["done_folder"] == "DeletedItems/dalyn completed"


def test_old_review_folder_key_is_not_needed(tmp_path: Path) -> None:
    """There is no review folder: green mail stays where it is."""
    config = _load(tmp_path, VALID_CONFIG)

    assert "review_folder" not in config["mailbox_actions"]


def _live_like(save: bool, move: bool) -> dict:
    """The valid config reading the Inbox into live STAC, with Save and moving as given."""
    config = _config_with(move_enabled=move)
    config["source_folder"] = "inbox"
    config["stac"].update(
        url="https://stac.example.com", is_test_instance=False, upload_enabled=True, save_enabled=save
    )
    return config


def test_inbox_with_save_and_no_moving_is_refused(tmp_path: Path) -> None:
    """A filed email left in the shared Inbox could be filed twice."""
    with pytest.raises(SystemProblem, match="move_enabled"):
        _load(tmp_path, _live_like(save=True, move=False))


@pytest.mark.parametrize(("save", "move"), [(True, True), (False, False)])
def test_inbox_is_fine_with_moving_on_or_save_off(tmp_path: Path, save: bool, move: bool) -> None:
    """Only the one combination is refused."""
    config = _load(tmp_path, _live_like(save=save, move=move))

    assert config["source_folder"] == "inbox"


def test_deleted_items_with_save_and_no_moving_is_allowed(tmp_path: Path) -> None:
    """Testing in Deleted Items is not affected by the Inbox rule."""
    config = _config_with()
    config["stac"].update(upload_enabled=True, save_enabled=True)

    assert _load(tmp_path, config)["stac"]["save_enabled"] is True


def test_tagging_the_inbox_against_test_stac_is_refused(tmp_path: Path) -> None:
    """Live mail must not go red Filed while its documents went to test STAC."""
    config = _config_with()
    config["source_folder"] = "inbox"

    with pytest.raises(SystemProblem, match="test STAC"):
        _load(tmp_path, config)


def test_reading_the_inbox_against_test_stac_untagged_is_allowed(tmp_path: Path) -> None:
    """Checking the Inbox listing writes nothing to the mailbox, so it may use test STAC."""
    config = _config_with(tag_enabled=False)
    config["source_folder"] = "inbox"

    assert _load(tmp_path, config)["source_folder"] == "inbox"


def test_uploading_deleted_items_into_live_stac_is_refused(tmp_path: Path) -> None:
    """Deleted Items is finished mail; uploading it to live would file it twice."""
    config = _live_like(save=False, move=False)
    config["source_folder"] = "deleteditems"

    with pytest.raises(SystemProblem, match="filed twice"):
        _load(tmp_path, config)


def test_reading_deleted_items_against_live_stac_without_upload_is_allowed(tmp_path: Path) -> None:
    """The live read-only check: cases are looked up, nothing is sent."""
    config = _live_like(save=False, move=False)
    config["source_folder"] = "deleteditems"
    config["stac"]["upload_enabled"] = False
    config["mailbox_actions"]["tag_enabled"] = False

    assert _load(tmp_path, config)["stac"]["is_test_instance"] is False


def test_source_folder_off_the_allowlist_is_refused_at_startup(tmp_path: Path) -> None:
    """A typo stops the run before Chrome opens, not at the first listing."""
    config = _config_with()
    config["source_folder"] = "inbx"

    with pytest.raises(SystemProblem, match="source_folder"):
        _load(tmp_path, config)


def test_retired_dry_run_key_is_refused(tmp_path: Path) -> None:
    """dry_run no longer protects anything, so it may not sit there looking like it does."""
    config = copy.deepcopy(VALID_CONFIG)
    config["dry_run"] = True

    with pytest.raises(SystemProblem, match="no longer used"):
        _load(tmp_path, config)


def _with_alerts(**alerts: object) -> dict:
    """Return a copy of the valid config with the alerts section changed."""
    config = copy.deepcopy(VALID_CONFIG)
    config["alerts"].update(alerts)
    return config


def test_alerts_off_with_blank_addresses_loads(tmp_path: Path) -> None:
    """Off means nothing is sent, so the addresses may wait."""
    assert _load(tmp_path, VALID_CONFIG)["alerts"]["enabled"] is False


def test_alerts_on_with_both_addresses_loads_stripped(tmp_path: Path) -> None:
    """Stray spaces around a pasted address are removed, not sent to Graph."""
    config = _load(
        tmp_path, _with_alerts(enabled=True, send_from=" alerts@example.com ", send_to="person@example.com")
    )

    assert config["alerts"]["send_from"] == "alerts@example.com"


@pytest.mark.parametrize("blank", ["send_from", "send_to"])
def test_alerts_on_without_an_address_is_refused(tmp_path: Path, blank: str) -> None:
    """An alert that could never be sent is found out at startup."""
    config = _with_alerts(enabled=True, send_from="alerts@example.com", send_to="person@example.com")
    config["alerts"][blank] = ""

    with pytest.raises(SystemProblem, match=f"alerts.{blank}"):
        _load(tmp_path, config)


def test_missing_alerts_section_is_refused(tmp_path: Path) -> None:
    """No defaults: an older config.yaml is told the section is needed."""
    config = copy.deepcopy(VALID_CONFIG)
    del config["alerts"]

    with pytest.raises(SystemProblem, match="alerts"):
        _load(tmp_path, config)


def test_quoted_reuse_case_page_is_refused(tmp_path: Path) -> None:
    """A quoted "false" is a non-empty string, which Python would read as on."""
    config = copy.deepcopy(VALID_CONFIG)
    config["stac"]["reuse_case_page"] = "false"

    with pytest.raises(SystemProblem, match="reuse_case_page"):
        _load(tmp_path, config)


def test_reuse_case_page_may_be_left_out(tmp_path: Path) -> None:
    """Optional: absent means off, today's behavior, so the live config needs no edit."""
    assert "reuse_case_page" not in _load(tmp_path, VALID_CONFIG)["stac"]


def test_alerts_enabled_must_be_a_boolean(tmp_path: Path) -> None:
    """A quoted "true" is a string, and is refused like the other switches."""
    with pytest.raises(SystemProblem, match="alerts.enabled"):
        _load(tmp_path, _with_alerts(enabled="true"))


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

    assert len(tags) == 15
    assert all(tag.startswith(ReviewTag.PREFIX) and tag != ReviewTag.PREFIX for tag in tags)
    assert len(set(tags)) == len(tags)


def test_colors_say_who_has_the_email() -> None:
    """Yellow while DALYN has it, red when filed, green for a person."""
    colors = ReviewTag.colors()

    assert set(colors) == set(ReviewTag.all())
    assert {tag for tag, color in colors.items() if color == ReviewTag.YELLOW_COLOR} == {
        ReviewTag.QUEUED,
        ReviewTag.PROCESSING,
        ReviewTag.SAVING,
    }
    assert {tag for tag, color in colors.items() if color == ReviewTag.RED_COLOR} == {
        ReviewTag.FILED,
        ReviewTag.NO_RULE,
    }
    assert colors[ReviewTag.INTERRUPTED] == ReviewTag.GREEN_COLOR
    assert colors[ReviewTag.NO_UCN] == ReviewTag.GREEN_COLOR


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
        self.headers: list[dict] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        """Record the call and return the next scripted response."""
        self.calls.append((method, url, kwargs.get("json")))
        self.params.append(kwargs.get("params"))
        self.headers.append(kwargs.get("headers") or {})
        return self.responses.pop(0)


def _client(responses: list[FakeResponse]) -> tuple[GraphClient, FakeSession]:
    """Build a GraphClient without MSAL, so nothing signs in or touches the network."""
    session = FakeSession(responses)
    client = GraphClient.__new__(GraphClient)
    client._session = session
    client._allowed_mailboxes = frozenset({MAILBOX})
    client._alert_sender = ALERT_SENDER
    client._get_token = lambda: "placeholder-token"
    return client, session


def test_ensure_categories_creates_only_missing_ones_in_their_color() -> None:
    """Existing categories are matched ignoring case and not created again."""
    master = {"value": [{"id": "cat-1", "displayName": "dalyn: filed", "color": "preset0"}]}
    client, session = _client([FakeResponse(200, master), FakeResponse(201)])

    client.ensure_categories(MAILBOX, {ReviewTag.FILED: "preset0", ReviewTag.NO_UCN: "preset4"})

    assert session.calls[1:] == [
        (
            "POST",
            f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/outlook/masterCategories",
            {"displayName": ReviewTag.NO_UCN, "color": "preset4"},
        )
    ]


def test_ensure_categories_recolors_its_own_category() -> None:
    """The first runs made every DALYN tag red; the wrong color is corrected."""
    master = {"value": [{"id": "cat-1", "displayName": ReviewTag.NO_UCN, "color": "preset0"}]}
    client, session = _client([FakeResponse(200, master), FakeResponse(200)])

    client.ensure_categories(MAILBOX, {ReviewTag.NO_UCN: "preset4"})

    assert session.calls[1:] == [
        (
            "PATCH",
            f"https://graph.microsoft.com/v1.0/users/{MAILBOX}/outlook/masterCategories/cat-1",
            {"color": "preset4"},
        )
    ]


def test_ensure_categories_never_touches_staff_categories() -> None:
    """Only the names passed in are created or recolored."""
    master = {"value": [
        {"id": "cat-1", "displayName": "Red category", "color": "preset0"},
        {"id": "cat-2", "displayName": ReviewTag.FILED, "color": "preset0"},
    ]}
    client, session = _client([FakeResponse(200, master)])

    client.ensure_categories(MAILBOX, {ReviewTag.FILED: "preset0"})

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
        client.ensure_categories("someone-else@example.com", {ReviewTag.FILED: "preset0"})

    assert session.calls == []


# list_messages


BASE = f"https://graph.microsoft.com/v1.0/users/{MAILBOX}"


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

    listed = client.list_messages(MAILBOX, days_back=2, folder="deleteditems", max_messages=2, keep=_untagged)

    assert [message["id"] for message in listed] == ["u1", "u2"]
    assert len(session.calls) == 2


def test_paging_stops_once_enough_are_kept() -> None:
    """The next page is not fetched when the first already has enough."""
    client, session = _client([_page([_message("u1"), _message("u2"), _message("u3")], more=True)])

    listed = client.list_messages(MAILBOX, days_back=2, folder="deleteditems", max_messages=2, keep=_untagged)

    assert [message["id"] for message in listed] == ["u1", "u2"]
    assert len(session.calls) == 1


def test_without_keep_every_message_counts() -> None:
    """skip_tagged off: tagged mail is listed and counted exactly as before."""
    client, _ = _client([_page([_message("t1", True), _message("u1"), _message("u2")], more=True)])

    listed = client.list_messages(MAILBOX, days_back=2, folder="deleteditems", max_messages=2)

    assert [message["id"] for message in listed] == ["t1", "u1"]


def test_keep_asks_for_full_pages() -> None:
    """--limit 3 with keep must not page through handled mail three at a time."""
    client, session = _client([_page([], more=False), _page([], more=False)])

    client.list_messages(MAILBOX, days_back=2, folder="deleteditems", max_messages=3, keep=_untagged)
    client.list_messages(MAILBOX, days_back=2, folder="deleteditems", max_messages=3)

    assert [params["$top"] for params in session.params] == [100, 3]


def test_listing_reads_the_folder_it_is_given() -> None:
    """The folder comes from the caller, not a name written into the client."""
    client, session = _client([_page([], more=False)])

    client.list_messages(MAILBOX, days_back=2, folder="deleteditems", max_messages=1)

    assert session.calls[0][1] == f"{BASE}/mailFolders/deleteditems/messages"


def test_inbox_listing_is_allowed() -> None:
    """Live reads the Inbox. config.source_folder picks it; the allowlist lets it through."""
    client, session = _client([_page([], more=False)])

    client.list_messages(MAILBOX, days_back=2, folder="inbox", max_messages=1)

    assert session.calls[0][1] == f"{BASE}/mailFolders/inbox/messages"


def test_read_allowlist_is_exactly_inbox_and_deleted_items() -> None:
    """Pinned, so widening where DALYN reads is a deliberate edit to this test too."""
    assert ALLOWED_FOLDERS == frozenset({"deleteditems", "inbox"})


def test_listing_a_folder_off_the_allowlist_is_refused_before_any_request() -> None:
    """Naming a folder is not permission to read it. ALLOWED_FOLDERS still decides."""
    client, session = _client([])

    with pytest.raises(SystemProblem, match="folder not on allowlist"):
        client.list_messages(MAILBOX, days_back=2, folder="archive", max_messages=1)
    assert session.calls == []


# Moving


def test_deleted_items_is_looked_up_by_its_well_known_name() -> None:
    """Deleted Items needs no search and is never created."""
    client, session = _client([FakeResponse(200, {"id": "deleted-id"})])

    assert client.get_move_target_id(MAILBOX, "deleteditems") == "deleted-id"
    assert session.calls == [("GET", f"{BASE}/mailFolders/deleteditems", None)]


def test_completed_test_folder_is_found_inside_deleted_items() -> None:
    """The child folder is looked up by name inside Deleted Items, any case in config."""
    client, session = _client([FakeResponse(200, {"value": [{"id": "completed-id"}]})])

    assert client.get_move_target_id(MAILBOX, "deleteditems/DALYN Completed") == "completed-id"
    assert session.calls == [("GET", f"{BASE}/mailFolders/deleteditems/childFolders", None)]
    assert session.params[0]["$filter"] == "displayName eq 'DALYN Completed'"


def test_missing_completed_folder_stops_the_run() -> None:
    """DALYN never creates it: a missing folder is a setup step to do by hand."""
    client, session = _client([FakeResponse(200, {"value": []})])

    with pytest.raises(SystemProblem, match="Create it in Outlook"):
        client.get_move_target_id(MAILBOX, "deleteditems/DALYN Completed")
    assert [call[0] for call in session.calls] == ["GET"]


@pytest.mark.parametrize(
    "name", ["inbox", "Archive", "Manual Review", "deleteditems/Other Folder", "inbox/DALYN Completed"]
)
def test_folder_off_the_move_list_is_refused_before_any_request(name: str) -> None:
    """Nothing outside MOVE_TARGETS can become a destination, even by config typo."""
    client, session = _client([])

    with pytest.raises(SystemProblem, match="may move into"):
        client.get_move_target_id(MAILBOX, name)
    assert session.calls == []


def test_move_target_lookup_still_checks_the_mailbox() -> None:
    """A destination lookup goes through the same mailbox allowlist."""
    client, session = _client([])

    with pytest.raises(SystemProblem, match="allowlist"):
        client.get_move_target_id("someone-else@example.com", "deleteditems")
    assert session.calls == []


def test_move_message_posts_the_destination() -> None:
    """The move is one POST naming the destination folder."""
    client, session = _client([FakeResponse(201, {"id": "moved-id"})])

    client.move_message(MAILBOX, "message-1", "review-id")

    assert session.calls == [("POST", f"{BASE}/messages/message-1/move", {"destinationId": "review-id"})]


def test_move_of_a_deleted_message_is_message_gone() -> None:
    """Purged before the move: skipped, not a broken system."""
    client, _ = _client([FakeResponse(404)])

    with pytest.raises(MessageGone):
        client.move_message(MAILBOX, "message-1", "review-id")


# Immutable IDs


def test_every_request_asks_for_immutable_ids() -> None:
    """IDs must survive a person moving the email, on reads and writes alike."""
    client, session = _client([FakeResponse(200), FakeResponse(200, {"id": "deleted-id"})])

    client.set_categories(MAILBOX, "message-1", [ReviewTag.FILED])
    client.get_move_target_id(MAILBOX, "deleteditems")

    assert [headers["Prefer"] for headers in session.headers] == ['IdType="ImmutableId"'] * 2


def test_immutable_ids_join_other_preferences() -> None:
    """The listing's plain-text body preference is kept, not overwritten."""
    client, session = _client([_page([], more=False)])

    client.list_messages(MAILBOX, days_back=2, folder="deleteditems", max_messages=1)

    assert session.headers[0]["Prefer"] == 'outlook.body-content-type="text", IdType="ImmutableId"'


# Alert mail


def test_send_mail_posts_one_plain_text_message_from_the_alert_sender() -> None:
    """One sendMail call, plain text, not kept in Sent Items."""
    client, session = _client([FakeResponse(202)])

    client.send_mail("Alerts@Example.com", "person@example.com", "DALYN stopped", "See the log.")

    method, url, payload = session.calls[0]
    assert (method, url) == ("POST", f"https://graph.microsoft.com/v1.0/users/{ALERT_SENDER}/sendMail")
    assert payload["saveToSentItems"] is False
    assert payload["message"]["body"] == {"contentType": "Text", "content": "See the log."}
    assert payload["message"]["toRecipients"] == [{"emailAddress": {"address": "person@example.com"}}]


def test_send_mail_from_any_other_address_is_refused_before_any_request() -> None:
    """Not even a monitored mailbox may send: only the one alert sender."""
    client, session = _client([])

    with pytest.raises(SystemProblem, match="alert sender"):
        client.send_mail(MAILBOX, "person@example.com", "subject", "body")
    assert session.calls == []


def test_send_mail_with_no_alert_sender_is_refused() -> None:
    """Alerts not configured: nothing can be sent at all."""
    client, session = _client([])
    client._alert_sender = None

    with pytest.raises(SystemProblem, match="alert sender"):
        client.send_mail(ALERT_SENDER, "person@example.com", "subject", "body")
    assert session.calls == []


def test_alert_sender_cannot_be_read() -> None:
    """Sending from a mailbox does not put it on the list DALYN may read."""
    client, session = _client([])

    with pytest.raises(SystemProblem, match="allowlist"):
        client.list_messages(ALERT_SENDER, days_back=2, folder="inbox", max_messages=1)
    assert session.calls == []
