"""Tests for tagging.py: which categories an email gets, and how they reach Outlook."""

import pytest

from exceptions import MessageGone, SystemProblem
from models import EmailDecision, Outcome, ReviewTag, StacResult
from tagging import categories_for, mark_processing, merge_categories, needs_handling, tag_email, was_interrupted


def _row(decision: str, outcome: str, stac_result: str = "") -> dict:
    """One attachment row, with only the fields tagging reads."""
    return {
        "email_number": 1,
        "email_decision": decision,
        "outcome": outcome,
        "stac_result": stac_result,
    }


class FakeClient:
    """Stands in for GraphClient.set_categories, recording what would be sent."""

    def __init__(self, error: Exception | None = None) -> None:
        """Optionally raise this error instead of recording the call."""
        self.error = error
        self.calls: list[tuple[str, str, list[str]]] = []

    def set_categories(self, mailbox: str, message_id: str, categories: list[str]) -> None:
        """Record the call, or raise the scripted error."""
        if self.error:
            raise self.error
        self.calls.append((mailbox, message_id, categories))


# categories_for


@pytest.mark.parametrize(
    ("stac_result", "tag"),
    [
        (StacResult.ENTERED, ReviewTag.FILED),
        (StacResult.REACHED_SAVE, ReviewTag.READY_TO_SAVE),
        (StacResult.REHEARSED, ReviewTag.REHEARSED),
    ],
)
def test_uploaded_email_is_tagged_by_how_far_stac_got(stac_result: str, tag: str) -> None:
    """Filed only when Save was really pressed."""
    rows = [_row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER, stac_result)]

    assert categories_for(rows) == [tag]


def test_upload_under_review_pair_adds_no_rule_tag() -> None:
    """Staff can see which uploads still need a Type sorted out inside STAC."""
    rows = [
        _row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER, StacResult.REHEARSED),
        _row(EmailDecision.UPLOAD, Outcome.PLS_RVW, StacResult.REHEARSED),
    ]

    assert categories_for(rows) == [ReviewTag.REHEARSED, ReviewTag.NO_RULE]


def test_uploaded_email_without_stac_result_is_a_bug() -> None:
    """A guessed tag would hide a broken loop, so it raises instead."""
    with pytest.raises(ValueError):
        categories_for([_row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER)])


@pytest.mark.parametrize(
    ("outcome", "tag"),
    [
        (Outcome.NO_FILES, ReviewTag.NO_ATTACHMENTS),
        (Outcome.NON_PDF, ReviewTag.CANT_READ),
        (Outcome.TOO_LARGE, ReviewTag.CANT_READ),
        (Outcome.UNREADABLE, ReviewTag.CANT_READ),
        (Outcome.NO_UCN, ReviewTag.NO_UCN),
        (Outcome.UCN_CONFLICT, ReviewTag.UCN_CONFLICT),
        (Outcome.UCN_OTHER_COUNTY, ReviewTag.OTHER_COUNTY),
        (Outcome.INTERRUPTED, ReviewTag.INTERRUPTED),
    ],
)
def test_manual_review_reason_maps_to_its_tag(outcome: str, tag: str) -> None:
    """Every reason an attachment is held back has a tag."""
    assert categories_for([_row(EmailDecision.MANUAL_REVIEW, outcome)]) == [tag]


def test_manual_review_gets_one_tag_per_distinct_reason() -> None:
    """Two unreadable attachments give one Can't read, not two."""
    rows = [
        _row(EmailDecision.MANUAL_REVIEW, Outcome.NON_PDF),
        _row(EmailDecision.MANUAL_REVIEW, Outcome.UNREADABLE),
        _row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN),
        _row(EmailDecision.MANUAL_REVIEW, Outcome.WOULD_ENTER),
    ]

    assert categories_for(rows) == [ReviewTag.CANT_READ, ReviewTag.NO_UCN]


@pytest.mark.parametrize(
    ("stac_result", "tag"),
    [
        (StacResult.FAILED, ReviewTag.STAC_FAILED),
        (StacResult.UNKNOWN, ReviewTag.MAY_BE_SAVED),
    ],
)
def test_stac_failure_is_tagged(stac_result: str, tag: str) -> None:
    """An upload STAC turned down becomes Manual Review with STAC's reason."""
    rows = [_row(EmailDecision.MANUAL_REVIEW, Outcome.WOULD_ENTER, stac_result)]

    assert categories_for(rows) == [tag]


def test_stac_tag_comes_before_other_reasons() -> None:
    """May be saved is what a person must check first, so it leads."""
    rows = [
        _row(EmailDecision.MANUAL_REVIEW, Outcome.PLS_RVW, StacResult.UNKNOWN),
        _row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN),
    ]

    assert categories_for(rows) == [ReviewTag.MAY_BE_SAVED, ReviewTag.NO_UCN]


def test_gone_email_gets_no_tag() -> None:
    """It is no longer in the folder, so there is nothing to tag."""
    assert categories_for([_row(EmailDecision.GONE, Outcome.GONE)]) == []


# merge_categories


def test_merge_keeps_staff_categories_and_replaces_old_dalyn_ones() -> None:
    """A re-run with a new result leaves one DALYN answer, and staff tags untouched."""
    existing = ["Staff category", ReviewTag.NO_UCN, "Another staff one"]

    assert merge_categories(existing, [ReviewTag.REHEARSED]) == [
        "Staff category",
        "Another staff one",
        ReviewTag.REHEARSED,
    ]


# tag_email


def test_tag_email_sends_merged_list() -> None:
    """The PATCH carries staff categories plus the new DALYN tag."""
    client = FakeClient()
    message = {"id": "message-1", "categories": ["Staff category"]}
    rows = [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)]

    assert tag_email(client, "box@example.com", message, rows) is True
    assert client.calls == [("box@example.com", "message-1", ["Staff category", ReviewTag.NO_UCN])]


def test_tag_email_skips_write_when_nothing_changes() -> None:
    """A dry run re-reading the same email does not rewrite the same tag."""
    client = FakeClient()
    message = {"id": "message-1", "categories": [ReviewTag.NO_UCN]}

    assert tag_email(client, "box@example.com", message, [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)]) is False
    assert client.calls == []


def test_tag_email_skips_gone_email() -> None:
    """Nothing to tag, and nothing to write."""
    client = FakeClient()
    message = {"id": "message-1", "categories": []}

    assert tag_email(client, "box@example.com", message, [_row(EmailDecision.GONE, Outcome.GONE)]) is False
    assert client.calls == []


def test_tag_email_handles_message_deleted_meanwhile() -> None:
    """Purged between processing and tagging: logged and skipped, run carries on."""
    client = FakeClient(error=MessageGone("404"))
    message = {"id": "message-1"}

    assert tag_email(client, "box@example.com", message, [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)]) is False


def test_tag_email_lets_graph_failure_stop_the_pass() -> None:
    """Untagged after filing would mean filing again next pass, so it is not swallowed."""
    client = FakeClient(error=SystemProblem("Graph PATCH failed with 500"))
    message = {"id": "message-1"}

    with pytest.raises(SystemProblem):
        tag_email(client, "box@example.com", message, [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)])


def test_gone_email_has_processing_tag_cleared() -> None:
    """Moved away mid-pass: Processing must not follow it to its new folder."""
    client = FakeClient()
    message = {"id": "message-1", "categories": ["Staff category", ReviewTag.PROCESSING]}

    assert tag_email(client, "box@example.com", message, [_row(EmailDecision.GONE, Outcome.GONE)]) is True
    assert client.calls == [("box@example.com", "message-1", ["Staff category"])]


# mark_processing


def test_mark_processing_replaces_old_dalyn_tags_and_keeps_staff_ones() -> None:
    """A re-read email shows Processing, not last pass's result, while it is worked on."""
    client = FakeClient()
    message = {"id": "message-1", "categories": ["Staff category", ReviewTag.NO_UCN]}

    mark_processing(client, "box@example.com", message, 1)

    assert client.calls == [("box@example.com", "message-1", ["Staff category", ReviewTag.PROCESSING])]
    assert message["categories"] == ["Staff category", ReviewTag.PROCESSING]


def test_result_tag_replaces_processing() -> None:
    """The end-of-email write swaps Processing for the result."""
    client = FakeClient()
    message = {"id": "message-1", "categories": []}
    mark_processing(client, "box@example.com", message, 1)

    tag_email(client, "box@example.com", message, [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)])

    assert client.calls[-1] == ("box@example.com", "message-1", [ReviewTag.NO_UCN])


def test_mark_processing_on_vanished_email_leaves_message_unchanged() -> None:
    """Gone before it was marked: logged, and the folder check finds it GONE."""
    client = FakeClient(error=MessageGone("404"))
    message = {"id": "message-1", "categories": ["Staff category"]}

    mark_processing(client, "box@example.com", message, 1)

    assert message["categories"] == ["Staff category"]


def test_mark_processing_lets_graph_failure_stop_the_pass() -> None:
    """An email DALYN cannot mark is one it could not mark as done either."""
    client = FakeClient(error=SystemProblem("Graph PATCH failed with 500"))

    with pytest.raises(SystemProblem):
        mark_processing(client, "box@example.com", {"id": "message-1"}, 1)


# was_interrupted


def test_leftover_processing_tag_means_interrupted() -> None:
    """Processing is always replaced at the end, so finding it means a run died."""
    assert was_interrupted({"categories": ["Staff category", ReviewTag.PROCESSING]})


def test_finished_or_untouched_email_is_not_interrupted() -> None:
    """A result tag, staff tags only, or no categories at all are all fine."""
    assert not was_interrupted({"categories": [ReviewTag.FILED]})
    assert not was_interrupted({"categories": ["Staff category"]})
    assert not was_interrupted({"categories": []})
    assert not was_interrupted({})


# needs_handling


def test_untagged_email_needs_handling() -> None:
    """No DALYN category, staff ones included, means DALYN has not touched it."""
    assert needs_handling({"categories": []})
    assert needs_handling({})
    assert needs_handling({"categories": ["Staff category"]})


def test_email_with_a_result_tag_is_skipped() -> None:
    """Any finished DALYN tag, including Interrupted, means leave it alone."""
    assert not needs_handling({"categories": [ReviewTag.READY_TO_SAVE]})
    assert not needs_handling({"categories": ["Staff category", ReviewTag.NO_UCN]})
    assert not needs_handling({"categories": [ReviewTag.INTERRUPTED]})


def test_interrupted_email_still_comes_through() -> None:
    """Processing left behind must reach the interrupted check, not be skipped."""
    assert needs_handling({"categories": [ReviewTag.PROCESSING]})
